import pytest
from app.connectors.models import RuntimeTargetCreate
from app.connectors.ssh import SSHConnector
from app.settings import Settings
from app.storage.database import Database
from conftest import wait_job
from test_business import onboard


def target(client, profile, scenario="mixed", docker=False):
    response = client.post(
        f"/api/v1/businesses/{profile['business_id']}/targets",
        json={
            "name": "隔离 Mock",
            "connector": "mock",
            "allowed_paths": ["/srv/catalog"],
            "mock_scenario": scenario,
            "docker_reads_enabled": docker,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def discover(client, profile, target):
    base = f"/api/v1/businesses/{profile['business_id']}"
    response = client.post(base + f"/targets/{target['id']}/discover")
    assert response.status_code == 202, response.text
    job = wait_job(client, response.json()["job_id"])
    assert job["status"] == "completed", job
    return job["result"]


def read(client, profile, target, tool, args=None):
    base = f"/api/v1/businesses/{profile['business_id']}"
    response = client.post(
        base + f"/targets/{target['id']}/tools", json={"tool_id": tool, "arguments": args or {}}
    )
    assert response.status_code == 202, response.text
    job = wait_job(client, response.json()["job_id"])
    assert job["status"] == "completed", job
    return job["result"]


def test_discovery_scope_review_and_unrelated_objects(client):
    profile = onboard(client)
    t = target(client, profile)
    result = discover(client, profile, t)
    assert result["connector"] == "mock"
    assert "unrelated-backup.service" in result["unassigned_runtime_objects"]
    assert "store" in result["unmatched_services"]
    assert all(m["verification_status"] == "pending" for m in result["matches"])
    assert (
        read(client, profile, t, "service.logs", {"name": "catalog-api.service"})["status"]
        == "denied"
    )
    binding = next(m for m in result["matches"] if m["kind"] == "service")
    response = client.post(
        f"/api/v1/businesses/{profile['business_id']}/bindings/{binding['id']}/review",
        json={"decision": "confirm", "reason": "人工核对业务服务名与部署端口"},
    )
    assert response.status_code == 200, response.text
    logs = read(client, profile, t, "service.logs", {"name": "catalog-api.service"})
    assert "connection refused" in logs["payload"]["text"]
    assert (
        read(client, profile, t, "service.logs", {"name": "unrelated-backup.service"})["status"]
        == "denied"
    )
    graph = client.get(f"/api/v1/systems/{profile['system_id']}/graph").json()
    assert any(e["relation"] == "observed_as" for e in graph["edges"])
    assert any(n["kind"] == "host" for n in graph["nodes"])


@pytest.mark.parametrize(
    "tool,args",
    [
        ("shell.exec", {"command": "touch /srv/business"}),
        ("service.logs", {"name": "api; rm -rf /"}),
        ("config.read", {"path": "/etc/shadow"}),
        ("container.list", {}),
    ],
)
def test_denied_tools_are_audited(client, tool, args):
    profile = onboard(client)
    t = target(client, profile)
    result = read(client, profile, t, tool, args)
    assert result["status"] in {"denied", "failed"}
    db = Database(
        __import__("app.settings", fromlist=["get_settings"]).get_settings().database_path
    )
    conn = db.connect()
    try:
        assert (
            conn.execute(
                "SELECT status FROM runtime_observations WHERE id=?", (result["observation_id"],)
            ).fetchone()[0]
            == result["status"]
        )
    finally:
        conn.close()


@pytest.mark.parametrize(
    "scenario,status",
    [("permission_denied", "denied"), ("timeout", "timed_out"), ("disconnect", "failed")],
)
def test_mock_errors_and_timeouts(client, scenario, status):
    profile = onboard(client)
    t = target(client, profile, scenario)
    assert read(client, profile, t, "host.info")["status"] == status


def test_document_runtime_conflict_preserves_both_sources(client):
    profile = onboard(client)
    t = target(client, profile, "datastore_conflict")
    read(client, profile, t, "config.read", {"path": "/srv/catalog/app.conf"})
    current = client.get(f"/api/v1/businesses/{profile['business_id']}").json()
    assert current["conflicts"][0]["status"] == "open"
    ids = current["conflicts"][0]["fact_ids"]
    facts = [f for f in current["facts"] if f["id"] in ids]
    assert {f["source_type"] for f in facts} == {"Static", "Runtime"}
    assert {f["object"] for f in facts} == {"MySQL", "PostgreSQL"}
    assert current["datastores"][0]["name"] == "MySQL"


def test_ssh_rejects_unverified_key_before_connect(monkeypatch):
    monkeypatch.setattr(
        "paramiko.SSHClient.connect", lambda *a, **k: pytest.fail("Must not connect")
    )
    config = RuntimeTargetCreate(
        name="test",
        connector="ssh",
        access_authorized=True,
        permission_boundary="forced_command_gateway",
        boundary_attestation="已核对受限用户与强制只读入口设置",
        host="test.invalid",
    ).model_dump()
    with pytest.raises(PermissionError, match="主机公钥"):
        SSHConnector(config, Settings(ssh_enabled=True)).read("host.info", {})


def test_ssh_not_enabled_and_global_scan_paths_rejected(client):
    profile = onboard(client)
    base = f"/api/v1/businesses/{profile['business_id']}/targets"
    assert client.post(base, json={"name": "bad", "allowed_paths": ["/"]}).status_code == 422
    assert (
        client.post(
            base, json={"name": "bad", "connector": "ssh", "host": "test.invalid"}
        ).status_code
        == 403
    )
