import queue
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from crewai import Agent, Crew, Process
from crewai.llms.base_llm import BaseLLM

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from pg_crew_run import PageCrewRun, _hook_hierarchical_manager, _inject_native_tools
from mcp_profiles import NativeProfileError


class Tool:
    def __init__(self, name):
        self.name = name


class DummyLLM(BaseLLM):
    model: str = "stage3-dummy"

    def call(self, messages, tools=None, callbacks=None, available_functions=None,
             from_task=None, from_agent=None, response_model=None):
        return "ok"


def test_native_tools_are_assigned_to_all_agents_and_manager_then_restored():
    first = SimpleNamespace(name="first")
    manager = SimpleNamespace(name="manager")
    crew = SimpleNamespace(agents=[first], manager_agent=manager)
    native = [Tool("fixture.read")]
    with _inject_native_tools(crew, native):
        assert first.tools == native
        assert manager.tools == native
    assert first.tools == []
    assert manager.tools == []


def test_existing_duplicate_fails_closed_and_restores():
    existing = Tool("fixture.read")
    agent = SimpleNamespace(name="first", tools=[existing])
    crew = SimpleNamespace(agents=[agent], manager_agent=None,
                           kickoff=lambda **kwargs: None)
    with pytest.raises(NativeProfileError, match="duplicate_tool_name"):
        with _inject_native_tools(crew, [Tool("fixture.read")]):
            pass
    assert agent.tools == [existing]


def test_real_crewai_hierarchical_manager_path_receives_tools_and_restores():
    crew = Crew(
        agents=[Agent(role="worker", goal="work", backstory="test", tools=[], allow_delegation=False,
                      llm=DummyLLM(model="stage3-dummy"))],
        tasks=[], process=Process.hierarchical, manager_llm=DummyLLM(model="stage3-dummy"), verbose=False,
    )
    native = [Tool("fixture.read")]
    with _hook_hierarchical_manager(crew, native):
        crew._create_manager_agent()
        assert crew.manager_agent is not None
        assert crew.manager_agent.tools == native
    assert crew.manager_agent is None


def test_runner_sanitizes_mcp_error_and_restores_after_kickoff_exception(monkeypatch):
    original = [Tool("selected")]
    agent = SimpleNamespace(name="first", tools=original)
    crew = SimpleNamespace(agents=[agent], manager_agent=None,
                           kickoff=lambda **kwargs: None)

    class Context:
        def __enter__(self):
            return ([Tool("fixture.read")], ({"profile_id": "project_fixture",
                                                "advertised_names": ["fixture.read"],
                                                "advertised_count": 1,
                                                "transport": "stdio", "status": "enabled"},))
        def __exit__(self, *args):
            return False

    monkeypatch.setattr("pg_crew_run.native_enabled_profiles", lambda **kwargs: Context())
    monkeypatch.setattr(crew, "kickoff", lambda **kwargs: (_ for _ in ()).throw(NativeProfileError("remote_error", "project_fixture", "fixture.read")))
    monkeypatch.setattr("pg_crew_run.ss", SimpleNamespace(console_capture=None, agentops_failed=True))
    messages = queue.Queue()
    PageCrewRun.run_crew(object(), crew, {}, messages)
    message = messages.get_nowait()
    assert message["result"] == "MCP run unavailable (remote_error profile=project_fixture tool=fixture.read)"
    assert agent.tools == original
