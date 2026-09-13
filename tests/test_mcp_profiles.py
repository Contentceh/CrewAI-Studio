import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import mcp_profiles


def profile(**changes):
    value = {
        "profile_id": "fixture",
        "display_name": "Fixture",
        "enabled": True,
        "unavailable_reason": None,
        "transport": "stdio",
        "command": "/bin/true",
        "args": [],
        "timeout_seconds": 1,
        "credential_files": {},
        "environment": {},
    }
    value.update(changes)
    return value


def write_registry(path, item):
    import yaml
    path.write_text(yaml.safe_dump({"schema_version": "studio.mcp-profiles.v1", "profiles": [item]}))


def test_canonical_registry_keeps_required_profiles_disabled():
    registry = mcp_profiles.load_profiles()
    for profile_id in (
        "github", "nocodb", "neo4j", "qdrant", "prometheus", "codex_websearch",
        "perplexity", "google_drive", "grafana", "supabase", "docker",
        "patchright", "searxng",
    ):
        item = registry[profile_id]
        assert item["enabled"] is False, profile_id
        assert item["command"] is None, profile_id
        assert item["unavailable_reason"], profile_id


@pytest.mark.parametrize("profile_id, fragments", [
    ("codex_websearch", (
        "provider credential file exists", "provider access is confirmed",
        "does not provide native mcp transport",
        "no verified project-owned native mcp server/transport is configured",
    )),
    ("perplexity", (
        "credential file exists", "validity/use is unverified",
        "not connected to studio runtime",
        "no verified project-owned native mcp transport is configured",
    )),
])
def test_canonical_registry_distinguishes_credentials_from_native_mcp(profile_id, fragments):
    registry = mcp_profiles.load_profiles()
    reason = registry[profile_id]["unavailable_reason"].lower()
    for fragment in fragments:
        assert fragment in reason, (profile_id, fragment)
    assert "no client credential" not in reason
    assert "no credential" not in reason
    assert "or client credential" not in reason


def test_registry_rejects_unknown_fields_duplicate_ids_and_invalid_enabled(tmp_path):
    path = tmp_path / "profiles.yaml"
    extra = profile(extra=True)
    write_registry(path, extra)
    with pytest.raises(mcp_profiles.ProfileError):
        mcp_profiles.load_profiles(path)
    write_registry(path, profile(enabled=False, unavailable_reason="not installed", command=None))
    data = mcp_profiles.load_profiles(path)
    assert data["fixture"]["enabled"] is False


def test_registry_rejects_traversal_and_unbounded_values(tmp_path):
    path = tmp_path / "profiles.yaml"
    write_registry(path, profile(command="/tmp/../bin/true"))
    with pytest.raises(mcp_profiles.ProfileError):
        mcp_profiles.load_profiles(path)
    write_registry(path, profile(args=["x" * 4097]))
    with pytest.raises(mcp_profiles.ProfileError):
        mcp_profiles.load_profiles(path)


def test_secret_refs_require_regular_owned_private_bounded_files(tmp_path, monkeypatch):
    root = tmp_path / "secrets"
    root.mkdir()
    secret = root / "token"
    secret.write_text("value\n")
    secret.chmod(stat.S_IRUSR)
    monkeypatch.setattr(mcp_profiles, "SECRET_ROOT", root)
    assert mcp_profiles._read_secret_ref("token") == "value"
    outside = tmp_path / "outside"
    outside.write_text("value")
    with pytest.raises(mcp_profiles.ProfileError):
        mcp_profiles._read_secret_ref("../outside")
    secret.unlink()
    secret.symlink_to(outside)
    with pytest.raises(mcp_profiles.ProfileError):
        mcp_profiles._read_secret_ref("token")


def test_minimal_child_env_and_unknown_profile_denial(monkeypatch):
    registry = {"fixture": profile(environment={"PYTHONUNBUFFERED": "1"})}
    monkeypatch.setattr(mcp_profiles, "_read_secret_ref", lambda ref: "secret")
    params = mcp_profiles.profile_parameters("fixture", registry)
    assert params.env == {"PYTHONUNBUFFERED": "1"}
    with pytest.raises(mcp_profiles.ProfileError):
        mcp_profiles.profile_parameters("unknown", registry)


def test_raw_secret_environment_is_rejected_and_refs_cannot_overlap(tmp_path):
    path = tmp_path / "profiles.yaml"
    write_registry(path, profile(environment={"API_KEY": "plaintext"}))
    with pytest.raises(mcp_profiles.ProfileError):
        mcp_profiles.load_profiles(path)
    write_registry(path, profile(credential_files={"NEO4J_PASSWORD": "password"}, environment={"NEO4J_PASSWORD": "plaintext"}))
    with pytest.raises(mcp_profiles.ProfileError):
        mcp_profiles.load_profiles(path)


def test_adapter_receives_bounded_timeout(monkeypatch):
    seen = {}
    class Adapter:
        def __init__(self, params, *names, connect_timeout):
            seen["timeout"] = connect_timeout
        def __enter__(self):
            return []
        def __exit__(self, *args):
            return False
    monkeypatch.setattr(mcp_profiles, "MCPServerAdapter", Adapter)
    with mcp_profiles.native_profile_tools("fixture", {"fixture": profile(timeout_seconds=7)}):
        pass
    assert seen["timeout"] == 7


def test_native_context_preserves_all_tools_without_filtering(monkeypatch):
    class Tool:
        def __init__(self, name):
            self.name = name

    class Adapter:
        def __init__(self, params):
            self.params = params
        def __enter__(self):
            return [Tool("read"), Tool("write"), Tool("delete")]
        def __exit__(self, *args):
            return False

    class AdapterWithTimeout(Adapter):
        def __init__(self, params, **kwargs):
            super().__init__(params)
    monkeypatch.setattr(mcp_profiles, "MCPServerAdapter", AdapterWithTimeout)
    registry = {"fixture": profile()}
    with mcp_profiles.native_profile_tools("fixture", registry) as tools:
        assert [tool.name for tool in tools] == ["read", "write", "delete"]
    metadata = mcp_profiles.profile_metadata("fixture", ("read", "write", "delete"), registry)
    assert metadata["advertised_count"] == 3
    assert metadata["advertised_names"] == ["read", "write", "delete"]


def test_enabled_profile_context_preserves_order_and_closes_reverse(monkeypatch):
    events = []
    class Tool:
        def __init__(self, name):
            self.name = name
    class Adapter:
        def __init__(self, params, **kwargs):
            self.name = params.command
        def __enter__(self):
            events.append(f"open:{self.name}")
            return [Tool(f"{self.name}-one"), Tool(f"{self.name}-two")]
        def __exit__(self, *args):
            events.append(f"close:{self.name}")
    monkeypatch.setattr(mcp_profiles, "MCPServerAdapter", Adapter)
    registry = {
        "first": profile(profile_id="first", command="/first"),
        "second": profile(profile_id="second", command="/second"),
    }
    with mcp_profiles.native_enabled_profiles(registry) as (tools, metadata):
        assert [tool.name for tool in tools] == ["/first-one", "/first-two", "/second-one", "/second-two"]
        assert [item["profile_id"] for item in metadata] == ["first", "second"]
    assert events == ["open:/first", "open:/second", "close:/second", "close:/first"]


def test_enabled_profile_context_fails_closed_on_duplicate(monkeypatch):
    class Tool:
        name = "same"
    class Adapter:
        def __init__(self, params, **kwargs):
            pass
        def __enter__(self):
            return [Tool()]
        def __exit__(self, *args):
            return False
    monkeypatch.setattr(mcp_profiles, "MCPServerAdapter", Adapter)
    registry = {"one": profile(profile_id="one"), "two": profile(profile_id="two")}
    with pytest.raises(mcp_profiles.NativeProfileError, match="duplicate_tool_name"):
        with mcp_profiles.native_enabled_profiles(registry):
            pass


def test_write_capability_evidence_is_unavailable_without_write_tools():
    evidence = mcp_profiles.write_capability_evidence((
        mcp_profiles.profile_metadata("fixture", ("fixture.read",), {"fixture": profile()}),
    ))
    assert evidence["write_action"] == "not_available"
    assert evidence["read_action"] == "available"
