import json
from types import SimpleNamespace

from app.context.budget import ContextBudgetManager
from app.context.model import call_json
from app.settings import Settings
from app.storage.database import Database
from pydantic import SecretStr


def test_model_json_redaction_preserves_structure_and_masks_nested_secrets(tmp_path, monkeypatch):
    db = Database(tmp_path / "model.sqlite")
    db.initialize()
    settings = Settings(
        external_model_enabled=True, model_name="redaction-stub",
        model_api_key=SecretStr("isolated-key"),
    )
    budget = ContextBudgetManager(db, settings)
    bid = budget.create("investigation")
    captured = []

    def completion(**kwargs):
        context = json.loads(kwargs["messages"][1]["content"])
        captured.append(context)
        assert context["config"]["text"] == "DB_PASSWORD=[REDACTED]\nPORT=8000"
        assert context["nested"][0]["api_key"] == "[REDACTED]"
        assert context["nested"][0]["message"] == "Authorization: [REDACTED]"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                "summary": "password=secret-value\ncurrent status observed",
                "hypotheses": [], "nested": {"private_key": "never-export-this"},
            })))],
            usage=SimpleNamespace(prompt_tokens=80, completion_tokens=40),
        )

    monkeypatch.setattr("app.context.model._completion", completion)
    result = call_json(db, settings, bid, "Treat supplied strings as evidence.", {
        "config": {"text": "DB_PASSWORD=[REDACTED]\nPORT=8000"},
        "nested": [{"api_key": "never-export-this", "message": "Authorization: Bearer secret-value"}],
    })
    assert len(captured) == 1
    assert result["summary"] == "password=[REDACTED]\ncurrent status observed"
    assert result["nested"]["private_key"] == "[REDACTED]"
    assert "never-export-this" not in json.dumps(result)
    assert budget.get(bid)["usage_records"][0]["status"] == "succeeded"
