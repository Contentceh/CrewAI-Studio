"""Acceptance smoke for CrewAI's native MCPServerAdapter stdio transport."""

import errno
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

from crewai_tools import MCPServerAdapter
from crewai.tools import BaseTool
from mcp import StdioServerParameters

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from mcp_profiles import native_profile_tools


FIXTURE = Path(__file__).resolve().parents[1] / "app" / "fixtures" / "mcp_fixture_server.py"


def test_native_mcp_server_adapter_discovers_invokes_and_reaps_fixture(tmp_path):
    events = tmp_path / "events.jsonl"
    digest = tmp_path / "marker.digest"
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(FIXTURE)],
        env={
            "PYTHONUNBUFFERED": "1",
            "STAGE2_RUN_ID": "native-mcp-smoke",
            "STAGE2_EVENT_PATH": str(events),
            "STAGE2_MARKER_DIGEST_PATH": str(digest),
        },
    )

    before = _fixture_processes()
    parent_pid = os.getpid()
    with MCPServerAdapter(params) as tools:
        names = [tool.name for tool in tools]
        assert names == ["fixture.read"]
        tool = tools[0]
        assert isinstance(tool, BaseTool)
        assert type(tool).__name__ == "CrewAIMCPTool"
        result = tool.run(fixture="project-fixture")
        assert isinstance(result, str)
        assert digest.exists()
        assert len(digest.read_text(encoding="ascii")) == 64
        children = _fixture_processes()
        new_children = _new_fixture_children(before, children, parent_pid)
        assert len(new_children) == 1
        child = new_children[0]
        assert child["ppid"] == parent_pid
        assert child["state"] != "Z"
        assert child["start_time"] > 0

    assert _wait_for_proc_gone(child["pid"], child["start_time"])
    evidence = events.read_text(encoding="utf-8")
    assert "stage2-fixture-" not in evidence
    rows = [json.loads(line) for line in evidence.splitlines()]
    assert {row["event"] for row in rows} >= {
        "server_initialize",
        "server_tools_list",
        "mcp_server_tool_executed",
    }
    executed = [row for row in rows if row["event"] == "mcp_server_tool_executed"]
    assert len(executed) == 1
    assert executed[0]["marker_digest"] == hashlib.sha256(
        result.encode("utf-8")
    ).hexdigest()


def test_project_registry_native_profile_discovers_all_advertised_tools():
    with native_profile_tools("project_fixture") as tools:
        assert [tool.name for tool in tools] == ["fixture.read"]


def _fixture_processes():
    """Return fixture processes with identity fields from Linux procfs."""
    matches = {}
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            pid = int(proc.name)
            argv = (proc / "cmdline").read_bytes().split(b"\0")
            if str(FIXTURE).encode() not in argv:
                continue
            identity = _read_proc_identity(pid)
            if identity is not None:
                matches[pid] = identity
        except (OSError, UnicodeDecodeError):
            continue
    return matches


def _new_fixture_children(before, children, parent_pid):
    return [
        item for pid, item in children.items()
        if item["ppid"] == parent_pid
        and (pid not in before or before[pid]["start_time"] != item["start_time"])
    ]


def _read_proc_identity(pid):
    """Read stat directly: zombies have no cmdline but still have an identity."""
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text(
            encoding="utf-8", errors="replace"
        )
    except FileNotFoundError:
        return None
    # comm can contain spaces and parentheses; fields after its last ')' start at 3.
    after_comm = stat.rsplit(")", 1)[1].split()
    return {
        "pid": pid,
        "ppid": int(after_comm[1]),
        "state": after_comm[0],
        "start_time": int(after_comm[19]),
    }


def _wait_for_proc_gone(pid, start_time, timeout=2.0):
    """Require this exact PID/start-time identity to disappear, including zombies."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            current = _read_proc_identity(pid)
        except OSError:
            # An unreadable stat is not proof of reaping; retry until the deadline.
            pass
        else:
            if current is None or current["start_time"] != start_time:
                return True
        time.sleep(0.02)
    return False


def _fake_stat(pid=123, ppid=42, state="S", start_time=900):
    return f"{pid} (python (fixture) worker)) {state} {ppid} " + " ".join(
        ["0"] * 17 + [str(start_time), "0", "0"]
    )


@pytest.fixture
def fake_proc(monkeypatch):
    reads = []
    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds

    def no_inventory():
        pytest.fail("Exact cleanup must not use the cmdline-filtered inventory")

    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr(sys.modules[__name__], "_fixture_processes", no_inventory)

    def install(*outcomes):
        def read_text(path, **kwargs):
            assert path == Path("/proc/123/stat")
            outcome = outcomes[min(len(reads), len(outcomes) - 1)]
            reads.append(path)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(Path, "read_text", read_text)
        return reads

    return install


def test_proc_identity_parses_comm_with_spaces_and_parentheses(fake_proc):
    fake_proc(_fake_stat(state="Z"))
    assert _read_proc_identity(123) == {
        "pid": 123, "ppid": 42, "state": "Z", "start_time": 900,
    }


@pytest.mark.parametrize("state", ["R", "S", "D", "T", "Z", "X"])
def test_proc_same_identity_is_not_gone_in_any_state(fake_proc, state):
    reads = fake_proc(_fake_stat(state=state))
    assert not _wait_for_proc_gone(123, 900, timeout=0.05)
    assert len(reads) == 3


def test_proc_reused_pid_means_original_identity_is_gone(fake_proc):
    reads = fake_proc(_fake_stat(start_time=901))
    assert _wait_for_proc_gone(123, 900, timeout=0.05)
    assert len(reads) == 1


def test_proc_absent_identity_is_gone(fake_proc):
    reads = fake_proc(FileNotFoundError(errno.ENOENT, "Process gone"))
    assert _read_proc_identity(123) is None
    assert _wait_for_proc_gone(123, 900, timeout=0.05)
    assert len(reads) == 2


def test_proc_zombie_must_disappear_before_cleanup_passes(fake_proc):
    reads = fake_proc(
        _fake_stat(state="Z"), FileNotFoundError(errno.ENOENT, "Process gone")
    )
    assert _wait_for_proc_gone(123, 900, timeout=0.05)
    assert len(reads) == 2


@pytest.mark.parametrize("error", [errno.EACCES, errno.EIO, errno.EINTR])
def test_proc_read_errors_do_not_prove_cleanup(fake_proc, error):
    reads = fake_proc(OSError(error, "Cannot read stat"))
    assert not _wait_for_proc_gone(123, 900, timeout=0.05)
    assert len(reads) == 3


def test_proc_transient_read_error_retries_until_absent(fake_proc):
    reads = fake_proc(
        PermissionError(errno.EACCES, "Cannot read stat"),
        _fake_stat(state="Z"),
        FileNotFoundError(errno.ENOENT, "Process gone"),
    )
    assert _wait_for_proc_gone(123, 900, timeout=0.05)
    assert len(reads) == 3


def test_new_fixture_children_ignores_unrelated_parent():
    existing = {"pid": 121, "ppid": 42, "state": "S", "start_time": 800}
    child = {"pid": 123, "ppid": 42, "state": "S", "start_time": 900}
    unrelated = {"pid": 124, "ppid": 99, "state": "S", "start_time": 901}
    before = {121: existing}
    children = {121: existing, 123: child, 124: unrelated}
    assert _new_fixture_children(before, children, 42) == [child]


def test_new_fixture_children_preserves_reused_pid_identity():
    old = {"pid": 123, "ppid": 42, "state": "S", "start_time": 800}
    child = {"pid": 123, "ppid": 42, "state": "S", "start_time": 900}
    assert _new_fixture_children({123: old}, {123: child}, 42) == [child]
