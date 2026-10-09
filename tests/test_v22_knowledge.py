import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from app.connectors.mock import MockConnector
from app.settings import get_settings
from conftest import wait_job
from test_business import SAMPLE, archive
from test_v22_adaptive import hypothesis, investigate, model_stub, prepared_profile, report


@pytest.fixture
def verification_url(monkeypatch):
    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Health)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    url = f"http://127.0.0.1:{server.server_port}/health"
    get_settings().allowed_health_urls = url
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)


def capture(client, run, url):
    response = client.post(f"/api/v1/incidents/{run['incident_id']}/repair", json={
        "summary": "Fixture operator repaired isolated service",
        "operator_verified": True, "verification_summary": "Actual loopback HTTP 200 after fixture repair",
        "verification_url": url,
    })
    assert response.status_code == 200, response.text
    return response.json()["pattern_version_id"]


def trusted(client, monkeypatch, url):
    profile = prepared_profile(client, monkeypatch)
    original = MockConnector.read
    def read(self, tool, args):
        if tool == "service.status" and args["name"] == "catalog-api.service":
            return {"connector": "mock", "state": "failed"}
        return original(self, tool, args)
    monkeypatch.setattr(MockConnector, "read", read)
    model_stub(monkeypatch, lambda context, n: report([hypothesis(context, "catalog-api", failure="startup_failure", all_evidence=True)]))
    run = investigate(client, profile, mode="graph_only")
    assert run["status"] == "supported"
    pattern_id = capture(client, run, url)
    response = client.post(f"/api/v1/patterns/{pattern_id}/review", json={"decision": "approve", "reason": "Operator verified synthetic fixture and scoped version"})
    assert response.status_code == 200, response.text
    return profile, pattern_id, run


def update_source(client, profile):
    uploaded = client.post(f"/api/v1/businesses/{profile['business_id']}/artifacts?filename=changed.zip&mode=replace&slot=source", content=archive({**SAMPLE, "src/api.py": SAMPLE["src/api.py"] + "\n# changed implementation"})).json()
    assert wait_job(client, uploaded["job_id"])["status"] == "completed"
    latest = client.get(f"/api/v1/businesses/{profile['business_id']}").json()
    response = client.post(f"/api/v1/businesses/{profile['business_id']}/review", json={"expected_version": latest["profile_version"], "decision": "approve", "reason": "Operator reviewed changed source version"})
    assert response.status_code == 200, response.text
    return response.json()


def test_verified_pattern_has_ordered_checks_and_source_scope(client, monkeypatch, verification_url):
    _, pattern_id, _ = trusted(client, monkeypatch, verification_url)
    pattern = next(p for p in client.get("/api/v1/patterns").json() if p["id"] == pattern_id)
    assert [c["tool_id"] for c in pattern["checks"]] == ["service.status", "service.logs"]
    assert pattern["validity"]["contract_schema"] == 2
    assert pattern["validity"]["source_digest"]
    assert pattern["validity"]["services"]
    assert all(c["source_call_id"] for c in pattern["checks"])
    assert not pattern["health"]["needs_review"]


def test_source_change_without_topology_change_blocks_trusted_reuse(client, monkeypatch, verification_url):
    profile, pattern_id, _ = trusted(client, monkeypatch, verification_url)
    profile = update_source(client, profile)
    run = investigate(client, profile, mode="graph_pattern")
    match = next(m for m in run["summary"]["pattern_matches"] if m["pattern"]["id"] == pattern_id)
    assert match["decision"] != "suggested"
    assert "source_digest_changed" in match["compatibility"]["reasons"]
    pattern = next(p for p in client.get("/api/v1/patterns").json() if p["id"] == pattern_id)
    assert pattern["status"] == "trusted" and pattern["health"]["needs_review"]
    assert any(e["event_type"] == "pattern_reuse_blocked" for e in run["events"])


def test_failed_validation_feedback_is_visible_and_blocks_future_reuse(client, monkeypatch, verification_url):
    profile, pattern_id, _ = trusted(client, monkeypatch, verification_url)
    first = investigate(client, profile, mode="graph_pattern")
    match = next(m for m in first["summary"]["pattern_matches"] if m["decision"] == "suggested")
    response = client.post(f"/api/v1/pattern-matches/{match['match_id']}/outcome", json={"validation": "failed", "usefulness": "not_useful"})
    assert response.status_code == 200, response.text
    assert client.post(f"/api/v1/pattern-matches/{match['match_id']}/outcome", json={"validation": "validated"}).status_code == 409
    latest = investigate(client, profile, mode="graph_pattern")
    assert not any(m["decision"] == "suggested" for m in latest["summary"]["pattern_matches"])
    pattern = next(p for p in client.get("/api/v1/patterns").json() if p["id"] == pattern_id)
    assert pattern["health"]["validation_failed"] == 1 and pattern["health"]["needs_review"]
    assert any(e["event_type"] == "pattern_review_needed" for e in client.get(f"/api/v1/runs/{first['id']}").json()["events"])


def test_failed_hypotheses_are_counterexamples_and_never_trusted(client, monkeypatch):
    profile = prepared_profile(client, monkeypatch)
    model_stub(monkeypatch, lambda context, n: report([hypothesis(context, "catalog-api", failure="startup_failure", all_evidence=True)]))
    run = investigate(client, profile)
    assert run["status"] == "unresolved"
    assert run["summary"]["counterexamples"]
    failures = client.get("/api/v1/knowledge-counterexamples")
    assert failures.status_code == 200, failures.text
    row = next(r for r in failures.json() if r["run_id"] == run["id"])
    assert any(h["status"] == "refuted" and h["counter_evidence"] for h in row["counterexamples"])
    assert client.get("/api/v1/patterns").json() == []
    assert any(e["event_type"] == "investigation_counterexamples_retained" for e in run["events"])


def test_check_capture_uses_final_supported_hypothesis_not_first_refuted_one(client, monkeypatch, verification_url):
    profile = prepared_profile(client, monkeypatch)
    model_stub(monkeypatch, lambda context, n: report([
        hypothesis(context, "catalog-api", failure="startup_failure", refute=True, all_evidence=True),
        hypothesis(context, "store", failure="startup_failure", all_evidence=True),
    ]))
    run = investigate(client, profile, mode="graph_only")
    assert run["status"] == "supported"
    pattern_id = capture(client, run, verification_url)
    pattern = next(p for p in client.get("/api/v1/patterns").json() if p["id"] == pattern_id)
    assert pattern["checks"] and all(c["target_role"] == "datastore" for c in pattern["checks"])
    calls = {c["id"]: json.loads(c["args_redacted_json"]) for c in run["tool_calls"]}
    assert all(calls[c["source_call_id"]]["name"] == "store.service" for c in pattern["checks"])


def test_candidate_cannot_be_promoted_after_source_changed(client, monkeypatch, verification_url):
    profile, pattern_id, _ = trusted(client, monkeypatch, verification_url)
    # A separately captured, unreviewed Candidate still requires a compatible current version.
    run = investigate(client, profile, mode="graph_only")
    candidate_id = capture(client, run, verification_url)
    update_source(client, profile)
    response = client.post(f"/api/v1/patterns/{candidate_id}/review", json={"decision": "approve", "reason": "Attempt to approve after source changes"})
    assert response.status_code == 409, response.text
    assert next(p for p in client.get("/api/v1/patterns").json() if p["id"] == candidate_id)["status"] == "candidate"
    assert next(p for p in client.get("/api/v1/patterns").json() if p["id"] == pattern_id)["status"] == "trusted"
