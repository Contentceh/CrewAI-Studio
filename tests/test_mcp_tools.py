import sys
import os
import re
import time
import json
import hashlib
import subprocess
from types import SimpleNamespace
from contextlib import contextmanager
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

import mcp_tools
from export_policy import UnsupportedExport, serialize_export_value, serialize_tool_parameters
from mcp_tools import MCPToolError, call_mcp_tool, close_mcp_clients, make_mcp_tool  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_trace(tmp_path, monkeypatch):
    close_mcp_clients()
    path = tmp_path / "events.jsonl"
    monkeypatch.setattr(mcp_tools, "MCP_CALL_COUNT", 0)
    monkeypatch.setattr(mcp_tools, "MCP_SUCCESS_COUNT", 0)
    with mcp_tools.mcp_run_scope("test-run", event_path=str(path)):
        yield path
        close_mcp_clients()


def events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def assert_single_handshake(entries, count=1):
    for name in ("server_initialize", "server_initialized", "server_tools_list"):
        assert sum(e["event"] == name and e["source"] == "server" for e in entries) == count


def test_real_fixture_mcp_call_and_result_cap():
    result = call_mcp_tool("project_fixture", "fixture.read", {"fixture": "project-fixture"})
    assert re.fullmatch(r"stage2-fixture-[0-9a-f]{32}", result["content"][0]["text"])
    assert re.search(r"stage2-fixture-[0-9a-f]{32}", make_mcp_tool().run(fixture="project-fixture"))


def test_unknown_adapter_and_tool_are_denied():
    with pytest.raises(MCPToolError, match="Unknown MCP adapter"):
        call_mcp_tool("not-approved", "fixture.read", {})
    with pytest.raises(MCPToolError, match="not approved"):
        call_mcp_tool("project_fixture", "shell.exec", {})


def test_invalid_arguments_are_rejected():
    with pytest.raises(MCPToolError, match="object"):
        call_mcp_tool("project_fixture", "fixture.read", [])
    with pytest.raises(MCPToolError, match="unsupported"):
        call_mcp_tool("project_fixture", "fixture.read", {"fixture": "project-fixture", "extra": True})
    with pytest.raises(MCPToolError, match="unsupported"):
        call_mcp_tool("project_fixture", "fixture.read", {"fixture": "project-fixture", "headers": {}})


def test_child_does_not_inherit_credentials_and_lifecycle_is_cached(monkeypatch, isolated_trace):
    monkeypatch.setenv("STAGE2_INHERITED_SECRET_SENTINEL", "must-not-inherit")
    close_mcp_clients()
    assert call_mcp_tool("project_fixture", "fixture.read", {"fixture": "project-fixture"})
    client = mcp_tools._CLIENTS["project_fixture"]
    assert client.initialized is True
    assert client.tools == {"fixture.read"}
    assert mcp_tools.MCP_SUCCESS_COUNT >= 1
    assert call_mcp_tool("project_fixture", "fixture.read", {})
    assert_single_handshake(events(isolated_trace))
    assert set(client._environment()) == {"PYTHONUNBUFFERED", "STAGE2_RUN_ID", "STAGE2_EVENT_PATH", "STAGE2_MARKER_DIGEST_PATH"}


def test_export_policy_redacts_nested_secrets_and_allows_only_refs():
    data = {"headers": {"Authorization": "secret", "Cookie": "cookie"}, "nested": [{"gh_token": "token"}], "credential_ref": "ref:operator/fixture"}
    redacted = serialize_export_value(data, mode="redact")
    assert "secret" not in str(redacted)
    assert redacted["credential_ref"] == "ref:operator/fixture"
    with pytest.raises(UnsupportedExport):
        serialize_export_value(data, mode="block")
    with pytest.raises(UnsupportedExport):
        serialize_export_value({"credential_ref": "raw-secret"}, mode="block")


def test_tool_id_is_namespaced():
    assert make_mcp_tool().name == "mcp_project_fixture_fixture_read"


def test_concurrent_calls_share_one_initialized_process(isolated_trace):
    from concurrent.futures import ThreadPoolExecutor
    close_mcp_clients()
    def invoke(_):
        with mcp_tools.mcp_run_scope("test-run", event_path=str(isolated_trace)):
            return call_mcp_tool("project_fixture", "fixture.read", {})
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(invoke, range(4)))
    assert len(results) == 4
    entries = events(isolated_trace)
    assert_single_handshake(entries)
    assert sum(e["event"] == "mcp_server_tool_executed" and e["source"] == "server" for e in entries) == 4


class _FakeSelector:
    instances = []

    def __init__(self):
        self.closed = False
        self.events = [(object(), 1)]
        self.__class__.instances.append(self)

    def register(self, *args):
        return None

    def select(self, _timeout):
        return self.events

    def close(self):
        self.closed = True


class _SilentSelector(_FakeSelector):
    def __init__(self):
        super().__init__()
        self.events = []


class _FakePipe:
    closed = False

    def fileno(self):
        return 42

    def close(self):
        self.closed = True


class _FakeProcess:
    def __init__(self, output):
        self.stdin = _FakePipe()
        self.stdout = _FakePipe()
        self.output = output
        self.returncode = None
        self.killed = False
        self.waits = 0

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def terminate(self):
        self.kill()

    def wait(self, timeout=None):
        self.waits += 1
        self.returncode = self.returncode or 0
        return self.returncode


def test_selector_closes_on_success_and_remote_error(monkeypatch):
    responses = [b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n', b'{"jsonrpc":"2.0","id":1,"error":{"message":"nope"}}\n']
    processes = [_FakeProcess(item) for item in responses]
    monkeypatch.setattr(mcp_tools.selectors, "DefaultSelector", _FakeSelector)
    monkeypatch.setattr(mcp_tools.os, "write", lambda *_args: None)
    monkeypatch.setattr(mcp_tools.os, "read", lambda *_args: processes.pop(0).output if processes else b"")
    for expected in (None, "remote_error"):
        client = mcp_tools._StdioClient({"command": (), "timeout_seconds": 1, "max_result_bytes": 1024})
        process = processes[0] if processes else None
        client._start = lambda p=process: setattr(client, "process", p)
        if expected:
            with pytest.raises(MCPToolError, match=expected):
                client.call("x", {})
        else:
            assert client.call("x", {}) == {"ok": True}
        assert _FakeSelector.instances[-1].closed


def test_timeout_resets_and_next_call_reinitializes(monkeypatch):
    client = mcp_tools._StdioClient({"command": (), "timeout_seconds": 0.001, "max_result_bytes": 1024})
    first = _FakeProcess(b"")
    second = _FakeProcess(b'{"jsonrpc":"2.0","id":2,"result":{"ok":true}}\n')
    client.process = first
    client.trace_identity = (mcp_tools._RUN_ID.get(), mcp_tools._EVENT_PATH.get(), mcp_tools._DIGEST_PATH.get())
    monkeypatch.setattr(mcp_tools.selectors, "DefaultSelector", _SilentSelector)
    monkeypatch.setattr(mcp_tools.os, "write", lambda *_args: None)
    monkeypatch.setattr(mcp_tools.os, "read", lambda *_args: b"")
    with pytest.raises(MCPToolError, match="timeout"):
        client.call("x", {}, timeout=0)
    assert client.process is None and not client.initialized and client.tools is None
    client.process = second
    client.trace_identity = (mcp_tools._RUN_ID.get(), mcp_tools._EVENT_PATH.get(), mcp_tools._DIGEST_PATH.get())
    monkeypatch.setattr(mcp_tools.selectors, "DefaultSelector", _FakeSelector)
    monkeypatch.setattr(mcp_tools.os, "read", lambda *_args: second.output)
    assert client.call("x", {}) == {"ok": True}


def test_oversized_stream_is_rejected_before_accumulation(monkeypatch):
    process = _FakeProcess(b"x" * 20)
    client = mcp_tools._StdioClient({"command": (), "timeout_seconds": 1, "max_result_bytes": 8})
    client.process = process
    client.trace_identity = (mcp_tools._RUN_ID.get(), mcp_tools._EVENT_PATH.get(), mcp_tools._DIGEST_PATH.get())
    monkeypatch.setattr(mcp_tools.selectors, "DefaultSelector", _FakeSelector)
    monkeypatch.setattr(mcp_tools.os, "write", lambda *_args: None)
    sizes = []
    def read(_fd, size):
        sizes.append(size)
        return process.output[:size]
    monkeypatch.setattr(mcp_tools.os, "read", read)
    with pytest.raises(MCPToolError, match="result_too_large"):
        client.call("x", {})
    assert client.process is None and process.killed
    assert sizes == [9]
    assert process.waits and process.stdin.closed and process.stdout.closed


def test_trace_contains_only_sanitized_fields(isolated_trace, tmp_path):
    close_mcp_clients()
    digest_path = tmp_path / "digest"
    with mcp_tools.mcp_run_scope("run-test", digest_path=str(digest_path)):
        result = json.loads(make_mcp_tool().run(fixture="project-fixture"))
    entries = events(isolated_trace)
    names = {e["event"] for e in entries}
    assert {"crewai_base_tool_enter", "mcp_tools_call_success", "mcp_server_tool_executed", "crewai_base_tool_success"} <= names
    assert all(e["run_id"] == "run-test" for e in entries)
    assert all(e["source"] == ("server" if e["event"].startswith("server_") or e["event"] == "mcp_server_tool_executed" else "client") for e in entries)
    expected = hashlib.sha256(result["content"][0]["text"].encode()).hexdigest()
    assert digest_path.read_text() == expected
    assert [e["marker_digest"] for e in entries if "marker_digest" in e] == [expected, expected]
    assert "stage2-fixture-" not in isolated_trace.read_text()
    assert "authorization" not in isolated_trace.read_text().lower()
    assert mcp_tools._RUN_ID.get() == "test-run"
    ordered = [e["event"] for e in entries]
    assert ordered.index("mcp_server_tool_executed") < ordered.index("mcp_tools_call_success") < ordered.index("crewai_base_tool_success")


def test_dead_initialized_child_is_reaped_before_single_new_handshake(isolated_trace):
    call_mcp_tool("project_fixture", "fixture.read", {})
    client = mcp_tools._CLIENTS["project_fixture"]
    first = client.process
    first.kill()
    first.wait(timeout=2)
    assert client.initialized and client.tools
    call_mcp_tool("project_fixture", "fixture.read", {})
    assert client.process is not first
    assert first.stdin.closed and first.stdout.closed
    assert_single_handshake(events(isolated_trace), count=2)


def test_cleanup_escalates_and_reaps(monkeypatch):
    process = _FakeProcess(b"")
    process.terminate = lambda: None
    original_wait = process.wait
    def wait(timeout):
        if not process.killed:
            raise subprocess.TimeoutExpired("fixture", timeout)
        return original_wait(timeout)
    process.wait = wait
    client = mcp_tools._StdioClient({})
    client.process = process
    client.initialized, client.tools = True, {"fixture.read"}
    client.close()
    assert process.killed and process.waits == 1
    assert client.process is None and not client.initialized and client.tools is None
    assert process.stdin.closed and process.stdout.closed


def test_cleanup_failure_refuses_spawn(monkeypatch):
    process = _FakeProcess(b"")
    def wait(timeout):
        raise subprocess.TimeoutExpired("fixture", timeout)
    process.wait = wait
    client = mcp_tools._StdioClient({})
    client.process = process
    with pytest.raises(MCPToolError, match="cleanup_failed"):
        client._start()
    assert client.process is process
    assert not client.initialized and client.tools is None


def test_failure_events_are_not_success_or_remote_values(monkeypatch, isolated_trace):
    client = mcp_tools._client_for("project_fixture")
    def fail(method, params):
        raise MCPToolError("remote_error", "synthetic-secret-never-log")
    monkeypatch.setattr(client, "_call_locked", fail)
    with pytest.raises(MCPToolError):
        make_mcp_tool()._run()
    names = [e["event"] for e in events(isolated_trace)]
    assert names == ["crewai_base_tool_enter", "mcp_tools_call_failure", "crewai_base_tool_failure"]
    assert "synthetic-secret-never-log" not in isolated_trace.read_text()


@pytest.mark.parametrize("value", [1, [], {}, True])
def test_resource_type_rejected(value):
    with pytest.raises(MCPToolError, match="invalid_arguments"):
        call_mcp_tool("project_fixture", "fixture.read", {"fixture": value})


def test_resource_and_schema_rejection():
    with pytest.raises(MCPToolError, match="denied"):
        call_mcp_tool("project_fixture", "fixture.read", {"fixture": "other"})
    from pydantic import ValidationError
    for arguments in ({"fixture": 1}, {"extra": True}):
        with pytest.raises(ValidationError):
            make_mcp_tool().args_schema.model_validate(arguments)


ATTACKS = [
    {"base_url": "//user:password@host/path"},
    {"base_url": "https://host/?redirect=https%3A%2F%2Fuser%3Apassword%40host"},
    {"base_url": "https://host/?redirect=%252F%252Fuser%253Apassword%2540host"},
    *({"base_url": "https://host/?" + key + "=x"} for key in ("sig", "signature", "session", "access_key", "unknown")),
    {"nested": [{"url": "//user:password@host"}]},
    {"unknown": "x"}, {"headers": {"custom": "x"}},
    {"credential_ref": "ref:unregistered"},
    {"credential_ref": "ref:operator/fixture", "unknown": "x"},
]


@pytest.mark.parametrize("parameters", ATTACKS)
@pytest.mark.parametrize("site", ["db", "crew", "standalone"])
def test_export_entry_points_fail_closed(parameters, site, tmp_path, monkeypatch):
    # Synthetic rows only: never open or scan a legacy/user database.
    import db_utils
    import pg_export_crew
    tool = SimpleNamespace(name="CustomApiTool", tool_id="test", description="test",
                           parameters=parameters, get_parameters=lambda: parameters)
    agent = SimpleNamespace(id="a", role="test", backstory="test", goal="test", allow_delegation=False,
                            verbose=False, cache=False, llm_provider_model="unused", temperature=0,
                            max_iter=2, tools=[tool])
    crew = SimpleNamespace(id="c", name="test", process="sequential", verbose=False, memory=False,
                           cache=False, planning=False, planning_llm=None, max_rpm=None,
                           manager_llm=None, manager_agent=None, created_at="test", agents=[agent], tasks=[])
    output = tmp_path / "export.json"
    if site == "db":
        @contextmanager
        def connection():
            yield SimpleNamespace(execute=lambda _: [SimpleNamespace(id="t", entity_type="tool", data=json.dumps({"name": tool.name, "parameters": parameters}))])
        monkeypatch.setattr(db_utils, "get_db_connection", connection)
        invoke = lambda: db_utils.export_to_json(output)
    else:
        page = pg_export_crew.PageExportCrew()
        monkeypatch.setattr(pg_export_crew, "ss", SimpleNamespace(tools=[tool]))
        invoke = (lambda: page.generate_streamlit_app(crew, str(tmp_path))) if site == "standalone" else (lambda: page.export_crew_to_json(crew))
    with pytest.raises(UnsupportedExport):
        invoke()
    assert not output.exists() and not (tmp_path / "app.py").exists()


@pytest.mark.parametrize("value", [
    "//user:password@host/path", "https://host/?sig=x",
    "https://host/?redirect=https%3A%2F%2Fuser%3Apassword%40host",
    "%252F%252Fuser%253Apassword%2540host", "https://host/?unknown=x",
    "%25252525252F%25252525252Fhost",
])
def test_export_nested_url_value_inspection(value):
    with pytest.raises(UnsupportedExport):
        serialize_export_value({"nested": [value]}, mode="block")


def test_export_safe_metadata_and_registered_ref():
    assert serialize_tool_parameters({}, mode="block") == {}
    ref = {"credential_ref": "ref:operator/fixture"}
    assert serialize_tool_parameters(ref, mode="block") == ref
    assert serialize_export_value({"url": "https://host/path"}, mode="block") == {"url": "https://host/path"}
    with pytest.raises(UnsupportedExport):
        serialize_export_value({"adapter_id": "unknown"}, mode="block")


@pytest.mark.parametrize("value", ["Bearer abc", "bAsIc abc", "prefix https://user:pass@example.test/path",
                                    "text //user:pass@example.test/path"])
def test_export_rejects_case_insensitive_auth_and_embedded_url_userinfo(value):
    with pytest.raises(UnsupportedExport):
        serialize_export_value({"value": value}, mode="block")


def test_export_allows_ordinary_question_text_and_safe_url_query():
    assert serialize_export_value({"prompt": "What should this agent do?"}, mode="block")["prompt"] == "What should this agent do?"
    assert serialize_export_value({"url": "https://example.test/search"}, mode="block") == {"url": "https://example.test/search"}
    with pytest.raises(UnsupportedExport):
        serialize_export_value({"url": "https://example.test/search?q=crew"}, mode="block")
    with pytest.raises(UnsupportedExport):
        serialize_export_value({"url": "https://example.test/?Api-Key=secret"}, mode="block")


def test_db_export_preserves_ordinary_question_text(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    import db_utils

    monkeypatch.setattr(db_utils, "engine", create_engine(f"sqlite:///{tmp_path / 'fixture.db'}"))
    db_utils.create_tables()
    db_utils.save_entity("task", "question", {"prompt": "What should this agent do?"})
    output = tmp_path / "export.json"
    db_utils.export_to_json(output)
    rows = json.loads(output.read_text())
    assert rows[0]["data"]["prompt"] == "What should this agent do?"


@pytest.mark.parametrize("value", ["prefix https://user:password@example.test/path", "text //user:password@example.test/path"])
def test_db_export_blocks_credential_urls_before_output(tmp_path, monkeypatch, value):
    from sqlalchemy import create_engine
    import db_utils

    monkeypatch.setattr(db_utils, "engine", create_engine(f"sqlite:///{tmp_path / 'fixture.db'}"))
    db_utils.create_tables()
    db_utils.save_entity("task", "credential", {"prompt": value})
    output = tmp_path / "export.json"
    with pytest.raises(UnsupportedExport):
        db_utils.export_to_json(output)
    assert not output.exists()


def test_smoke_static_and_real_crew_configuration():
    import smoke_stage2_crewai_mcp as smoke
    assert smoke.static_report()["model_calls"] == 0
    crew = smoke.build_crew("synthetic-not-a-credential")
    assert crew.tasks[0].agent is crew.agents[0]
    assert crew.cache is False
    assert smoke.static_report()["model"] == "gpt-5.6-luna"


def test_smoke_model_override_cannot_change_pinned_model(monkeypatch):
    import smoke_stage2_crewai_mcp as smoke
    monkeypatch.setenv("STAGE2_MODEL", "arbitrary-provider-model")
    assert smoke.build_crew("synthetic-not-a-credential").agents[0].llm.model == "gpt-5.6-luna"


def test_smoke_summarize_distinguishes_no_tool_and_kickoff_exception():
    import smoke_stage2_crewai_mcp as smoke
    no_tool = smoke.summarize([{"run_id": "r", "event": "tool_selection_finished", "outcome": "completed"}], "r", None, "answer")
    assert no_tool["boundary"] == "kickoff_completed_without_tool_selection"
    assert no_tool["failure_category"] is None
    exception = smoke.summarize([{"run_id": "r", "event": "tool_selection_failure", "outcome": "failed", "category": "kickoff_exception", "text": "secret"}], "r", None, "")
    assert exception["boundary"] == "kickoff_exception"
    assert exception["failure_category"] == "kickoff_exception"
    assert "secret" not in json.dumps(exception)


def test_smoke_parent_preserves_valid_blocked_worker_report(monkeypatch, tmp_path, capsys):
    import smoke_stage2_crewai_mcp as smoke
    from test_smoke_provenance import run_fake_parent
    code, publication, _ = run_fake_parent(monkeypatch, tmp_path, "kickoff_exception")
    assert code == 1
    report = smoke.read_committed(publication)
    output = json.loads(capsys.readouterr().out)
    assert output == report
    assert output["status"] == "blocked"
    assert output["counts_complete"] is True
    assert output["counts"]["mcp_tools_call_started"] == 0
    assert output["model"] == smoke.MODEL_ID
    assert output["run_id"].startswith("stage2-")
    assert output["boundary"] == "kickoff_exception"
    assert output["attempts"] == 1 and output["provider_state"] == "not_started"


def test_smoke_final_report_requires_matching_exit_status_and_complete_counts(tmp_path):
    import smoke_stage2_crewai_mcp as smoke

    report_path = tmp_path / "report.json"
    blocked = smoke._report("stage2-r", report_path, counts_complete=True, boundary="tool_chain_or_marker_mismatch")
    smoke._write_report_atomic(report_path, blocked)
    assert smoke._read_final_report(report_path, "stage2-r", 1) == blocked
    assert smoke._read_final_report(report_path, "stage2-r", 0) is None
    incomplete = dict(blocked, counts_complete=False)
    smoke._write_report_atomic(report_path, incomplete)
    assert smoke._read_final_report(report_path, "stage2-r", 1) is None


def test_smoke_requires_exact_correlated_chain_and_marker(isolated_trace, tmp_path):
    import smoke_stage2_crewai_mcp as smoke
    digest = tmp_path / "digest"
    with mcp_tools.mcp_run_scope("smoke-test", digest_path=str(digest)):
        raw = make_mcp_tool()._run()
    entries = events(isolated_trace)
    expected = digest.read_text()
    assert smoke.summarize(entries, "smoke-test", expected, raw)["status"] == "passed"
    for bad_events, bad_raw in [(entries + entries, raw), (entries, "stage2-fixture-" + "a" * 32),
                                ([{**e, "run_id": "wrong"} for e in entries], raw),
                                ([{**e, "source": "client"} for e in entries], raw)]:
        assert smoke.summarize(bad_events, "smoke-test", expected, bad_raw)["status"] == "blocked"


def _run_parent_failure_case(monkeypatch, tmp_path, capsys, *, timeout, term_process_lookup=False):
    import smoke_stage2_crewai_mcp as smoke
    from test_smoke_provenance import run_fake_parent
    signals = []
    original_killpg = smoke.os.killpg
    def killpg(pid, sig):
        signals.append(sig)
        if sig == smoke.signal.SIGTERM:
            if term_process_lookup:
                raise ProcessLookupError
            return
        return original_killpg(pid, sig)
    if timeout:
        monkeypatch.setattr(smoke.os, "killpg", killpg)
    code, publication, processes = run_fake_parent(monkeypatch, tmp_path, "provider_timeout" if timeout else "setup_exception")
    assert code == 1
    output = capsys.readouterr().out
    report = smoke.read_committed(publication)
    assert json.loads(output) == report
    assert report["status"] == "blocked"
    assert report["boundary"] == ("kickoff_timeout" if timeout else "setup_exception")
    assert report["model"] == smoke.MODEL_ID
    assert report["counts"] == {key: 0 for key in smoke.COUNT_KEYS}
    assert report["counts_complete"] is (not timeout)
    assert report["attempts"] == int(timeout)
    assert report["provider_state"] == ("started" if timeout else "not_started")
    assert "sentinel" not in output and "error" not in report
    assert set(report) == smoke.REPORT_KEYS
    assert not list(tmp_path.glob(".*.tmp"))
    if timeout:
        process = processes[0]
        assert process.waits == [120, 2, 2]
        assert signals == [smoke.signal.SIGTERM, smoke.signal.SIGKILL, 0]
        assert process.returncode == -9


def test_smoke_observed_counts_ignore_valid_nonterminated_jsonl(tmp_path):
    import smoke_stage2_crewai_mcp as smoke

    path = tmp_path / "events.jsonl"
    event = {"run_id": "r", "event": "mcp_tools_call_started", "source": "client"}
    path.write_bytes((json.dumps(event) + "\n").encode() + json.dumps(event).encode())
    assert smoke._observed_counts(path, "r")["mcp_tools_call_started"] == 1


def test_parent_worker_failure_overwrites_sanitized_report(monkeypatch, tmp_path, capsys):
    _run_parent_failure_case(monkeypatch, tmp_path, capsys, timeout=False)


def test_parent_kickoff_timeout_overwrites_sanitized_report(monkeypatch, tmp_path, capsys):
    _run_parent_failure_case(monkeypatch, tmp_path, capsys, timeout=True, term_process_lookup=False)


def test_parent_kickoff_timeout_survives_term_process_lookup(monkeypatch, tmp_path, capsys):
    _run_parent_failure_case(monkeypatch, tmp_path, capsys, timeout=True, term_process_lookup=True)


def test_run_budget_blocks_framework_tool_retries(isolated_trace):
    with mcp_tools.mcp_run_scope("bounded", max_tool_calls=1):
        make_mcp_tool()._run()
        with pytest.raises(MCPToolError, match="call_budget_exhausted"):
            make_mcp_tool()._run()
    assert sum(e["event"] == "mcp_server_tool_executed" for e in events(isolated_trace)) == 1
    assert mcp_tools.MCP_CALL_COUNT == 1


def test_real_timeout_restart_handshake(isolated_trace, monkeypatch):
    call_mcp_tool("project_fixture", "fixture.read", {})
    client = mcp_tools._CLIENTS["project_fixture"]
    first = client.process
    with monkeypatch.context() as patch:
        patch.setattr(mcp_tools.selectors, "DefaultSelector", _SilentSelector)
        with pytest.raises(MCPToolError, match="timeout"):
            call_mcp_tool("project_fixture", "fixture.read", {})
    assert client.process is None and not client.initialized and client.tools is None
    assert first.poll() is not None and first.stdin.closed and first.stdout.closed
    call_mcp_tool("project_fixture", "fixture.read", {})
    assert_single_handshake(events(isolated_trace), count=2)


def test_stream_read_uses_remaining_capacity(monkeypatch):
    process = _FakeProcess(b"")
    client = mcp_tools._StdioClient({"timeout_seconds": 1, "max_result_bytes": 8})
    client.process = process
    client.trace_identity = (mcp_tools._RUN_ID.get(), mcp_tools._EVENT_PATH.get(), mcp_tools._DIGEST_PATH.get())
    chunks, sizes = [b"12345", b"6789"], []
    def read(_fd, size):
        sizes.append(size)
        chunk = chunks.pop(0)
        assert len(chunk) <= size
        return chunk
    monkeypatch.setattr(mcp_tools.selectors, "DefaultSelector", _FakeSelector)
    monkeypatch.setattr(mcp_tools.os, "write", lambda *_: None)
    monkeypatch.setattr(mcp_tools.os, "read", read)
    with pytest.raises(MCPToolError, match="result_too_large"):
        client.call("x", {})
    assert sizes == [9, 4]


def test_child_death_during_operation_does_not_spawn_uninitialized(isolated_trace):
    call_mcp_tool("project_fixture", "fixture.read", {})
    client = mcp_tools._CLIENTS["project_fixture"]
    client.process.kill()
    client.process.wait(timeout=2)
    with pytest.raises(MCPToolError, match="eof"):
        client.call("tools/list", {})
    assert client.process is None and not client.initialized
    assert_single_handshake(events(isolated_trace))
