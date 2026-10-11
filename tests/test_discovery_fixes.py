import json
import shlex

import pytest
from app.connectors.diagnostics import CapabilityUnavailable, ToolParseError
from app.connectors.docker_reader import parse_list
from app.connectors.models import ListArgs, RuntimeTargetCreate, ServiceArgs, ToolRequest
from app.connectors.service import RuntimeService
from app.connectors.standard_ssh import StandardSSHConnector, command_for, parse
from app.settings import get_settings
from app.storage.database import Database


@pytest.mark.parametrize("name", ["catalog.service", "worker@tenant:blue.service", r"worker@tenant\x2dblue.service",
                                  "x" * 247 + ".service", "-a.service", ".hidden.service"])
def test_unit_schema_parser_and_cursor_agree(name):
    assert ServiceArgs(name=name).name == name
    raw = (name + " loaded active running Synthetic service\n").encode()
    item = parse("service.list", raw, {"limit": 100})["services"][0]
    assert item["id"] == name and item["load"] == "loaded" and item["sub"] == "running"
    assert item["description"] == "Synthetic service"
    assert ListArgs(cursor="1:" + name).cursor == "1:" + name
    assert shlex.split(command_for("service.status", {"name": name}))[-1] == name


@pytest.mark.parametrize("name", ["a.service;id", "$(id).service", r"a\q.service", "a.service\n", "-a", "a@b@c.service"])
def test_unit_injection_denied(name):
    with pytest.raises(ValueError):
        ServiceArgs(name=name)


def test_service_partial_does_not_make_bad_line_actionable():
    raw = b"a.service loaded active running First\nbad sensitive-secret\nb.service loaded inactive dead Second\n"
    result = parse("service.list", raw, {})
    assert result["partial"] and result["invalid_records"] == 1 and result["invalid_line_numbers"] == [2]
    assert [x["id"] for x in result["services"]] == ["a.service", "b.service"]
    assert "sensitive-secret" not in json.dumps(result)
    with pytest.raises(ToolParseError) as e:
        parse("service.list", b"bad\n" * 17, {})
    assert e.value.invalid_item_count == 17


def test_runtime_safe_diagnostics_survive_database(client, monkeypatch):
    settings = get_settings()
    settings.ssh_enabled = True
    profile = client.post("/api/v1/businesses", json={"name": "安全错误验收"}).json()
    db = Database(settings.database_path)
    runtime = RuntimeService(db, settings)
    target = runtime.create(profile["business_id"], RuntimeTargetCreate(name="fixture", connector="ssh",
        host="fixture", username="reader", credential_ref="test", host_public_key="ssh-ed25519 AAAA",
        execution_mode="standard_ssh_readonly", permission_boundary="client_readonly", access_authorized=True,
        boundary_attestation="Isolated fixture authorization"))
    def read(*args):
        raise ToolParseError(line_number=3, invalid_item_count=2, output_bytes=128)
    monkeypatch.setattr(StandardSSHConnector, "read", read)
    r = runtime.execute(profile["business_id"], target["id"], ToolRequest(tool_id="service.list"))
    assert r["error_code"] == "tool_parse_error" and r["failure_stage"] == "parse" and r["line_number"] == 3
    assert r["bytes"] == 128 and r["retryable"] is False and r["status"] == "failed"
    with db.connect() as c:
        row = c.execute("SELECT * FROM runtime_observations WHERE id=?", (r["observation_id"],)).fetchone()
        assert json.loads(row["diagnostics_json"]) == r["diagnostic"] and row["payload_json"] == "{}"


def test_restricted_reader_formats_and_safe_partial():
    for separator in ("\t", "  ", " "):
        raw = (separator.join(["catalog-api", "catalog:fixture", "Up 2 hours"]) + "\n").encode()
        r = parse_list(raw, {})
        assert r["containers"][0]["id"] == "catalog-api" and r["containers"][0]["identity_kind"] == "name"
        assert r["capabilities"] == ["container.list"]
    r = parse_list(b"NAMES IMAGE STATUS\na catalog:fixture Up 1 hour\nb;id bad secret\n", {})
    assert r["partial"] and len(r["containers"]) == 1 and "secret" not in json.dumps(r)
    with pytest.raises(ToolParseError):
        parse_list(b"secret unknown incompatible\n", {})


def test_restricted_no_fallback_or_extra_arguments(client, monkeypatch):
    from test_standard_ssh import standard
    cfg = standard(docker_reader="restricted_list", _approved={("container", "a" * 64)})
    remote = StandardSSHConnector(cfg, get_settings())
    monkeypatch.setattr(remote, "connection", lambda: pytest.fail("Must not connect for unsupported capability"))
    for tool in ("container.status", "container.logs"):
        with pytest.raises(CapabilityUnavailable):
            remote.read(tool, {"container_id": "a" * 64})
    with pytest.raises(ValueError):
        remote.read("container.list", {"command": "sudo docker ps"})


def test_process_state_and_bounded_pagination():
    raw = "\n".join(f"{i} worker S" for i in range(1, 151)).encode()
    r = parse("process.list", raw, {"limit": 100})
    assert len(r["processes"]) == 100 and r["scanned"] == 150 and r["partial"]
    assert r["processes"][0]["state"] == "S" and isinstance(r["processes"][0]["pid"], int)
    second = parse("process.list", raw, {"limit": 100, "cursor": r["next_cursor"]})
    assert len(second["processes"]) == 50
    assert parse("process.list", b"1 legacy\n", {})["processes"][0]["state"] == "unknown"
