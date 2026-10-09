import json

from app.evaluation import assess_truth
from app.settings import get_settings
from app.storage.database import Database
from test_v22_adaptive import investigate, model_stub, prepared_profile, report


def case_and_run(client, monkeypatch, mode="graph_adaptive"):
    profile = prepared_profile(client, monkeypatch)
    model_stub(monkeypatch, lambda c, n: report())
    run = investigate(client, profile, mode=mode)
    response = client.post("/api/v1/evaluation-cases", json={
        "case_key": "isolated-costs", "system_id": profile["system_id"],
        "fault_class": "database_unavailable", "ground_truth": {"service": "store"},
    })
    assert response.status_code == 200, response.text
    case = response.json()
    return case, run, {
        "case_id": case["id"], "run_id": run["id"], "pair_key": "costs-triple",
        "arm": mode, "repetition": 1, "reset_digest": case["fixture_digest"], "verdict": "unresolved",
    }


def test_adaptive_trial_metrics_come_from_persisted_run(client, monkeypatch):
    _, run, request = case_and_run(client, monkeypatch)
    response = client.post("/api/v1/evaluation-trials", json=request)
    assert response.status_code == 200, response.text
    metrics = response.json()["metrics"]
    assert metrics["diagnostic_tool_calls"] == len(run["tool_calls"])
    assert metrics["model_calls"] == run["context_budget"]["calls"] == 1
    assert metrics["input_tokens"] == 64 and metrics["output_tokens"] == 32
    assert metrics["usage_kinds"] == ["actual"]
    assert metrics["elapsed_seconds"] >= 0 and metrics["tt_rca_seconds"] is None
    assert metrics["stop_reason"] == run["summary"]["stop_reason"]
    assert metrics["estimated_cost"] is None
    assert client.post("/api/v1/evaluation-trials", json=request).status_code == 409


def test_unresolved_run_cannot_be_scored_as_correct_root_cause(client, monkeypatch):
    _, _, request = case_and_run(client, monkeypatch, "graph_only")
    request["verdict"] = "correct"
    response = client.post("/api/v1/evaluation-trials", json=request)
    assert response.status_code == 409, response.text
    assert client.get("/api/v1/evaluations").json() == []


def test_comparison_uses_only_completed_three_arm_groups(client, monkeypatch):
    case, _, request = case_and_run(client, monkeypatch, "graph_only")
    assert client.post("/api/v1/evaluation-trials", json=request).status_code == 200
    assert client.get("/api/v1/metrics").json()["three_arm_evaluation"]["completed_groups"] == 0
    database = Database(get_settings().database_path)
    with database.transaction() as tx:
        profile_id = tx.execute("SELECT id FROM business_profiles").fetchone()[0]
    profile = client.get(f"/api/v1/businesses/{profile_id}").json()
    for mode in ("graph_pattern", "graph_adaptive"):
        run = investigate(client, profile, mode=mode)
        request.update(run_id=run["id"], arm=mode)
        response = client.post("/api/v1/evaluation-trials", json=request)
        assert response.status_code == 200, response.text
    comparison = client.get("/api/v1/metrics").json()["three_arm_evaluation"]
    assert comparison["completed_groups"] == 1
    assert set(comparison["arms"]) == {"graph_only", "graph_pattern", "graph_adaptive"}
    assert all(m["correct_rate"] == 0 and m["unresolved_rate"] == 1 for m in comparison["arms"].values())
    with database.transaction() as tx:
        assert tx.execute("SELECT COUNT(*) FROM evaluation_trials WHERE case_id=?", (case["id"],)).fetchone()[0] == 3
        rows = tx.execute("SELECT metrics_json FROM evaluation_trials").fetchall()
    assert all(json.loads(row[0])["model_calls"] == 1 for row in rows)


def test_multiple_fault_partial_diagnosis_is_not_a_wrong_cause(client, monkeypatch):
    _, run, _ = case_and_run(client, monkeypatch, "graph_only")
    database = Database(get_settings().database_path)
    connection = database.connect()
    try:
        node = connection.execute("SELECT id FROM graph_nodes WHERE snapshot_id=? AND name='store' AND role!='runtime'", (run["graph_snapshot_id"],)).fetchone()[0]
        measured_run = {**run, "status": "supported", "summary_json": json.dumps({"gate": {"hypotheses": [{"target_node_id": node, "failure_mode": "database_unavailable", "status": "supported"}]}})}
        case = {"ground_truth_json": json.dumps({"truth": {"store": "database_unavailable", "cache": "redis_unavailable"}})}
        result = assess_truth(connection, case, measured_run)
        assert result["expected_verdict"] == "incorrect" and result["partial_diagnosis"]
        assert not result["false_positive"]
        case["ground_truth_json"] = json.dumps({"truth": {"cache": "redis_unavailable"}})
        assert assess_truth(connection, case, measured_run)["false_positive"]
    finally:
        connection.close()
