"""Closed operator registry and native MCP profile lifecycle.

The registry is the command-injection boundary.  It deliberately does not
contain tool, resource, capability, or action allowlists: an enabled server's
advertised tools are passed through unchanged.
"""

from __future__ import annotations

import os
import stat
import tempfile
import threading
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

import yaml

MCPServerAdapter = None


REGISTRY_PATH = Path(__file__).resolve().parent.parent / "config" / "mcp-profiles.yaml"
SECRET_ROOT = Path("/run/runtime-secrets").resolve()
MAX_REGISTRY_BYTES = 128 * 1024
MAX_FILE_BYTES = 64 * 1024
MAX_TIMEOUT_SECONDS = 30
PROFILE_KEYS = {
    "profile_id", "display_name", "enabled", "unavailable_reason", "transport",
    "command", "args", "timeout_seconds", "credential_files", "environment",
}
ALLOWED_RAW_ENV_KEYS = {"PYTHONUNBUFFERED", "GITHUB_HOST", "NEO4J_URI", "QDRANT_URL", "PROMETHEUS_URL", "NOCODB_URL", "PROMETHEUS_CONFIG"}
ALLOWED_CREDENTIAL_KEYS = {"GITHUB_PERSONAL_ACCESS_TOKEN", "NOCODB_API_TOKEN", "NEO4J_USERNAME", "NEO4J_PASSWORD", "QDRANT_API_KEY", "PROMETHEUS_USERNAME", "PROMETHEUS_PASSWORD"}


class ProfileError(ValueError):
    pass


class NativeProfileError(ProfileError):
    """Sanitized failure while preparing run-scoped native MCP tools."""

    def __init__(self, code: str, profile_id: str | None = None, tool_name: str | None = None):
        self.code = code
        self.profile_id = profile_id
        self.tool_name = tool_name
        super().__init__(code)


def _strict_loader() -> type[yaml.SafeLoader]:
    class Loader(yaml.SafeLoader):
        pass

    def construct_mapping(loader, node, deep=False):
        mapping = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in mapping:
                raise ProfileError("duplicate registry key")
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)
    return Loader


def _text(value: Any, field: str, *, empty=False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise ProfileError(f"invalid {field}")
    if len(value) > 4096 or "\x00" in value:
        raise ProfileError(f"invalid {field}")
    return value


def load_profiles(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    path = Path(path or os.getenv("STUDIO_MCP_POLICY_PATH", REGISTRY_PATH))
    if path.stat().st_size > MAX_REGISTRY_BYTES:
        raise ProfileError("registry too large")
    raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_strict_loader())
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "profiles"}:
        raise ProfileError("registry schema")
    if raw["schema_version"] != "studio.mcp-profiles.v1" or not isinstance(raw["profiles"], list):
        raise ProfileError("registry schema")
    profiles = {}
    for item in raw["profiles"]:
        if not isinstance(item, dict) or set(item) != PROFILE_KEYS:
            raise ProfileError("profile schema")
        profile_id = _text(item["profile_id"], "profile_id")
        if profile_id in profiles or len(profile_id) > 64:
            raise ProfileError("duplicate or invalid profile_id")
        enabled = item["enabled"]
        if not isinstance(enabled, bool):
            raise ProfileError("invalid enabled")
        reason = item["unavailable_reason"]
        if reason is not None:
            reason = _text(reason, "unavailable_reason")
        if enabled and reason is not None:
            raise ProfileError("enabled profile cannot have unavailable_reason")
        if not enabled and not reason:
            raise ProfileError("disabled profile requires unavailable_reason")
        transport = _text(item["transport"], "transport")
        if transport != "stdio":
            raise ProfileError("unsupported transport")
        command = item["command"]
        if enabled:
            command = _text(command, "command")
            if not command.startswith("/") or "/../" in command or command.endswith("/.."):
                raise ProfileError("command must be absolute and bounded")
        elif command is not None:
            raise ProfileError("unavailable profile cannot define command")
        args = item["args"]
        if not isinstance(args, list) or len(args) > 32 or any(not isinstance(x, str) or not x or len(x) > 4096 or "\x00" in x for x in args):
            raise ProfileError("invalid args")
        timeout = item["timeout_seconds"]
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_TIMEOUT_SECONDS:
            raise ProfileError("invalid timeout_seconds")
        credential_files = item["credential_files"]
        environment = item["environment"]
        if not isinstance(credential_files, dict) or not isinstance(environment, dict):
            raise ProfileError("invalid environment schema")
        if any(not isinstance(k, str) or k not in ALLOWED_CREDENTIAL_KEYS or not isinstance(v, str) for k, v in credential_files.items()):
            raise ProfileError("invalid credential environment key/value")
        if any(not isinstance(k, str) or k not in ALLOWED_RAW_ENV_KEYS or not isinstance(v, str) for k, v in environment.items()):
            raise ProfileError("invalid raw environment key/value")
        if set(credential_files) & set(environment):
            raise ProfileError("credential and raw environment keys overlap")
        profiles[profile_id] = {**item, "profile_id": profile_id, "command": command, "timeout_seconds": float(timeout)}
    return profiles


def _read_secret_ref(relative: str) -> str:
    if not isinstance(relative, str) or not relative or relative.startswith("/"):
        raise ProfileError("credential ref must be relative")
    candidate = SECRET_ROOT / relative
    if candidate.is_symlink():
        raise ProfileError("credential ref is a symlink")
    target = candidate.resolve()
    if SECRET_ROOT not in target.parents:
        raise ProfileError("credential ref escapes secret root")
    info = target.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
        raise ProfileError("credential ref is not a regular bounded file")
    if info.st_uid not in {os.getuid(), 0} or stat.S_IMODE(info.st_mode) & 0o077:
        raise ProfileError("credential ref has unsafe owner or mode")
    return target.read_text(encoding="utf-8").rstrip("\r\n")


def profile_parameters(profile_id: str, registry: dict[str, dict[str, Any]] | None = None):
    from mcp import StdioServerParameters
    registry = registry or load_profiles()
    profile = registry.get(profile_id)
    if profile is None or not profile["enabled"]:
        raise ProfileError("unknown or unavailable profile")
    env = {"PYTHONUNBUFFERED": "1"}
    for key, ref in profile["credential_files"].items():
        env[key] = _read_secret_ref(ref)
    env.update(profile["environment"])
    return StdioServerParameters(command=profile["command"], args=profile["args"], env=env)


def profile_spawn_spec(profile_id: str, registry: dict[str, dict[str, Any]] | None = None) -> tuple[list[str], dict[str, str], float]:
    registry = registry or load_profiles()
    profile = registry.get(profile_id)
    params = profile_parameters(profile_id, registry)
    return [params.command, *params.args], dict(params.env or {}), float(profile["timeout_seconds"])


@contextmanager
def native_profile_tools(profile_id: str, registry: dict[str, dict[str, Any]] | None = None,
                         event_path: str | None = None):
    adapter_class = MCPServerAdapter
    if adapter_class is None:
        from crewai_tools import MCPServerAdapter as adapter_class
    registry = registry or load_profiles()
    profile = registry.get(profile_id)
    params = profile_parameters(profile_id, registry)
    # These diagnostic values are non-secret runtime metadata required only by
    # the project fixture; registry credentials remain untouched.
    with tempfile.TemporaryDirectory(prefix="studio-native-mcp-") as directory:
        params.env = {**(params.env or {}), "STAGE2_RUN_ID": "native-profile-discovery",
                      "STAGE2_EVENT_PATH": event_path or str(Path(directory) / "events.jsonl")}
        with adapter_class(params, connect_timeout=int(profile["timeout_seconds"])) as tools:
            # No filtering, copying, or name-based policy is applied here.
            yield tuple(tools)


def _crew_tool_name(tool: Any) -> str | None:
    name = getattr(tool, "name", None)
    return name if isinstance(name, str) and name else None


@contextmanager
def native_enabled_profiles(registry: dict[str, dict[str, Any]] | None = None,
                            event_path: str | None = None):
    """Open every enabled native profile in registry order.

    The ExitStack deliberately owns all adapters on the calling thread.  Tool
    order is preserved and no advertised tool is filtered or deduplicated.
    """
    registry = registry or load_profiles()
    opened = []
    metadata = []
    seen: dict[str, str] = {}
    stack = ExitStack()
    context_thread_id = threading.get_ident()
    try:
        for profile_id, profile in registry.items():
            if not profile["enabled"]:
                continue
            try:
                tools = stack.enter_context(native_profile_tools(profile_id, registry, event_path))
            except NativeProfileError:
                raise
            except Exception as exc:
                raise NativeProfileError("setup_failed", profile_id) from exc
            names = []
            for tool in tools:
                name = _crew_tool_name(tool)
                if name is None:
                    raise NativeProfileError("invalid_tool_name", profile_id)
                if name in seen:
                    raise NativeProfileError("duplicate_tool_name", profile_id, name)
                seen[name] = profile_id
                names.append(name)
            opened.extend(tools)
            metadata.append(profile_metadata(profile_id, tuple(names), registry))
        for item in metadata:
            item["context_open_thread_id"] = context_thread_id
    except BaseException as exc:
        try:
            stack.close()
        except BaseException as cleanup_exc:
            raise NativeProfileError("cleanup_failed") from cleanup_exc
        raise

    try:
        yield tuple(opened), tuple(metadata)
    except BaseException as body_exc:
        try:
            stack.close()
        except BaseException as cleanup_exc:
            raise NativeProfileError("cleanup_failed") from body_exc
        raise
    else:
        try:
            stack.close()
        except BaseException as cleanup_exc:
            raise NativeProfileError("cleanup_failed") from cleanup_exc
        for item in metadata:
            item["context_close_thread_id"] = threading.get_ident()


def profile_metadata(profile_id: str, names: tuple[str, ...] = (), registry: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    profile = (registry or load_profiles())[profile_id]
    return {
        "profile_id": profile_id,
        "transport": profile["transport"],
        "advertised_names": list(names),
        "advertised_count": len(names),
        "status": "enabled" if profile["enabled"] else "unavailable",
        "category": "native_mcp",
    }


def write_capability_evidence(metadata: tuple[dict[str, Any], ...], *, output: str | None = None) -> dict[str, Any]:
    """Return bounded Stage 3 capability evidence; never executes a write tool."""
    write_tools = [name for item in metadata for name in item["advertised_names"]
                   if any(token in name.lower() for token in ("write", "create", "update", "delete"))]
    if write_tools:
        raise NativeProfileError("unexpected_write_capability")
    evidence = {
        "profiles": [{key: item[key] for key in ("profile_id", "transport", "advertised_names", "advertised_count", "status")}
                     for item in metadata],
        "context_open_thread_id": metadata[0].get("context_open_thread_id") if metadata else None,
        "context_close_thread_id": metadata[0].get("context_close_thread_id") if metadata else None,
        "read_action": "available" if any(item["advertised_count"] for item in metadata) else "not_available",
        "write_action": "not_available",
        "write_reason": "no enabled native profile advertises a write tool",
    }
    if output is not None:
        Path(output).write_text(__import__("json").dumps(evidence, sort_keys=True), encoding="utf-8")
    return evidence
