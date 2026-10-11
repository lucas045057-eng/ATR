import json

import pytest
from app.common import dumps
from app.connectors.diagnostics import ToolParseError
from app.connectors.mock import MockConnector
from app.connectors.models import ToolRequest
from app.connectors.service import RuntimeService
from app.connectors.standard_ssh import IdentityRejected
from app.settings import get_settings
from app.storage.database import Database
from conftest import wait_job
from test_business import onboard
from test_runtime import target
from test_v22_adaptive import model_stub, report


def unbound(client):
    profile = onboard(client)
    t = target(client, profile, docker=True)
    base = f"/api/v1/businesses/{profile['business_id']}"
    profile = client.post(base + "/review", json={"expected_version": profile["profile_version"],
        "decision": "approve", "reason": "审核隔离画像，尚未确认运行映射"}).json()
    return profile, t


def mapping(context):
    c = next(c for c in context["candidates"] if c["kind"] == "service" and c["name"] == "catalog-api")
    service = next(s for s in context["services"] if s["name"] == "catalog-api")
    return {"summary": "候选映射需要人工核验，尚无根因结论", "proposals": [{
        "service": service["name"], "role": service["role"], "candidate_key": c["candidate_key"], "kind": c["kind"],
        "evidence_ids": c["evidence_ids"], "reasons": ["清单名称与声明服务相同，仅为线索"],
        "counterevidence": ["尚未采集已授权日志"], "uncertainty": "可能是其他部署，需人工确认范围",
        "next_tools": ["service.status", "service.logs"]}]}


def start(client, profile):
    incident = client.post("/api/v1/incidents", json={"system_id": profile["system_id"], "symptom": "catalog-api unavailable"}).json()
    r = client.post(f"/api/v1/incidents/{incident['id']}/runs")
    assert r.status_code == 202, r.text
    job = wait_job(client, r.json()["job_id"])
    assert job["status"] == "completed", job
    return client.get("/api/v1/runs/" + job["result"]["run_id"]).json()


def confirm(client, profile, result, decision="confirm"):
    proposal = result["summary"]["mapping_proposals"][0]
    r = client.post(f"/api/v1/businesses/{profile['business_id']}/bindings/{proposal['binding_id']}/review",
                    json={"decision": decision, "reason": "人工核验隔离服务身份及授权范围"})
    assert r.status_code == 200, r.text


def test_zero_binding_actual_provider_then_same_run_resume(client, monkeypatch):
    profile, t = unbound(client)
    contexts = model_stub(monkeypatch, lambda c, _: mapping(c) if c.get("phase") == "discovery" else report())
    result = start(client, profile)
    assert result["target_id"] == t["id"]
    assert result["runtime_target"]["id"] == t["id"]
    assert result["status"] == "awaiting_review" and result["summary"]["model_status"] == "completed"
    assert len(contexts) == 1 and contexts[0]["phase"] == "discovery"
    assert {c["tool_id"] for c in result["tool_calls"]} == {"host.info", "host.memory", "host.disk", "network.listeners", "service.list", "container.list", "process.list"}
    assert result["context_budget"]["tool_calls"] == 7 and result["context_budget"]["calls"] == 1
    assert result["context_budget"]["elapsed_ms"] > 0 and result["context_budget"]["retrieved_bytes"] > 0
    assert result["summary"]["gate"]["status"] == "unresolved" and not result["summary"]["evidence_ids"]
    runtime = RuntimeService(Database(get_settings().database_path), get_settings())
    denied = runtime.execute(profile["business_id"], t["id"], ToolRequest(tool_id="service.logs", arguments={"name": "catalog-api.service"}))
    assert denied["status"] == "denied"
    assert client.post(f"/api/v1/runs/{result['id']}/resume").status_code == 409


    confirm(client, profile, result)
    resume = client.post(f"/api/v1/runs/{result['id']}/resume")
    assert resume.status_code == 202, resume.text
    job = wait_job(client, resume.json()["job_id"])
    assert job["status"] == "completed", job
    final = client.get(f"/api/v1/runs/{result['id']}").json()
    assert final["target_id"] == result["target_id"]
    assert {call["target_id"] for call in final["tool_calls"]} == {t["id"]}
    assert final["status"] == "unresolved" and final["summary"]["resumed_after_review"]
    assert final["summary"]["budget_id"] == result["summary"]["budget_id"]
    assert final["context_budget"]["tool_calls"] == 9 and final["context_budget"]["calls"] == 2
    assert len(contexts) == 2
    assert {e["evidence_id"] for e in contexts[1]["current_evidence"]}.isdisjoint(result["summary"]["discovery_evidence_ids"])
    assert client.post(f"/api/v1/runs/{result['id']}/resume").status_code == 409


def test_partial_parse_failure_continues_container_and_model(client, monkeypatch):
    profile, _ = unbound(client)
    original = MockConnector.read
    def read(self, tool, args):
        if tool == "service.list":
            raise ToolParseError(line_number=2, output_bytes=100, invalid_item_count=1)
        return original(self, tool, args)
    monkeypatch.setattr(MockConnector, "read", read)
    contexts = model_stub(monkeypatch, lambda c, _: {"summary": "服务清单失败，其他可靠证据可供人工审核", "proposals": []})
    result = start(client, profile)
    assert result["status"] == "awaiting_review" and len(contexts) == 1
    assert result["summary"]["collection_status"] == "partial"
    assert any(c["tool_id"] == "container.list" and c["status"] == "succeeded" for c in result["tool_calls"])
    error = result["summary"]["tool_errors"][0]
    assert error["failure_stage"] == "parse" and error["line_number"] == 2
    assert result["context_budget"]["retrieved_bytes"] >= 100


def test_terminal_identity_failure_no_more_tools_or_model(client, monkeypatch):
    profile, _ = unbound(client)
    monkeypatch.setattr(MockConnector, "read", lambda *a: (_ for _ in ()).throw(IdentityRejected("credential-secret")))
    contexts = model_stub(monkeypatch, lambda c, _: pytest.fail("Must stop before model"))
    result = start(client, profile)
    assert result["status"] == "unresolved" and len(result["tool_calls"]) == 1 and not contexts
    assert result["summary"]["stop_reason"] == "ssh_identity_rejected"
    assert "credential-secret" not in json.dumps(result)


@pytest.mark.parametrize("mutation", ["unknown_evidence", "extra_command", "process_identity"])
def test_invalid_mapping_never_grants_authority(client, monkeypatch, mutation):
    profile, _ = unbound(client)
    def reply(c, _):
        r = mapping(c)
        p = r["proposals"][0]
        if mutation == "unknown_evidence":
            p["evidence_ids"] = ["uncollected-evidence"]
        elif mutation == "extra_command":
            p["next_tools"] = ["shell.exec"]
        else:
            candidate = next(x for x in c["candidates"] if x["kind"] == "process")
            p.update(candidate_key=candidate["candidate_key"], kind="process", evidence_ids=candidate["evidence_ids"], next_tools=["process.list"])
        return r
    model_stub(monkeypatch, reply)
    r = start(client, profile)
    assert r["status"] == "unresolved" and r["summary"]["stop_reason"] == "model_schema_error"
    assert not r["summary"]["mapping_proposals"]
    assert not any(c["tool_id"].endswith(".logs") or c["tool_id"].startswith("config.") for c in r["tool_calls"])
    with Database(get_settings().database_path).connect() as c:
        assert c.execute("SELECT count(*) FROM service_bindings WHERE status='confirmed'").fetchone()[0] == 0


@pytest.mark.parametrize("changed", ["expired", "rejected", "scope", "profile"])
def test_resume_revalidates_review_version_and_permissions(client, monkeypatch, changed):
    profile, t = unbound(client)
    contexts = model_stub(monkeypatch, lambda c, _: mapping(c))
    r = start(client, profile)
    confirm(client, profile, r, "reject" if changed == "rejected" else "confirm")
    db = Database(get_settings().database_path)
    with db.transaction() as c:
        if changed == "expired":
            c.execute("UPDATE service_bindings SET expires_at='2000-01-01T00:00:00+00:00'")
        elif changed == "scope":
            row = c.execute("SELECT config_json FROM runtime_targets WHERE id=?", (t["id"],)).fetchone()
            cfg = json.loads(row[0])
            cfg["allowed_paths"] = []
            c.execute("UPDATE runtime_targets SET config_json=? WHERE id=?", (dumps(cfg), t["id"]))
        elif changed == "profile":
            from app.business.service import BusinessService
            business = BusinessService(db, get_settings())
    if changed == "profile":
        updated = business._profile(profile["business_id"])
        updated.description += " updated fixture"
        business.save(updated, updated.profile_version)
    response = client.post(f"/api/v1/runs/{r['id']}/resume")
    assert response.status_code in {409, 404}, response.text
    assert len(contexts) == 1
    if changed in {"expired", "rejected"}:
        denied = RuntimeService(db, get_settings()).execute(profile["business_id"], t["id"], ToolRequest(tool_id="service.logs", arguments={"name": "catalog-api.service"}))
        assert denied["status"] == "denied"


def test_global_tool_budget_stops_without_ai_or_new_discovery_budget(client, monkeypatch):
    profile, _ = unbound(client)
    get_settings().investigation_tool_calls = 2
    model_stub(monkeypatch, lambda c, _: pytest.fail("No remaining global budget"))
    r = start(client, profile)
    assert r["status"] == "unresolved" and r["summary"]["stop_reason"] == "global_budget_exhausted"
    assert r["context_budget"]["tool_calls"] == 2 and r["context_budget"]["calls"] == 0
    with Database(get_settings().database_path).connect() as c:
        assert c.execute("SELECT count(*) FROM context_budgets WHERE phase='discovery'").fetchone()[0] == 0


def test_expired_mapping_rediscovery_requires_new_review(client, monkeypatch):
    from test_runtime import discover
    profile, t = unbound(client)
    model_stub(monkeypatch, lambda c, _: mapping(c))
    r = start(client, profile)
    confirm(client, profile, r)
    db = Database(get_settings().database_path)
    with db.transaction() as c:
        c.execute("UPDATE service_bindings SET expires_at='2000-01-01T00:00:00+00:00' WHERE status='confirmed'")
    discovery = discover(client, profile, t)
    assert any(m["business_service"] == "catalog-api" and m["verification_status"] == "pending" for m in discovery["matches"])
    denied = RuntimeService(db, get_settings()).execute(profile["business_id"], t["id"], ToolRequest(tool_id="service.status", arguments={"name": "catalog-api.service"}))
    assert denied["status"] == "denied"


def test_approved_code_only_profile_still_collects_and_invokes_provider(client, monkeypatch):
    profile = onboard(client, {"app.py": "def handle():\n    return 'synthetic'\n"})
    target(client, profile, docker=True)
    profile = client.post(f"/api/v1/businesses/{profile['business_id']}/review", json={"expected_version": profile["profile_version"],
        "decision": "approve", "reason": "已审核纯源码画像，尚无部署服务声明"}).json()
    contexts = model_stub(monkeypatch, lambda c, _: {"summary": "已采集基础清单；缺少源服务声明，不能猜测映射或根因", "proposals": []})
    r = start(client, profile)
    assert r["status"] == "awaiting_review" and len(r["tool_calls"]) == 7 and r["ended_at"] is None
    assert len(contexts) == 1 and not contexts[0]["services"] and contexts[0]["candidates"]
    assert not r["summary"]["mapping_proposals"] and not r["summary"]["evidence_ids"]


def test_process_only_confirmation_does_not_block_basic_discovery(client, monkeypatch):
    from test_runtime import discover
    profile, t = unbound(client)
    original = MockConnector.read
    def read(self, tool, args):
        if tool == "process.list":
            return {"processes": [{"id": "42", "name": "catalog-api", "kind": "process"}]}
        return original(self, tool, args)
    monkeypatch.setattr(MockConnector, "read", read)
    d = discover(client, profile, t)
    process = next(m for m in d["matches"] if m["kind"] == "process")
    client.post(f"/api/v1/businesses/{profile['business_id']}/bindings/{process['id']}/review", json={"decision": "confirm", "reason": "人工仅确认进程线索，未授权服务日志"}).raise_for_status()
    contexts = model_stub(monkeypatch, lambda c, _: mapping(c))
    r = start(client, profile)
    assert r["status"] == "awaiting_review" and len(contexts) == 1 and len(r["tool_calls"]) == 7


def test_resume_password_expiry_is_safe_403_and_refresh_preserves_budget(client, monkeypatch):
    from app.connectors.credentials import SSH_SESSIONS
    from app.connectors.standard_ssh import StandardSSHConnector
    from test_standard_ssh import standard
    profile = onboard(client)
    settings = get_settings()
    settings.ssh_enabled = True
    cfg = standard(credential_ref="")
    cfg.pop("id")
    path = f"/api/v1/businesses/{profile['business_id']}"
    target_response = client.post(path + "/targets", json=cfg)
    assert target_response.status_code == 201, target_response.text
    t = target_response.json()
    credential_path = path + f"/targets/{t['id']}/credential"
    client.post(credential_path, json={"password": "synthetic-session-only"}).raise_for_status()
    def read(self, tool, args):
        SSH_SESSIONS.resolve(self.config, settings.operator_id)
        return MockConnector(self.config).read(tool, args)
    monkeypatch.setattr(StandardSSHConnector, "read", read)
    profile = client.post(path + "/review", json={"expected_version": profile["profile_version"], "decision": "approve", "reason": "隔离密码目标与业务画像已审核"}).json()
    contexts = model_stub(monkeypatch, lambda c, _: mapping(c) if c.get("phase") == "discovery" else report())
    r = start(client, profile)
    confirm(client, profile, r)
    client.delete(credential_path).raise_for_status()
    response = client.post(f"/api/v1/runs/{r['id']}/resume")
    assert response.status_code == 403 and "synthetic-session-only" not in response.text and len(contexts) == 1
    client.post(credential_path, json={"password": "synthetic-session-refreshed"}).raise_for_status()
    response = client.post(f"/api/v1/runs/{r['id']}/resume")
    assert response.status_code == 202, response.text
    job = wait_job(client, response.json()["job_id"])
    assert job["status"] == "completed", job
    final = client.get(f"/api/v1/runs/{r['id']}").json()
    assert final["context_budget"]["id"] == r["context_budget"]["id"] and final["context_budget"]["calls"] == 2
    assert "synthetic-session" not in json.dumps(final)
