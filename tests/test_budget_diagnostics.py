import json
from copy import deepcopy
from types import SimpleNamespace

from app.agent.workflow import _planner
from app.context.budget import ContextBudgetManager
from app.context.model import model_messages
from app.settings import get_settings
from app.storage.database import Database
from pydantic import SecretStr
from test_model_output_compatibility import budget_regression_state
from test_v22_adaptive import candidate, investigate, model_stub, prepared_profile, proposal, report


def test_provider_usage_above_estimate_does_not_stop_investigation(client, monkeypatch):
    profile = prepared_profile(client, monkeypatch)
    contexts = model_stub(monkeypatch, lambda context, _: report(
        action=proposal(candidate(context, "service.status", "store"))))
    from app.context import model
    original = model._completion

    def reported_usage(**request):
        response = original(**request)
        response.usage = SimpleNamespace(prompt_tokens=27809, completion_tokens=2620)
        return response

    monkeypatch.setattr(model, "_completion", reported_usage)
    settings = get_settings()
    settings.investigation_input_tokens, settings.budget_total_tokens = 30000, 200000
    result = investigate(client, profile)
    assert result["status"] == "unresolved"
    assert result["context_budget"]["stop_reason"] is None
    assert result["summary"]["stop_reason"] != "provider_usage_exceeded_reservation"
    assert "工具调用预算已耗尽" not in result["summary"]["unresolved_reason"]
    assert len(contexts) == result["context_budget"]["calls"] > 1
    assert result["context_budget"]["input_tokens"] == 27809 * len(contexts)
    assert all(u["status"] == "succeeded" for u in result["context_budget"]["usage_records"])


def test_setting_increase_changes_only_new_budget_snapshots(client):
    settings = get_settings()
    settings.investigation_input_tokens, settings.budget_total_tokens = 20000, 100000
    manager = ContextBudgetManager(Database(settings.database_path), settings)
    old_id = manager.create("investigation")
    old = manager.get(old_id)
    settings.investigation_input_tokens, settings.budget_total_tokens = 30000, 200000
    new = manager.get(manager.create("investigation"))
    assert manager.get(old_id) == old
    assert old["limits"]["max_input_tokens"] == 20000
    assert old["limits"]["max_total_tokens"] == 100000
    assert new["limits"] == {**old["limits"], "max_input_tokens": 30000, "max_total_tokens": 200000}


def test_large_input_records_estimate_without_echoing_context(client):
    settings = get_settings()
    database = Database(settings.database_path)
    manager = ContextBudgetManager(database, settings)
    budget_id = manager.create("investigation")
    messages = model_messages("NEVER-ECHO-PROMPT", {"secret": "NEVER-ECHO-CONTEXT", "text": "x" * 13000})
    reservation = manager.reserve(budget_id, messages, settings.model_max_output_tokens)
    assert reservation[1] > manager.get(budget_id)['limits']['max_input_tokens']
    budget = manager.get(budget_id)
    assert budget["calls"] == 1 and budget["reserved_tokens"] == reservation[2]
    assert "NEVER-ECHO" not in json.dumps(budget['usage_records'])


def test_compaction_removes_duplicate_history_even_with_room(client, monkeypatch):
    settings = get_settings()
    settings.external_model_enabled = True
    settings.model_name = "isolated-model"
    settings.model_api_key = SecretStr("synthetic-not-a-provider-key")
    database = Database(settings.database_path)
    manager = ContextBudgetManager(database, settings)
    budget_id = manager.create("investigation")
    state = budget_regression_state()
    state["evidence"] = state["evidence"][:3]
    state["action_history"] = [{"action_id": "a" * 64, "tool_id": "service.status",
                                "target_node_id": state["graph"]["nodes"][0]["id"],
                                "status": "succeeded", "new_information": True}]
    before = deepcopy(state)
    requests = []

    def completion(**request):
        requests.append(request)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(
            {"summary": "synthetic", "hypotheses": [], "next_action": None, "action": None})))],
            usage=SimpleNamespace(prompt_tokens=64, completion_tokens=32))

    monkeypatch.setattr("app.context.model._completion", completion)
    result = _planner(settings, state, database, budget_id)
    assert "model_error" not in result
    context = json.loads(requests[0]["messages"][1]["content"])
    assert context["hypothesis_history"] == []
    assert context["previous_hypotheses"][1]["counter_evidence"] == before["previous_hypotheses"][1]["counter_evidence"]
    assert [e["evidence_id"] for e in context["current_evidence"]] == [e["evidence_id"] for e in before["evidence"]]
    assert "action_id" not in context["prior_action_outcomes"][0]
    assert all("permission_allowed" not in a["scheduling"] for a in context["available_actions"])
    assert state == before
    audit = manager.get(budget_id)["usage_records"][0]["diagnostic"]
    assert audit["estimated_input_tokens"] > 0


def test_planner_ignores_small_legacy_input_budget(client, monkeypatch):
    settings = get_settings()
    settings.external_model_enabled = True
    settings.model_name = "isolated-model"
    settings.model_api_key = SecretStr("synthetic-not-a-provider-key")
    settings.investigation_input_tokens = 1000
    database = Database(settings.database_path)
    manager = ContextBudgetManager(database, settings)
    budget_id = manager.create("investigation")
    monkeypatch.setattr('app.context.model._completion', lambda **_: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(report())))],
        usage=SimpleNamespace(prompt_tokens=64, completion_tokens=32)))
    result = _planner(settings, budget_regression_state(), database, budget_id)
    assert 'model_error' not in result
    assert manager.get(budget_id)["calls"] == 1
