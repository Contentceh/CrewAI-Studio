"""Single export policy for legacy and MCP-backed tool data."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote, urlsplit
from typing import Any


class UnsupportedExport(ValueError):
    pass


_SENSITIVE = re.compile(r"(?:secret|password|api[_-]?key|access[_-]?key|token|authorization|headers|cookie|private[_-]?key|credential|signature|session|^sig$)", re.I)
_AUTH_SCHEME = re.compile(r"\b(?:bearer|basic)\s+[^\s,;]+", re.I)
_URL_FRAGMENT = re.compile(r"(?:(?:[a-z][a-z0-9+.-]*://)|//)[^\s<>\"']+", re.I)
_ALLOWED_REF = re.compile(r"^ref:[a-z0-9][a-z0-9/_-]*$", re.I)
REGISTERED_CREDENTIAL_REFS = {
    "ref:operator/fixture",
    "ref:opencode/codex-lb",
    "ref:opencode/github",
    "ref:opencode/nocodb",
    "ref:opencode/supabase",
    "ref:operator/google-drive-service-account",
    "ref:operator/docker-broker",
    "ref:operator/patchright-broker",
    "ref:operator/postgres-readonly",
    "ref:operator/qdrant-fixture",
}


def _is_sensitive(key: str) -> bool:
    return bool(_SENSITIVE.search(key))


def _reject_value(value: Any, path: str, depth: int = 0) -> None:
    if not isinstance(value, str):
        return
    if depth > 4:
        raise UnsupportedExport("Export blocked: excessive URL encoding depth")
    if _AUTH_SCHEME.search(value) or re.search(r"(?:authorization|api[_-]?key|token|secret|password|cookie)=", value, re.I):
        raise UnsupportedExport("Export blocked: credential-like value")
    decoded = unquote(value)
    if decoded != value:
        _reject_value(decoded, path, depth + 1)
    for match in _URL_FRAGMENT.finditer(value):
        url_text = match.group(0).rstrip(".,);]}")
        try:
            parts = urlsplit(url_text)
            if parts.username is not None or parts.password is not None:
                raise UnsupportedExport("Export blocked: URL userinfo")
            for query_key, query_value in parse_qsl(parts.query, keep_blank_values=True, max_num_fields=32):
                if _is_sensitive(query_key):
                    raise UnsupportedExport("Export blocked: credential-like query value")
                _reject_value(query_value, path, depth + 1)
            if parts.query:
                raise UnsupportedExport("Export blocked: URL query has no approved manifest")
        except ValueError as exc:
            raise UnsupportedExport("Export blocked: unsafe URL") from None


def serialize_export_value(value: Any, *, mode: str = "redact", path: str = "") -> Any:
    if mode not in {"redact", "block"}:
        raise UnsupportedExport("Export blocked: unknown policy mode")
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            key_text = str(key)
            item_path = f"{path}.{key_text}" if path else key_text
            if key_text.lower() == "parameters" and mode == "block":
                result[key] = serialize_tool_parameters(item, mode=mode)
                continue
            if key_text.lower() in {"credential_ref", "credential_reference"}:
                if not (isinstance(item, str) and _ALLOWED_REF.fullmatch(item) and item in REGISTERED_CREDENTIAL_REFS):
                    raise UnsupportedExport("Export blocked: unregistered credential reference")
                result[key] = item
                continue
            if _is_sensitive(key_text):
                if mode == "block":
                    raise UnsupportedExport("Export blocked: sensitive field")
                result[key] = {"__redacted__": True, "reason": "credential-bearing field"}
                continue
            if key_text.lower() in {"adapter_id", "adapter", "mcp_adapter"} and item != "project_fixture":
                raise UnsupportedExport("Export blocked: unknown adapter")
            _reject_value(item, item_path)
            result[key] = serialize_export_value(item, mode=mode, path=item_path)
        return result
    if isinstance(value, list):
        return [serialize_export_value(item, mode=mode, path=f"{path}[]") for item in value]
    _reject_value(value, path)
    return value


def serialize_tool_parameters(parameters: Any, *, mode: str = "redact") -> Any:
    if mode == "block":
        if not isinstance(parameters, dict):
            raise UnsupportedExport("Export blocked: invalid tool parameters")
        # Registered references are metadata, not permission for arbitrary values.
        # No portable connector manifest is approved in this stage.
        if set(parameters) - {"credential_ref", "credential_reference"}:
            raise UnsupportedExport("Export blocked: tool parameters have no approved manifest")
    return serialize_export_value(parameters, mode=mode, path="tool.parameters")
