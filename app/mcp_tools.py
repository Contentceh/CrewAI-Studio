"""Bounded, operator-configured MCP adapters for CrewAI Studio.

The registry is intentionally closed: the UI can select an operator-defined
adapter, but cannot supply an arbitrary command, URL, credential or scope.
"""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import threading
import time
import re
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import socket
import fcntl
from pathlib import Path
from typing import Any

from crewai.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field


MAX_TIMEOUT_SECONDS = 30
MAX_RESULT_BYTES = 64 * 1024
MCP_CALL_COUNT = 0
MCP_SUCCESS_COUNT = 0
_RUN_ID: ContextVar[str] = ContextVar("crewai_run_id", default="unknown")
_EVENT_PATH: ContextVar[str] = ContextVar("mcp_event_path", default="/tmp/crewai-stage2-mcp-events.jsonl")
_DIGEST_PATH: ContextVar[str] = ContextVar("mcp_digest_path", default="")
_CALL_BUDGET: ContextVar[list[int] | None] = ContextVar("mcp_call_budget", default=None)
_EVENT_SINK: ContextVar[Any] = ContextVar("mcp_event_sink", default=None)
_SERVER_FD: ContextVar[int | None] = ContextVar("mcp_server_fd", default=None)


class EventCapability:
    """One inherited socket, one parent-issued authority; acknowledgements are durable."""
    def __init__(self, fd: int):
        self.socket = socket.socket(fileno=fd)
        self.socket.settimeout(10)
        self.token = self.socket.recv(4096).decode("ascii")
        self.lock = threading.Lock()

    def request(self, payload):
        with self.lock:
            self.socket.sendall(json.dumps({"token": self.token, "payload": payload}).encode())
            response = json.loads(self.socket.recv(65536))
        if response.get("ok") is not True:
            raise OSError("event acknowledgement denied")
        return response.get("rows")

    def append(self, event, source, outcome="observed"):
        return self.request({"event": event, "source": source, "outcome": outcome})

    def close(self):
        self.socket.close()


@contextmanager
def mcp_run_scope(run_id: str, *, event_path: str | None = None, digest_path: str = "", max_tool_calls: int | None = None,
                  event_sink=None, server_fd: int | None = None):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", run_id):
        raise MCPToolError("invalid_run_id", "Invalid trace identifier")
    tokens = [_RUN_ID.set(run_id), _EVENT_PATH.set(event_path or _EVENT_PATH.get()), _DIGEST_PATH.set(digest_path)]
    budget_token = _CALL_BUDGET.set([max_tool_calls] if max_tool_calls is not None else _CALL_BUDGET.get())
    sink_token = _EVENT_SINK.set(event_sink if event_sink is not None else _EVENT_SINK.get())
    server_token = _SERVER_FD.set(server_fd if server_fd is not None else _SERVER_FD.get())
    try:
        yield
    finally:
        _SERVER_FD.reset(server_token)
        _EVENT_SINK.reset(sink_token)
        _CALL_BUDGET.reset(budget_token)
        _DIGEST_PATH.reset(tokens[2])
        _EVENT_PATH.reset(tokens[1])
        _RUN_ID.reset(tokens[0])


class MCPToolError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


MCP_ADAPTER_REGISTRY = {
    "project_fixture": {
        "transport": "stdio",
        "command": (sys.executable, str(Path(__file__).parent / "fixtures" / "mcp_fixture_server.py")),
        "credential_ref": None,
        "approved_resources": ("project-fixture",),
        "tools": ("fixture.read",),
        "timeout_seconds": 10,
        "max_result_bytes": 16 * 1024,
        "max_retries": 0,
        "env": {"PYTHONUNBUFFERED": "1"},
    },
}


def _bounded_json(value: Any, limit: int) -> str:
    try:
        result = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise MCPToolError("invalid_result", "MCP result is not JSON serializable") from exc
    if len(result.encode("utf-8")) > limit:
        raise MCPToolError("result_too_large", "MCP result exceeds the configured limit")
    return result


class _StdioClient:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.process: subprocess.Popen[str] | None = None
        self.lock = threading.RLock()
        self.request_id = 0
        self.initialized = False
        self.tools: set[str] | None = None
        self.trace_identity: tuple[str, str, str] | None = None

    def _environment(self) -> dict[str, str]:
        env = {key: value for key, value in self.config.get("env", {}).items() if key == "PYTHONUNBUFFERED"}
        env.update(STAGE2_RUN_ID=_RUN_ID.get(), STAGE2_EVENT_PATH=_EVENT_PATH.get(), STAGE2_MARKER_DIGEST_PATH=_DIGEST_PATH.get())
        if _SERVER_FD.get() is not None:
            env = {"PYTHONUNBUFFERED": "1", "STAGE2_RUN_ID": _RUN_ID.get(), "STAGE2_SERVER_FD": str(_SERVER_FD.get())}
        return env

    def _start(self) -> None:
        identity = (_RUN_ID.get(), _EVENT_PATH.get(), _DIGEST_PATH.get())
        if self.process:
            if self.process.poll() is None and self.trace_identity == identity:
                return
            self._reset_process()
        self.process = subprocess.Popen(
            list(self.config["command"]),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            close_fds=True,
            env=self._environment(),
            pass_fds=(() if _SERVER_FD.get() is None else (_SERVER_FD.get(),)),
        )
        self.trace_identity = identity

    def _reset_process(self) -> None:
        process = self.process
        self.initialized = False
        self.tools = None
        self.trace_identity = None
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    # Retain ownership and refuse replacement if the OS cannot reap.
                    raise MCPToolError("cleanup_failed", "MCP child could not be reaped") from None
        finally:
            for pipe in (process.stdin, process.stdout):
                if pipe:
                    pipe.close()
        self.process = None

    def close(self) -> None:
        with self.lock:
            self._reset_process()

    def _call_locked(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        """Call while ``self.lock`` is held; all failures invalidate the child."""
        with self.lock:
            if method in {"tools/list", "tools/call"}:
                if not self.process or self.process.poll() is not None or not self.initialized:
                    self._reset_process()
                    raise MCPToolError("eof", "MCP child exited during the operation")
            else:
                self._start()
            assert self.process and self.process.stdin and self.process.stdout
            self.request_id += 1
            request = {"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params}
            payload = (json.dumps(request) + "\n").encode("utf-8")
            limit = int(self.config.get("max_result_bytes", MAX_RESULT_BYTES))
            selector = selectors.DefaultSelector()
            try:
                os.write(self.process.stdin.fileno(), payload)
                deadline = time.monotonic() + (timeout or self.config["timeout_seconds"])
                selector.register(self.process.stdout, selectors.EVENT_READ)
                buffer = b""
                while time.monotonic() < deadline:
                    events = selector.select(max(0, deadline - time.monotonic()))
                    if not events:
                        raise MCPToolError("timeout", "MCP request timed out")
                    chunk = os.read(self.process.stdout.fileno(), limit - len(buffer) + 1)
                    if not chunk:
                        raise MCPToolError("eof", "MCP process closed stdout")
                    buffer += chunk
                    if len(buffer) > limit:
                        raise MCPToolError("result_too_large", "MCP response exceeds the configured limit")
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if not line:
                            continue
                        try:
                            response = json.loads(line.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                            raise MCPToolError("protocol_error", "Invalid MCP JSON-RPC response") from exc
                        if not isinstance(response, dict):
                            raise MCPToolError("protocol_error", "Invalid MCP response object")
                        if response.get("id") != request["id"]:
                            continue
                        if "error" in response:
                            raise MCPToolError("remote_error", "MCP request failed")
                        return response.get("result")
                raise MCPToolError("timeout", "MCP request timed out")
            except (MCPToolError, OSError) as exc:
                self._reset_process()
                if isinstance(exc, MCPToolError):
                    raise
                raise MCPToolError("transport_error", "MCP transport failed") from exc
            finally:
                selector.close()

    def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        with self.lock:
            return self._call_locked(method, params, timeout)

    def initialize_once(self) -> None:
        with self.lock:
            self._start()
            if self.initialized:
                return
            self._call_locked("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "crewai-studio", "version": "stage2"}})
            assert self.process and self.process.stdin
            try:
                os.write(self.process.stdin.fileno(), b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n')
                self.initialized = True
            except OSError as exc:
                self._reset_process()
                raise MCPToolError("transport_error", "MCP initialization failed") from exc


_CLIENTS: dict[str, _StdioClient] = {}
_CLIENTS_LOCK = threading.Lock()


def _client_for(adapter_id: str) -> _StdioClient:
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(adapter_id)
        if client is None:
            client = _StdioClient(MCP_ADAPTER_REGISTRY[adapter_id])
            _CLIENTS[adapter_id] = client
        return client


def close_mcp_clients() -> None:
    with _CLIENTS_LOCK:
        for client in _CLIENTS.values():
            client.close()
        _CLIENTS.clear()


def _validate_config(adapter_id: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    config = MCP_ADAPTER_REGISTRY.get(adapter_id)
    if not config:
        raise MCPToolError("denied", "Unknown MCP adapter")
    if tool_name not in config["tools"]:
        raise MCPToolError("denied", "MCP tool is not approved for this adapter")
    if not isinstance(arguments, dict):
        raise MCPToolError("invalid_arguments", "Tool arguments must be an object")
    if set(arguments) - {"fixture"}:
        raise MCPToolError("invalid_arguments", "Tool arguments contain unsupported fields")
    resource = arguments.get("fixture", "project-fixture")
    if not isinstance(resource, str):
        raise MCPToolError("invalid_arguments", "MCP resource must be a string")
    if resource not in config["approved_resources"]:
        raise MCPToolError("denied", "MCP resource is not approved")
    return config


def _event(event: str, *, adapter: str = "project_fixture", tool: str = "fixture.read", resource: str = "project-fixture", outcome: str, arg_keys: list[str] | None = None, marker_digest: str | None = None, marker_present: bool | None = None) -> None:
    payload = {"event": event, "source": "client", "run_id": _RUN_ID.get(), "adapter": adapter, "tool": tool, "resource": resource, "outcome": outcome}
    if arg_keys is not None:
        payload["arg_keys"] = sorted(arg_keys)
    if marker_digest is not None:
        payload["marker_digest"] = marker_digest
    if marker_present is not None:
        payload["marker_present"] = marker_present
    if _EVENT_SINK.get() is not None:
        _EVENT_SINK.get().request(payload)
        return
    try:
        with open(_EVENT_PATH.get(), "a", encoding="utf-8") as event_file:
            fcntl.flock(event_file.fileno(), fcntl.LOCK_EX)
            event_file.write(json.dumps(payload, separators=(",", ":")) + "\n")
            event_file.flush()
            os.fsync(event_file.fileno())
            fcntl.flock(event_file.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise MCPToolError("trace_write_failed", "MCP diagnostic evidence could not be persisted") from exc


def call_mcp_tool(adapter_id: str, tool_name: str, arguments: dict[str, Any], *, run_id: str | None = None) -> Any:
    if run_id is not None:
        with mcp_run_scope(run_id):
            return call_mcp_tool(adapter_id, tool_name, arguments)
    global MCP_CALL_COUNT, MCP_SUCCESS_COUNT
    try:
        config = _validate_config(adapter_id, tool_name, arguments)
        client = _client_for(adapter_id)
        with client.lock:
            budget = _CALL_BUDGET.get()
            if budget is not None:
                if budget[0] <= 0:
                    raise MCPToolError("call_budget_exhausted", "MCP run call budget exhausted")
                budget[0] -= 1
            client.initialize_once()
            if client.tools is None:
                listed = client._call_locked("tools/list", {}) or {}
                client.tools = {item.get("name") for item in listed.get("tools", [])}
            if tool_name not in client.tools:
                raise MCPToolError("unavailable", "Approved MCP tool is not advertised by the server")
            MCP_CALL_COUNT += 1
            _event("mcp_tools_call_started", outcome="started", arg_keys=list(arguments))
            result = client._call_locked("tools/call", {"name": tool_name, "arguments": arguments})
        result = json.loads(_bounded_json(result, config["max_result_bytes"]))
        if not isinstance(result, dict) or result.get("isError"):
            raise MCPToolError("remote_error", "MCP tool returned an error")
        _event("mcp_tools_call_success", outcome="success", arg_keys=list(arguments))
        MCP_SUCCESS_COUNT += 1
        return result
    except Exception:
        _event("mcp_tools_call_failure", outcome="failed")
        raise


def returned_marker_digest(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    content = result.get("content")
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict):
        return None
    marker = content[0].get("text")
    if not isinstance(marker, str) or not re.fullmatch(r"stage2-fixture-[0-9a-f]{32}", marker):
        return None
    return hashlib.sha256(marker.encode()).hexdigest()


class MCPArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fixture: str = Field(default="project-fixture")
    # CrewAI 1.5 injects this internal context before BaseTool validation.
    # It is accepted only for framework compatibility and never forwarded.
    security_context: dict[str, Any] | None = Field(default=None, exclude=True)


class MCPToolAdapter(BaseTool):
    name: str
    description: str
    args_schema: type[BaseModel] = MCPArguments
    adapter_id: str = ""
    remote_tool_name: str = ""
    result_as_answer: bool = True

    def _run(self, fixture: str = "project-fixture", **kwargs: Any) -> str:
        kwargs.pop("security_context", None)
        if kwargs:
            raise MCPToolError("invalid_arguments", "Tool arguments contain unsupported fields")
        arguments = {"fixture": fixture}
        _event("crewai_base_tool_enter", outcome="started")
        try:
            result = call_mcp_tool(self.adapter_id, self.remote_tool_name, arguments)
            output = _bounded_json(result, MAX_RESULT_BYTES)
            digest = returned_marker_digest(result)
            _event("crewai_base_tool_success", outcome="success", marker_digest=digest, marker_present=digest is not None)
            return output
        except Exception:
            _event("crewai_base_tool_failure", outcome="failed")
            raise


def make_mcp_tool(tool_id: str | None = None, adapter_id: str = "project_fixture", remote_tool_name: str = "fixture.read", **kwargs: Any) -> MCPToolAdapter:
    _validate_config(adapter_id, remote_tool_name, {})
    config = MCP_ADAPTER_REGISTRY[adapter_id]
    return MCPToolAdapter(
        # CrewAI/OpenAI function names must be identifier-like; retain the
        # adapter and remote tool namespaces without punctuation.
        name=f"mcp_{adapter_id}_{remote_tool_name.replace('.', '_')}",
        description="Read the bounded project fixture through the approved MCP adapter.",
        adapter_id=adapter_id,
        remote_tool_name=remote_tool_name,
        **kwargs,
    )


def mcp_tool_metadata() -> dict[str, Any]:
    config = MCP_ADAPTER_REGISTRY["project_fixture"]
    return {
        "adapter_id": "project_fixture",
        "remote_tool_name": "fixture.read",
        "credential_ref": config["credential_ref"],
        "approved_resources": list(config["approved_resources"]),
        "timeout_seconds": config["timeout_seconds"],
        "max_result_bytes": config["max_result_bytes"],
        "max_retries": config["max_retries"],
    }
