"""Bounded discovery probes; never call a tool or share a crew's MCP client."""

import atexit
import json
import os
import selectors
import signal
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

TTL_SECONDS = 300
MESSAGES = {
    "unavailable": "Сервер недоступен или завершил соединение.",
    "timeout": "Истёк таймаут проверки.",
    "auth": "Ошибка авторизации MCP.",
    "protocol": "Некорректный ответ MCP или ошибка протокола.",
    "remote": "MCP-сервер отклонил запрос проверки.",
    "profile": "MCP-профиль недоступен: prerequisite не подтверждён.",
    "transport": "Проверка этого транспорта пока не поддерживается.",
    "cleanup": "Не удалось завершить процесс проверки.",
}


class ProbeError(Exception):
    pass


@dataclass(frozen=True)
class Result:
    fingerprint: str
    checked_at: float
    ok: bool
    tools: tuple = ()
    error: str = ""


def status(connection, result=None, pending=False, now=None):
    if not connection.enabled:
        return "⚪ Выключен"
    if pending:
        return "⏳ Проверяется"
    if (result is None or result.fingerprint != connection.fingerprint
            or (time.time() if now is None else now) - result.checked_at >= TTL_SECONDS):
        return "⚪ Не проверен"
    return "🟢 Работает" if result.ok else "🔴 Не работает"


def _discover(config):
    timeout = max(0.1, min(float(config.get("timeout_seconds", 10)), 30))
    limit = max(1024, min(int(config.get("max_result_bytes", 65536)), 65536))
    deadline = time.monotonic() + timeout
    process = None
    with tempfile.TemporaryDirectory(prefix="studio-mcp-health-") as directory:
        try:
            # Match the closed runtime adapter's minimal environment contract.
            env = {**config.get("env", {}), "STAGE2_RUN_ID": "health-check",
                   "STAGE2_EVENT_PATH": str(Path(directory) / "events.jsonl")}
            command = config["command"] if isinstance(config["command"], list) else [config["command"], *config.get("args", [])]
            process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       env=env, start_new_session=True, close_fds=True)
            os.set_blocking(process.stdin.fileno(), False)
            os.set_blocking(process.stdout.fileno(), False)
            buffer = b""
            total = 0

            def ready(pipe, event):
                with selectors.DefaultSelector() as selector:
                    selector.register(pipe, event)
                    if not selector.select(max(0, deadline - time.monotonic())):
                        raise ProbeError("timeout")

            def send(message):
                payload = (json.dumps(message) + "\n").encode()
                while payload:
                    if time.monotonic() >= deadline:
                        raise ProbeError("timeout")
                    ready(process.stdin, selectors.EVENT_WRITE)
                    try:
                        payload = payload[os.write(process.stdin.fileno(), payload):]
                    except BlockingIOError:
                        continue

            def request(request_id, method, params):
                nonlocal buffer, total
                send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
                while time.monotonic() < deadline:
                    if b"\n" not in buffer:
                        ready(process.stdout, selectors.EVENT_READ)
                        try:
                            chunk = os.read(process.stdout.fileno(), 4096)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            raise ProbeError("unavailable")
                        total += len(chunk)
                        if total > limit:
                            raise ProbeError("protocol")
                        buffer += chunk
                        continue
                    line, buffer = buffer.split(b"\n", 1)
                    try:
                        response = json.loads(line)
                    except (ValueError, UnicodeError):
                        raise ProbeError("protocol") from None
                    if not isinstance(response, dict) or response.get("jsonrpc") != "2.0":
                        raise ProbeError("protocol")
                    if response.get("id") != request_id:
                        continue
                    if "error" in response:
                        error = response["error"]
                        code = error.get("code") if isinstance(error, dict) else None
                        raise ProbeError("auth" if code in (401, 403) else "remote")
                    if not isinstance(response.get("result"), dict):
                        raise ProbeError("protocol")
                    return response["result"]
                raise ProbeError("timeout")

            initialized = request(1, "initialize", {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "studio-health", "version": "1"},
            })
            info = initialized.get("serverInfo")
            capabilities = initialized.get("capabilities")
            if (initialized.get("protocolVersion") not in ("2024-11-05", "2025-03-26", "2025-06-18")
                    or not isinstance(info, dict) or not isinstance(info.get("name"), str)
                    or not isinstance(info.get("version"), str)
                    or not isinstance(capabilities, dict) or not isinstance(capabilities.get("tools"), dict)):
                raise ProbeError("protocol")
            send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
            names = []
            cursor = None
            for page in range(10):
                result = request(page + 2, "tools/list", {"cursor": cursor} if cursor else {})
                if not isinstance(result.get("tools"), list):
                    raise ProbeError("protocol")
                for tool in result["tools"]:
                    if (not isinstance(tool, dict) or not isinstance(tool.get("name"), str)
                            or not tool["name"] or len(tool["name"]) > 128
                            or not isinstance(tool.get("inputSchema"), dict)
                            or tool["inputSchema"].get("type") != "object"):
                        raise ProbeError("protocol")
                    names.append(tool["name"])
                cursor = result.get("nextCursor")
                if cursor is None:
                    return tuple(sorted(set(names)))
                if not isinstance(cursor, str) or not cursor:
                    raise ProbeError("protocol")
            raise ProbeError("protocol")
        finally:
            if process:
                try:
                    # Include descendants, even if the direct child already exited.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    raise ProbeError("cleanup") from None
                finally:
                    process.stdin.close()
                    process.stdout.close()


def probe(connection):
    if not connection.enabled:
        return Result(connection.fingerprint, time.time(), False)
    error = ""
    tools = ()
    try:
        if not connection.registered:
            raise ProbeError("profile")
        if connection.transport != "stdio":
            raise ProbeError("transport")
        if connection.native:
            from mcp_profiles import ProfileError, profile_spawn_spec
            try:
                command, env, timeout = profile_spawn_spec(connection.adapter_id)
            except (ProfileError, OSError):
                raise ProbeError("profile") from None
            config = {"command": command, "env": env, "timeout_seconds": timeout}
            tools = _discover(config)
        else:
            if not connection.config.get("command"):
                raise ProbeError("profile")
            tools = _discover(connection.config)
    except ProbeError as exc:
        error = MESSAGES.get(str(exc), MESSAGES["protocol"])
    except OSError:
        error = MESSAGES["unavailable"]
    except Exception:
        error = MESSAGES["protocol"]
    return Result(connection.fingerprint, time.time(), not error, tools, error)


class HealthChecks:
    """One bounded, shared queue per Studio process; reruns only read results."""

    def __init__(self, check=probe):
        self.check = check
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mcp-health")
        self.lock = threading.Lock()
        self.jobs = {}
        self.results = {}

    def _collect(self):
        for key, future in list(self.jobs.items()):
            if future.done():
                self.results[key] = future.result()
                del self.jobs[key]

    def submit(self, connection):
        with self.lock:
            self._collect()
            key = connection.adapter_id
            if not connection.enabled or key in self.jobs or len(self.jobs) >= 32:
                return False
            self.jobs[key] = self.pool.submit(self.check, connection)
            return True

    def snapshot(self, connection):
        with self.lock:
            self._collect()
            return self.results.get(connection.adapter_id), connection.adapter_id in self.jobs

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)


CHECKS = HealthChecks()
atexit.register(CHECKS.close)
