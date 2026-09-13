import sys
import time
from pathlib import Path

from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))


def test_mcp_page_click_and_rerun_preserve_results_and_assignments():
    script = '''
import streamlit as st
from types import SimpleNamespace
from pg_mcp import PageMCP
if 'tools' not in st.session_state:
    fixture = SimpleNamespace(name='MCPFixtureTool', tool_id='fixture-1', parameters={})
    st.session_state.tools = [fixture]
    st.session_state.agents = [SimpleNamespace(id='agent-1', role='Researcher', tools=[fixture])]
PageMCP().draw()
'''
    at = AppTest.from_string(script).run(timeout=60)
    assert not at.exception
    assert any('MCP Fixture' in item.value for item in at.subheader)
    assert any('⚪ Не проверен' in item.value for item in at.markdown)
    at.button(key='mcp_check_project_fixture').click().run(timeout=30)
    assert not at.exception
    for _ in range(40):
        at.run(timeout=30)
        if any('🟢 Работает' in item.value for item in at.markdown):
            break
        time.sleep(.05)
    assert not at.exception
    assert any('🟢 Работает' in item.value for item in at.markdown)
    timestamp = [c.value for c in at.caption if c.value.startswith('Последняя проверка:')][0]
    at.run(timeout=30)
    assert [c.value for c in at.caption if c.value.startswith('Последняя проверка:')][0] == timestamp
    assert at.session_state.agents[0].tools[0].tool_id == 'fixture-1'
    assert 'Researcher (agent-1)' in at.dataframe[0].value['Агенты'].tolist()
    assert all(b.disabled for b in at.button if b.key.startswith('mcp_check_') and b.key not in ('mcp_check_all','mcp_check_project_fixture'))
    at.button(key='mcp_check_all').click().run(timeout=30)
    assert not at.exception


def test_sidebar_tools_and_persisted_assignment(tmp_path, monkeypatch):
    import db_utils
    from sqlalchemy import create_engine
    from my_tools import MyMCPFixtureTool

    monkeypatch.setattr(db_utils, 'engine', create_engine(f'sqlite:///{tmp_path}/studio.db'))
    db_utils.initialize_db()
    tool = MyMCPFixtureTool(tool_id='mcp-persisted')
    db_utils.save_tool(tool)
    db_utils.save_entity('agent', 'agent-persisted', {
        'role': 'Persisted agent', 'goal': 'Test', 'backstory': 'Test',
        'llm_provider_model': 'OpenAI: gpt-4o-mini', 'tool_ids': ['mcp-persisted'],
    })
    before = db_utils.load_entities('agent')
    app = Path(__file__).resolve().parents[1] / 'app' / 'app.py'
    at = AppTest.from_file(str(app)).run(timeout=60)
    assert not at.exception
    assert 'MCP' in at.sidebar.radio[0].options
    at.sidebar.radio[0].set_value('Tools').run(timeout=30)
    assert not at.exception
    assert any('MCP-сервер: MCP Fixture' in element.value for element in at.text)
    at.sidebar.radio[0].set_value('MCP').run(timeout=30)
    assert not at.exception
    assert db_utils.load_entities('agent') == before
    assert at.session_state.agents[0].tools[0].tool_id == 'mcp-persisted'
