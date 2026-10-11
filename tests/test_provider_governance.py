"""Provider governance through real isolated LiteLLM and local fake HTTP only."""
import json
import subprocess
from datetime import datetime
from types import SimpleNamespace

import pytest
from app.context.budget import BudgetLimits, ContextBudgetManager
from app.context.model import call_json
from app.context.model_errors import ModelCallError
from app.context.model_worker import provider_request_id
from conftest import wait_job
from test_model_isolation import configure_client, fake_model  # noqa: F401
from test_v22_adaptive import (
    candidate,
    hypothesis,
    investigate,
    model_stub,
    prepared_profile,
    proposal,
    report,
)


@pytest.mark.parametrize("status,code", [(400, "provider_bad_request"), (422, "provider_bad_request"), (401, "provider_auth_error"), (403, "provider_auth_error"), (429, "provider_rate_limit"), (500, "provider_unavailable"), (502, "provider_unavailable"), (503, "provider_unavailable"), (504, "provider_unavailable"), (529, "provider_unavailable")])
def test_http_failure_is_audited_without_retry_or_secrets(fake_model, status, code, caplog):  # noqa: F811
    database, settings, state, _ = fake_model
    state["status"] = status
    manager = ContextBudgetManager(database, settings)
    bid = manager.create("investigation")
    with pytest.raises(Exception) as caught:
        call_json(database, settings, bid, "local mock", {})
    assert caught.value.code == code
    record = manager.get(bid)["usage_records"][0]
    audit = record["diagnostic"]
    assert audit["request_attempt_id"] == record["id"]
    assert audit["error_category"] == code and audit["http_status"] == status
    assert audit["exception_type"] != "ModelCallError"
    assert audit["model"] == "gpt-6.1-sol" and audit["provider"] == "openai-compatible"
    assert audit["retry_count"] == 0 and audit["request_stage"] == "request"
    assert datetime.fromisoformat(audit["ended_at"]) >= datetime.fromisoformat(audit["started_at"])
    assert audit["elapsed_ms"] >= audit["network_ms"] >= 0
    assert audit["worker_initialize_ms"] >= 0 and audit["parse_ms"] >= 0
    assert record["status"] == audit["usage_status"] == "failed"
    assert record["usage_kind"] == audit["usage_kind"] == "estimate"
    assert record["cost"] is None and manager.get(bid)["reserved_tokens"] == 0
    assert state["requests"] == 1
    serialized = json.dumps(audit) + caplog.text
    assert "synthetic-secret" not in serialized and "NEVER-ECHO-REQUEST" not in serialized


def start_investigation(client, profile):
    incident = client.post("/api/v1/incidents", json={"system_id": profile["system_id"], "symptom": "catalog-api unavailable", "mode": "graph_adaptive"})
    response = client.post(f"/api/v1/incidents/{incident.json()['id']}/runs")
    assert response.status_code == 202
    return response.json()


@pytest.mark.parametrize("message", [
    "Model gpt-6.1-sol has not been priced by the administrator yet. Bearer synthetic-secret NEVER-ECHO-REQUEST",
    "模型 gpt-6.1-sol 的价格尚未由管理员配置，请联系管理员。synthetic-secret NEVER-ECHO-REQUEST",
])
def test_provider_model_pricing_failure_is_safe_and_actionable(fake_model, message, caplog):  # noqa: F811
    database, settings, state, _ = fake_model
    state.update(status=400, error={"type": "invalid_request_error", "message": message})
    manager = ContextBudgetManager(database, settings)
    bid = manager.create("investigation")
    with pytest.raises(ModelCallError) as caught:
        call_json(database, settings, bid, "local mock", {})
    assert caught.value.code == "provider_model_configuration_error"
    assert "供应商" in caught.value.diagnostic["message"] and "价格未配置" in caught.value.diagnostic["message"]
    audit = manager.get(bid)["usage_records"][0]["diagnostic"]
    assert audit["provider_error_reason"] == "model_price_not_configured"
    assert audit["error_category"] == "provider_model_configuration_error"
    assert audit["http_status"] == 400 and audit["request_stage"] == "request"
    assert state["requests"] == 1 and manager.get(bid)["reserved_tokens"] == 0
    serialized = json.dumps(audit) + str(caught.value) + caplog.text
    assert message not in serialized and "synthetic-secret" not in serialized and "NEVER-ECHO-REQUEST" not in serialized


@pytest.mark.parametrize("status,kind,message,want", [
    (403, "invalid_request_error", "has not been priced by the administrator yet", "provider_auth_error"),
    (400, "unknown_error", "has not been priced by the administrator yet", "provider_bad_request"),
    (400, "invalid_request_error", "unsupported output format", "provider_bad_request"),
])
def test_unverified_provider_message_does_not_override_http_category(fake_model, status, kind, message, want):  # noqa: F811
    database, settings, state, _ = fake_model
    state.update(status=status, error={"type": kind, "message": message})
    manager = ContextBudgetManager(database, settings)
    bid = manager.create("investigation")
    with pytest.raises(ModelCallError) as caught:
        call_json(database, settings, bid, "local mock", {})
    assert caught.value.code == want
    assert "provider_error_reason" not in manager.get(bid)["usage_records"][0]["diagnostic"]


def test_529_reaches_run_and_database_without_offline_hypotheses(client, fake_model, monkeypatch):  # noqa: F811
    _, settings, state, _ = fake_model
    profile = prepared_profile(client, monkeypatch, store=False)
    configure_client(settings)
    state["status"] = 529
    task = start_investigation(client, profile)
    job = wait_job(client, task["job_id"], timeout=20)
    assert job["status"] == "completed"  # Safe collection/closure finished, model failed separately.
    result = client.get('/api/v1/runs/' + job['result']['id']).json()
    assert result["status"] == "unresolved" and result["final_hypothesis_id"] is None
    assert result["summary"]["stage"] == "stopped"
    error = result["summary"]["model_error"]
    assert error["code"] == "provider_unavailable" and error["status_code"] == 529
    assert result["summary"]["stop_reason"] == "provider_unavailable"
    assert result["summary"]["model_status"] == "failed"
    assert len(result["evidence"]) == len(result["tool_calls"]) == 2
    assert result["hypotheses"] == [] and result["summary"]["hypothesis_history"] == []
    assert all(json.loads(c['args_redacted_json']).get('name') != 'store.service' for c in result['tool_calls'])
    audit = result["context_budget"]["usage_records"][0]["diagnostic"]
    assert audit["investigation_run_id"] == result["id"]
    assert error["request_attempt_id"] == audit["request_attempt_id"]
    assert any(e["event_type"] == "model_attempt_failed" for e in result["events"])
    assert state["requests"] == 1
    assert client.get('/api/v1/health').status_code == 200


@pytest.mark.parametrize("usage", [(-1, 4), ("11", 4), (11.5, 4), (True, 4), (11, None)])
def test_invalid_usage_never_claims_actual(fake_model, monkeypatch, usage):  # noqa: F811
    database, settings, _, _ = fake_model
    monkeypatch.setattr('app.context.model._completion', lambda **_: SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{}'))], usage=SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1])))
    manager = ContextBudgetManager(database, settings)
    bid = manager.create('initial')
    assert call_json(database, settings, bid, 'local fake', {}) == {}
    record = manager.get(bid)['usage_records'][0]
    assert record['usage_kind'] == 'estimate'
    assert record['diagnostic']['usage_kind'] == 'estimate'


def test_process_start_failure_releases_budget_and_has_initialize_stage(fake_model, monkeypatch):  # noqa: F811
    database, settings, _, _ = fake_model
    def fail(*_, **__):
        raise OSError('synthetic-secret cannot start process')
    monkeypatch.setattr(subprocess, 'run', fail)
    manager = ContextBudgetManager(database, settings)
    bid = manager.create('initial')
    with pytest.raises(Exception) as caught:
        call_json(database, settings, bid, 'local fake', {})
    assert caught.value.code == 'model_import_error'
    assert caught.value.diagnostic['stage'] == 'initialize'
    assert manager.get(bid)['reserved_tokens'] == 0


def test_overreported_usage_blocks_until_explicit_budget_review(fake_model, monkeypatch):  # noqa: F811
    database, settings, _, _ = fake_model
    def completion(**_):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{}'))], usage=SimpleNamespace(prompt_tokens=20000, completion_tokens=10))
    monkeypatch.setattr('app.context.model._completion', completion)
    manager = ContextBudgetManager(database, settings)
    bid = manager.create('initial')
    call_json(database, settings, bid, 'local fake', {})
    budget = manager.get(bid)
    assert budget['stop_reason'] == 'provider_usage_exceeded_reservation'
    with pytest.raises(Exception):
        call_json(database, settings, bid, 'local fake', {})
    assert manager.get(bid)['calls'] == 1
    manager.update(bid, BudgetLimits(**budget['limits']))
    assert manager.get(bid)['stop_reason'] is None


@pytest.mark.parametrize("variant", ["missing_summary", "missing_action", "wrong_hypotheses", "missing_causal_chain", "missing_prediction", "missing_missing_evidence", "missing_rationale", "missing_confidence", "unknown_target", "wrong_role", "unknown_evidence", "bad_relation", "extra_arguments"])
def test_malformed_investigation_output_fails_usage_before_followup(client, monkeypatch, variant):
    profile = prepared_profile(client, monkeypatch, store=False)
    def reply(context, _):
        data = report([hypothesis(context, "catalog-api")], proposal(candidate(context, "config.list")))
        if variant.startswith("missing_"):
            key = variant.removeprefix("missing_")
            del (data if key in {"summary", "action"} else data["hypotheses"][0])[key]
        elif variant == "wrong_hypotheses":
            data["hypotheses"] = {}
        elif variant == "unknown_target":
            data["hypotheses"][0]["target_node_id"] = "invented-id"
        elif variant == "wrong_role":
            data["hypotheses"][0]["target_role"] = "database"
        elif variant == "unknown_evidence":
            data["hypotheses"][0]["evidence_links"][0]["evidence_id"] = "invented-evidence"
        elif variant == "bad_relation":
            data["hypotheses"][0]["evidence_links"][0]["relation"] = "proves"
        else:
            data["action"]["arguments"] = {"command": "untrusted command"}
        return data
    contexts = model_stub(monkeypatch, reply)
    result = investigate(client, profile)
    assert len(contexts) == 1 and len(result["tool_calls"]) == 2
    assert result["summary"]["model_error"]["code"] == "model_schema_error"
    assert result["status"] == "unresolved" and result["hypotheses"] == []
    record = result["context_budget"]["usage_records"][0]
    assert record["usage_kind"] == "actual" and record["status"] == "failed"
    assert record["diagnostic"]["request_stage"] == "validate"
    assert result["context_budget"]["reserved_tokens"] == 0


def test_failure_after_a_round_preserves_refutation_and_prior_tools(client, fake_model, monkeypatch):  # noqa: F811
    _, settings, state, _ = fake_model
    profile = prepared_profile(client, monkeypatch, store=False)
    configure_client(settings)
    def reply(context, number):
        if number == 1:
            return report([hypothesis(context, "catalog-api", refute=True)], proposal(candidate(context, "config.list")))
        state["status"] = 529
        return report()
    state["reply"] = reply
    task = start_investigation(client, profile)
    job = wait_job(client, task["job_id"], timeout=25)
    assert job["status"] == "completed"
    result = client.get('/api/v1/runs/' + job['result']['id']).json()
    assert result["summary"]["stop_reason"] == "provider_unavailable"
    assert result["summary"]["hypotheses"][0]["status"] == "refuted"
    assert result["summary"]["hypothesis_history"][0]["counter_evidence"]
    assert result["summary"]["analysis_status"] == "model_failed"
    assert len(result["tool_calls"]) == len(result["evidence"]) == 3
    assert [r["status"] for r in result["context_budget"]["usage_records"]] == ["succeeded", "failed"]
    assert state["requests"] == 2 and result["status"] == "unresolved"
    # Explicit new task can recover; failure neither poisons backend nor causes a retry.
    state["status"] = 200
    state["reply"] = lambda context, _: report()
    task = start_investigation(client, profile)
    job = wait_job(client, task["job_id"], timeout=15)
    recovered = client.get('/api/v1/runs/' + job['result']['id']).json()
    assert recovered["summary"]["model_error"] is None
    assert recovered["summary"]["model_status"] == "completed"
    assert state["requests"] == 3


@pytest.mark.parametrize("exception", ["socket.gaierror", "ssl.SSLError"])
def test_worker_dns_and_tls_failures_are_network_not_http(fake_model, tmp_path, monkeypatch, exception):  # noqa: F811
    database, settings, state, _ = fake_model
    (tmp_path / 'litellm.py').write_text('import socket, ssl\ndef completion(**kwargs):\n    raise ' + exception + '("synthetic-secret")\n', encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    manager = ContextBudgetManager(database, settings)
    bid = manager.create('initial')
    with pytest.raises(ModelCallError) as caught:
        call_json(database, settings, bid, 'mock only', {})
    assert caught.value.code == 'provider_connection_error'
    assert caught.value.diagnostic['status_code'] is None
    assert caught.value.diagnostic['stage'] == 'request'
    assert state['requests'] == 0 and manager.get(bid)['reserved_tokens'] == 0


def test_provider_id_is_only_a_real_safe_header_not_completion_id():
    assert provider_request_id(SimpleNamespace(headers={'x-request-id': 'req-safe1234'}), 'synthetic-secret') == 'req-safe1234'
    assert provider_request_id(SimpleNamespace(id='completion-id', _hidden_params=None), 'secret') is None
    for value in ('Bearer synthetic-secret', 'body SECRET', 'synthetic-secret', 'req-safe1234'):
        assert provider_request_id(SimpleNamespace(headers={'x-request-id': value}), value) is None
    assert provider_request_id(SimpleNamespace(headers={'x-request-id': 'req-sk-synthetic-key'}), 'sk-synthetic-key') is None


def test_old_usage_rows_have_unknown_diagnostics_and_idempotent_settlement(fake_model):  # noqa: F811
    database, settings, _, _ = fake_model
    manager = ContextBudgetManager(database, settings)
    bid = manager.create('initial')
    reservation = manager.reserve(bid, [{"content": "mock"}], 20)
    manager.settle(bid, reservation, None, status='failed')
    before = manager.get(bid)
    with pytest.raises(ValueError, match="重复"):
        manager.settle(bid, reservation, SimpleNamespace(prompt_tokens=500, completion_tokens=500))
    after = manager.get(bid)
    assert after == before and after['reserved_tokens'] == 0
    assert after['usage_records'][0]['diagnostic'] == {}


@pytest.mark.parametrize('usage', [{'prompt_tokens': '11', 'completion_tokens': 4}, {'prompt_tokens': True, 'completion_tokens': 4}, {'prompt_tokens': 11.5, 'completion_tokens': 4}, {'prompt_tokens': -1, 'completion_tokens': 4}, {'prompt_tokens': 11}, {}, None, 'omit'])
def test_raw_http_usage_is_validated_before_litellm_coercion(fake_model, usage):  # noqa: F811
    database, settings, state, _ = fake_model
    state['usage'] = usage
    manager = ContextBudgetManager(database, settings)
    bid = manager.create('initial')
    try:
        assert call_json(database, settings, bid, 'mock only', {})['summary']
    except ModelCallError as exc:
        # SDK rejects fractional usage; preserve the actual HTTP 200 and schema stage.
        assert exc.code == 'model_schema_error' and exc.diagnostic['status_code'] == 200
    record = manager.get(bid)['usage_records'][0]
    assert record['usage_kind'] == 'estimate'
    assert record['input_tokens'] > 0 and record['cost'] is None
    assert state['requests'] == 1


def test_real_safe_provider_header_is_retained_in_audit(fake_model):  # noqa: F811
    database, settings, state, _ = fake_model
    manager = ContextBudgetManager(database, settings)
    bid = manager.create('initial')
    call_json(database, settings, bid, 'mock only', {})
    record = manager.get(bid)['usage_records'][0]
    assert record['usage_kind'] == 'actual'
    assert record['diagnostic']['provider_request_id'] == 'req-local-safe1234'
    assert state['requests'] == 1


def test_http_200_headers_then_body_timeout_remains_a_timeout(fake_model):  # noqa: F811
    database, settings, state, _ = fake_model
    state['body_delay'] = 4
    manager = ContextBudgetManager(database, settings)
    bid = manager.create('initial')
    with pytest.raises(ModelCallError) as caught:
        call_json(database, settings, bid, 'mock only', {})
    assert caught.value.code == 'model_timeout'
    assert caught.value.diagnostic['stage'] == 'request'
    assert state['requests'] == 1 and manager.get(bid)['reserved_tokens'] == 0


def test_bad_sdk_structure_preserves_valid_wire_usage_on_failure(fake_model):  # noqa: F811
    database, settings, state, _ = fake_model
    state['choices'] = 'not-a-list'
    manager = ContextBudgetManager(database, settings)
    bid = manager.create('initial')
    with pytest.raises(ModelCallError) as caught:
        call_json(database, settings, bid, 'mock only', {})
    assert caught.value.code == 'model_schema_error' and caught.value.diagnostic['status_code'] == 200
    record = manager.get(bid)['usage_records'][0]
    assert record['usage_kind'] == 'actual' and record['status'] == 'failed'
    assert (record['input_tokens'], record['output_tokens']) == (11, 5)
    assert state['requests'] == 1 and manager.get(bid)['reserved_tokens'] == 0


def test_worker_connection_error_is_network_not_synthetic_http_500(fake_model, tmp_path, monkeypatch):  # noqa: F811
    database, settings, state, _ = fake_model
    (tmp_path / 'litellm.py').write_text('class APIConnectionError(Exception):\n    status_code = 500\ndef completion(**kwargs):\n    raise APIConnectionError("synthetic-secret")\n', encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    manager = ContextBudgetManager(database, settings)
    bid = manager.create('initial')
    with pytest.raises(ModelCallError) as caught:
        call_json(database, settings, bid, 'local mock only', {})
    assert caught.value.code == 'provider_connection_error'
    assert caught.value.diagnostic['status_code'] is None
    assert state['requests'] == 0 and manager.get(bid)['reserved_tokens'] == 0
