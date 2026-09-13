"""Deterministic CrewAI -> BaseTool -> stdio MCP vertical smoke."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

from crewai import Agent, Crew, Task  # noqa: E402
from crewai.llms.base_llm import BaseLLM  # noqa: E402

import mcp_tools  # noqa: E402


class DeterministicLocalLLM(BaseLLM):
    """A local deterministic response source using CrewAI's normal executor."""

    model: str = "local-deterministic-fixture"
    calls: int = 0

    def call(self, messages, tools=None, callbacks=None, available_functions=None,
             from_task=None, from_agent=None, response_model=None):
        self.calls += 1
        if self.calls == 1:
            return (
                "Thought: use the only registered tool.\n"
                "Action: mcp_project_fixture_fixture_read\n"
                'Action Input: {"fixture": "project-fixture"}'
            )
        observation = json.dumps(messages)
        verified = bool(re.search(r"stage2-fixture-[0-9a-f]{32}", observation))
        return f"Thought: inspect the tool observation.\nFinal Answer: fixture_response_verified={str(verified).lower()}"


def run_vertical_smoke(event_path: Path) -> dict:
    run_id = "vertical-mcp-smoke"
    event_path.parent.mkdir(parents=True, exist_ok=True)
    event_path.touch()
    mcp_tools.close_mcp_clients()
    mcp_tools.MCP_CALL_COUNT = 0
    mcp_tools.MCP_SUCCESS_COUNT = 0
    llm = DeterministicLocalLLM(model="local-deterministic-fixture")
    tool = mcp_tools.make_mcp_tool(result_as_answer=False)
    agent = Agent(
        role="Vertical MCP verifier",
        goal="Use the registered MCP tool once and report a safe verification boolean.",
        backstory="A deterministic local adapter drives the normal CrewAI executor.",
        llm=llm,
        tools=[tool],
        allow_delegation=False,
        cache=False,
        verbose=False,
        max_iter=2,
        max_retry_limit=0,
    )
    task = Task(
        description="Call the only available MCP tool and report fixture_response_verified=true.",
        expected_output="A safe boolean assertion derived from the tool response.",
        agent=agent,
    )
    crew = Crew(agents=[agent], tasks=[task], cache=False, verbose=False)
    with mcp_tools.mcp_run_scope(run_id, event_path=str(event_path), max_tool_calls=1):
        result = crew.kickoff()
    mcp_tools.close_mcp_clients()
    events = [json.loads(line) for line in event_path.read_text().splitlines()]
    marker_digests = [e["marker_digest"] for e in events if e["event"] == "mcp_server_tool_executed"]
    final = result.raw
    final_assertion = final == "fixture_response_verified=true"
    exact_chain = (
        mcp_tools.MCP_CALL_COUNT == mcp_tools.MCP_SUCCESS_COUNT == 1
        and sum(e["event"] == "mcp_tools_call_started" for e in events) == 1
        and sum(e["event"] == "mcp_server_tool_executed" for e in events) == 1
        and sum(e["event"] == "mcp_tools_call_success" for e in events) == 1
        and sum(e["event"] == "crewai_base_tool_success" for e in events) == 1
    )
    return {
        "status": "passed" if exact_chain and final_assertion else "blocked",
        "run_id": run_id,
        "agent_calls": llm.calls,
        "mcp_call_count": mcp_tools.MCP_CALL_COUNT,
        "mcp_success_count": mcp_tools.MCP_SUCCESS_COUNT,
        "client_tool_events": sum(e["event"] == "mcp_tools_call_started" for e in events),
        "server_tool_events": sum(e["event"] == "mcp_server_tool_executed" for e in events),
        "client_success_events": sum(e["event"] == "mcp_tools_call_success" for e in events),
        "base_tool_success_events": sum(e["event"] == "crewai_base_tool_success" for e in events),
        "provider_events": sum(e["event"] == "provider_request_started" for e in events),
        "network_calls": 0,
        "final_assertion": final_assertion,
        "marker_digest": marker_digests[0] if len(marker_digests) == 1 else None,
        "raw_marker_emitted": bool(re.search(r"stage2-fixture-[0-9a-f]{32}", final)),
        "marker_digest_sha256": hashlib.sha256(marker_digests[0].encode()).hexdigest() if marker_digests else None,
    }


if __name__ == "__main__":
    import tempfile

    path = Path(tempfile.mktemp(prefix="vertical-mcp-", suffix=".jsonl"))
    evidence = run_vertical_smoke(path)
    path.unlink(missing_ok=True)
    print(json.dumps(evidence, sort_keys=True))
