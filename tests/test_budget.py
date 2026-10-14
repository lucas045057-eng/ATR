from types import SimpleNamespace

import pytest
from app.context.budget import BudgetLimits, ContextBudgetManager
from app.settings import Settings
from app.storage.database import Database
from pydantic import ValidationError


@pytest.fixture
def manager(tmp_path):
    db = Database(tmp_path / "budget.sqlite")
    db.initialize()
    return ContextBudgetManager(db, Settings(model_name="test-model"))


def test_usage_above_legacy_limits_is_recorded_without_blocking(manager):
    bid = manager.create("initial", limits=BudgetLimits(max_input_tokens=10, max_calls=1, max_total_tokens=1))
    reservation = manager.reserve(bid, [{"content": "hello"}], 20)
    response = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=8000, completion_tokens=500))
    manager.settle(bid, reservation, response)
    result = manager.get(bid)
    assert result["input_tokens"] == 8000
    assert result["usage_records"][0]["usage_kind"] == "actual"
    assert result["mode"] == "accounting_only" and not result["enforcement_enabled"]
    assert result["stop_reason"] is None
    manager.reserve(bid, [{"content": "again"}], 20)
    assert manager.get(bid)["calls"] == 2
    with pytest.raises(ValueError, match="仅用于用量记录"):
        manager.update(bid, BudgetLimits())


def test_reservations_and_estimates(manager):
    bid = manager.create(
        "investigation", limits=BudgetLimits(max_total_tokens=600, max_input_tokens=1000)
    )
    reservation = manager.reserve(bid, [{"content": "hello"}], 100)
    another = manager.reserve(bid, [{"content": "hello"}], 100)
    manager.settle(bid, reservation, None, "failed")
    manager.settle(bid, another, None, "failed")
    result = manager.get(bid)
    assert result["reserved_tokens"] == 0
    assert result["usage_records"][0]["usage_kind"] == "estimate"
    assert result["usage_records"][0]["cost"] is None


def test_tool_retrieval_and_elapsed_time_are_accounting_only(manager):
    bid = manager.create("investigation", limits=BudgetLimits(max_tools=1, max_retrieved_bytes=10))
    manager.consume_tool(bid)
    manager.consume_tool(bid)
    manager.retrieval(bid, 10)
    manager.retrieval(bid, 1)
    manager.record_time(bid, 2000)
    result = manager.get(bid)
    assert result["tool_calls"] == 2 and result["retrieved_bytes"] == 11
    assert result["elapsed_ms"] == 2000000 and result["stop_reason"] is None
    assert manager.remaining_seconds(bid) == float('inf')


def test_requested_investigation_limits_are_accepted_and_snapshotted(manager):
    settings = Settings(
        investigation_model_rounds=100,
        investigation_tool_calls=500,
        budget_total_tokens=1_000_000,
    )
    budget_manager = ContextBudgetManager(manager.db, settings)
    budget_id = budget_manager.create("investigation")

    limits = budget_manager.get(budget_id)["limits"]
    assert limits["max_calls"] == 100
    assert limits["max_tools"] == 500
    assert limits["max_total_tokens"] == 1_000_000

    with pytest.raises(ValidationError):
        Settings(investigation_model_rounds=101)
    with pytest.raises(ValidationError):
        BudgetLimits(max_tools=501)
