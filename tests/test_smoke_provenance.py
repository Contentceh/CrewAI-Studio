import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import smoke_stage2_crewai_mcp as smoke


def setup_parent(monkeypatch, tmp_path):
    monkeypatch.setenv("STAGE2_REPORT_PATH", str(tmp_path / "canonical.json"))
    monkeypatch.setenv("STAGE2_CONTROLLER_RUN_ID", "run-offline-rescue")
    monkeypatch.setattr(smoke.sys, "argv", ["smoke"])
    return tmp_path


def publication_for(tmp_path):
    bundles = list(tmp_path.glob("canonical.stage2-*.bundle"))
    assert len(bundles) == 1
    return bundles[0]


def optional_publication_for(tmp_path):
    bundles = list(tmp_path.glob("canonical.stage2-*.bundle"))
    return bundles[0] if bundles else tmp_path / "missing.bundle"


def fake_crew(mode, calls):
    def outbound(*args, **kwargs):
        calls.append("outbound")
        if mode == "provider_timeout":
            time.sleep(300)
        return None

    lower = SimpleNamespace(create=outbound)
    llm = SimpleNamespace(client=SimpleNamespace(chat=SimpleNamespace(completions=lower)), stream=False)
    llm.call = lambda: lower.create()
    agent = SimpleNamespace(llm=llm)

    def kickoff():
        calls.append("kickoff")
        if mode == "kickoff_exception":
            raise ValueError("offline")
        if mode != "no_provider":
            llm.call()
        raw = smoke.mcp_tools.make_mcp_tool()._run()
        return SimpleNamespace(raw=raw)

    return SimpleNamespace(agents=[agent], tasks=[SimpleNamespace(agent=agent)], kickoff=kickoff)


def run_fake_parent(monkeypatch, tmp_path, mode="pass", *, fail_reap=False):
    """Fork only at the Popen boundary; child executes the real CLI and worker.

    Crew/LLM are offline fakes; broker, provider installation, tool adapter,
    stdio fixture, worker candidate, cleanup and manifest are production code.
    """
    setup_parent(monkeypatch, tmp_path)
    key = tmp_path / "synthetic-key"
    key.write_text("synthetic-offline-not-a-credential")
    monkeypatch.setenv("STAGE2_MODEL_KEY_FILE", str(key))
    if mode == "configuration_missing":
        monkeypatch.delenv("STAGE2_MODEL_KEY_FILE")
    processes = []
    real_popen = subprocess.Popen

    def popen(argv, **kwargs):
        assert argv[2] == "--worker" and len(argv) == 5
        assert kwargs["start_new_session"] and kwargs["close_fds"]
        assert len(kwargs["pass_fds"]) == 4
        assert not any(k in kwargs["env"] for k in ("STAGE2_REPORT_PATH", "STAGE2_CANDIDATE_PATH", "STAGE2_EVENT_PATH"))
        pid = os.fork()
        if pid == 0:
            try:
                os.setsid()
                os.environ.clear()
                os.environ.update(kwargs["env"])
                smoke.sys.argv = argv[1:]
                smoke.subprocess.Popen = real_popen
                smoke.build_crew = lambda _key: fake_crew(mode, [])
                if mode == "removed_hook":
                    smoke.install_provider_hook = lambda *args: lambda: None
                if mode in {"missing_candidate", "candidate_identity", "candidate_schema", "candidate_status_exit", "late_timeout_candidate"}:
                    original_write = smoke._write_report_atomic
                    def candidate_write(path, report):
                        if mode == "missing_candidate":
                            return
                        if mode == "candidate_identity":
                            report = dict(report, run_id="stage2-stale")
                        if mode == "candidate_schema":
                            report = dict(report, extra=True)
                        if mode == "candidate_status_exit":
                            report = dict(report, status="blocked", boundary="worker_failure")
                        original_write(path, report)
                        if mode == "late_timeout_candidate":
                            time.sleep(300)
                    smoke._write_report_atomic = candidate_write
                if mode == "worker_forge":
                    capability = smoke.mcp_tools.EventCapability(int(os.environ["STAGE2_WORKER_FD"]))
                    capability.append("cleanup_finished", "parent", "success")
                if mode == "setup_exception":
                    def broken(_key):
                        raise ValueError("offline setup")
                    smoke.build_crew = broken
                if mode == "prekickoff_timeout":
                    smoke.build_crew = lambda _key: time.sleep(300)
                code = smoke.main()
            except BaseException:
                code = 90
            os._exit(code)

        class Process:
            returncode = None
            def __init__(self):
                self.pid = pid
                self.waits = []
            def wait(self, timeout):
                self.waits.append(timeout)
                if fail_reap and timeout != 120:
                    raise subprocess.TimeoutExpired("offline", timeout)
                deadline = time.monotonic() + (0.8 if timeout == 120 and "timeout" in mode else timeout)
                while time.monotonic() < deadline:
                    if self.returncode is not None:
                        return self.returncode
                    found, status = os.waitpid(pid, os.WNOHANG)
                    if found:
                        self.returncode = os.waitstatus_to_exitcode(status)
                        return self.returncode
                    time.sleep(0.01)
                raise subprocess.TimeoutExpired("offline", timeout)
        process = Process()
        processes.append(process)
        return process

    monkeypatch.setattr(smoke.subprocess, "Popen", popen)
    result = smoke.main()
    if fail_reap:
        os.waitpid(processes[0].pid, 0)
    return result, optional_publication_for(tmp_path), processes


@pytest.mark.parametrize("mode,attempts,provider,status", [
    ("pass", 1, "started", "passed"),
    ("configuration_missing", 0, "not_started", "blocked"),
    ("setup_exception", 0, "not_started", "blocked"),
    ("kickoff_exception", 1, "not_started", "blocked"),
    ("no_provider", 1, "not_started", "blocked"),
    ("prekickoff_timeout", 0, "not_started", "blocked"),
    ("provider_timeout", 1, "started", "blocked"),
])
def test_actual_main_worker_flow(monkeypatch, tmp_path, capsys, mode, attempts, provider, status):
    code, publication, processes = run_fake_parent(monkeypatch, tmp_path, mode)
    report = smoke.read_committed(publication)
    assert code == (0 if status == "passed" else 1)
    assert report["status"] == status
    assert report["attempts"] == attempts and report["provider_state"] == provider
    assert json.loads(capsys.readouterr().out) == report
    assert report["cleanup_outcome"] == "success"
    assert processes[0].returncode is not None
    assert not (tmp_path / "canonical.json").exists()
    assert report["counts_complete"] is ("timeout" not in mode)
    if status == "passed":
        assert all(report["counts"][k] == 1 for k in smoke.COUNT_KEYS[:5])
    if "timeout" in mode:
        assert processes[0].waits == [120, 2, 2]


def test_actual_cli_popen_dispatch_no_recursion(monkeypatch, tmp_path, capsys):
    setup_parent(monkeypatch, tmp_path)
    monkeypatch.delenv("STAGE2_MODEL_KEY_FILE", raising=False)
    original = smoke.subprocess.Popen
    calls = []
    def popen(argv, **kwargs):
        calls.append(argv)
        assert argv[2] == "--worker" and len(argv) == 5
        return original(argv, **kwargs)
    monkeypatch.setattr(smoke.subprocess, "Popen", popen)
    assert smoke.main() == 1
    publication = publication_for(tmp_path)
    report = smoke.read_committed(publication)
    assert len(calls) == 1
    assert report["boundary"] == "configuration_missing"
    assert report["counts_complete"] is True
    assert report["attempts"] == 0


def test_actual_main_no_manifest_when_reap_fails(monkeypatch, tmp_path, capsys):
    code, publication, _ = run_fake_parent(monkeypatch, tmp_path, "provider_timeout", fail_reap=True)
    assert code == 1 and not publication.exists()
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("mode", ["removed_hook", "missing_candidate", "candidate_identity", "candidate_schema", "candidate_status_exit", "late_timeout_candidate"])
def test_actual_parent_rejects_missing_or_forged_proofs(monkeypatch, tmp_path, mode):
    code, publication, _ = run_fake_parent(monkeypatch, tmp_path, mode)
    report = smoke.read_committed(publication)
    assert code == 1 and report["status"] == "blocked" and report["attempts"] == 1
    assert report["provider_state"] == ("not_started" if mode == "removed_hook" else "started")
    if mode == "late_timeout_candidate":
        assert report["boundary"] == "kickoff_timeout" and report["counts_complete"] is False


def test_actual_main_forgery_no_publication(monkeypatch, tmp_path, capsys):
    code, publication, _ = run_fake_parent(monkeypatch, tmp_path, "worker_forge")
    assert code == 1 and not publication.exists() and capsys.readouterr().out == ""


def test_spawn_failure_commits_prekickoff_only(monkeypatch, tmp_path):
    setup_parent(monkeypatch, tmp_path)
    def denied(*args, **kwargs):
        raise OSError("offline spawn failure")
    monkeypatch.setattr(smoke.subprocess, "Popen", denied)
    assert smoke.main() == 1
    publication = publication_for(tmp_path)
    report = smoke.read_committed(publication)
    assert report["attempts"] == 0 and report["provider_state"] == "not_started"
    assert report["boundary"] == "spawn_failure" and report["counts_complete"] is False


def test_attestation_write_failure_does_not_commit(monkeypatch, tmp_path, capsys):
    original = smoke._write_bytes_atomic
    def fail_attestation(path, data):
        if path.name.startswith("attestation-"):
            raise OSError("offline fsync failure")
        return original(path, data)
    monkeypatch.setattr(smoke, "_write_bytes_atomic", fail_attestation)
    code, publication, _ = run_fake_parent(monkeypatch, tmp_path, "configuration_missing")
    assert code == 1 and not (publication / "committed-manifest.json").exists()
    assert capsys.readouterr().out == ""


def test_publication_reservation_competitor_fails_before_worker(tmp_path):
    requested = tmp_path / "canonical.json"
    first = smoke.PublicationReservation(requested, "stage2-owner")
    with pytest.raises(FileExistsError):
        smoke.PublicationReservation(requested, "stage2-owner")
    assert not (first.directory / "committed-manifest.json").exists()
    first.cleanup_uncommitted()
    retry = smoke.PublicationReservation(requested, "stage2-owner")
    retry.cleanup_uncommitted()


def test_publication_committed_bundle_is_not_removed(tmp_path):
    reservation = smoke.PublicationReservation(tmp_path / "canonical.json", "stage2-owner")
    reservation.mark_committed()
    reservation.cleanup_uncommitted()
    assert reservation.directory.exists()
    assert json.loads((reservation.policy_lock / "owner.json").read_text())["state"] == "committed"


@pytest.mark.parametrize("failure", ["write", "fsync"])
def test_policy_lock_token_failure_rolls_back_for_retry(monkeypatch, tmp_path, failure):
    requested = tmp_path / "canonical.json"
    original_write = smoke.PublicationReservation._write_owner_token
    original_fsync = smoke.os.fsync
    if failure == "write":
        def denied(self, path):
            raise OSError("offline token write failure")
        monkeypatch.setattr(smoke.PublicationReservation, "_write_owner_token", denied)
    else:
        calls = []
        def fail_once(fd):
            calls.append(fd)
            if len(calls) == 2:
                raise OSError("offline token fsync failure")
            return original_fsync(fd)
        monkeypatch.setattr(smoke.os, "fsync", fail_once)
    with pytest.raises(OSError):
        smoke.PublicationReservation(requested, "stage2-fault")
    assert not (tmp_path / ".canonical.json.stage2-policy.lock").exists()
    assert not list(tmp_path.glob("canonical.stage2-*.bundle"))
    monkeypatch.setattr(smoke.PublicationReservation, "_write_owner_token", original_write)
    retry = smoke.PublicationReservation(requested, "stage2-retry")
    retry.cleanup_uncommitted()


def test_actual_main_existing_reservation_fails_before_all_runtime_work(monkeypatch, tmp_path, capsys):
    setup_parent(monkeypatch, tmp_path)
    requested = tmp_path / "canonical.json"
    owner = smoke.PublicationReservation(requested, "stage2-existing")
    calls = []
    for name in ("LifecycleWriter", "ParentBroker", "_write_bytes_atomic"):
        monkeypatch.setattr(smoke, name, lambda *args, _name=name, **kwargs: calls.append(_name))
    monkeypatch.setattr(smoke.subprocess, "Popen", lambda *args, **kwargs: calls.append("Popen"))
    monkeypatch.setattr(smoke, "read_committed", lambda *args, **kwargs: calls.append("reader"))
    assert smoke.main() == 1
    assert calls == []
    assert capsys.readouterr().out == ""
    owner.cleanup_uncommitted()
    retry = smoke.PublicationReservation(requested, "stage2-retry")
    retry.cleanup_uncommitted()


def test_actual_main_policy_barrier_second_owner_stops_before_popen(monkeypatch, tmp_path, capsys):
    setup_parent(monkeypatch, tmp_path)
    requested = tmp_path / "canonical.json"
    owner = smoke.PublicationReservation(requested, "stage2-first")
    calls = []
    monkeypatch.setattr(smoke.subprocess, "Popen", lambda *args, **kwargs: calls.append("Popen"))
    monkeypatch.setattr(smoke, "ParentBroker", lambda *args, **kwargs: calls.append("broker"))
    assert smoke.main() == 1
    assert calls == []
    assert capsys.readouterr().out == ""
    owner.cleanup_uncommitted()


def test_same_run_foreign_generation_is_standalone_valid_but_local_expected_rejects(monkeypatch, tmp_path):
    setup_parent(monkeypatch, tmp_path)
    code, publication, _ = run_fake_parent(monkeypatch, tmp_path, "configuration_missing")
    assert code == 1
    manifest_path = publication / "committed-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    foreign = tmp_path / "foreign.bundle"
    foreign.mkdir()
    for path in publication.iterdir():
        if path.name != "committed-manifest.json":
            target = foreign / path.name.replace(manifest["generation"], "e" * 32)
            target.write_bytes(path.read_bytes())
    foreign_manifest = dict(manifest, generation="e" * 32)
    foreign_token = "f" * 64
    (foreign / ".owner-token").write_text(foreign_token)
    foreign_manifest["owner_token_digest"] = hashlib.sha256(foreign_token.encode()).hexdigest()
    for kind in ("report", "attestation", "lifecycle", "mcp"):
        entry = dict(foreign_manifest[kind])
        entry["file"] = entry["file"].replace(manifest["generation"], "e" * 32)
        foreign_manifest[kind] = entry
    report_path = foreign / foreign_manifest["report"]["file"]
    report = json.loads(report_path.read_text())
    report["report_path"] = str(report_path)
    report_path.write_text(json.dumps(report, separators=(",", ":"), sort_keys=True))
    attestation_path = foreign / foreign_manifest["attestation"]["file"]
    attestation = json.loads(attestation_path.read_text())
    attestation.update(generation="e" * 32, owner_token_digest=foreign_manifest["owner_token_digest"])
    attestation["report_digest"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
    attestation_path.write_text(json.dumps(attestation, separators=(",", ":"), sort_keys=True))
    # Rebuild all generation links and digests as a complete foreign bundle.
    for kind in ("report", "attestation", "lifecycle", "mcp"):
        path = foreign / foreign_manifest[kind]["file"]
        foreign_manifest[kind]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    (foreign / "committed-manifest.json").write_text(json.dumps(foreign_manifest, sort_keys=True))
    assert smoke.read_committed(foreign)["run_id"] == manifest["run_id"]
    with pytest.raises(ValueError):
        smoke.read_committed(publication, expected=foreign_manifest)


def test_actual_main_postcommit_reread_failure_preserves_committed_bundle(monkeypatch, tmp_path, capsys):
    original = smoke.read_committed
    calls = []

    def fail_second(directory, *, expected=None):
        calls.append(expected is not None)
        if len(calls) == 2:
            raise OSError("offline reread failure")
        return original(directory, expected=expected)

    monkeypatch.setattr(smoke, "read_committed", fail_second)
    code, publication, _ = run_fake_parent(monkeypatch, tmp_path, "configuration_missing")
    assert code == 1 and calls == [True, True]
    assert (publication / "committed-manifest.json").exists()
    assert list(publication.glob("report-*.json"))
    assert list(publication.glob("attestation-*.json"))
    assert list(publication.glob("lifecycle-*.jsonl"))
    assert list(publication.glob("mcp-*.jsonl"))
    assert capsys.readouterr().out == ""


def test_actual_main_rejects_same_run_foreign_generation(monkeypatch, tmp_path, capsys):
    original = smoke.read_committed
    calls = []

    def substitute(directory, *, expected=None):
        calls.append(expected is not None)
        if len(calls) == 2:
            manifest_path = Path(directory) / "committed-manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["generation"] = "f" * 32
            manifest_path.write_text(json.dumps(manifest))
        return original(directory, expected=expected)

    publication = None
    monkeypatch.setattr(smoke, "read_committed", substitute)
    code, publication, _ = run_fake_parent(monkeypatch, tmp_path, "configuration_missing")
    assert code == 1 and calls == [True, True]
    assert (publication / "committed-manifest.json").exists()
    assert capsys.readouterr().out == ""


def test_actual_installed_lower_hook_ack_then_one_original_and_return(monkeypatch):
    calls = []
    crew = fake_crew("no_provider", calls)

    class Capability:
        def append(self, event, source):
            calls.append("durable_ack")
            assert (event, source) == ("provider_request_started", "llm_wrapper")

    original = crew.agents[0].llm.client.chat.completions.create
    def sentinel(*args, **kwargs):
        calls.append("original")
        return {"offline": True}
    crew.agents[0].llm.client.chat.completions.create = sentinel
    restore = smoke.install_provider_hook(crew, Capability())
    try:
        result = crew.agents[0].llm.call()
    finally:
        restore()
    assert result == {"offline": True}
    assert calls == ["durable_ack", "original"]
    assert crew.agents[0].llm.client.chat.completions.create is sentinel
    assert original is not sentinel


def test_installed_crewai_lower_object_hook_fail_closed(monkeypatch):
    crew = smoke.build_crew("synthetic-not-a-credential")
    llm = crew.agents[0].llm
    assert type(llm).__module__ == "crewai.llms.providers.openai.completion"
    assert crew.tasks[0].agent is crew.agents[0]
    calls = []
    monkeypatch.setattr(llm.client.chat.completions, "create", lambda **kw: calls.append("outbound"))
    class Denied:
        def append(self, *args):
            calls.append(args)
            raise OSError("durability denied")
    restore = smoke.install_provider_hook(crew, Denied())
    with pytest.raises(Exception):
        llm.call("synthetic offline prompt")
    restore()
    assert calls and all(item == ("provider_request_started", "llm_wrapper") for item in calls)


def test_worker_provider_emission_failure_prevents_lower_call(monkeypatch, tmp_path):
    calls = []
    crew = fake_crew("pass", calls)
    class Denied:
        def append(self, *args):
            raise OSError("durability denied")
    smoke.install_provider_hook(crew, Denied())
    with pytest.raises(OSError):
        crew.kickoff()
    assert calls == ["kickoff"]


@pytest.mark.parametrize("role,event,source", [
    ("worker", "cleanup_finished", "parent"),
    ("worker", "provider_request_started", "llm_wrapper"),
    ("llm_wrapper", "kickoff_entered", "worker"),
    ("llm_wrapper", "cleanup_finished", "parent"),
])
def test_parent_source_forgery_denied(tmp_path, role, event, source):
    broker = smoke.ParentBroker(tmp_path, "stage2-forge")
    broker.lifecycle.append("preflight_passed", "parent")
    broker.lifecycle.append("worker_spawned", "parent")
    broker.start()
    capability = smoke.mcp_tools.EventCapability(os.dup(broker.channels[role][1].fileno()))
    try:
        with pytest.raises(OSError):
            capability.append(event, source)
        assert broker.invalid
        rows, error = smoke.validate_lifecycle(broker.lifecycle.path, "stage2-forge")
        assert error is None and len(rows) == 2
    finally:
        capability.close()
        broker.close()


def test_late_writes_and_live_seal_denied(tmp_path):
    broker = smoke.ParentBroker(tmp_path, "stage2-seal")
    broker.lifecycle.append("preflight_passed", "parent")
    broker.lifecycle.append("worker_spawned", "parent")
    broker.start()
    capability = smoke.mcp_tools.EventCapability(os.dup(broker.channels["worker"][1].fileno()))
    capability.append("kickoff_entered", "worker")
    with pytest.raises(ValueError):
        broker.seal()
    broker.close()
    broker.lifecycle.append("cleanup_finished", "parent", "success")
    before = broker.seal()
    with pytest.raises(OSError):
        capability.append("worker_final_report_written", "worker")
    with pytest.raises(ValueError):
        broker.lifecycle.append("worker_final_report_written", "worker")
    assert broker.seal() == before
    capability.close()


@pytest.mark.parametrize("names", [
    ["cleanup_finished"], ["worker_spawned", "cleanup_finished"],
    ["preflight_passed", "kickoff_entered", "cleanup_finished"],
    ["preflight_passed", "worker_spawned", "provider_request_started", "cleanup_finished"],
    ["preflight_passed", "worker_spawned", "worker_final_report_written", "kickoff_entered", "cleanup_finished"],
])
def test_exact_lifecycle_branches_reject_skipped_or_reordered(tmp_path, names):
    path = tmp_path / "lifecycle.jsonl"
    writer = smoke.LifecycleWriter(path, "stage2-test")
    for name in names:
        writer.append(name, smoke.OWNER[name], "success" if name == "cleanup_finished" else "observed")
    assert smoke.validate_lifecycle(path, "stage2-test", accepted=True)[1]


@pytest.mark.parametrize("corruption", ["seq", "source", "identity", "partial", "duplicate_key", "extra", "duplicate", "unknown"])
def test_lifecycle_corruption(tmp_path, corruption):
    path = tmp_path / "lifecycle.jsonl"
    row = {"run_id": "stage2-test", "seq": 1, "event": "preflight_passed", "source": "parent", "outcome": "observed"}
    if corruption == "seq": row["seq"] = 2
    if corruption == "source": row["source"] = "worker"
    if corruption == "identity": row["run_id"] = "run-controller"
    if corruption == "extra": row["payload"] = "forbidden"
    if corruption == "unknown": row["event"] = []
    raw = json.dumps(row) + "\n"
    if corruption == "partial": raw = raw.rstrip()
    if corruption == "duplicate_key": raw = raw.replace('"seq": 1', '"seq": 1, "seq": 1')
    if corruption == "duplicate": raw += raw
    path.write_text(raw)
    assert smoke.validate_lifecycle(path, "stage2-test")[1]


def real_mcp_rows(tmp_path):
    path = tmp_path / "mcp.jsonl"
    with smoke.mcp_tools.mcp_run_scope("stage2-mcp", event_path=str(path)):
        raw = smoke.mcp_tools.make_mcp_tool()._run()
        smoke.mcp_tools.close_mcp_clients()
    rows, error = smoke.validate_mcp_journal(path, "stage2-mcp")
    assert error is None and len(rows) == 8
    digest = smoke.mcp_tools.returned_marker_digest(json.loads(raw))
    assert smoke.mcp_chain(rows, digest)
    return path, rows


@pytest.mark.parametrize("change", ["source", "outcome", "adapter", "tool", "resource", "arg_keys", "marker_present", "digest", "digest_placement", "extra", "unknown", "partial", "missing"])
def test_strict_real_server_journal(tmp_path, change):
    path, rows = real_mcp_rows(tmp_path)
    if change == "source": rows[1]["source"] = "client"
    if change == "outcome": rows[1]["outcome"] = "started"
    if change in {"adapter", "tool", "resource"}: rows[1][change] = "unknown"
    if change == "arg_keys": rows[4]["arg_keys"] = ["fixture", "fixture"]
    if change == "marker_present": rows[-1]["marker_present"] = 1
    if change == "digest": rows[-1]["marker_digest"] = 7
    if change == "digest_placement": rows[1]["marker_digest"] = "a" * 64
    if change == "extra": rows[1]["payload"] = "forbidden"
    if change == "unknown": rows[1]["event"] = "server_other"
    if change == "missing": del rows[1]["outcome"]
    raw = "".join(json.dumps(row) + "\n" for row in rows)
    path.write_text(raw.rstrip() if change == "partial" else raw)
    assert smoke.validate_mcp_journal(path, "stage2-mcp")[1]


@pytest.mark.parametrize("change", ["report_digest", "manifest_extra", "mixed_generation", "attestation_extra", "attestation_link", "report_extra", "report_boolean", "controller_identity"])
def test_committed_manifest_tamper(monkeypatch, tmp_path, change):
    _, publication, _ = run_fake_parent(monkeypatch, tmp_path, "configuration_missing")
    manifest_path = publication / "committed-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if change == "manifest_extra": manifest["extra"] = True
    elif change == "mixed_generation": manifest["generation"] = "f" * 32
    else:
        kind = "attestation" if change.startswith("attestation") else "report"
        path = publication / manifest[kind]["file"]
        row = json.loads(path.read_text())
        if change.endswith("extra"): row["extra"] = True
        if change == "attestation_link": row["parent_trusted_link"] = False
        if change == "report_boolean": row["attempts"] = False
        if change == "controller_identity": row["run_id"] = "run-offline-rescue"
        if change == "report_digest": row["boundary"] = "spawn_failure"
        path.write_text(json.dumps(row))
        if change != "report_digest":
            manifest[kind]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        smoke.read_committed(publication)


@pytest.mark.parametrize("args", [["--static"], ["--static", "--worker"], ["--worker"], ["--worker", "x", "run-controller"], ["--other"]])
def test_static_and_invalid_cli_no_dynamic_artifacts(monkeypatch, tmp_path, args):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(smoke.tempfile, "tempdir", str(tmp_path))
    for name in ("STAGE2_REPORT_PATH", "STAGE2_CANDIDATE_PATH", "STAGE2_EVENT_PATH", "STAGE2_MARKER_DIGEST_PATH", "HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name))
    def forbidden(*args, **kwargs):
        pytest.fail("static path invoked dynamic writer or process")
    monkeypatch.setattr(smoke.subprocess, "Popen", forbidden)
    monkeypatch.setattr(smoke, "LifecycleWriter", forbidden)
    monkeypatch.setattr(smoke, "ParentBroker", forbidden)
    monkeypatch.setattr(smoke, "_write_bytes_atomic", forbidden)
    monkeypatch.setattr(smoke.mcp_tools, "_event", forbidden)
    monkeypatch.setattr(smoke.sys, "argv", ["smoke", *args])
    assert smoke.main() == (0 if args == ["--static"] else 2)
    assert list(tmp_path.iterdir()) == []


def test_cleanup_signal_order_and_disappearance(monkeypatch):
    signals, waits = [], []
    class Process:
        pid = 4242
        returncode = None
        def wait(self, timeout):
            waits.append(timeout)
            if len(waits) == 1:
                raise subprocess.TimeoutExpired("offline", timeout)
            self.returncode = -9
    def killpg(pid, sig):
        assert pid == 4242
        signals.append(sig)
        if sig in (signal.SIGTERM, 0):
            raise ProcessLookupError
    monkeypatch.setattr(smoke.os, "killpg", killpg)
    monkeypatch.setattr(smoke.os, "waitpid", lambda *args: (_ for _ in ()).throw(ChildProcessError()))
    assert smoke.cleanup_group(Process())
    assert waits == [2, 2] and signals == [signal.SIGTERM, signal.SIGKILL, 0]
