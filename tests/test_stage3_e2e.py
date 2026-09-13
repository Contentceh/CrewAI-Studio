"""Threaded Stage 3 runner E2E: canonical native MCP fixture, no provider."""

import json
import os
import re
import sys
import threading
from pathlib import Path
from queue import Queue

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from crewai import Agent, Crew, Task
from crewai.llms.base_llm import BaseLLM

import mcp_profiles
from pg_crew_run import PageCrewRun


class LocalToolLLM(BaseLLM):
    model: str = "stage3-local-deterministic"
    calls: int = 0
    observed_tool_names: list[str] = []
    call_threads: list[int] = []

    def call(self, messages, tools=None, callbacks=None, available_functions=None,
             from_task=None, from_agent=None, response_model=None):
        self.calls += 1
        self.call_threads.append(threading.get_ident())
        candidates = tools or available_functions or []
        for item in candidates:
            if isinstance(item, dict):
                name = item.get("function", {}).get("name", "") or item.get("name", "")
            else:
                name = getattr(item, "name", "")
            if name:
                self.observed_tool_names.append(name)
        if self.calls == 1:
            name = self.observed_tool_names[0] if self.observed_tool_names else "fixture.read"
            return f'Thought: use native MCP.\nAction: {name}\nAction Input: {{"fixture":"project-fixture"}}'
        verified = bool(re.search(r"stage2-fixture-[0-9a-f]{32}", json.dumps(messages)))
        return f"Thought: verify observation.\nFinal Answer: fixture_response_verified={str(verified).lower()}"


def _run_once(event_path: Path, evidence_path: Path):
    llm = LocalToolLLM(model="stage3-local-deterministic")
    agent = Agent(role="Stage 3 verifier", goal="Use native fixture once.",
                  backstory="Local deterministic verifier.", llm=llm,
                  allow_delegation=False, cache=False, verbose=False,
                  max_iter=2, max_retry_limit=0)
    task = Task(description="Read the fixture and return the safe boolean.",
                expected_output="fixture_response_verified=true", agent=agent)
    crew = Crew(agents=[agent], tasks=[task], cache=False, verbose=False)
    original_tools = list(agent.tools)
    queue = Queue()
    runner = PageCrewRun.__new__(PageCrewRun)
    worker = threading.Thread(target=runner.run_crew,
                              kwargs={"crewai_crew": crew, "inputs": {},
                                      "message_queue": queue,
                                      "mcp_event_path": str(event_path),
                                      "mcp_evidence_path": str(evidence_path)})
    worker.start()
    worker.join(60)
    assert not worker.is_alive()
    message = queue.get(timeout=2)
    assert "mcp_failure" not in message
    assert agent.tools == original_tools
    return message, llm, crew, worker.ident


def test_real_threaded_runner_native_chain_reload_and_write_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOPS_ENABLED", "False")
    first_registry = mcp_profiles.load_profiles()
    second_registry = mcp_profiles.load_profiles()
    assert first_registry == second_registry
    assert [key for key, value in first_registry.items() if value["enabled"]] == ["project_fixture"]

    event_path = tmp_path / "events.jsonl"
    evidence_path = tmp_path / "evidence.json"
    message, llm, crew, worker_thread_id = _run_once(event_path, evidence_path)
    raw = message["result"].raw
    rows = [json.loads(line) for line in event_path.read_text().splitlines()]
    evidence = json.loads(evidence_path.read_text())
    server_calls = [row for row in rows if row["event"] == "mcp_server_tool_executed"]
    assert len(server_calls) == 1
    assert llm.call_threads == [worker_thread_id, worker_thread_id]
    assert raw == "fixture_response_verified=true"
    assert "stage2-fixture-" not in raw
    assert "stage2-fixture-" not in json.dumps(evidence)
    assert llm.observed_tool_names == []
    assert evidence["profiles"][0]["advertised_names"] == ["fixture.read"]
    assert message["mcp_evidence"]["write_action"] == "not_available"
    assert evidence["write_action"] == "not_available"
    assert evidence["context_open_thread_id"] == evidence["context_close_thread_id"]
    assert re.fullmatch(r"[0-9a-f]{64}", server_calls[0]["marker_digest"])
    assert not any("write" in row.get("tool", "").lower() for row in rows)
    assert crew.manager_agent is None

    second_event_path = tmp_path / "events-second.jsonl"
    second_evidence_path = tmp_path / "evidence-second.json"
    second_message, _, _second_crew, second_worker_thread_id = _run_once(
        second_event_path, second_evidence_path
    )
    assert isinstance(second_worker_thread_id, int)
    assert second_message["result"].raw == "fixture_response_verified=true"
    second_rows = [json.loads(line) for line in second_event_path.read_text().splitlines()]
    assert sum(row["event"] == "mcp_server_tool_executed" for row in second_rows) == 1
    assert json.loads(second_evidence_path.read_text())["write_action"] == "not_available"
