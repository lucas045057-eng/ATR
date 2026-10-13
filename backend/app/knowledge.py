"""Historical investigation knowledge is scoped guidance, never current evidence."""

import json

from app.common import digest, dumps, now


def experience_business(connection, experience_id):
    row=connection.execute('''SELECT e.incident_id,e.graph_snapshot_id,r.incident_id AS run_incident,
        r.graph_snapshot_id AS run_snapshot,r.summary_json,r.target_id,i.system_id,g.system_id AS graph_system
        FROM experiences e JOIN diagnosis_runs r ON r.id=e.run_id JOIN incidents i ON i.id=e.incident_id
        JOIN graph_snapshots g ON g.id=e.graph_snapshot_id WHERE e.id=?''',(experience_id,)).fetchone()
    if (not row or row['incident_id']!=row['run_incident'] or row['graph_snapshot_id']!=row['run_snapshot']
            or row['system_id']!=row['graph_system']):
        return None, {'reason':'source_chain_inconsistent'}
    try:
        summary=json.loads(row['summary_json'])
        if not isinstance(summary,dict):
            raise ValueError
    except (TypeError,ValueError):
        return None, {'reason':'source_metadata_invalid'}
    explicit=[]
    if summary.get('business_id'):
        explicit.append(summary['business_id'])
    if row['target_id']:
        target=connection.execute('SELECT business_id FROM runtime_targets WHERE id=?',(row['target_id'],)).fetchone()
        if not target:
            return None, {'reason':'source_target_unavailable'}
        explicit.append(target['business_id'])
    owners=[item['id'] for item in connection.execute('SELECT id FROM business_profiles WHERE system_id=?',(row['system_id'],))]
    if explicit:
        if len(set(explicit))!=1 or explicit[0] not in owners:
            return None, {'reason':'explicit_business_conflict'}
        owner=explicit[0]
        basis='run_and_target_business'
    elif len(owners)==1:
        owner=owners[0]
        basis='unique_system_business'
    else:
        return None, {'reason':'business_ownership_unknown_or_ambiguous'}
    return owner, {'basis':basis,'system_id':row['system_id'],'snapshot_id':row['graph_snapshot_id']}


def refresh_business_ownership(connection, experience_id=None):
    rows=connection.execute('SELECT id FROM experiences'+(' WHERE id=?' if experience_id else ''),
                            (experience_id,) if experience_id else ()).fetchall()
    for row in rows:
        owner,proof=experience_business(connection,row['id'])
        serialized=dumps(proof)
        previous=connection.execute('SELECT business_id,proof_json FROM experience_ownership WHERE experience_id=?',(row['id'],)).fetchone()
        if previous and previous['business_id']==owner and previous['proof_json']==serialized:
            continue
        connection.execute('''INSERT INTO experience_ownership VALUES(?,?,?,?) ON CONFLICT(experience_id)
            DO UPDATE SET business_id=excluded.business_id,proof_json=excluded.proof_json,updated_at=excluded.updated_at''',
                           (row['id'],owner,serialized,now()))


def owned_patterns(connection, business_id: str, snapshot_id: str) -> list[dict]:
    snapshot=connection.execute('SELECT system_id FROM graph_snapshots WHERE id=?',(snapshot_id,)).fetchone()
    business=connection.execute('SELECT system_id FROM business_profiles WHERE id=?',(business_id,)).fetchone()
    if not snapshot or not business or snapshot['system_id']!=business['system_id']:
        return []
    # Ownership is selected before any matching by source content or symptom.
    rows=connection.execute('''SELECT p.* FROM pattern_versions p JOIN experience_ownership o
        ON o.experience_id=p.source_experience_id WHERE o.business_id=? AND p.status='trusted'
        ORDER BY p.pattern_key,p.version DESC''',(business_id,)).fetchall()
    result=[]
    for row in rows:
        owner,_=experience_business(connection,row['source_experience_id'])
        if owner==business_id:
            result.append(dict(row))
    return result


def compatible_patterns(connection, business_id: str, snapshot_id: str) -> list[dict]:
    return [pattern for pattern in owned_patterns(connection,business_id,snapshot_id)
            if compatibility(connection,pattern,snapshot_id)['compatible']]


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
    original=connection.execute('SELECT system_id FROM graph_snapshots WHERE id=?',(validity.get('snapshot_id'),)).fetchone()
    target=connection.execute('SELECT system_id FROM graph_snapshots WHERE id=?',(snapshot_id,)).fetchone()
    if not original or not target or original['system_id']!=target['system_id']:
        reasons.append('system_ownership_changed')
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
