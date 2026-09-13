"""Exercise actual discovery subprocesses, cleanup and UI rerun behavior."""
import json
import os
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from mcp_health import HealthChecks, MESSAGES, Result, probe, status
from mcp_inventory import Connection, load_inventory, tool_rows


@pytest.fixture
def connection(tmp_path):
    server = tmp_path / "server.py"
    log = tmp_path / "requests.jsonl"
    pid = tmp_path / "pid"
    server.write_text('''import json,sys,os,time
from pathlib import Path
log,pid,mode=sys.argv[1:]
Path(pid).write_text(str(os.getpid()))
for line in sys.stdin:
 r=json.loads(line)
 with open(log,'a') as f: f.write(json.dumps(r)+'\\n')
 if mode=='timeout': time.sleep(60)
 if mode=='exit': sys.exit(1)
 if 'id' not in r: continue
 if mode=='auth':
  print(json.dumps({'jsonrpc':'2.0','id':r['id'],'error':{'code':401,'message':'SECRET_AUTH_VALUE'}}),flush=True)
  continue
 if r['method']=='initialize':
  result={'protocolVersion':'2025-06-18','capabilities':{'tools':{}},'serverInfo':{'name':'fixture','version':'1'}}
  if mode=='bad_init': result={}
 else:
  result={'tools':[{'name':'fixture.read','inputSchema':{'type':'object'}}]}
  if mode=='bad_tools': result={}
  if mode=='list_error':
   print(json.dumps({'jsonrpc':'2.0','id':r['id'],'error':{'code':-32601,'message':'SECRET_DETAIL'}}),flush=True)
   continue
  if mode=='pagination' and not r.get('params',{}).get('cursor'): result['nextCursor']='next'
  if mode=='endless': result['nextCursor']='next'
 response={'jsonrpc':'2.0','id':r['id'],'result':result}
 if mode=='bad_rpc': response.pop('jsonrpc')
 print(json.dumps(response),flush=True)
''')
    return Connection("test", "Test", "stdio", True, True, ("fixture.read",), {
        "command": [sys.executable, str(server), str(log), str(pid), "ok"],
        "timeout_seconds": 0.5,
    })


def mode(connection, value):
    config = dict(connection.config)
    config['command'] = [*config['command'][:-1], value]
    return replace(connection, config=config)


def assert_reaped(connection):
    pid = int(Path(connection.config['command'][3]).read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_success_only_discovers_and_reaps(connection):
    result = probe(connection)
    assert result.ok and result.tools == ('fixture.read',)
    assert status(connection, result) == '🟢 Работает'
    requests = [json.loads(line) for line in Path(connection.config['command'][2]).read_text().splitlines()]
    assert [r['method'] for r in requests] == ['initialize', 'notifications/initialized', 'tools/list']
    assert_reaped(connection)


@pytest.mark.parametrize('value,error', [
    ('timeout', 'timeout'), ('exit', 'unavailable'), ('bad_init', 'protocol'),
    ('bad_tools', 'protocol'), ('bad_rpc', 'protocol'), ('list_error', 'remote'),
    ('auth', 'auth'), ('endless', 'protocol'),
])
def test_failure_never_green_and_sanitized(connection, value, error):
    connection = mode(connection, value)
    started = time.monotonic()
    result = probe(connection)
    assert not result.ok and result.error == MESSAGES[error]
    assert 'SECRET' not in repr(result)
    assert status(connection, result) == '🔴 Не работает'
    assert time.monotonic() - started < 3
    assert_reaped(connection)


def test_pagination(connection):
    assert probe(mode(connection, 'pagination')).ok


def test_missing_command(connection):
    connection = replace(connection, config={'command': ['/nonexistent/studio-mcp']})
    assert probe(connection).error == MESSAGES['unavailable']


def test_disabled_never_starts(connection):
    connection = replace(connection, enabled=False)
    assert not probe(connection).ok
    assert not Path(connection.config['command'][3]).exists()
    assert status(connection) == '⚪ Выключен'


def test_unknown_stale_and_config_changed(connection):
    assert status(connection) == '⚪ Не проверен'
    result = Result(connection.fingerprint, 100, True)
    assert status(connection, result, now=400) == '⚪ Не проверен'
    assert status(mode(connection, 'exit'), result, now=101) == '⚪ Не проверен'
    assert status(connection, result, pending=True, now=101) == '⏳ Проверяется'


def test_policy_cannot_supply_commands_or_enable_adapter(tmp_path):
    path = tmp_path / 'policy.yaml'
    path.write_text('''schema_version: studio.mcp-policy.v1
services:
  external:
    adapter_id: external
    adapter_kind: native_mcp
    enabled: true
    command: [forbidden]
  grafana:
    adapter_kind: reference_only
''')
    connections, warning = load_inventory({}, path)
    assert warning is None and len(connections) == 1
    assert not connections[0].registered
    assert probe(connections[0]).error == MESSAGES['profile']
    path.write_text('broken: [')
    assert load_inventory({}, path)[1]


def test_fixture_mapping_and_agent_assignment_are_read_only():
    fixture = SimpleNamespace(name='MCPFixtureTool', tool_id='T1', parameters={})
    agent = SimpleNamespace(id='A1', role='Researcher', tools=[fixture])
    conn = Connection('project_fixture', 'Fixture', 'stdio', True, True, ('fixture.read',))
    rows = tool_rows(conn, [fixture], [agent], ('unapproved.tool',))
    assert rows[0]['Объявлен сервером'] == 'Нет'
    assert rows[1]['Объявлен сервером'] == 'Да'
    assert tool_rows(conn, [fixture], [agent])[0]['Объявлен сервером'] == 'Не проверено'
    assert rows[0]['В Tools'] == 'T1'
    assert rows[0]['Агенты'] == 'Researcher (A1)'
    assert rows[1]['Разрешён адаптером'] == 'Нет'
    assert agent.tools == [fixture] and fixture.parameters == {}


def test_background_dedup_and_snapshot_do_not_run_checks(connection):
    started, finish = threading.Event(), threading.Event()
    calls = []
    def check(conn):
        calls.append(conn)
        started.set()
        finish.wait(2)
        return Result(conn.fingerprint, time.time(), True)
    checks = HealthChecks(check)
    try:
        assert checks.snapshot(connection) == (None, False)
        assert not calls
        assert checks.submit(connection)
        assert started.wait(1)
        assert not checks.submit(connection)
        for _ in range(4):
            assert checks.snapshot(connection)[1]
        assert len(calls) == 1
        assert not checks.submit(replace(connection, enabled=False))
        finish.set()
        for _ in range(100):
            result, pending = checks.snapshot(connection)
            if not pending: break
            time.sleep(.01)
        assert result.ok and not pending
    finally:
        finish.set()
        checks.close()
