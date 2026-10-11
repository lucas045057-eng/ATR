from datetime import datetime, timedelta

import pytest
from app.connectors.mock import MockConnector
from app.settings import get_settings
from app.storage.database import Database
from conftest import wait_job
from test_business import onboard
from test_runtime import discover, read, target
from test_v22_scope import current_binding, setup_binding


def test_observed_container_identity_change_is_stale_even_when_no_longer_matching(client, monkeypatch):
    profile = onboard(client)
    runtime = target(client, profile, docker=True)
    snapshot = discover(client, profile, runtime)
    binding = next(m for m in snapshot["matches"] if m["kind"] == "container"
                   and m["business_service"] == "catalog-api")
    reviewed = client.post(
        f"/api/v1/businesses/{profile['business_id']}/bindings/{binding['id']}/review",
        json={"decision": "confirm", "reason": "已核对隔离业务容器与部署名称"},
    )
    assert reviewed.status_code == 200, reviewed.text
    original = MockConnector.read

    def changed(self, tool, args):
        payload = original(self, tool, args)
        if tool == "container.list":
            for item in payload["containers"]:
                if item["id"] == "a" * 64:
                    item.update(name="other-business", image="unrelated:latest",
                                ports=[], environment_keys=[])
        return payload

    monkeypatch.setattr(MockConnector, "read", changed)
    snapshot = discover(client, profile, runtime)
    row = current_binding(client, profile, binding)
    assert row["status"] == "confirmed"  # Existing read authorization survives the observation.
    assert row["runtime_current"] is False
    assert row["freshness_status"] == "stale"
    assert any(c["binding_id"] == binding["id"] and c["reason"] == "identity_changed"
               for c in snapshot["binding_changes"])


def test_missing_inventory_field_does_not_prove_service_absence(client, monkeypatch):
    profile, runtime, binding = setup_binding(client)
    before = current_binding(client, profile, binding)
    original = MockConnector.read

    def malformed(self, tool, args):
        if tool == "service.list":
            return {"connector": "mock", "text": "inventory metadata unavailable"}
        return original(self, tool, args)

    monkeypatch.setattr(MockConnector, "read", malformed)
    snapshot = discover(client, profile, runtime)
    after = current_binding(client, profile, binding)
    assert after["runtime_current"] is True
    assert after["observed_at"] == before["observed_at"]
    assert snapshot["collection_status"] == "partial"


@pytest.mark.parametrize("items", [None, "invalid-list", [None]])
def test_malformed_inventory_does_not_leave_running_snapshot(client, monkeypatch, items):
    profile, runtime, binding = setup_binding(client)
    before = current_binding(client, profile, binding)
    original = MockConnector.read

    def malformed(self, tool, args):
        if tool == "service.list":
            return {"services": items, "partial": False, "truncated": False, "timeout": False}
        return original(self, tool, args)

    monkeypatch.setattr(MockConnector, "read", malformed)
    response = client.post(
        f"/api/v1/businesses/{profile['business_id']}/targets/{runtime['id']}/discover"
    )
    assert response.status_code == 202, response.text
    job = wait_job(client, response.json()["job_id"])
    after = current_binding(client, profile, binding)
    assert after["status"] == "confirmed"
    assert after["runtime_current"] is True
    assert after["observed_at"] == before["observed_at"]
    database = Database(get_settings().database_path)
    with database.transaction() as tx:
        assert tx.execute(
            "SELECT COUNT(*) FROM discovery_runs WHERE target_id=? AND status='running'",
            (runtime["id"],),
        ).fetchone()[0] == 0
    assert job["status"] in {"completed", "failed"}


def test_filtered_snapshot_preserves_binding_outside_the_filter(client, monkeypatch):
    original = MockConnector.read
    stage = {"filtered": False}

    def legacy_name(self, tool, args):
        if tool != "service.list":
            return original(self, tool, args)
        candidate = {"id": "legacy-web.service", "name": "legacy-web", "kind": "service",
                     "state": "active", "ports": [8000]}
        hints = args.get("name_hints", [])
        covered = any(h.casefold() in candidate["id"].casefold() for h in hints)
        return {"services": [candidate] if not stage["filtered"] or covered else [],
                "filtered": stage["filtered"], "partial": False, "truncated": False,
                "timeout": False, "name_hints": hints}

    monkeypatch.setattr(MockConnector, "read", legacy_name)
    profile, runtime, binding = setup_binding(client)
    stage["filtered"] = True
    discover(client, profile, runtime)
    assert current_binding(client, profile, binding)["runtime_current"] is True


def test_inventory_array_order_is_not_a_scope_identity_change(client, monkeypatch):
    original = MockConnector.read
    stage = {"reversed": False}

    def reordered(self, tool, args):
        payload = original(self, tool, args)
        if tool == "service.list":
            for item in payload["services"]:
                if item["id"] == "catalog-api.service":
                    item["ports"] = [8001, 8000] if stage["reversed"] else [8000, 8001]
                    item["environment_keys"] = (["CACHE_HOST", "DB_HOST"] if stage["reversed"]
                                                else ["DB_HOST", "CACHE_HOST"])
        return payload

    monkeypatch.setattr(MockConnector, "read", reordered)
    profile, runtime, binding = setup_binding(client)
    stage["reversed"] = True
    snapshot = discover(client, profile, runtime)
    assert current_binding(client, profile, binding)["runtime_current"] is True
    assert any(m["id"] == binding["id"] for m in snapshot["matches"])


def test_graph_view_does_not_present_elapsed_binding_as_current(client, monkeypatch):
    profile, runtime, binding = setup_binding(client)

    class LaterClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=2)

    monkeypatch.setattr("app.system.scope.datetime", LaterClock)
    assert current_binding(client, profile, binding)["runtime_current"] is False
    graph = client.get(f"/api/v1/systems/{profile['system_id']}/graph").json()
    assert not any(n["name"] == "runtime:catalog-api.service"
                   and n["attributes"].get("freshness_status") == "confirmed"
                   for n in graph["nodes"])


@pytest.mark.parametrize("error", [ConnectionError, TimeoutError, PermissionError])
def test_total_collection_failure_keeps_authorized_binding_and_provenance(client, monkeypatch, error):
    profile, runtime, binding = setup_binding(client)
    before = current_binding(client, profile, binding)
    original = MockConnector.read

    def failed(self, tool, args):
        if tool.endswith(".list") or tool in {"host.info", "host.memory", "host.disk", "network.listeners"}:
            raise error("isolated discovery failure")
        return original(self, tool, args)

    monkeypatch.setattr(MockConnector, "read", failed)
    snapshot = discover(client, profile, runtime)
    after = current_binding(client, profile, binding)
    assert snapshot["collection_status"] == "failed"
    assert after["status"] == "confirmed"
    assert after["runtime_current"] is True
    assert after["observed_at"] == before["observed_at"]
    assert after["source"] == before["source"]
    assert read(client, profile, runtime, "service.status", {"name": "catalog-api.service"})["payload"]


def test_complete_replacement_is_pending_until_explicit_review(client, monkeypatch):
    profile, runtime, binding = setup_binding(client)
    original = MockConnector.read

    def replaced(self, tool, args):
        payload = original(self, tool, args)
        if tool == "service.list":
            for item in payload["services"]:
                if item["id"] == "catalog-api.service":
                    item["id"] = "catalog-api-v2.service"
                    item["name"] = "catalog-api-v2"
        return payload

    monkeypatch.setattr(MockConnector, "read", replaced)
    snapshot = discover(client, profile, runtime)
    assert current_binding(client, profile, binding)["runtime_current"] is False
    replacement = next(m for m in snapshot["matches"]
                       if m["runtime_candidate"] == "catalog-api-v2.service")
    assert replacement["verification_status"] == "pending"
    assert read(client, profile, runtime, "service.status", {"name": "catalog-api-v2.service"})["status"] == "denied"
    response = client.post(
        f"/api/v1/businesses/{profile['business_id']}/bindings/{replacement['id']}/review",
        json={"decision": "confirm", "reason": "已人工核对隔离环境新部署对象"},
    )
    assert response.status_code == 200, response.text
    assert current_binding(client, profile, binding)["status"] == "superseded"
    assert read(client, profile, runtime, "service.status", {"name": "catalog-api-v2.service"})["payload"]


def test_expired_pending_candidate_cannot_be_approved(client):
    profile = onboard(client)
    runtime = target(client, profile)
    snapshot = discover(client, profile, runtime)
    binding = next(m for m in snapshot["matches"] if m["kind"] == "service")
    database = Database(get_settings().database_path)
    with database.transaction() as tx:
        tx.execute("UPDATE service_bindings SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                   (binding["id"],))
    response = client.post(
        f"/api/v1/businesses/{profile['business_id']}/bindings/{binding['id']}/review",
        json={"decision": "confirm", "reason": "尝试审核过期隔离发现候选"},
    )
    assert response.status_code == 422, response.text
    assert current_binding(client, profile, binding)["status"] == "pending"
    assert read(client, profile, runtime, "service.status", {"name": "catalog-api.service"})["status"] == "denied"
