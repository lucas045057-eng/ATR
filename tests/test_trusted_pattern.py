import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

from app.settings import get_settings
from conftest import wait_job
from pydantic import SecretStr
from test_investigation import approved_scope


def test_trusted_requires_recorded_repair_actual_http_verification_and_review(client, monkeypatch):
    profile, _ = approved_scope(client)
    settings = get_settings()
    settings.external_model_enabled = True
    settings.model_name = "unit-test-stub"
    settings.model_api_key = SecretStr("unit-test-private-key")

    def completion(**kwargs):
        c = json.loads(kwargs["messages"][1]["content"])
        node = next(
            n for n in c["system_graph"] if n["kind"] == "service" and n["role"] == "application"
        )
        h = {
            "target_node_id": node["id"],
            "target_role": node["role"],
            "failure_mode": "startup_failure",
            "prediction": "Startup fails with connection refusal",
            "causal_chain": ["dependency refused", "startup exits"],
            "rationale": "The current timestamped connection refusal explains the separately observed failed state.",
            "confidence": 0.9,
            "evidence_links": [
                {
                    "evidence_id": e["evidence_id"],
                    "relation": "supports",
                    "explanation": "Current failed startup observation on the exact target.",
                }
                for e in c["current_evidence"]
            ],
        }
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {"summary": "Mock fixture diagnosis", "hypotheses": [h], "action": None}
                        )
                    )
                )
            ],
            usage=SimpleNamespace(prompt_tokens=400, completion_tokens=150),
        )

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    incident = client.post(
        "/api/v1/incidents",
        json={"system_id": profile["system_id"], "symptom": "catalog-api startup failure"},
    ).json()
    job = client.post(f"/api/v1/incidents/{incident['id']}/runs").json()
    result = wait_job(client, job["job_id"])
    assert result["result"]["status"] == "supported"
    assert client.get("/api/v1/patterns").json() == []
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ready"}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    url = f"http://127.0.0.1:{server.server_port}/health"
    settings.allowed_health_urls = url
    try:
        request = {
            "summary": "Operator repaired isolated fixture",
            "operator_verified": False,
            "verification_summary": "Observed actual loopback HTTP 200 after test repair",
            "verification_url": url,
        }
        assert (
            client.post(f"/api/v1/incidents/{incident['id']}/repair", json=request).status_code
            == 422
        )
        assert calls == []
        request["operator_verified"] = True
        created = client.post(f"/api/v1/incidents/{incident['id']}/repair", json=request)
        assert created.status_code == 200, created.text
        assert calls == ["/health"]
        pid = created.json()["pattern_version_id"]
        assert client.get("/api/v1/patterns").json()[0]["status"] == "candidate"
        reviewed = client.post(
            f"/api/v1/patterns/{pid}/review",
            json={"decision": "approve", "reason": "已核对人工修复记录、实际验证调用和精确版本"},
        )
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["status"] == "trusted"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)
