#!/usr/bin/env python3
"""Tiny JSON-RPC MCP fixture used by bounded Stage 2 tests and smoke checks."""

import json
import hashlib
import os
import secrets
import sys
import socket
import fcntl
from pathlib import Path


EVENT_LOG = Path(os.environ["STAGE2_EVENT_PATH"]) if "STAGE2_EVENT_PATH" in os.environ else None
EVENT_SOCKET = socket.socket(fileno=int(os.environ["STAGE2_SERVER_FD"])) if "STAGE2_SERVER_FD" in os.environ else None
if EVENT_SOCKET:
    EVENT_SOCKET.settimeout(10)
    EVENT_TOKEN = EVENT_SOCKET.recv(4096).decode("ascii")
RUN_ID = os.environ["STAGE2_RUN_ID"]
ENV_SENTINEL = "STAGE2_INHERITED_SECRET_SENTINEL"
SERVER_MARKER = f"stage2-fixture-{secrets.token_hex(16)}"
MARKER_DIGEST = hashlib.sha256(SERVER_MARKER.encode()).hexdigest()
if os.environ.get("STAGE2_MARKER_DIGEST_PATH"):
    Path(os.environ["STAGE2_MARKER_DIGEST_PATH"]).write_text(MARKER_DIGEST, encoding="ascii")


def reply(request, result=None, error=None):
    response = {"jsonrpc": "2.0", "id": request.get("id")}
    if error:
        response["error"] = error
    else:
        response["result"] = result
    print(json.dumps(response), flush=True)


def event(name, **fields):
    payload = {"event": name, "source": "server", "run_id": RUN_ID, "adapter": "project_fixture", "tool": "fixture.read", "resource": "project-fixture", "outcome": "success", **fields}
    if EVENT_SOCKET:
        EVENT_SOCKET.sendall(json.dumps({"token": EVENT_TOKEN, "payload": payload}).encode())
        if json.loads(EVENT_SOCKET.recv(4096)).get("ok") is not True:
            raise OSError("event acknowledgement denied")
        return
    with EVENT_LOG.open("a", encoding="utf-8") as log:
        fcntl.flock(log.fileno(), fcntl.LOCK_EX)
        log.write(json.dumps(payload) + "\n")
        log.flush()
        os.fsync(log.fileno())
        fcntl.flock(log.fileno(), fcntl.LOCK_UN)


for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        event("server_initialize")
        reply(request, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "stage2-fixture", "version": "1"}})
    elif method == "notifications/initialized":
        event("server_initialized")
    elif method == "tools/list":
        event("server_tools_list")
        reply(request, {"tools": [{"name": "fixture.read", "description": "Read safe fixture", "inputSchema": {"type": "object", "properties": {"fixture": {"type": "string"}}, "additionalProperties": False}}]})
    elif method == "tools/call":
        if request.get("params", {}).get("name") != "fixture.read":
            reply(request, error={"code": -32601, "message": "unknown tool"})
        else:
            if os.getenv(ENV_SENTINEL):
                event("mcp_server_tool_failure", outcome="failed")
                reply(request, error={"code": -32001, "message": "unexpected inherited environment"})
                continue
            event("mcp_server_tool_executed", outcome="success", marker_digest=MARKER_DIGEST, arg_keys=["fixture"])
            reply(request, {"content": [{"type": "text", "text": SERVER_MARKER}], "isError": False})
    else:
        reply(request, error={"code": -32601, "message": "unknown method"})
