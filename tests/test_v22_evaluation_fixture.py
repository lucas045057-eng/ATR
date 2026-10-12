from scripts import evaluate_faults


def test_repeatable_stub_harness_measures_three_arms_and_keeps_history(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluate_faults, "CASES", [evaluate_faults.CASES[0], evaluate_faults.CASES[8]])
    output = tmp_path / "measured.json"
    result = evaluate_faults.evaluate(tmp_path / "isolated", output)
    assert result["category"] == "mock_observations_stub_model"
    assert result["comparison"]["completed_groups"] == 2
    assert len(result["trials"]) == 6 and not result["external_model_verified"]
    for trial in result["trials"]:
        assert len(trial["tool_calls"]) == trial["metrics"]["diagnostic_tool_calls"]
        if trial["metrics"]["model_calls"]:
            assert trial["metrics"]["usage_kinds"] == ["estimate"]
        else:
            assert trial["metrics"]["usage_kinds"] == []
        assert not trial["false_positive"]
    assert result["trials"][0]["verdict"] == "correct"
    assert all(t["verdict"] == "unresolved" for t in result["trials"][3:])
    try:
        evaluate_faults.evaluate(tmp_path / "isolated", output)
    except ValueError as error:
        assert "history is preserved" in str(error)
    else:
        raise AssertionError("Existing evaluation database was overwritten")


def test_legacy_collector_cannot_claim_adaptive_execution(client):
    system = client.post("/api/v1/systems/onboard", json={"source_key": "system-a", "display_name": "legacy"}).json()
    response = client.post("/api/v1/incidents", json={"system_id": system["system_id"], "symptom": "unavailable", "mode": "graph_adaptive"})
    assert response.status_code == 409, response.text
