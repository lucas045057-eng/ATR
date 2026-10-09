"""Measured run costs and comparable three-arm groups; no assumed provider usage."""

import json
from datetime import datetime

ARMS = ("graph_only", "graph_pattern", "graph_adaptive")


def run_metrics(connection, run):
    calls = connection.execute(
        "SELECT status FROM tool_calls WHERE run_id=? AND purpose='diagnosis'", (run["id"],)
    ).fetchall()
    steps = connection.execute(
        "SELECT COUNT(*) FROM diagnostic_steps WHERE run_id=?", (run["id"],)
    ).fetchone()[0]
    usages = connection.execute(
        """SELECT u.* FROM model_usage_records u JOIN context_budgets b ON b.id=u.budget_id
        WHERE b.run_id=? ORDER BY u.created_at""", (run["id"],)
    ).fetchall()
    start = datetime.fromisoformat(run["started_at"])

    def seconds(end):
        return max(0, (datetime.fromisoformat(end) - start).total_seconds()) if end else None

    summary = json.loads(run["summary_json"])
    return {
        "diagnostic_tool_calls": len(calls),
        "failed_or_denied_calls": sum(c["status"] in {"failed", "denied", "timed_out"} for c in calls),
        "diagnostic_steps": steps, "tt_rca_seconds": seconds(run["supported_at"]),
        "elapsed_seconds": seconds(run["ended_at"]), "run_status": run["status"],
        "stop_reason": summary.get("stop_reason") if isinstance(summary, dict) else None,
        "model_calls": len(usages), "input_tokens": sum(u["input_tokens"] or 0 for u in usages),
        "output_tokens": sum(u["output_tokens"] or 0 for u in usages),
        "usage_kinds": sorted({u["usage_kind"] for u in usages}),
        "estimated_cost": sum(u["cost"] for u in usages) if usages and all(u["cost"] is not None for u in usages) else None,
        "cost_kinds": sorted({u["cost_kind"] for u in usages}),
    }


def assess_truth(connection, case, run):
    truth = json.loads(case["ground_truth_json"]).get("truth")
    if not isinstance(truth, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in truth.items()):
        return None
    names = {r["id"]: r["name"] for r in connection.execute(
        "SELECT id,name FROM graph_nodes WHERE snapshot_id=?", (run["graph_snapshot_id"],)
    )}
    summary = json.loads(run["summary_json"])
    diagnosed = {names.get(h.get("target_node_id")): h.get("failure_mode")
                 for h in summary.get("gate", {}).get("hypotheses", []) if h.get("status") == "supported"}
    wrong = any(truth.get(service) != mechanism for service, mechanism in diagnosed.items())
    verdict = "unresolved" if run["status"] == "unresolved" else "correct" if diagnosed == truth else "incorrect"
    return {"expected_verdict": verdict, "false_positive": wrong,
            "partial_diagnosis": bool(diagnosed and not wrong and diagnosed != truth), "diagnosed": diagnosed}


def compare_trials(trials):
    groups = {}
    for row in trials:
        groups.setdefault((row["pair_key"], row["repetition"]), {})[row["arm"]] = row
    complete = [group for group in groups.values() if all(arm in group for arm in ARMS)]
    results = {}
    for arm in ARMS:
        rows = [group[arm] for group in complete]
        metrics = [json.loads(row["metrics_json"]) for row in rows]
        count = len(rows)
        results[arm] = {
            "trials": count,
            "correct_rate": sum(row["verdict"] == "correct" for row in rows) / count if count else None,
            "false_positive_rate": sum(m.get("false_positive", row["verdict"] == "incorrect") for row, m in zip(rows, metrics, strict=True)) / count if count else None,
            "partial_diagnosis_rate": sum(m.get("partial_diagnosis", False) for m in metrics) / count if count else None,
            "unresolved_rate": sum(m["run_status"] == "unresolved" for m in metrics) / count if count else None,
            **{f"mean_{field}": sum(m.get(field, 0) or 0 for m in metrics) / count if count else None
               for field in ("diagnostic_tool_calls", "model_calls", "input_tokens", "output_tokens", "elapsed_seconds")},
        }
    return {"completed_groups": len(complete), "excluded_incomplete_groups": len(groups) - len(complete),
            "verdict_source": "operator_or_fixture_ground_truth", "arms": results}
