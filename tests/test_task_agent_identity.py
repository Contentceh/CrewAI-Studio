import sys
import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

from my_task import MyTask  # noqa: E402


def test_task_uses_the_prebuilt_crewai_agent_identity():
    class SourceAgent:
        role = "identity"

        def get_crewai_agent(self):
            raise AssertionError("fallback agent construction was used")

    class TaskAgent:
        role = "identity"

        def get_crewai_agent(self):
            raise AssertionError("fallback agent construction was used")

    import crewai

    class StubLLM:
        def call(self, *args, **kwargs):
            return "unused"

    os.environ.setdefault("OPENAI_API_KEY", "stage2-test-only")
    provided = crewai.Agent(role="identity", goal="test", backstory="test", llm=StubLLM())
    task = MyTask.__new__(MyTask)
    task.description = "identity"
    task.expected_output = "identity"
    task.async_execution = False
    task.agent = SourceAgent()
    created = task.get_crewai_task(crewai_agent=provided)
    assert created.agent is provided
