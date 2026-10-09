from collections import Counter

import httpx
from app.common import now
from app.tools.registry import CollectorClient
from conftest import wait_job


def test_legacy_demo_does_not_repeat_failed_direction(client, monkeypatch):
    def get(self, path, timeout=12):
        if path.endswith("/containers"):
            return [{"id": "a" * 64, "name": "python_api", "service": "python_api", "state": "exited"}]
        if "/logs?" in path:
            return {"text": now() + " startup failed: connection refused"}
        raise httpx.ConnectError("isolated failed process summary")

    monkeypatch.setattr(CollectorClient, "get", get)
    system = client.post("/api/v1/systems/onboard", json={"source_key": "system-a", "display_name": "isolated legacy"}).json()
    incident = client.post("/api/v1/incidents", json={"system_id": system["system_id"], "symptom": "startup failure"}).json()
    response = client.post(f"/api/v1/incidents/{incident['id']}/runs").json()
    job = wait_job(client, response["job_id"])
    assert job["status"] == "completed"
    run = client.get(f"/api/v1/runs/{job['result']['id']}").json()
    counts = Counter((c["tool_id"], c["args_redacted_json"]) for c in run["tool_calls"])
    assert all(count == 1 for count in counts.values())
    assert run["status"] == "unresolved"
