import json


def test_crewai_agent_invokes_real_stdio_mcp_tool_once(tmp_path):
    import smoke_vertical_crewai_mcp as smoke

    evidence = smoke.run_vertical_smoke(tmp_path / "events.jsonl")
    assert evidence["status"] == "passed"
    assert evidence["agent_calls"] == 2
    assert evidence["mcp_call_count"] == evidence["mcp_success_count"] == 1
    assert evidence["client_tool_events"] == evidence["server_tool_events"] == 1
    assert evidence["client_success_events"] == evidence["base_tool_success_events"] == 1
    assert evidence["provider_events"] == evidence["network_calls"] == 0
    assert evidence["final_assertion"] is True
    assert evidence["raw_marker_emitted"] is False
    assert len(evidence["marker_digest"]) == 64
    assert len(evidence["marker_digest_sha256"]) == 64
    assert "stage2-fixture-" not in json.dumps(evidence)
