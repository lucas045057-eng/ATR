import json
import shlex
from contextlib import contextmanager

import pytest
from app.connectors.models import RuntimeTargetCreate
from app.connectors.policy import SCHEMAS, check
from app.connectors.service import RuntimeService
from app.connectors.ssh import SSHConnector
from app.connectors.standard_ssh import (
    FILE_READER,
    IDENTITY_COMMANDS,
    IdentityRejected,
    StandardSSHConnector,
    command_for,
    identity,
    parse,
)
from app.settings import get_settings
from app.storage.database import Database
from test_ssh_password import PASSWORD, config


def standard(**changes):
    return {**config(), "execution_mode": "standard_ssh_readonly", "permission_boundary": "client_readonly", **changes}


def test_defaults_and_legacy_gateway():
    cfg = RuntimeTargetCreate(name="legacy").model_dump()
    assert cfg["execution_mode"] == "forced_gateway" and cfg["auth_mode"] == "private_key"
    assert cfg["docker_reads_enabled"] is True
    legacy = config()
    legacy.pop("execution_mode")
    SSHConnector(legacy, get_settings()).check_boundary()
    with pytest.raises(PermissionError):
        SSHConnector(standard(), get_settings()).check_boundary()
    with pytest.raises(PermissionError):
        StandardSSHConnector(config(), get_settings()).check_boundary()


@pytest.mark.parametrize("mode", ["forced_gateway", "standard_ssh_readonly"])
@pytest.mark.parametrize("auth", ["private_key", "password"])
def test_four_combinations_and_immutable_targets(client, mode, auth):
    settings = get_settings()
    settings.ssh_enabled = True
    profile = client.post("/api/v1/businesses", json={"name": "四种组合"}).json()
    base = f"/api/v1/businesses/{profile['business_id']}/targets"
    cfg = standard() if mode == "standard_ssh_readonly" else config()
    cfg.pop("id")
    cfg.update(auth_mode=auth, credential_ref="key-reference" if auth == "private_key" else "")
    r = client.post(base, json=cfg)
    assert r.status_code == 201, r.text
    target = r.json()
    assert target["execution_mode"] == mode
    path = base + "/" + target["id"]
    if auth == "password":
        response = client.post(path + "/credential", json={"password": PASSWORD})
        assert response.status_code == 200 and PASSWORD not in response.text
    assert client.patch(path, json={"execution_mode": "forced_gateway"}).status_code in {404, 405}
    with Database(settings.database_path).connect() as db:
        assert PASSWORD not in "\n".join(db.iterdump())


@pytest.mark.parametrize("tool,args", [
    ("shell.exec", {"command": "id"}), ("service.restart", {"name": "catalog.service"}),
    ("service.status", {"name": "catalog.service;touch /tmp/owned"}),
    ("service.status", {"name": "$(id)"}), ("service.status", {"name": "`id`"}),
    ("service.status", {"name": " a.service"}), ("service.status", {"name": "a.service\n"}),
    ("service.status", {"name": ""}), ("service.status", {"name": "a" * 129}),
    ("service.status", {"name": "-a.service"}), ("service.status", {"name": "other.service"}),
    ("service.logs", {"name": "catalog.service", "tail": "10"}),
    ("process.list", {"limit": True}), ("process.list", {"limit": 101}),
    ("process.list", {"name_hints": ["a|id"]}), ("process.list", {"name_hints": [" a"]}),
    ("host.info", {"command": "whoami"}), ("host.disk", {"path": "/"}),
    ("config.read", {"path": "/etc/shadow"}), ("config.read", {"path": "/srv/atr-fixture/../x"}),
    ("container.status", {"container_id": "f" * 64}),
])
def test_invalid_parameters_never_connect(tool, args, monkeypatch):
    monkeypatch.setattr(StandardSSHConnector, "connection", lambda *a: pytest.fail("Must reject before SSH"))
    with pytest.raises((ValueError, PermissionError)):
        StandardSSHConnector(standard(allowed_paths=["/srv/atr-fixture"], _approved={("service", "catalog.service")}), get_settings()).read(tool, args)


@pytest.mark.parametrize("index,value", [
    (0, "Darwin"), (1, "0"), (1, "-1"), (1, "1\n2"), (2, "root"), (2, "other"),
    (3, "0"), (4, "10003 0"), (5, "atrpassword sudo"), (5, "atrpassword docker"),
    (5, "atrpassword lxd"), (5, "atrpassword wheel"), (5, ""),
])
def test_high_privileges_and_bad_identity(index, value):
    values = ["Linux", "10003", "atrpassword", "10003", "10003 10009", "atrpassword readers"]
    values[index] = value
    with pytest.raises(IdentityRejected):
        identity(values, "atrpassword")


def test_identity_is_a_precheck_not_readonly_proof():
    result = identity(["Linux", "10003", "atrpassword", "10003", "10003 10009", "atrpassword readers"], "atrpassword")
    assert result["precheck"] == "passed" and result["server_enforced_readonly"] is False
    assert result["primary_group"] == "atrpassword" and "ACL" in result["warning"]


def test_all_registered_read_tools_have_fixed_commands():
    cfg = standard(_approved={("service", "catalog.service"), ("container", "a" * 64)}, allowed_paths=["/srv/atr-fixture"])
    for tool in SCHEMAS:
        arguments = {"name": "catalog.service"} if tool.startswith("service.") and tool not in {"service.list"} else (
            {"path": "/srv/atr-fixture/app.conf"} if tool.startswith("config.") else
            {"container_id": "a" * 64} if tool in {"container.status", "container.logs"} else {})
        args = check(cfg, tool, arguments, cfg["_approved"])
        argv = shlex.split(command_for(tool, args))
        assert argv[0].startswith("/usr/bin/") and argv[0] not in {"/usr/bin/bash", "/usr/bin/sudo"}
        assert "atr-read-v1" not in argv
    assert shlex.split(command_for("config.read", {})) == ["/usr/bin/python3", "-I", "-c", FILE_READER]


@pytest.mark.parametrize("tool,raw", [
    ("host.info", b"not linux"), ("host.memory", b"Mem: invalid"), ("host.disk", b"no disks"),
    ("process.list", b"nonsense"), ("service.list", b"bad"), ("network.listeners", b"header\nbad"),
    ("host.info", b"Linux\x00 x y z"), ("config.read", b"[]"),
])
def test_unparseable_output_is_not_evidence(tool, raw):
    with pytest.raises(ValueError):
        parse(tool, raw, {"path": "/srv/a", "name": "catalog.service"})


def test_identity_check_precedes_tools_and_each_call(monkeypatch):
    captured = []
    values = [b"Linux", b"10003", b"atrpassword", b"10003", b"10003", b"atrpassword"]
    @contextmanager
    def connection(self):
        yield object()
    def receive(self, client, command, stdin=None, **kwargs):
        captured.append(command)
        if command in IDENTITY_COMMANDS:
            return values[IDENTITY_COMMANDS.index(command)]
        return b"Linux host 6.1 x86_64\n"
    monkeypatch.setattr(StandardSSHConnector, "connection", connection)
    monkeypatch.setattr(StandardSSHConnector, "receive", receive)
    remote = StandardSSHConnector(standard(username="atrpassword"), get_settings())
    for _ in range(2):
        assert remote.read("host.info", {})["identity"]["precheck"] == "passed"
    assert captured == [*IDENTITY_COMMANDS, "/usr/bin/uname -s -n -r -m"] * 2
    values[1] = b"0"
    with pytest.raises(IdentityRejected):
        remote.read("host.info", {})
    assert captured[-6:] == list(IDENTITY_COMMANDS)


def test_manual_observations_enter_existing_graph(client, monkeypatch):
    from conftest import wait_job
    settings = get_settings()
    settings.ssh_enabled = True
    business = client.post("/api/v1/businesses", json={"name": "运行观测图谱"}).json()
    base = f"/api/v1/businesses/{business['business_id']}"
    cfg = standard()
    cfg.pop("id")
    target = client.post(base + "/targets", json=cfg).json()
    client.post(base + f"/targets/{target['id']}/credential", json={"password": PASSWORD}).raise_for_status()
    account = identity(["Linux", "10003", "atrreader", "10003", "10003", "atrreader"], "atrreader")
    monkeypatch.setattr(StandardSSHConnector, "read", lambda self, tool, args: {
        "execution_mode": "standard_ssh_readonly", "identity": account, "os": "Linux"})
    job = client.post(base + f"/targets/{target['id']}/tools", json={"tool_id": "host.info"}).json()
    result = wait_job(client, job["job_id"])["result"]
    assert "payload" in result
    profile = client.get(base).json()
    graph = client.get(f"/api/v1/systems/{profile['system_id']}/graph").json()
    host = next(n for n in graph["nodes"] if n["kind"] == "host" and n["name"] == target["id"])
    assert host["attributes"]["observation_id"] == result["observation_id"]
    assert host["attributes"]["server_enforced_readonly"] is False


def test_agent_uses_authorized_standard_tools_and_keeps_scope_gate(client, monkeypatch):
    from app.connectors.mock import MockConnector
    from test_v22_adaptive import (
        candidate,
        investigate,
        model_stub,
        prepared_profile,
        proposal,
        report,
    )
    profile = prepared_profile(client, monkeypatch, store=False)
    settings = get_settings()
    settings.ssh_enabled = True
    runtime = RuntimeService(Database(settings.database_path), settings)
    with runtime.db.transaction() as tx:
        row = tx.execute("SELECT id,config_json FROM runtime_targets WHERE business_id=?", (profile["business_id"],)).fetchone()
        cfg = {**json.loads(row["config_json"]), **standard(), "id": row["id"]}
        cfg["allowed_paths"] = ["/srv/catalog"]
        tx.execute("UPDATE runtime_targets SET config_json=? WHERE id=?", (json.dumps({k: v for k, v in cfg.items() if k != "id"}), row["id"]))
    runtime.provide_password(profile["business_id"], row["id"], __import__("pydantic").SecretStr(PASSWORD))
    reads = []
    def read(self, tool, args):
        reads.append(tool)
        return {**MockConnector(self.config).read(tool, args), "connector": "ssh", "execution_mode": "standard_ssh_readonly"}
    monkeypatch.setattr(StandardSSHConnector, "read", read)
    def reply(context, n):
        assert not any(a.get("service") == "store" for a in context["available_actions"])
        if n == 1:
            return report(action=proposal(candidate(context, "config.list")))
        return report(action={"action_id": "injected-shell-command", "rationale": "touch /tmp/owned"})
    contexts = model_stub(monkeypatch, reply)
    result = investigate(client, profile, mode="graph_adaptive")
    assert reads == ["service.status", "service.logs", "config.list"]
    assert result["status"] == "unresolved" and result["summary"]["stop_reason"] == "model_schema_error"
    assert PASSWORD not in json.dumps(contexts) and PASSWORD not in json.dumps(result)
    assert all("shell" not in call["tool_id"] for call in result["tool_calls"])
