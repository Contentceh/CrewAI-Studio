"""Offline provenance harness for one bounded CrewAI model smoke.

The dynamic entry point is deliberately not exercised by the test suite.  All
publication and lifecycle decisions are pure, fail-closed code so they can be
tested with fakes without a provider or network.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
import fcntl
import socket
import time
import ctypes
from importlib.metadata import version
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))
import mcp_tools  # noqa: E402
from crewai import Agent, Crew, LLM, Task  # noqa: E402

MODEL_ID = "gpt-5.6-luna"
COUNT_KEYS = ("crewai_base_tool_enter", "mcp_tools_call_started", "mcp_tools_call_success",
              "crewai_base_tool_success", "mcp_server_tool_executed", "tool_selection_finished",
              "tool_selection_failure")
FAILURE_CATEGORIES = frozenset({"configuration_missing", "setup_exception", "kickoff_exception"})
LIFECYCLE_EVENTS = ("preflight_passed", "worker_spawned", "kickoff_entered", "provider_request_started",
                    "worker_final_report_written", "cleanup_finished", "parent_final_report_accepted")
LIFECYCLE_SOURCES = frozenset({"parent", "worker", "llm_wrapper"})
OWNER = {"preflight_passed": "parent", "worker_spawned": "parent", "kickoff_entered": "worker",
         "provider_request_started": "llm_wrapper", "worker_final_report_written": "worker",
         "cleanup_finished": "parent", "parent_final_report_accepted": "parent"}
MCP_EVENTS = frozenset({"crewai_base_tool_enter", "crewai_base_tool_success", "crewai_base_tool_failure",
                        "mcp_tools_call_started", "mcp_tools_call_success", "mcp_tools_call_failure",
                        "server_initialize", "server_initialized", "server_tools_list", "mcp_server_tool_executed", "mcp_server_tool_failure"})
REPORT_KEYS = {"status", "run_id", "counts", "counts_complete", "marker_match", "marker_digest", "boundary",
               "failure_category", "model", "usage", "report_path", "attempts", "provider_state", "identity_class",
               "lifecycle_digest", "mcp_digest", "cleanup_outcome"}
BOUNDARIES = {"worker_in_progress", "configuration_missing", "setup_exception", "kickoff_exception",
              "kickoff_completed_without_tool_selection", "tool_chain_or_marker_mismatch", "complete",
              "preflight_failure", "spawn_failure", "worker_failure", "prekickoff_timeout", "kickoff_timeout"}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _json(data):
    return json.loads(data, object_pairs_hook=_object)


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _valid_run_id(run_id: str) -> bool:
    return isinstance(run_id, str) and bool(re.fullmatch(r"stage2-[A-Za-z0-9_-]{1,96}", run_id))


class PublicationReservation:
    """Exclusive terminal owner for one normalized report-root policy."""
    def __init__(self, report_path: Path, run_id: str):
        if not _valid_run_id(run_id):
            raise ValueError("invalid harness run id")
        requested = Path(report_path).resolve()
        self.report_root = requested
        self.policy_lock = requested.parent / f".{requested.name}.stage2-policy.lock"
        self.directory = requested.parent / f"{requested.stem}.{run_id}.bundle"
        self.run_id = run_id
        self.owner_token = uuid.uuid4().hex + uuid.uuid4().hex
        self.committed = False
        self._lock_created = False
        self._bundle_created = False
        try:
            # This is the single policy gate.  It is intentionally stable across
            # run IDs, and precedes bundle, broker, Popen, writer, and stdout.
            self.policy_lock.mkdir(parents=False, exist_ok=False)
            self._lock_created = True
            self._write_owner_metadata()
            self.directory.mkdir(parents=False, exist_ok=False)
            self._bundle_created = True
            self._write_owner_token(self.directory / ".owner-token")
        except BaseException:
            self._rollback_reservation()
            raise

    def _write_owner_metadata(self):
        metadata = {"run_id": self.run_id, "report_root": str(self.report_root), "state": "reserved"}
        self._write_exclusive(self.policy_lock / "owner.json", json.dumps(metadata, separators=(",", ":"), sort_keys=True).encode())

    def _write_owner_token(self, token_path):
        self._write_exclusive(token_path, self.owner_token.encode("ascii"))

    @staticmethod
    def _write_exclusive(path, data):
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(path, flags, 0o600)
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("owner write made no progress")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _rollback_reservation(self):
        if self._bundle_created and self.directory.exists() and not (self.directory / "committed-manifest.json").exists():
            for path in sorted(self.directory.iterdir(), reverse=True):
                if path.is_file() or path.is_symlink():
                    path.unlink()
            self.directory.rmdir()
        if self._lock_created and self.policy_lock.exists():
            for path in sorted(self.policy_lock.iterdir(), reverse=True):
                if path.is_file() or path.is_symlink():
                    path.unlink()
            self.policy_lock.rmdir()

    def assert_owner(self):
        token = (self.directory / ".owner-token").read_text(encoding="ascii")
        if token != self.owner_token:
            raise ValueError("publication owner mismatch")

    def mark_committed(self):
        self.assert_owner()
        metadata = {"run_id": self.run_id, "report_root": str(self.report_root), "state": "committed"}
        metadata_path = self.policy_lock / "owner.json"
        temporary = self.policy_lock / ".owner.json.commit.tmp"
        _write_bytes_atomic(temporary, json.dumps(metadata, separators=(",", ":"), sort_keys=True).encode())
        os.replace(temporary, metadata_path)
        directory_fd = os.open(self.policy_lock, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        self.committed = True

    def cleanup_uncommitted(self):
        if self.committed or (self.directory / "committed-manifest.json").exists():
            return
        self.assert_owner()
        for path in sorted(self.directory.iterdir(), reverse=True):
            if path.is_file() or path.is_symlink():
                path.unlink()
        self.directory.rmdir()
        self._rollback_reservation()


class LifecycleWriter:
    """Single append-only lifecycle stream; sequence allocation is serialized."""
    def __init__(self, path: Path, run_id: str):
        if not _valid_run_id(run_id):
            raise ValueError("invalid harness run id")
        self.path, self.run_id, self._lock = Path(path), run_id, threading.Lock()
        self.pid, self.sealed = os.getpid(), False

    def append(self, event: str, source: str, outcome: str = "observed") -> dict:
        if event not in LIFECYCLE_EVENTS[:-1] or source not in LIFECYCLE_SOURCES:
            raise ValueError("invalid lifecycle event")
        if OWNER[event] != source:
            raise ValueError("wrong lifecycle source")
        with self._lock:
            if self.sealed or os.getpid() != self.pid:
                raise ValueError("lifecycle writer closed or foreign process")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                handle.seek(0)
                sequence = sum(1 for line in handle if line.endswith("\n")) + 1
                row = {"run_id": self.run_id, "seq": sequence, "event": event, "source": source, "outcome": outcome}
                handle.seek(0, os.SEEK_END)
                handle.write(json.dumps(row, separators=(",", ":")) + "\n")
                handle.flush(); os.fsync(handle.fileno())
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return row

    def seal(self):
        with self._lock:
            self.sealed = True
            return self.path.read_bytes()


class ParentBroker:
    """No child receives a journal descriptor/path or parent event authority.

    Capabilities protect the cooperative harness protocol, not against arbitrary
    hostile code with the parent's UID and ptrace/filesystem authority.
    """
    def __init__(self, directory, run_id):
        self.run_id = run_id
        self.lifecycle = LifecycleWriter(directory / "lifecycle.jsonl", run_id)
        self.mcp_path = directory / "mcp.jsonl"
        self.mcp_path.touch(exist_ok=False)
        self.lock = threading.Lock()
        self.closed = False
        self.invalid = False
        self.channels = {}
        self.threads = []
        for role in ("worker", "llm_wrapper", "client", "server"):
            parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            parent.settimeout(0.1)
            token = uuid.uuid4().hex + uuid.uuid4().hex
            parent.sendall(token.encode("ascii"))
            self.channels[role] = (parent, child, token)

    def start(self):
        for role in self.channels:
            thread = threading.Thread(target=self._serve, args=(role,), daemon=True)
            thread.start()
            self.threads.append(thread)

    def _serve(self, role):
        connection, _, token = self.channels[role]
        while not self.closed:
            try:
                try:
                    data = connection.recv(65536)
                except ConnectionResetError:
                    # An unused capability still has its initial token queued.
                    # Closing it on pre-kickoff exit/kill is not a forged event.
                    break
                if not data:
                    break
                request = _json(data)
                with self.lock:
                    if self.closed:
                        raise ValueError("sealed")
                    if not isinstance(request, dict) or set(request) != {"token", "payload"} or request["token"] != token:
                        raise ValueError("capability")
                    payload = request["payload"]
                    response = {"ok": True}
                    if role == "worker" and payload == {"snapshot": "mcp"}:
                        rows, error = validate_mcp_journal(self.mcp_path, self.run_id)
                        if error:
                            raise ValueError("MCP snapshot")
                        response["rows"] = rows
                    elif role in {"worker", "llm_wrapper"}:
                        if not isinstance(payload, dict) or set(payload) != {"event", "source", "outcome"}:
                            raise ValueError("lifecycle schema")
                        allowed = {"kickoff_entered", "worker_final_report_written"} if role == "worker" else {"provider_request_started"}
                        if payload["source"] != role or payload["event"] not in allowed or payload["outcome"] != "observed":
                            raise ValueError("lifecycle authority")
                        self.lifecycle.append(**payload)
                        if validate_lifecycle(self.lifecycle.path, self.run_id)[1]:
                            raise ValueError("lifecycle transition")
                    else:
                        if not isinstance(payload, dict) or payload.get("source") != role or not valid_mcp_row(payload, self.run_id):
                            raise ValueError("MCP authority or schema")
                        with self.mcp_path.open("ab") as handle:
                            handle.write(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
                            handle.flush()
                            os.fsync(handle.fileno())
                connection.sendall(json.dumps(response).encode())
            except socket.timeout:
                continue
            except (ValueError, TypeError, KeyError, OSError):
                if not self.closed:
                    self.invalid = True
                    try:
                        connection.sendall(b'{"ok":false}')
                    except OSError:
                        pass
                break

    def close(self):
        with self.lock:
            self.closed = True
        for parent, child, _ in self.channels.values():
            for connection in (parent, child):
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
        for thread in self.threads:
            thread.join(timeout=2)
            if thread.is_alive():
                raise ValueError("broker did not close")

    def seal(self):
        if not self.closed or any(t.is_alive() for t in self.threads):
            raise ValueError("live broker")
        return self.lifecycle.seal(), self.mcp_path.read_bytes()


def kickoff_with_provenance(crew, writer: LifecycleWriter):
    """Acknowledge kickoff immediately before entering CrewAI."""
    writer.append("kickoff_entered", "worker")
    return crew.kickoff()


def provider_call_with_provenance(call, writer: LifecycleWriter):
    """The only provider hook: durable event acknowledgement precedes call."""
    writer.append("provider_request_started", "llm_wrapper")
    return call()


def _read_jsonl(path: Path) -> tuple[list[dict], str | None]:
    try:
        data = path.read_bytes()
    except OSError:
        return [], "unreadable"
    if not data or not data.endswith(b"\n"):
        return [], "partial"
    rows = []
    for raw in data.splitlines():
        try:
            row = _json(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return [], "malformed"
        if not isinstance(row, dict):
            return [], "schema"
        rows.append(row)
    return rows, None


def validate_lifecycle(path: Path, run_id: str, *, accepted: bool = False) -> tuple[list[dict], str | None]:
    rows, error = _read_jsonl(Path(path))
    if error or not _valid_run_id(run_id):
        return [], error or "identity"
    expected = 1
    seen = set()
    for row in rows:
        if set(row) != {"run_id", "seq", "event", "source", "outcome"} or row["run_id"] != run_id:
            return [], "identity_or_schema"
        if type(row["seq"]) is not int or row["seq"] != expected or row["seq"] in seen:
            return [], "sequence"
        if not isinstance(row["event"], str) or row["event"] not in LIFECYCLE_EVENTS[:-1] or row["source"] != OWNER[row["event"]]:
            return [], "event_source"
        if row["event"] == "cleanup_finished" and row["outcome"] not in {"success", "failure"}:
            return [], "cleanup_outcome"
        if row["event"] != "cleanup_finished" and row["outcome"] != "observed":
            return [], "outcome"
        seen.add(row["seq"]); expected += 1
    names = [row["event"] for row in rows]
    if names != sorted(names, key=lambda n: LIFECYCLE_EVENTS.index(n)) or len(names) != len(set(names)):
        return [], "transition"
    if accepted and (not names or names[-1] != "cleanup_finished"):
        return [], "cleanup_missing"
    if "provider_request_started" in names and "kickoff_entered" not in names:
        return [], "provider_before_kickoff"
    # A preflight failure is not represented by a lone cleanup event. Without
    # independently established launch preconditions there is no publication.
    if not names or names[0] != "preflight_passed":
        return [], "preflight_missing"
    if any(n in names for n in ("kickoff_entered", "worker_final_report_written")) and "worker_spawned" not in names:
        return [], "spawn_missing"
    return rows, None


def valid_mcp_row(row, run_id):
    if not isinstance(row, dict) or not isinstance(row.get("event"), str) or row["event"] not in MCP_EVENTS:
        return False
    event = row["event"]
    server = event.startswith("server_") or event.startswith("mcp_server_")
    outcome = "failed" if event.endswith("failure") else "started" if event.endswith(("enter", "started")) else "success"
    keys = {"run_id", "source", "event", "adapter", "tool", "resource", "outcome"}
    if event in {"mcp_tools_call_started", "mcp_tools_call_success", "mcp_server_tool_executed"}:
        keys.add("arg_keys")
        if row.get("arg_keys") not in ([], ["fixture"]):
            return False
    if event == "mcp_server_tool_executed":
        keys.add("marker_digest")
        if not _sha(row.get("marker_digest")):
            return False
    if event == "crewai_base_tool_success":
        keys.add("marker_present")
        if type(row.get("marker_present")) is not bool:
            return False
        if row["marker_present"]:
            keys.add("marker_digest")
            if not _sha(row.get("marker_digest")):
                return False
    return (set(row) == keys and row["run_id"] == run_id and row["source"] == ("server" if server else "client")
            and row["outcome"] == outcome and row["adapter"] == "project_fixture"
            and row["tool"] == "fixture.read" and row["resource"] == "project-fixture")


def validate_mcp_journal(path: Path, run_id: str) -> tuple[list[dict], str | None]:
    try:
        if Path(path).read_bytes() == b"":
            return [], None
    except OSError:
        return [], "unreadable"
    rows, error = _read_jsonl(Path(path))
    if error:
        return [], error
    if not all(valid_mcp_row(row, run_id) for row in rows):
        return [], "schema"
    return rows, None


def _digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try: os.fsync(directory_fd)
        finally: os.close(directory_fd)
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass


def _write_report_atomic(report_path: Path, report: dict) -> None:
    _write_bytes_atomic(Path(report_path), json.dumps(report, separators=(",", ":"), sort_keys=True).encode())


def _report(run_id, report_path, *, status="blocked", counts=None, counts_complete=False, marker_match=False,
            marker_digest=None, boundary="worker_in_progress", failure_category=None, attempts=None,
            provider_state="unknown", lifecycle_digest=None, mcp_digest=None, cleanup_outcome=None):
    return {"status": status, "run_id": run_id, "counts": {key: int((counts or {}).get(key, 0)) for key in COUNT_KEYS},
            "counts_complete": bool(counts_complete), "marker_match": bool(marker_match),
            "marker_digest": marker_digest if isinstance(marker_digest, str) else None, "boundary": boundary,
            "failure_category": failure_category if failure_category in FAILURE_CATEGORIES else None,
            "model": MODEL_ID, "usage": "unknown", "report_path": str(report_path),
            "attempts": attempts, "provider_state": provider_state, "identity_class": "harness_run",
            "lifecycle_digest": lifecycle_digest, "mcp_digest": mcp_digest, "cleanup_outcome": cleanup_outcome}


def publish_canonical(directory: Path, report: dict, attestation: dict, lifecycle: Path, mcp_journal: Path,
                       controller_run_id: str, owner_token: str, on_commit=None) -> dict:
    """Publish immutable generation files, then one reader-trusted manifest."""
    directory = Path(directory); generation = uuid.uuid4().hex
    report_path = directory / f"report-{generation}.json"; attestation_path = directory / f"attestation-{generation}.json"
    rows, error = validate_lifecycle(lifecycle, report["run_id"], accepted=True)
    if error or rows[-1]["outcome"] != "success":
        raise ValueError("lifecycle not sealed")
    diagnostics, error = validate_mcp_journal(mcp_journal, report["run_id"])
    if error:
        raise ValueError("MCP journal not sealed")
    attempts, provider = lifecycle_boundary(rows)
    if report["attempts"] != attempts or report["provider_state"] != provider or report["cleanup_outcome"] != "success":
        raise ValueError("untrusted report boundary")
    if report["status"] == "passed" and ([r["event"] for r in rows] != list(LIFECYCLE_EVENTS[:-1]) or not mcp_chain(diagnostics, report["marker_digest"])):
        raise ValueError("incomplete pass provenance")
    lifecycle_digest = _digest_file(lifecycle); mcp_digest = _digest_file(mcp_journal)
    report = dict(report, lifecycle_digest=lifecycle_digest, mcp_digest=mcp_digest,
                  report_path=str(report_path), identity_class="harness_run")
    lifecycle_snapshot = directory / f"lifecycle-{generation}.jsonl"
    mcp_snapshot = directory / f"mcp-{generation}.jsonl"
    _write_bytes_atomic(lifecycle_snapshot, lifecycle.read_bytes())
    _write_bytes_atomic(mcp_snapshot, mcp_journal.read_bytes())
    if set(attestation) != {"accepted_boundary"} or attestation["accepted_boundary"] != report["boundary"]:
        raise ValueError("attestation input schema")
    owner_token_digest = hashlib.sha256(owner_token.encode("ascii")).hexdigest()
    attestation = dict(attestation, controller_run_id=controller_run_id, harness_run_id=report["run_id"],
                       report_digest=None, lifecycle_digest=lifecycle_digest, mcp_digest=mcp_digest,
                       identity_class="controller_attestation", generation=generation,
                       parent_trusted_link=True, event="parent_final_report_accepted",
                       owner_token_digest=owner_token_digest)
    if not valid_report(report, canonical=True):
        raise ValueError("report schema")
    _write_report_atomic(report_path, report)
    if _digest_file(report_path) != hashlib.sha256(json.dumps(report, separators=(",", ":"), sort_keys=True).encode()).hexdigest():
        raise ValueError("report reread digest mismatch")
    attestation["report_digest"] = _digest_file(report_path)
    _write_bytes_atomic(attestation_path, json.dumps(attestation, separators=(",", ":"), sort_keys=True).encode())
    if _json(attestation_path.read_bytes()) != attestation:
        raise ValueError("attestation reread mismatch")
    manifest = {"generation": generation, "run_id": report["run_id"], "identity_class": report["identity_class"],
                "owner_token_digest": owner_token_digest,
                "report": {"file": report_path.name, "sha256": _digest_file(report_path)},
                "attestation": {"file": attestation_path.name, "sha256": _digest_file(attestation_path)},
                "lifecycle": {"file": lifecycle_snapshot.name, "sha256": lifecycle_digest},
                "mcp": {"file": mcp_snapshot.name, "sha256": mcp_digest}}
    manifest_path = directory / "committed-manifest.json"
    _validate_bundle(directory, manifest)
    _write_bytes_atomic(manifest_path, json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode())
    if on_commit is not None:
        on_commit()
    read_committed(directory, expected=manifest)
    return manifest


def valid_report(report, *, canonical=False):
    if not isinstance(report, dict) or set(report) != REPORT_KEYS or not _valid_run_id(report["run_id"]):
        return False
    if (not isinstance(report["status"], str) or report["status"] not in {"blocked", "passed"}
            or report["model"] != MODEL_ID or report["usage"] != "unknown"
            or report["identity_class"] != "harness_run" or not isinstance(report["report_path"], str)
            or not isinstance(report["boundary"], str) or report["boundary"] not in BOUNDARIES):
        return False
    counts = report["counts"]
    if not isinstance(counts, dict) or set(counts) != set(COUNT_KEYS) or any(type(v) is not int or v < 0 for v in counts.values()):
        return False
    if type(report["counts_complete"]) is not bool or type(report["marker_match"]) is not bool:
        return False
    if report["attempts"] is not None and (type(report["attempts"]) is not int or report["attempts"] not in (0, 1)):
        return False
    if report["provider_state"] not in ("unknown", "started", "not_started") or report["failure_category"] not in (*FAILURE_CATEGORIES, None):
        return False
    if report["marker_digest"] is not None and not _sha(report["marker_digest"]):
        return False
    for key in ("lifecycle_digest", "mcp_digest"):
        if (canonical or report[key] is not None) and not _sha(report[key]):
            return False
    if report["cleanup_outcome"] not in (None, "success", "failure"):
        return False
    if report["status"] == "passed":
        if (report["boundary"] != "complete" or not report["counts_complete"] or not report["marker_match"]
                or not _sha(report["marker_digest"]) or any(counts[k] != 1 for k in COUNT_KEYS[:5])
                or report["failure_category"] is not None):
            return False
        if canonical and (report["attempts"] != 1 or report["provider_state"] != "started" or report["cleanup_outcome"] != "success"):
            return False
    elif report["boundary"] == "complete":
        return False
    return True


def _validate_bundle(directory, manifest):
    if not isinstance(manifest, dict) or set(manifest) != {"generation", "run_id", "identity_class", "owner_token_digest", "report", "attestation", "lifecycle", "mcp"}:
        raise ValueError("manifest schema")
    generation = manifest["generation"]
    if not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{32}", generation):
        raise ValueError("generation")
    if not _valid_run_id(manifest["run_id"]) or manifest["identity_class"] != "harness_run" or not _sha(manifest["owner_token_digest"]):
        raise ValueError("manifest identity")
    loaded = {}
    for kind in ("report", "attestation", "lifecycle", "mcp"):
        entry = manifest[kind]
        extension = "jsonl" if kind in {"lifecycle", "mcp"} else "json"
        if (not isinstance(entry, dict) or set(entry) != {"file", "sha256"}
                or entry["file"] != f"{kind}-{generation}.{extension}" or not _sha(entry["sha256"])):
            raise ValueError("generation file")
        path = Path(directory) / entry["file"]
        if path.is_symlink():
            raise ValueError("symlink")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ValueError("digest")
        if kind in {"report", "attestation"}:
            loaded[kind] = _json(data)
    report, attestation = loaded["report"], loaded["attestation"]
    if not valid_report(report, canonical=True) or report["report_path"] != str(Path(directory) / manifest["report"]["file"]):
        raise ValueError("canonical schema")
    keys = {"accepted_boundary", "controller_run_id", "harness_run_id", "report_digest", "lifecycle_digest", "mcp_digest",
            "identity_class", "generation", "parent_trusted_link", "event", "owner_token_digest"}
    if (not isinstance(attestation, dict) or set(attestation) != keys or attestation["generation"] != generation
            or attestation["parent_trusted_link"] is not True or attestation["event"] != "parent_final_report_accepted"
            or attestation["identity_class"] != "controller_attestation"
            or attestation["owner_token_digest"] != manifest["owner_token_digest"]
            or not isinstance(attestation["controller_run_id"], str)
            or not re.fullmatch(r"run-[A-Za-z0-9_-]{1,96}", attestation["controller_run_id"])
            or attestation["harness_run_id"] != report["run_id"] or attestation["report_digest"] != manifest["report"]["sha256"]
            or attestation["accepted_boundary"] != report["boundary"]
            or any(attestation[k] != report[k] for k in ("lifecycle_digest", "mcp_digest"))):
        raise ValueError("attestation schema or link")
    lifecycle, error = validate_lifecycle(Path(directory) / manifest["lifecycle"]["file"], report["run_id"], accepted=True)
    diagnostics, mcp_error = validate_mcp_journal(Path(directory) / manifest["mcp"]["file"], report["run_id"])
    if (error or mcp_error or lifecycle[-1]["outcome"] != "success" or report["cleanup_outcome"] != "success"
            or (report["attempts"], report["provider_state"]) != lifecycle_boundary(lifecycle)
            or report["lifecycle_digest"] != manifest["lifecycle"]["sha256"]
            or report["mcp_digest"] != manifest["mcp"]["sha256"]
            or report["counts"] != {k: sum(r["event"] == k for r in diagnostics) for k in COUNT_KEYS}):
        raise ValueError("sealed journal bridge")
    if report["status"] == "passed" and ([r["event"] for r in lifecycle] != list(LIFECYCLE_EVENTS[:-1]) or not mcp_chain(diagnostics, report["marker_digest"])):
        raise ValueError("sealed pass chain")
    return report


def read_committed(directory, *, expected=None):
    directory = Path(directory)
    manifest = _json((directory / "committed-manifest.json").read_bytes())
    if expected is not None and manifest != expected:
        raise ValueError("manifest local commit mismatch")
    report = _validate_bundle(directory, manifest)
    if expected is not None and (report["run_id"] != expected["run_id"] or report["identity_class"] != expected["identity_class"]):
        raise ValueError("manifest local identity mismatch")
    return report


def lifecycle_boundary(rows):
    names = [row["event"] for row in rows]
    return int("kickoff_entered" in names), "started" if "provider_request_started" in names else "not_started"


def mcp_chain(rows, digest):
    names = [r["event"] for r in rows]
    expected = ["crewai_base_tool_enter", "server_initialize", "server_initialized", "server_tools_list",
                "mcp_tools_call_started", "mcp_server_tool_executed", "mcp_tools_call_success", "crewai_base_tool_success"]
    return (names == expected and _sha(digest) and rows[-1].get("marker_present") is True
            and all(r.get("marker_digest") == digest for r in rows if r["event"] in {"mcp_server_tool_executed", "crewai_base_tool_success"}))


def _observed_counts(event_path: Path, run_id: str) -> dict:
    counts = {key: 0 for key in COUNT_KEYS}
    try: lines = Path(event_path).read_bytes().splitlines(keepends=True)
    except OSError: lines = []
    for raw in lines:
        if not raw.endswith(b"\n"): continue
        try: event = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError): continue
        if not isinstance(event, dict): continue
        if event.get("run_id") != run_id: continue
        name, source = event.get("event"), event.get("source")
        if name in COUNT_KEYS and ((name.startswith("crewai_") or name.startswith("mcp_")) and source in {"client", "server"} or name.startswith("tool_selection_")):
            counts[name] += 1
    return counts


def _read_final_report(report_path: Path, run_id: str, returncode: int) -> dict | None:
    try: report = _json(Path(report_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError): return None
    if not valid_report(report) or report.get("run_id") != run_id or report["report_path"] != str(report_path) or Path(report_path).is_symlink():
        return None
    if report.get("counts_complete") is not True or (returncode, report.get("status")) not in {(0, "passed"), (1, "blocked")}:
        return None
    return report


def install_provider_hook(crew, capability):
    """Wrap the installed native OpenAI outbound object, not an unused LLM.

    Agent executor calls this agent's llm.call; native OpenAI completion reaches
    its client.chat.completions.create. Unknown/async/stream paths fail preflight.
    """
    if len(crew.agents) != 1 or len(crew.tasks) != 1 or crew.tasks[0].agent is not crew.agents[0]:
        raise ValueError("unexpected crew")
    llm = crew.agents[0].llm
    if not callable(llm.call) or getattr(llm, "stream", False):
        raise ValueError("unsupported LLM boundary")
    outbound = llm.client.chat.completions
    original = outbound.create
    if not callable(original):
        raise ValueError("missing outbound callable")
    def wrapped(*args, **kwargs):
        capability.append("provider_request_started", "llm_wrapper")
        return original(*args, **kwargs)
    outbound.create = wrapped
    if crew.agents[0].llm.client.chat.completions.create is not wrapped:
        raise ValueError("hook not installed")
    return lambda: setattr(outbound, "create", original)


def build_crew(key: str):
    llm = LLM(model=MODEL_ID, base_url=os.environ.get("STAGE2_BASE_URL", "http://127.0.0.1:2455/v1"),
              api_key=key, temperature=0, max_tokens=160, max_retries=0, timeout=30)
    agent = Agent(role="Stage2 tool verifier", goal="Use the only available tool before answering.",
                  backstory="The random marker exists only in the tool result.", llm=llm,
                  tools=[mcp_tools.make_mcp_tool()], allow_delegation=False, cache=False,
                  verbose=False, max_iter=2, max_retry_limit=0, respect_context_window=False)
    task = Task(description='Call mcp_project_fixture_fixture_read with {"fixture":"project-fixture"}. Return its result; do not guess the marker.',
                expected_output="The exact fixture tool result.", agent=agent)
    return Crew(agents=[agent], tasks=[task], verbose=False, cache=False)


def summarize(events, run_id, expected_digest, raw, report_path=None):
    events = [e for e in events if e.get("run_id") == run_id]
    source_for = {"mcp_server_tool_executed": "server", "crewai_base_tool_enter": "client",
                  "mcp_tools_call_started": "client", "mcp_tools_call_success": "client",
                  "crewai_base_tool_success": "client"}
    diagnostics_valid = all(e.get("event") not in source_for or e.get("source") == source_for[e["event"]] for e in events)
    counts = {name: sum(e.get("event") == name and (e.get("source") == source_for.get(name) or name.startswith("tool_selection_")) for e in events) for name in COUNT_KEYS}
    try: returned = mcp_tools.returned_marker_digest(json.loads(raw))
    except (ValueError, TypeError): returned = None
    marker_events = [e.get("marker_digest") for e in events if e.get("event") in {"crewai_base_tool_success", "mcp_server_tool_executed"}]
    marker_match = bool(expected_digest and returned == expected_digest and len(marker_events) == 2 and all(x == expected_digest for x in marker_events))
    chain = {n: counts[n] for n in COUNT_KEYS[:5]}; passed = all(v == 1 for v in chain.values()) and marker_match
    failure = next((e.get("category") for e in events if e.get("event") == "tool_selection_failure"), None)
    if counts["tool_selection_failure"] and failure == "kickoff_exception": boundary = "kickoff_exception"
    elif counts["tool_selection_finished"] and not counts["crewai_base_tool_enter"]: boundary = "kickoff_completed_without_tool_selection"
    else: boundary = "complete" if passed else "tool_chain_or_marker_mismatch"
    if len(events) != len({(e.get("event"), e.get("source"), e.get("run_id")) for e in events}) or not diagnostics_valid: passed = False
    return _report(run_id, report_path or "", status="passed" if passed else "blocked", counts=counts, counts_complete=True,
                   marker_match=marker_match, marker_digest=returned, boundary=boundary,
                   failure_category=failure, attempts=1, provider_state="started" if passed else "unknown")


def worker(directory: Path, run_id: str) -> int:
    capabilities = []
    restore = None
    try:
        lifecycle = mcp_tools.EventCapability(int(os.environ["STAGE2_WORKER_FD"]))
        provider = mcp_tools.EventCapability(int(os.environ["STAGE2_LLM_WRAPPER_FD"]))
        client = mcp_tools.EventCapability(int(os.environ["STAGE2_CLIENT_FD"]))
        capabilities.extend((lifecycle, provider, client))
        server_fd = int(os.environ["STAGE2_SERVER_FD"])
        candidate = Path(directory) / "candidate-report.json"
        raw, failure, entered = "", None, False
        with mcp_tools.mcp_run_scope(run_id, event_path="", max_tool_calls=1, event_sink=client, server_fd=server_fd):
            try:
                key_path = os.environ.get("STAGE2_MODEL_KEY_FILE")
                if not key_path:
                    failure = "configuration_missing"
                    raise ValueError("configuration missing")
                key = Path(key_path).read_text(encoding="utf-8").strip()
                if not key:
                    failure = "configuration_missing"
                    raise ValueError("configuration missing")
                crew = build_crew(key)
                del key
                restore = install_provider_hook(crew, provider)
                lifecycle.append("kickoff_entered", "worker")
                entered = True
                raw = crew.kickoff().raw
            except Exception:
                failure = failure or ("kickoff_exception" if entered else "setup_exception")
            finally:
                mcp_tools.close_mcp_clients()
        events = lifecycle.request({"snapshot": "mcp"})
        digests = [e["marker_digest"] for e in events if e["event"] == "mcp_server_tool_executed"]
        report = summarize(events, run_id, digests[0] if len(digests) == 1 else None, raw, candidate)
        report.update(attempts=None, provider_state="unknown")
        if failure:
            report.update(status="blocked", boundary=failure, failure_category=failure)
        _write_report_atomic(candidate, report)
        lifecycle.append("worker_final_report_written", "worker")
        return 0 if report["status"] == "passed" else 1
    except Exception:
        return 1
    finally:
        if restore:
            restore()
        for capability in capabilities:
            capability.close()


def static_report():
    from crewai import LLM
    from crewai.agents.crew_agent_executor import CrewAgentExecutor
    symbols = [LLM.__new__, CrewAgentExecutor._invoke_loop]
    evidence = []
    for symbol in symbols:
        lines, start = inspect.getsourcelines(symbol)
        evidence.append({"symbol": symbol.__qualname__, "path": inspect.getfile(symbol), "start_line": start, "end_line": start + len(lines) - 1})
    assert version("crewai") == "1.5.0"
    return {"status": "static", "crewai_version": version("crewai"), "model": MODEL_ID, "evidence": evidence, "model_calls": 0}


def _enable_subreaper():
    # Reap orphaned fixture descendants as well as the worker, on Linux.
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise OSError("subreaper unavailable")


def cleanup_group(child):
    if child is None:
        return True
    try:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(child.pid, sig)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=2)
            except (subprocess.TimeoutExpired, ProcessLookupError):
                continue
        if child.returncode is None:
            return False
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                while os.waitpid(-child.pid, os.WNOHANG)[0]:
                    pass
            except ChildProcessError:
                pass
            try:
                os.killpg(child.pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.01)
    except OSError:
        return False
    return False


def main() -> int:
    if sys.argv[1:] == ["--static"]:
        print(json.dumps(static_report())); return 0
    if len(sys.argv) >= 2 and sys.argv[1] == "--worker":
        if len(sys.argv) != 4 or not _valid_run_id(sys.argv[3]):
            return 2
        return worker(Path(sys.argv[2]), sys.argv[3])
    if len(sys.argv) != 1:
        return 2
    run_id = "stage2-" + uuid.uuid4().hex
    controller = os.environ.get("STAGE2_CONTROLLER_RUN_ID", "")
    if not re.fullmatch(r"run-[A-Za-z0-9_-]{1,96}", controller):
        return 1
    requested = Path(os.environ.get("STAGE2_REPORT_PATH", tempfile.gettempdir() + f"/stage2-sanitized-report-{run_id}.json")).resolve()
    try:
        reservation = PublicationReservation(requested, run_id)
    except (OSError, ValueError):
        return 1
    publication = reservation.directory
    try:
        _enable_subreaper()
        with tempfile.TemporaryDirectory(prefix="stage2-parent-") as private, tempfile.TemporaryDirectory(prefix="stage2-worker-") as directory:
            broker = ParentBroker(Path(private), run_id)
            child, timed_out, spawn_failed = None, False, False
            try:
                broker.lifecycle.append("preflight_passed", "parent")
                env = {k: v for k, v in os.environ.items() if not k.startswith("STAGE2_")}
                if "STAGE2_MODEL_KEY_FILE" in os.environ:
                    env["STAGE2_MODEL_KEY_FILE"] = os.environ["STAGE2_MODEL_KEY_FILE"]
                if "STAGE2_BASE_URL" in os.environ:
                    env["STAGE2_BASE_URL"] = os.environ["STAGE2_BASE_URL"]
                fds = []
                for role, (_, connection, _) in broker.channels.items():
                    fds.append(connection.fileno())
                    env[f"STAGE2_{role.upper()}_FD"] = str(connection.fileno())
                try:
                    child = subprocess.Popen([sys.executable, __file__, "--worker", directory, run_id],
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                             start_new_session=True, close_fds=True, pass_fds=tuple(fds), env=env)
                except OSError:
                    spawn_failed = True
                if child is not None:
                    broker.lifecycle.append("worker_spawned", "parent")
                    broker.start()
                    for _, connection, _ in broker.channels.values():
                        connection.close()
                    try:
                        child.wait(timeout=120)
                    except subprocess.TimeoutExpired:
                        timed_out = True
            finally:
                cleaned = cleanup_group(child)
                # Stop all request handlers before cleanup/seal; no event can race
                # the digest cutoff or be acknowledged after it.
                broker.close()
                broker.lifecycle.append("cleanup_finished", "parent", "success" if cleaned else "failure")
                lifecycle_bytes, mcp_bytes = broker.seal()
            if not cleaned or broker.invalid:
                return 1
            lifecycle_path, mcp_path = broker.lifecycle.path, broker.mcp_path
            rows, error = validate_lifecycle(lifecycle_path, run_id, accepted=True)
            diagnostics, mcp_error = validate_mcp_journal(mcp_path, run_id)
            if error or mcp_error:
                return 1
            attempts, provider_state = lifecycle_boundary(rows)
            candidate_path = Path(directory) / "candidate-report.json"
            candidate = _read_final_report(candidate_path, run_id, child.returncode) if child else None
            names = [r["event"] for r in rows]
            counts = {key: sum(e["event"] == key for e in diagnostics) for key in COUNT_KEYS}
            candidate_valid = (candidate is not None and "worker_final_report_written" in names
                               and candidate["counts"] == counts and candidate["attempts"] is None
                               and candidate["provider_state"] == "unknown"
                               and candidate["lifecycle_digest"] is None and candidate["mcp_digest"] is None
                               and candidate["cleanup_outcome"] is None)
            if candidate_valid:
                before_kickoff = candidate["boundary"] in {"configuration_missing", "setup_exception"}
                candidate_valid = (before_kickoff == (attempts == 0)
                                   and (not before_kickoff or candidate["status"] == "blocked")
                                   and candidate["failure_category"] == (candidate["boundary"] if candidate["boundary"] in FAILURE_CATEGORIES else None))
            passed = (not timed_out and candidate_valid and candidate["status"] == "passed"
                      and names == list(LIFECYCLE_EVENTS[:-1]) and mcp_chain(diagnostics, candidate["marker_digest"]))
            boundary = "spawn_failure" if spawn_failed else ("kickoff_timeout" if attempts else "prekickoff_timeout") if timed_out else "worker_failure"
            if candidate_valid and not timed_out and (candidate["status"] == "blocked" or passed):
                boundary = candidate["boundary"]
            report = _report(run_id, "pending", status="passed" if passed else "blocked", counts=counts,
                             counts_complete=bool(candidate_valid and not timed_out),
                             marker_match=bool(candidate_valid and candidate["marker_match"]),
                             marker_digest=candidate["marker_digest"] if candidate_valid else None,
                             boundary=boundary, failure_category=candidate["failure_category"] if candidate_valid else None,
                             attempts=attempts, provider_state=provider_state, cleanup_outcome="success")
            if lifecycle_path.read_bytes() != lifecycle_bytes or mcp_path.read_bytes() != mcp_bytes:
                return 1
            reservation.assert_owner()
            expected_manifest = publish_canonical(
                publication, report, {"accepted_boundary": report["boundary"]}, lifecycle_path, mcp_path,
                controller, reservation.owner_token, on_commit=reservation.mark_committed)
            committed = read_committed(publication, expected=expected_manifest)
            reservation.assert_owner()
            if committed["run_id"] != run_id or committed["identity_class"] != "harness_run":
                return 1
            print(json.dumps(committed, sort_keys=True))
            return 0 if committed["status"] == "passed" else 1
    except (OSError, ValueError, TypeError):
        return 1
    finally:
        try:
            reservation.cleanup_uncommitted()
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    raise SystemExit(main())
