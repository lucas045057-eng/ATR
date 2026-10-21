import json
from types import SimpleNamespace

import paramiko
import pytest
from app.connectors import credentials
from app.connectors.credentials import SSH_SESSIONS, CredentialUnavailable, SessionCredentials
from app.connectors.models import RuntimeTargetCreate, ToolRequest
from app.connectors.service import RuntimeService
from app.connectors.ssh import SSHConnector
from app.settings import get_settings
from app.storage.database import Database
from conftest import wait_job
from pydantic import SecretStr
from test_business import onboard

PASSWORD = "synthetic-ssh-session-unique-9E!"


def config(**changes):
    key = paramiko.RSAKey.generate(1024)
    return {"id": "isolated-target", **RuntimeTargetCreate(name="password fixture",
        connector="ssh", auth_mode="password", host="127.0.0.1", username="atrreader",
        host_public_key=f"{key.get_name()} {key.get_base64()}", access_authorized=True,
        permission_boundary="forced_command_gateway", boundary_attestation="隔离测试强制只读入口已核验",
    ).model_dump(), **changes}


def setup_target(client):
    profile = onboard(client)
    settings = get_settings()
    settings.ssh_enabled = True
    data = config()
    data.pop("id")
    base = f"/api/v1/businesses/{profile['business_id']}/targets"
    response = client.post(base, json=data)
    assert response.status_code == 201, response.text
    return base + "/" + response.json()["id"], profile


def test_session_scope_expiry_and_clear(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(credentials, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    store = SessionCredentials()
    cfg = config()
    cfg["credential_ref"] = store.put(cfg, "owner", SecretStr(PASSWORD))
    assert store.resolve(cfg, "owner").get_secret_value() == PASSWORD
    assert PASSWORD not in repr(store._entries)
    assert PASSWORD not in json.dumps(store.status(cfg, "owner"))
    for change in [{"id": "another"}, {"host": "other.invalid"}, {"port": 2222},
                   {"username": "someone"}, {"host_public_key": "different"}]:
        with pytest.raises(CredentialUnavailable):
            store.resolve({**cfg, **change}, "owner")
    with pytest.raises(CredentialUnavailable):
        store.resolve(cfg, "other-owner")
    clock[0] += credentials.TTL_SECONDS
    with pytest.raises(CredentialUnavailable):
        store.resolve(cfg, "owner")
    assert not store._entries
    cfg["credential_ref"] = store.put(cfg, "owner", SecretStr(PASSWORD))
    store.clear()
    assert store.status(cfg, "owner")["state"] == "required"


def test_replacement_and_capacity(monkeypatch):
    store = SessionCredentials()
    cfg = config()
    old = store.put(cfg, "owner", SecretStr(PASSWORD))
    new = store.put(cfg, "owner", SecretStr(PASSWORD))
    assert old != new and len(store._entries) == 1
    with pytest.raises(CredentialUnavailable):
        store.resolve({**cfg, "credential_ref": old}, "owner")
    for i in range(99):
        store.put({**cfg, "id": str(i)}, "owner", SecretStr(PASSWORD))
    with pytest.raises(PermissionError, match="上限"):
        store.put({**cfg, "id": "full"}, "owner", SecretStr(PASSWORD))


def test_explicit_clear_covers_concurrent_refresh_with_old_reference():
    store = SessionCredentials()
    cfg = config()
    cfg["credential_ref"] = store.put(cfg, "owner", SecretStr(PASSWORD))
    refreshed = {**cfg, "credential_ref": store.put(cfg, "owner", SecretStr(PASSWORD))}
    store.clear_target(cfg, "different-owner")
    assert store.status(refreshed, "owner")["state"] == "ready"
    store.clear_target(cfg, "owner")
    assert store.status(refreshed, "owner")["state"] == "required"


def test_api_async_password_and_no_persistence(client, monkeypatch, caplog):
    path, profile = setup_target(client)
    assert client.post(path + "/discover").status_code == 403
    response = client.post(path + "/credential", json={"password": PASSWORD})
    assert response.status_code == 200, response.text
    cfg = response.json()
    assert cfg["credential_status"]["state"] == "ready"
    assert PASSWORD not in response.text

    def read(self, tool, args):
        assert SSH_SESSIONS.resolve(self.config, self.settings.operator_id).get_secret_value() == PASSWORD
        return {"os": "synthetic Linux"}

    monkeypatch.setattr(SSHConnector, "read", read)
    result = client.post(path + "/tools", json={"tool_id": "host.info"})
    assert result.status_code == 202
    job = wait_job(client, result.json()["job_id"])
    assert job["result"]["payload"]["os"] == "synthetic Linux"
    database = Database(get_settings().database_path)
    with database.connect() as conn:
        assert PASSWORD not in "\n".join(conn.iterdump())
    assert PASSWORD not in caplog.text
    assert PASSWORD not in json.dumps(job)
    assert PASSWORD not in client.get(path.rsplit("/", 1)[0]).text
    client.delete(path + "/credential").raise_for_status()
    assert client.post(path + "/tools", json={"tool_id": "host.info"}).status_code == 403
    assert not SSH_SESSIONS._entries
    # Jobs queued before expiry still resolve at execution, with no fallback to keys.
    runtime = RuntimeService(database, get_settings())
    denied = runtime.execute(profile["business_id"], cfg["id"], ToolRequest(tool_id="host.info"))
    assert denied["status"] == "denied" and denied["error_code"] == "ssh_credential_unavailable"
    assert "重新输入密码" in denied["message"]


@pytest.mark.parametrize("body", [{"password": ""}, {"password": [PASSWORD]},
    {"password": PASSWORD + "\n"}, {"password": "X" * 1025}, {PASSWORD: PASSWORD},
    {"password": PASSWORD, "unexpected": PASSWORD}])
def test_invalid_password_body_is_never_echoed(client, body):
    path, profile = setup_target(client)
    response = client.post(path + "/credential", json=body)
    assert response.status_code == 422
    assert PASSWORD not in response.text
    assert "X" * 30 not in response.text


def test_authentication_and_target_bound_reference(client):
    path, profile = setup_target(client)
    assert client.post(path + "/credential", json={"password": PASSWORD}).status_code == 200
    cfg = client.post(path + "/credential", json={"password": PASSWORD}).json()
    data = config(credential_ref=cfg["credential_ref"])
    data.pop("id")
    assert client.post(path.rsplit("/", 1)[0], json=data).status_code == 403
    assert client.post(path.replace(profile["business_id"], "unrelated") + "/credential",
                       json={"password": PASSWORD}).status_code == 404


def test_root_rejected_even_for_old_unvalidated_configuration(client, monkeypatch):
    monkeypatch.setattr(paramiko.SSHClient, "connect", lambda *a, **k: pytest.fail("No network"))
    with pytest.raises(ValueError, match="root"):
        RuntimeTargetCreate(name="bad", connector="ssh", username="root")
    with pytest.raises(PermissionError, match="root"):
        SSHConnector(config(username="root"), get_settings()).read("host.info", {})


def test_password_only_strategy_and_no_retry(client, monkeypatch):
    path, profile = setup_target(client)
    cfg = client.post(path + "/credential", json={"password": PASSWORD}).json()
    calls = []

    def connect(self, *args, **kwargs):
        assert kwargs["allow_agent"] is False and kwargs["look_for_keys"] is False
        assert "key_filename" not in kwargs and "password" not in kwargs
        class Transport:
            def auth_password(self, username, password, fallback):
                assert password == PASSWORD and fallback is False
                calls.append(username)
                raise paramiko.AuthenticationException("untrusted-" + password)
        kwargs["auth_strategy"].authenticate(Transport())

    monkeypatch.setattr(paramiko.SSHClient, "connect", connect)
    runtime = RuntimeService(Database(get_settings().database_path), get_settings())
    result = runtime.execute(profile["business_id"],
                             cfg["id"], ToolRequest(tool_id="host.info"))
    assert result["error"] == "AuthenticationException" and len(calls) == 1
    assert PASSWORD not in json.dumps(result)
    assert SSH_SESSIONS.status(cfg, get_settings().operator_id)["state"] == "required"
    assert client.post(path + "/tools", json={"tool_id": "host.info"}).status_code == 403


def test_shutdown_clears_passwords(client):
    path, _ = setup_target(client)
    cfg = client.post(path + "/credential", json={"password": PASSWORD}).json()
    SSH_SESSIONS.clear()  # Same process lifecycle operation as startup/shutdown.
    assert SessionCredentials().status(cfg, get_settings().operator_id)["state"] == "required"
    assert client.post(path + "/discover").status_code == 403


def test_password_whitespace_is_preserved(client):
    path, _ = setup_target(client)
    response = client.post(path + "/credential", json={"password": "  " + PASSWORD + "  "})
    assert response.status_code == 200
    assert SSH_SESSIONS.resolve(response.json(), get_settings().operator_id).get_secret_value() == "  " + PASSWORD + "  "


def test_queued_job_expires_before_execution(client, monkeypatch):
    path, _ = setup_target(client)
    cfg = client.post(path + "/credential", json={"password": PASSWORD}).json()
    queued = []
    monkeypatch.setattr("app.api.business.submit", lambda db, job, fn: queued.append(fn))
    response = client.post(path + "/tools", json={"tool_id": "host.info"})
    assert response.status_code == 202 and len(queued) == 1
    deadline = SSH_SESSIONS._entries[cfg["credential_ref"]].deadline
    monkeypatch.setattr(credentials, "time", SimpleNamespace(monotonic=lambda: deadline + 1))
    monkeypatch.setattr(paramiko.SSHClient, "connect", lambda *a, **k: pytest.fail("Expired job must not connect"))
    denied = queued[0]()
    assert denied["status"] == "denied" and denied["error_code"] == "ssh_credential_unavailable"


def test_existing_private_key_target_cannot_accept_password(client):
    path, profile = setup_target(client)
    data = config(auth_mode="private_key", credential_ref="old-key")
    data.pop("id")
    created = client.post(path.rsplit("/", 1)[0], json=data).json()
    assert client.post(path.rsplit("/", 1)[0] + "/" + created["id"] + "/credential",
                       json={"password": PASSWORD}).status_code == 403
    with Database(get_settings().database_path).connect() as conn:
        assert PASSWORD not in "\n".join(conn.iterdump())


def test_investigation_context_never_receives_session_password(client, monkeypatch):
    from app.connectors.mock import MockConnector
    from test_v22_adaptive import investigate, model_stub, prepared_profile, report

    profile = prepared_profile(client, monkeypatch, store=False)
    settings = get_settings()
    settings.ssh_enabled = True
    runtime = RuntimeService(Database(settings.database_path), settings)
    with runtime.db.transaction() as tx:
        row = tx.execute("SELECT id,config_json FROM runtime_targets WHERE business_id=?", (profile["business_id"],)).fetchone()
        cfg = {**json.loads(row["config_json"]), **config(), "id": row["id"]}
        tx.execute("UPDATE runtime_targets SET config_json=? WHERE id=?",
                   (json.dumps({k: v for k, v in cfg.items() if k != "id"}), row["id"]))
    runtime.provide_password(profile["business_id"], row["id"], SecretStr(PASSWORD))

    def read(self, tool, args):
        assert SSH_SESSIONS.resolve(self.config, settings.operator_id).get_secret_value() == PASSWORD
        return MockConnector(self.config).read(tool, args)

    monkeypatch.setattr(SSHConnector, "read", read)
    contexts = model_stub(monkeypatch, lambda context, round_number: report())
    result = investigate(client, profile, mode="graph_adaptive")
    assert contexts and PASSWORD not in json.dumps(contexts)
    assert PASSWORD not in json.dumps(result)
    with runtime.db.connect() as conn:
        assert PASSWORD not in "\n".join(conn.iterdump())


def test_discovery_stops_after_authentication_failure(client, monkeypatch):
    path, _ = setup_target(client)
    client.post(path + "/credential", json={"password": PASSWORD}).raise_for_status()
    attempts = []

    def fail(self, *args, **kwargs):
        attempts.append(1)
        raise paramiko.AuthenticationException(PASSWORD)

    monkeypatch.setattr(paramiko.SSHClient, "connect", fail)
    job = client.post(path + "/discover").json()
    result = wait_job(client, job["job_id"])["result"]
    assert len(attempts) == len(result["observations"]) == 1
    assert result["collection_status"] == "failed"
    assert result["errors"][0]["error_code"] == "ssh_authentication_failed"
    assert PASSWORD not in json.dumps(result)


def test_expiry_during_connection_prevents_exec(client, monkeypatch):
    path, profile = setup_target(client)
    cfg = client.post(path + "/credential", json={"password": PASSWORD}).json()
    deadline = SSH_SESSIONS._entries[cfg["credential_ref"]].deadline
    clock = [deadline - 1]
    monkeypatch.setattr(credentials, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    closed = []

    class Channel:
        def settimeout(self, timeout):
            clock[0] = deadline + 1

        def exec_command(self, command):
            pytest.fail("Expired session must not execute")

        def close(self):
            closed.append("channel")

    class Transport:
        def open_session(self, timeout):
            return Channel()

        def close(self):
            closed.append("transport")

    def connect(self, *args, **kwargs):
        self._transport = Transport()

    monkeypatch.setattr(paramiko.SSHClient, "connect", connect)
    runtime = RuntimeService(Database(get_settings().database_path), get_settings())
    result = runtime.execute(profile["business_id"], cfg["id"], ToolRequest(tool_id="host.info"))
    assert result["error_code"] == "ssh_credential_unavailable"
    assert closed == ["channel", "transport"]
