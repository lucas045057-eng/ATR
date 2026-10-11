import json

from app import jobs
from app.common import now, uid
from app.settings import get_settings
from app.storage.database import Database
from conftest import wait_job
from test_business import onboard
from test_runtime import discover


def add_target(client, profile, name):
    response = client.post(f"/api/v1/businesses/{profile['business_id']}/targets", json={
        "name": name, "connector": "mock", "allowed_paths": ["/srv/catalog"],
    })
    assert response.status_code == 201, response.text
    return response.json()


def confirm_catalog(client, profile, target):
    discovery = discover(client, profile, target)
    binding = next(item for item in discovery["matches"]
                   if item["kind"] == "service" and item["business_service"] == "catalog-api")
    response = client.post(
        f"/api/v1/businesses/{profile['business_id']}/bindings/{binding['id']}/review",
        json={"decision": "confirm", "reason": "测试中核对隔离 Mock 目标映射"},
    )
    assert response.status_code == 200, response.text


def approve_profile(client, profile):
    latest = client.get(f"/api/v1/businesses/{profile['business_id']}").json()
    response = client.post(f"/api/v1/businesses/{profile['business_id']}/review", json={
        "expected_version": latest["profile_version"], "decision": "approve",
        "reason": "测试中审核隔离业务画像",
    })
    assert response.status_code == 200, response.text


def incident(client, profile):
    response = client.post("/api/v1/incidents", json={
        "system_id": profile["system_id"], "symptom": "catalog-api unavailable",
    })
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_selected_target_is_persisted_and_old_bindings_cannot_leak(client):
    profile = onboard(client)
    old = add_target(client, profile, "旧目标")
    new = add_target(client, profile, "随想记安全网关2")
    confirm_catalog(client, profile, old)
    confirm_catalog(client, profile, new)
    approve_profile(client, profile)
    event_id = incident(client, profile)

    missing = client.post(f"/api/v1/incidents/{event_id}/runs", json={})
    assert missing.status_code == 409 and "多个 SSH 目标" in missing.text

    started = client.post(f"/api/v1/incidents/{event_id}/runs", json={"target_id": new["id"]})
    assert started.status_code == 202, started.text
    job = wait_job(client, started.json()["job_id"])
    assert job["status"] == "completed", job
    run = client.get(f"/api/v1/runs/{job['result']['run_id']}").json()
    assert run["target_id"] == new["id"]
    assert run["runtime_target"]["name"] == "随想记安全网关2"
    assert {call["target_id"] for call in run["tool_calls"]} == {new["id"]}
    assert {call["actual_target_id"] for call in run["tool_calls"] if call["actual_target_id"]} == {new["id"]}
    with Database(get_settings().database_path).connect() as connection:
        linked = connection.execute(
            "SELECT DISTINCT o.target_id FROM tool_calls t JOIN runtime_observations o ON o.id=t.request_id WHERE t.run_id=?",
            (run["id"],),
        ).fetchall()
        assert {row["target_id"] for row in linked} == {new["id"]}


def test_registry_denies_action_retargeted_after_planning(client, monkeypatch):
    from app.tools.business import BusinessToolRegistry

    profile = onboard(client)
    old = add_target(client, profile, "旧目标")
    new = add_target(client, profile, "新目标")
    confirm_catalog(client, profile, old)
    confirm_catalog(client, profile, new)
    approve_profile(client, profile)
    original = BusinessToolRegistry.execute

    def retarget(self, action):
        return original(self, {**action, "target_id": old["id"]})

    monkeypatch.setattr(BusinessToolRegistry, "execute", retarget)
    event_id = incident(client, profile)
    started = client.post(f"/api/v1/incidents/{event_id}/runs", json={"target_id": new["id"]})
    assert started.status_code == 202, started.text
    job = wait_job(client, started.json()["job_id"])
    run = client.get(f"/api/v1/runs/{job['result']['run_id']}").json()
    assert run["target_id"] == new["id"]
    assert run["tool_calls"][0]["status"] == "denied"
    assert run["tool_calls"][0]["target_id"] == new["id"]
    with Database(get_settings().database_path).connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM runtime_observations WHERE id IN (SELECT request_id FROM tool_calls WHERE run_id=?)",
            (run["id"],),
        ).fetchone()[0] == 0


def test_unmapped_new_target_does_not_fall_back_to_old_confirmed_mapping(client):
    profile = onboard(client)
    old = add_target(client, profile, "旧目标")
    new = add_target(client, profile, "新目标")
    confirm_catalog(client, profile, old)
    approve_profile(client, profile)
    event_id = incident(client, profile)
    started = client.post(f"/api/v1/incidents/{event_id}/runs", json={"target_id": new["id"]})
    assert started.status_code == 202, started.text
    job = wait_job(client, started.json()["job_id"])
    assert job["status"] == "completed", job
    run = client.get(f"/api/v1/runs/{job['result']['run_id']}").json()
    assert run["target_id"] == new["id"]
    assert run["summary"]["stage"] == "awaiting_review"
    assert {call["actual_target_id"] for call in run["tool_calls"] if call["actual_target_id"]} == {new["id"]}
    assert not any(call["tool_id"].endswith(".logs") for call in run["tool_calls"])


def test_one_ready_target_is_not_implicitly_selected_from_multiple_configured_targets(client):
    profile = onboard(client)
    ready = add_target(client, profile, "唯一可用目标")
    unavailable = add_target(client, profile, "凭据待补目标")
    database = Database(get_settings().database_path)
    with database.transaction() as connection:
        row = connection.execute("SELECT config_json FROM runtime_targets WHERE id=?", (unavailable["id"],)).fetchone()
        config = json.loads(row["config_json"])
        config.update(connector="ssh", auth_mode="password")
        connection.execute("UPDATE runtime_targets SET config_json=? WHERE id=?", (json.dumps(config), unavailable["id"]))
    event_id = incident(client, profile)
    response = client.post(f"/api/v1/incidents/{event_id}/runs", json={})
    assert response.status_code == 409 and "不可用目标不会自动回退" in response.text
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs WHERE kind=?", ("investigate:" + event_id,)).fetchone()[0] == 0
    explicit = client.post(f"/api/v1/incidents/{event_id}/runs", json={"target_id": ready["id"]})
    assert explicit.status_code == 202, explicit.text


def test_active_event_job_is_only_reused_for_same_target(client, monkeypatch):
    profile = onboard(client)
    first = add_target(client, profile, "目标 A")
    second = add_target(client, profile, "目标 B")
    event_id = incident(client, profile)

    class PausedExecutor:
        def submit(self, function):
            return None

    monkeypatch.setattr(jobs, "POOL", PausedExecutor())
    one = client.post(f"/api/v1/incidents/{event_id}/runs", json={"target_id": first["id"]})
    assert one.status_code == 202, one.text
    same = client.post(f"/api/v1/incidents/{event_id}/runs", json={"target_id": first["id"]})
    assert same.status_code == 202 and same.json()["job_id"] == one.json()["job_id"] and same.json()["reused"]
    different = client.post(f"/api/v1/incidents/{event_id}/runs", json={"target_id": second["id"]})
    assert different.status_code == 409
    with Database(get_settings().database_path).connect() as connection:
        row = connection.execute("SELECT target_id FROM jobs WHERE id=?", (one.json()["job_id"],)).fetchone()
        assert row["target_id"] == first["id"]


def test_revoked_ssh_target_is_rejected_without_starting_worker(client, monkeypatch):
    settings = get_settings()
    settings.ssh_enabled = True
    profile = onboard(client)
    response = client.post(f"/api/v1/businesses/{profile['business_id']}/targets", json={
        "name": "隔离测试 SSH", "connector": "ssh", "host": "fixture.invalid", "username": "atr-reader",
        "auth_mode": "private_key", "credential_ref": "fixture-key", "host_public_key": "ssh-ed25519 AQIDBA==",
        "permission_boundary": "forced_command_gateway", "boundary_attestation": "synthetic approved boundary",
        "access_authorized": True,
    })
    assert response.status_code == 201, response.text
    target_id = response.json()["id"]
    event_id = incident(client, profile)

    class PausedExecutor:
        def submit(self, function):
            return None

    monkeypatch.setattr(jobs, "POOL", PausedExecutor())
    first = client.post(f"/api/v1/incidents/{event_id}/runs", json={"target_id": target_id})
    assert first.status_code == 202, first.text
    database = Database(settings.database_path)
    with database.transaction() as connection:
        row = connection.execute("SELECT config_json FROM runtime_targets WHERE id=?", (target_id,)).fetchone()
        config = json.loads(row["config_json"])
        config["access_authorized"] = False
        connection.execute("UPDATE runtime_targets SET config_json=? WHERE id=?", (json.dumps(config), target_id))
    denied = client.post(f"/api/v1/incidents/{event_id}/runs", json={"target_id": target_id})
    assert denied.status_code == 409 and "撤销" in denied.text
    settings.ssh_enabled = False


def test_historical_run_without_target_remains_queryable_as_unknown(client):
    profile = onboard(client)
    event_id = incident(client, profile)
    database = Database(get_settings().database_path)
    run_id = uid()
    with database.transaction() as connection:
        snapshot = connection.execute(
            "SELECT id FROM graph_snapshots WHERE system_id=? ORDER BY version DESC LIMIT 1",
            (profile["system_id"],),
        ).fetchone()["id"]
        connection.execute(
            "INSERT INTO diagnosis_runs(id,incident_id,graph_snapshot_id,mode,model_config_json,budget_json,started_at,status,summary_json) VALUES(?,?,?,'normal','{}','{}',?,'unresolved','{}')",
            (run_id, event_id, snapshot, now()),
        )
    response = client.get(f"/api/v1/runs/{run_id}")
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["target_id"] is None
    assert run["runtime_target"] is None
    assert run["target_binding_status"] == "unknown"
    assert client.get(f"/api/v1/incidents/{event_id}/runs").json()[0]["target_id"] is None
