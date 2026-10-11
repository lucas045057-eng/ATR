import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from app.agent.workflow import _planner
from app.context.budget import BudgetExceeded, ContextBudgetManager
from app.context.model import model_messages
from app.settings import get_settings
from app.storage.database import Database
from pydantic import SecretStr
from test_model_output_compatibility import budget_regression_state


def test_input_rejection_reports_numbers_without_creating_request(client):
    settings = get_settings()
    database = Database(settings.database_path)
    manager = ContextBudgetManager(database, settings)
    budget_id = manager.create("investigation")
    messages = model_messages("NEVER-ECHO-PROMPT", {"secret": "NEVER-ECHO-CONTEXT", "text": "x" * 13000})
    with pytest.raises(BudgetExceeded) as caught:
        manager.reserve(budget_id, messages, settings.model_max_output_tokens)
    details = caught.value.diagnostic
    assert details["budget_reason"] == "request_input_limit"
    assert details["estimated_input_tokens"] > details["input_limit"] == 12000
    assert "NEVER-ECHO" not in json.dumps(details)
    budget = manager.get(budget_id)
    assert budget["calls"] == budget["reserved_tokens"] == 0
    assert budget["usage_records"] == []


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


def test_planner_preserves_safe_budget_details_in_error(client):
    settings = get_settings()
    settings.external_model_enabled = True
    settings.model_name = "isolated-model"
    settings.model_api_key = SecretStr("synthetic-not-a-provider-key")
    settings.investigation_input_tokens = 1000
    database = Database(settings.database_path)
    manager = ContextBudgetManager(database, settings)
    budget_id = manager.create("investigation")
    result = _planner(settings, budget_regression_state(), database, budget_id)
    error = result["model_error"]
    assert error["code"] == "model_budget_exceeded"
    assert error["budget_reason"] == "request_input_limit"
    assert error["estimated_input_tokens"] > error["input_limit"] == 1000
    assert manager.get(budget_id)["calls"] == 0
