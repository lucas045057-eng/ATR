"""Historical investigation knowledge is scoped guidance, never current evidence."""

import json

from app.common import digest


def graph_contract(connection, snapshot_id):
    snapshot = connection.execute("SELECT * FROM graph_snapshots WHERE id=?", (snapshot_id,)).fetchone()
    if not snapshot:
        return None
    coverage = json.loads(snapshot["coverage_json"])
    sources = sorted({r[0] for r in connection.execute(
        "SELECT source_revision FROM fact_claims WHERE snapshot_id=?", (snapshot_id,)
    ) if r[0]})
    services = sorted((r["name"], r["role"]) for r in connection.execute(
        "SELECT name,role FROM graph_nodes WHERE snapshot_id=? AND kind='service' AND role!='runtime'", (snapshot_id,)
    ))
    return {"contract_schema": 2, "snapshot_id": snapshot_id,
            "deployment_digest": snapshot["deployment_digest"],
            "source_digest": coverage.get("source_digest") or digest(sources),
            "services": [list(s) for s in services]}


def feedback_health(connection, pattern_id):
    rows = connection.execute(
        "SELECT validation_outcome,usefulness_outcome FROM pattern_matches WHERE pattern_version_id=? AND decision='suggested'", (pattern_id,)
    ).fetchall()
    result = {"suggested": len(rows),
              **{key: sum(r[column] == value for r in rows) for key, column, value in (
                  ("validated", "validation_outcome", "validated"), ("validation_failed", "validation_outcome", "failed"),
                  ("useful", "usefulness_outcome", "useful"), ("not_useful", "usefulness_outcome", "not_useful"))}}
    result["needs_review"] = bool(result["validation_failed"] or result["not_useful"] >= 2)
    return result


def compatibility(connection, pattern, snapshot_id):
    validity = json.loads(pattern["validity_json"])
    current = graph_contract(connection, snapshot_id)
    # V2.1 versions retain their review; derive constraints from their immutable source snapshot.
    previous = validity if validity.get("contract_schema") == 2 else graph_contract(connection, validity.get("snapshot_id"))
    reasons = []
    if not previous or not current:
        reasons.append("source_snapshot_unavailable")
    else:
        for key in ("deployment_digest", "source_digest", "services"):
            if previous.get(key) != current.get(key):
                reasons.append(key + "_changed")
    if feedback_health(connection, pattern["id"])["needs_review"]:
        reasons.append("negative_validation_feedback")
    return {"compatible": not reasons, "reasons": reasons, "source_snapshot_id": validity.get("snapshot_id"),
            "current_snapshot_id": snapshot_id}


def successful_checks(connection, hypothesis_id, snapshot_id):
    rows = connection.execute(
        """SELECT t.id AS source_call_id,t.tool_id,t.purpose,n.name AS target_service,n.role AS target_role
        FROM hypothesis_evidence h JOIN evidence e ON e.id=h.evidence_id
        JOIN tool_calls t ON t.id=e.tool_call_id JOIN graph_nodes n ON n.id=e.target_node_id
        WHERE h.hypothesis_id=? AND h.stance='support' AND t.purpose='diagnosis'
        AND t.status='succeeded' AND n.snapshot_id=? AND n.kind='service'
        ORDER BY t.started_at,t.rowid""", (hypothesis_id, snapshot_id)
    ).fetchall()
    checks, seen = [], set()
    for row in rows:
        key = (row["tool_id"], row["target_service"])
        if key not in seen:
            checks.append({**dict(row), "sequence_no": len(checks) + 1})
            seen.add(key)
    return checks


def counterexamples(summary, status):
    examples, seen = [], set()
    hypotheses = summary.get("hypothesis_history", []) + summary.get("gate", {}).get("hypotheses", [])
    for h in hypotheses:
        if h.get("status") != "refuted" and status != "unresolved":
            continue
        key = (h.get("target_node_id"), h.get("failure_mode"))
        if key in seen:
            continue
        seen.add(key)
        examples.append({"target_node_id": key[0], "failure_mode": key[1], "status": h.get("status", "unresolved"),
                         "counter_evidence": h.get("counter_evidence", h.get("verification", {}).get("counter_evidence", []))[:8],
                         "missing_evidence": h.get("missing_evidence", h.get("verification", {}).get("missing_evidence", []))[:4],
                         "stop_reason": summary.get("stop_reason"), "knowledge_status": "unverified_counterexample"})
    if not examples and status == "unresolved":
        examples.append({"status": "unresolved", "stop_reason": summary.get("stop_reason"),
                         "knowledge_status": "unverified_counterexample"})
    return examples[:24]
