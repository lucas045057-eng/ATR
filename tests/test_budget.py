from types import SimpleNamespace

import pytest
from app.context.budget import BudgetExceeded, BudgetLimits, ContextBudgetManager
from app.settings import Settings
from app.storage.database import Database


@pytest.fixture
def manager(tmp_path):
    db = Database(tmp_path / "budget.sqlite")
    db.initialize()
    return ContextBudgetManager(db, Settings(model_name="test-model"))


def test_hard_budget_prevents_calls_and_can_resume(manager):
    bid = manager.create("initial", limits=BudgetLimits(max_input_tokens=10))
    with pytest.raises(BudgetExceeded, match="request_input_limit"):
        manager.reserve(bid, [{"content": "x"}], 10)
    assert manager.get(bid)["calls"] == 0
    manager.update(bid, BudgetLimits(max_input_tokens=1000, max_total_tokens=10000, max_calls=1))
    reservation = manager.reserve(bid, [{"content": "hello"}], 20)
    response = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=8, completion_tokens=5))
    manager.settle(bid, reservation, response)
    result = manager.get(bid)
    assert result["input_tokens"] == 8
    assert result["usage_records"][0]["usage_kind"] == "actual"
    with pytest.raises(BudgetExceeded, match="model_call_limit"):
        manager.reserve(bid, [{"content": "again"}], 20)


def test_reservations_and_estimates(manager):
    bid = manager.create(
        "investigation", limits=BudgetLimits(max_total_tokens=600, max_input_tokens=1000)
    )
    reservation = manager.reserve(bid, [{"content": "hello"}], 100)
    with pytest.raises(BudgetExceeded, match="total_token_limit"):
        manager.reserve(bid, [{"content": "hello"}], 100)
    manager.settle(bid, reservation, None, "failed")
    result = manager.get(bid)
    assert result["reserved_tokens"] == 0
    assert result["usage_records"][0]["usage_kind"] == "estimate"
    assert result["usage_records"][0]["cost"] is None


def test_tool_and_retrieval_limits(manager):
    bid = manager.create("investigation", limits=BudgetLimits(max_tools=1, max_retrieved_bytes=10))
    manager.consume_tool(bid)
    with pytest.raises(BudgetExceeded):
        manager.consume_tool(bid)
    manager.retrieval(bid, 10)
    with pytest.raises(BudgetExceeded):
        manager.retrieval(bid, 1)
