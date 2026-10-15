import sqlite3

import pytest
from app.connectors.mock import MockConnector
from app.settings import get_settings
from test_v22_adaptive import (
    candidate,
    hypothesis,
    investigate,
    model_stub,
    prepared_profile,
    proposal,
    report,
)


def test_binding_expiring_during_reasoning_blocks_dependency_read(client, monkeypatch):
    profile = prepared_profile(client, monkeypatch)

    def reply(context, _):
        action = candidate(context, "service.status", "store")
        with sqlite3.connect(get_settings().database_path) as conn:
            conn.execute(
                "UPDATE service_bindings SET expires_at='2000-01-01T00:00:00+00:00' "
                "WHERE business_id=? AND service='store'",
                (profile["business_id"],),
            )
        return report(action=proposal(action))

    contexts = model_stub(monkeypatch, reply)
    result = investigate(client, profile)
    assert result["status"] == "unresolved"
    assert result["summary"]["stop_reason"] == "permission_blocked"
    assert len(contexts) == 1
    assert not any(
        call["status"] == "succeeded" and 'store.service' in call["args_redacted_json"]
        for call in result["tool_calls"]
    )
    assert any(event["event_type"] == "read_tool_denied" for event in result["events"])


def test_failed_direction_is_not_repeated_forever(client, monkeypatch):
    profile = prepared_profile(client, monkeypatch)
    original = MockConnector.read

    def read(self, tool, args):
        if tool == "service.status" and args.get("name") == "store.service":
            raise TimeoutError("isolated transport timeout")
        return original(self, tool, args)

    monkeypatch.setattr(MockConnector, "read", read)

    def reply(context, number):
        if number == 1:
            return report(action=proposal(candidate(context, "service.status", "store")))
        assert not any(
            action["tool_id"] == "service.status" and action["service"] == "store"
            for action in context["available_actions"]
        )
        return report()

    contexts = model_stub(monkeypatch, reply)
    result = investigate(client, profile)
    assert result["status"] == "unresolved"
    assert result["summary"]["stop_reason"] == "no_new_actions"
    assert len(contexts) == 2
    calls = [call for call in result["tool_calls"] if "store.service" in call["args_redacted_json"]]
    assert len(calls) == 1
    assert calls[0]["status"] == "timed_out"
    assert calls[0]["error_code"] == "tool_timeout"


def test_model_cannot_replace_candidate_arguments(client, monkeypatch):
    profile = prepared_profile(client, monkeypatch)

    def reply(context, number):
        if number == 1:
            action = proposal(candidate(context, "service.status", "store"))
            action.update(arguments={"name": "unauthorized.service"}, tool_id="shell.exec")
            return report(action=action)
        return report()

    model_stub(monkeypatch, reply)
    result = investigate(client, profile)
    assert result["status"] == "unresolved"
    assert len(result["tool_calls"]) == 2
    assert result["summary"]["stop_reason"] == "model_schema_error"
    assert result["context_budget"]["usage_records"][0]["status"] == "failed"
    assert not any("unauthorized.service" in call["args_redacted_json"] for call in result["tool_calls"])
    assert not any(call["tool_id"] == "shell.exec" for call in result["tool_calls"])


def test_omitting_a_refuted_hypothesis_does_not_erase_its_counterevidence(client, monkeypatch):
    get_settings().investigation_input_tokens = 30000
    profile = prepared_profile(client, monkeypatch)
    original = MockConnector.read

    def read(self, tool, args):
        if tool == "service.status":
            return {"connector": "mock", "state": "failed"}
        return original(self, tool, args)

    monkeypatch.setattr(MockConnector, "read", read)

    def reply(context, number):
        if number == 1:
            return report(
                [hypothesis(context, "catalog-api", failure="startup_failure", refute=True)],
                proposal(candidate(context, "config.list")),
            )
        if number == 2:
            return report(action=proposal(candidate(context, "config.read")))
        return report([hypothesis(context, "catalog-api", failure="startup_failure", all_evidence=True)])

    contexts = model_stub(monkeypatch, reply)
    result = investigate(client, profile)
    assert len(contexts) == 3
    assert result["status"] == "unresolved"
    assert result["summary"]["gate"]["status"] == "unresolved"
    assert result["summary"]["stop_reason"] == "evidence_conflict"
    latest = next(h for h in result["summary"]["hypotheses"] if h["failure_mode"] == "startup_failure")
    assert latest["status"] == "refuted"
    assert latest["counter_evidence"]


def test_server_denial_of_initial_read_stops_before_other_reads_or_model(client, monkeypatch):
    profile = prepared_profile(client, monkeypatch)
    original = MockConnector.read

    def read(self, tool, args):
        if tool == "service.status":
            raise PermissionError("isolated server-side policy changed")
        return original(self, tool, args)

    monkeypatch.setattr(MockConnector, "read", read)
    contexts = model_stub(monkeypatch, lambda context, _: report())
    result = investigate(client, profile)
    assert result["status"] == "unresolved"
    assert result["summary"]["stop_reason"] == "permission_blocked"
    assert contexts == []
    assert len(result["tool_calls"]) == 1
    assert result["tool_calls"][0]["status"] == "denied"


@pytest.mark.parametrize("service", [["unapproved"], {"name": "unapproved"}, None])
def test_malformed_model_scope_request_stops_without_failing_job(client, monkeypatch, service):
    profile = prepared_profile(client, monkeypatch)
    model_stub(monkeypatch, lambda context, _: report(scope_requests=[{"service": service, "reason": "needs access"}]))
    result = investigate(client, profile)
    assert result["status"] == "unresolved"
    assert result["summary"]["stop_reason"] == "model_schema_error"
    assert len(result["tool_calls"]) == 2


def test_listed_dotenv_is_a_bounded_configuration_direction(client, monkeypatch):
    profile = prepared_profile(client, monkeypatch)
    original = MockConnector.read

    def read(self, tool, args):
        if tool == "config.list":
            return {"connector": "mock", "files": [args["path"] + "/.env"]}
        return original(self, tool, args)

    monkeypatch.setattr(MockConnector, "read", read)

    def reply(context, number):
        if number == 1:
            return report(action=proposal(candidate(context, "config.list")))
        if number == 2:
            return report(action=proposal(candidate(context, "config.read", path="/srv/catalog/.env")))
        return report()

    contexts = model_stub(monkeypatch, reply)
    result = investigate(client, profile)
    assert len(contexts) == 3
    assert any(c["tool_id"] == "config.read" and "/srv/catalog/.env" in c["args_redacted_json"] for c in result["tool_calls"])
    assert result["status"] == "unresolved"
