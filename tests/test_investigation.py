import json
from types import SimpleNamespace

from app.settings import get_settings
from conftest import wait_job
from pydantic import SecretStr
from test_business import onboard
from test_runtime import discover, target


def approved_scope(client):
    profile = onboard(client)
    t = target(client, profile)
    result = discover(client, profile, t)
    binding = next(m for m in result["matches"] if m["kind"] == "service")
    client.post(
        f"/api/v1/businesses/{profile['business_id']}/bindings/{binding['id']}/review",
        json={"decision": "confirm", "reason": "已核对隔离环境中的 catalog-api 服务"},
    )
    base = f"/api/v1/businesses/{profile['business_id']}"
    latest = client.get(base).json()
    approved = client.post(
        base + "/review",
        json={
            "expected_version": latest["profile_version"],
            "decision": "approve",
            "reason": "已审核资料骨架与映射，允许受限只读调查",
        },
    )
    assert approved.status_code == 200, approved.text
    return approved.json(), t


def run(client, profile):
    response = client.post(
        "/api/v1/incidents",
        json={"system_id": profile["system_id"], "symptom": "catalog-api startup failure"},
    )
    incident = response.json()
    response = client.post(f"/api/v1/incidents/{incident['id']}/runs")
    assert response.status_code == 202, response.text
    job = wait_job(client, response.json()["job_id"])
    assert job["status"] == "completed", job
    response = client.get(f"/api/v1/runs/{job['result']['id']}")
    assert response.status_code == 200, response.text
    return response.json()


def test_complete_onboarding_offline_investigation(client):
    profile, t = approved_scope(client)
    result = run(client, profile)
    assert result["status"] == "unresolved"
    assert result["summary"]["connector_types"] == ["mock"]
    assert {t["tool_id"] for t in result["tool_calls"]} >= {"service.status", "service.logs"}
    assert result["context_budget"]["calls"] == 0
    assert result["summary"]["unresolved_reason"]


def test_unreviewed_profile_does_not_execute_runtime_reads(client):
    profile = onboard(client)
    target(client, profile)
    result = run(client, profile)
    assert result["status"] == "unresolved"
    assert result["tool_calls"] == []


def test_controlled_fault_with_stub_provider_and_real_gate(client, monkeypatch):
    profile, t = approved_scope(client)
    settings = get_settings()
    settings.external_model_enabled = True
    settings.model_name = "unit-test-stub"
    settings.model_api_key = SecretStr("test-provider-key")
    captured = []

    def completion(**kwargs):
        captured.append(kwargs)
        context = json.loads(kwargs["messages"][1]["content"])
        current = context["current_evidence"]
        node = next(
            n
            for n in context["system_graph"]
            if n["kind"] == "service" and n["role"] == "application"
        )
        body = {
            "summary": "隔离 Mock 中应用依赖连接被拒绝，启动退出。",
            "next_action": "由人工核验依赖状态",
            "action": None,
            "hypotheses": [
                {
                    "target_node_id": node["id"],
                    "target_role": node["role"],
                    "failure_mode": "startup_failure",
                    "prediction": "Application exited after connection refusal",
                    "causal_chain": ["dependency connection refused", "startup aborts"],
                    "rationale": "The timestamped startup log explains the independently observed failed service state.",
                    "confidence": 0.9,
                    "missing_evidence": [],
                    "evidence_links": [
                        {
                            "evidence_id": e["evidence_id"],
                            "relation": "supports",
                            "explanation": "Current failed application startup observation.",
                        }
                        for e in current
                    ],
                }
            ],
        }
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(body)))],
            usage=SimpleNamespace(prompt_tokens=400, completion_tokens=200),
        )

    monkeypatch.setattr("app.context.model._completion", completion)
    result = run(client, profile)
    assert result["status"] == "supported"
    assert result["context_budget"]["input_tokens"] == 400
    assert result["context_budget"]["usage_records"][0]["usage_kind"] == "actual"
    assert len(captured) == 1
    assert captured[0]["num_retries"] == 0
    assert "def record" not in captured[0]["messages"][1]["content"]
    assert client.get("/api/v1/patterns").json() == []
