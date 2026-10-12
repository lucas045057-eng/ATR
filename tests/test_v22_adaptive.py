import json
from types import SimpleNamespace

import pytest
from app.common import now
from app.connectors.mock import MockConnector
from app.settings import get_settings
from conftest import wait_job
from pydantic import SecretStr
from test_business import SAMPLE, onboard
from test_runtime import discover, target


def prepared_profile(client, monkeypatch, *, store=True, files=SAMPLE):
    original = MockConnector.read

    def read(self, tool, args):
        if tool == "service.list":
            return {
                "connector": "mock", "observed_at": now(),
                "services": [
                    {"id": "catalog-api.service", "name": "catalog-api", "kind": "service", "ports": [8000]},
                    {"id": "store.service", "name": "store", "kind": "service"},
                ],
            }
        if tool == "service.status":
            return {"connector": "mock", "state": "failed" if args["name"] == "store.service" else "active"}
        if tool == "service.logs":
            return {"connector": "mock", "text": now() + " startup failed: store connection refused"}
        if tool == "config.list":
            return {"connector": "mock", "files": [args["path"] + "/app.conf", args["path"] + "/cache.conf"]}
        return original(self, tool, args)

    monkeypatch.setattr(MockConnector, "read", read)
    profile = onboard(client, files)
    runtime = target(client, profile)
    result = discover(client, profile, runtime)
    for binding in result["matches"]:
        if binding["kind"] == "service" and (store or binding["business_service"] == "catalog-api"):
            reviewed = client.post(
                f"/api/v1/businesses/{profile['business_id']}/bindings/{binding['id']}/review",
                json={"decision": "confirm", "reason": "核验隔离夹具的受限业务服务"},
            )
            assert reviewed.status_code == 200, reviewed.text
    base = f"/api/v1/businesses/{profile['business_id']}"
    latest = client.get(base).json()
    response = client.post(
        base + "/review",
        json={"expected_version": latest["profile_version"], "decision": "approve", "reason": "审核隔离业务范围与资料"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def model_stub(monkeypatch, reply):
    contexts = []

    def completion(**kwargs):
        context = json.loads(kwargs["messages"][1]["content"])
        contexts.append(context)
        body = reply(context, len(contexts))
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(body)))],
            usage=SimpleNamespace(prompt_tokens=64, completion_tokens=32),
        )

    settings = get_settings()
    settings.external_model_enabled = True
    settings.model_name = "adaptive-test-stub"
    settings.model_api_key = SecretStr("test-adaptive-key")
    monkeypatch.setattr("app.context.model._completion", completion)
    return contexts


def investigate(client, profile, *, mode="normal", symptom="catalog-api unavailable"):
    # These tests assert the legacy V2.2 adaptive planner contract; newer task-stop
    # tests mutate the cached settings singleton to minimal_v2 within their cases.
    get_settings().agent_decision_protocol = "legacy_v22"
    incident = client.post(
        "/api/v1/incidents", json={"system_id": profile["system_id"], "symptom": symptom, "mode": mode},
    )
    assert incident.status_code == 200, incident.text
    response = client.post(f"/api/v1/incidents/{incident.json()['id']}/runs")
    assert response.status_code == 202, response.text
    job = wait_job(client, response.json()["job_id"])
    assert job["status"] == "completed", job
    return client.get(f"/api/v1/runs/{job['result']['id']}").json()


def candidate(context, tool, service=None, path=None):
    return next(
        a for a in context["available_actions"]
        if a["tool_id"] == tool and (service is None or a["service"] == service)
        and (path is None or a.get("arguments", {}).get("path") == path)
    )


def proposal(action):
    return {"action_id": action["action_id"], "rationale": "区分当前假设的限定只读检查"}


def report(hypotheses=(), action=None, **extra):
    return {"summary": "隔离调查尚待因果核验", "next_action": "继续核验独立运行证据", "hypotheses": list(hypotheses), "action": action, **extra}


def hypothesis(context, service, *, failure="service_unavailable", refute=False, all_evidence=False):
    node = next(n for n in context["system_graph"] if n["name"] == service and n["role"] != "runtime")
    evidence = [e for e in context["current_evidence"] if e["target_node_id"] == node["id"]]
    if not all_evidence:
        evidence = evidence[:1]
    return {
        "target_node_id": node["id"], "target_role": node["role"], "failure_mode": failure,
        "prediction": "The affected service is failed during this incident",
        "causal_chain": ["required service failed", "request cannot complete"],
        "rationale": "Current independent runtime status and logs must establish this candidate mechanism.",
        "missing_evidence": ["独立服务日志"] if not all_evidence else [],
        "evidence_links": [{"evidence_id": e["evidence_id"], "relation": "refutes" if refute else "supports", "explanation": "Current service state refutes the claimed outage." if refute else "Current runtime observation is relevant to service availability."} for e in evidence],
        "confidence": 0.99, "status": "supported",
        "verification_predictions": [{"target_node_id": node["id"], "tool_id": "service.status", "field": "state", "operator": "eq", "value": "failed"}],
    }


@pytest.mark.parametrize("input_limit", [12000, 30000])
def test_adaptive_mode_pivots_to_dependency_and_keeps_refuted_history(client, monkeypatch, input_limit):
    profile = prepared_profile(client, monkeypatch)
    get_settings().investigation_input_tokens = input_limit

    def reply(context, round_number):
        if round_number == 1:
            return report([hypothesis(context, "catalog-api")], proposal(candidate(context, "service.status", "store")))
        if round_number == 2:
            return report([hypothesis(context, "catalog-api", refute=True), hypothesis(context, "store")], proposal(candidate(context, "service.logs", "store")))
        return report([hypothesis(context, "store", failure="startup_failure", all_evidence=True)])

    contexts = model_stub(monkeypatch, reply)
    result = investigate(client, profile, mode="graph_adaptive")
    if input_limit == 12000:
        assert result["status"] == "unresolved"
        assert result["summary"]["model_error"]["budget_reason"] == "request_input_limit"
        assert len(contexts) == result["context_budget"]["calls"] == 2
        assert len(result["tool_calls"]) == 4
        assert any(h["status"] == "refuted" and h["counter_evidence"] for h in result["summary"]["hypothesis_history"])
        return
    assert result["status"] == "supported"
    calls = result["tool_calls"]
    assert [(c["tool_id"], json.loads(c["args_redacted_json"])["name"]) for c in calls] == [
        ("service.status", "catalog-api.service"), ("service.logs", "catalog-api.service"),
        ("service.status", "store.service"), ("service.logs", "store.service"),
    ]
    assert result["summary"]["stop_reason"] == "evidence_supported"
    assert len(contexts) == 3
    assert contexts[1]["previous_hypotheses"]
    assert any(h["status"] == "refuted" for h in result["summary"]["hypothesis_history"])
    assert any(h["status"] == "refuted" for h in contexts[2]["hypothesis_history"])
    assert all(len([h for h in i["hypotheses"] if h["status"] != "refuted"]) <= 3 for i in result["summary"]["iterations"])
    assert result["context_budget"]["calls"] == 3


def test_unapproved_dependency_generates_scope_request_without_access(client, monkeypatch):
    profile = prepared_profile(client, monkeypatch, store=False)

    def reply(context, _):
        assert not any(a.get("service") == "store" and a["tool_id"].startswith("service.") for a in context["available_actions"])
        return report([hypothesis(context, "store")], scope_requests=[{"service": "store", "reason": "需要核验数据库是否不可用"}])

    model_stub(monkeypatch, reply)
    result = investigate(client, profile)
    assert result["status"] == "unresolved"
    assert result["summary"]["stop_reason"] == "permission_blocked"
    assert any(r["service"] == "store" for r in result["summary"]["scope_requests"])
    assert all(json.loads(c["args_redacted_json"]).get("name") != "store.service" for c in result["tool_calls"])


def test_config_files_have_distinct_actions_and_each_is_read_once(client, monkeypatch):
    profile = prepared_profile(client, monkeypatch)

    def reply(context, n):
        if n == 1:
            return report(action=proposal(candidate(context, "config.list")))
        if n in {2, 3}:
            path = "/srv/catalog/" + ("app.conf" if n == 2 else "cache.conf")
            return report(action=proposal(candidate(context, "config.read", path=path)))
        return report()

    contexts = model_stub(monkeypatch, reply)
    result = investigate(client, profile)
    reads = [c for c in result["tool_calls"] if c["tool_id"] == "config.read"]
    assert [json.loads(c["args_redacted_json"])["path"] for c in reads] == ["/srv/catalog/app.conf", "/srv/catalog/cache.conf"]
    assert len({i["executed_action"]["action_id"] for i in result["summary"]["iterations"] if i["executed_action"]}) == 3
    assert all(a.get("scheduling", {}).get("permission_allowed") for a in result["summary"]["iterations"][0]["candidate_actions"])
    assert all("permission_allowed" not in a.get("scheduling", {}) for a in contexts[0]["available_actions"])
    assert result["summary"]["stop_reason"] == "no_new_actions"
    assert result["status"] == "unresolved"


def test_multiple_source_contexts_remain_distinct_and_static_cannot_support(client, monkeypatch):
    files = {**SAMPLE, "src/other.py": "def record(): return 'isolated reference'"}
    profile = prepared_profile(client, monkeypatch, files=files)

    def reply(context, n):
        if n == 1:
            action = next(a for a in context["available_actions"] if a["tool_id"] == "code.search" and a["arguments"]["keyword"] == "record")
            return report(action=proposal(action))
        if n in {2, 3}:
            return report(action=proposal(candidate(context, "code.context")))
        return report()

    contexts = model_stub(monkeypatch, reply)
    result = investigate(client, profile, symptom="record unavailable")
    reads = [c for c in result["tool_calls"] if c["tool_id"] == "code.context"]
    assert len(reads) == 2
    assert len({json.loads(c["args_redacted_json"])["file_id"] for c in reads}) == 2
    assert len(result["summary"]["code_sources"]) == 2
    assert result["status"] == "unresolved"
    context_actions = [a for a in contexts[1]["available_actions"] if a["tool_id"] == "code.context"]
    assert len({a["action_id"] for a in context_actions}) == 2
    assert all(u["status"] == "succeeded" for u in result["context_budget"]["usage_records"])


@pytest.mark.parametrize("limit,stop", [(1, "tool_budget_exhausted"), (2, "tool_budget_exhausted")])
def test_tool_budget_limits_seed_and_never_starts_model(client, monkeypatch, limit, stop):
    profile = prepared_profile(client, monkeypatch)
    contexts = model_stub(monkeypatch, lambda context, n: report())
    get_settings().investigation_tool_calls = limit
    result = investigate(client, profile)
    assert result["status"] == "unresolved"
    assert result["summary"]["stop_reason"] == stop
    assert len(result["tool_calls"]) == limit
    assert result["context_budget"]["tool_calls"] == limit
    assert contexts == []


def test_round_limit_does_not_execute_unreviewed_followup(client, monkeypatch):
    profile = prepared_profile(client, monkeypatch)
    model_stub(monkeypatch, lambda context, n: report(action=proposal(candidate(context, "config.list"))))
    get_settings().investigation_model_rounds = 1
    result = investigate(client, profile)
    assert result["summary"]["stop_reason"] == "round_limit"
    assert len(result["tool_calls"]) == 2
    assert result["context_budget"]["calls"] == 1


def test_unknown_action_id_and_injected_arguments_never_execute(client, monkeypatch):
    profile = prepared_profile(client, monkeypatch)
    model_stub(monkeypatch, lambda context, n: report(action={"action_id": "not-a-candidate", "tool_id": "shell.exec", "arguments": {"command": "touch /srv/catalog/owned"}}))
    result = investigate(client, profile)
    assert result["summary"]["stop_reason"] == "model_schema_error"
    assert result["context_budget"]["usage_records"][0]["status"] == "failed"
    assert len(result["tool_calls"]) == 2
    assert not any(c["tool_id"] == "shell.exec" for c in result["tool_calls"])


def test_model_budget_rejection_has_machine_stop_reason(client, monkeypatch):
    profile = prepared_profile(client, monkeypatch)
    contexts = model_stub(monkeypatch, lambda context, n: report())
    get_settings().investigation_input_tokens = 1
    result = investigate(client, profile)
    assert result["summary"]["stop_reason"] == "model_budget_exhausted"
    assert result["context_budget"]["calls"] == 0
    assert contexts == []
