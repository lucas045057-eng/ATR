"""Opt-in real local Docker + pinned OpenSSH + API/Worker; Fake Provider is explicit."""

import json
import os
import socket
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import docker
import httpx
import paramiko
import pytest
from app.connectors.ssh import SSHConnector
from app.settings import Settings, get_settings
from conftest import wait_job
from dotenv import dotenv_values
from test_business import onboard
from test_model_isolation import configure_client, fake_model  # noqa: F401
from test_runtime import discover
from test_v22_adaptive import proposal, report

ROOT = Path(__file__).resolve().parents[2]
PROFILE = {
    "src/api.py": (ROOT / "tests/fixtures/final-e2e/api.py").read_text(encoding="utf-8"),
    "compose.yaml": "services:\n  catalog-api: {build: ., ports: ['8000:8000'], depends_on: [store]}\n  store: {image: postgres:16-alpine, expose: [5432]}",
    "README.md": "# Catalog\nAn isolated API with a PostgreSQL dependency. Verify current evidence before concluding.\n",
}


def investigate_fixture(client, profile, target_id, window_start):
    incident = client.post(
        "/api/v1/incidents",
        json={
            "system_id": profile["system_id"],
            "symptom": "catalog-api startup/dependency investigation; identify the underlying cause if failure is observed, rather than only confirming the symptom. Inspect current runtime and reviewed configuration; do not invent a fault.",
            "mode": "graph_adaptive",
            "window_start": window_start,
        },
    )
    assert incident.status_code == 200
    response = client.post(
        f"/api/v1/incidents/{incident.json()['id']}/runs", json={"target_id": target_id}
    )
    assert response.status_code == 202, response.text
    job = wait_job(
        client,
        response.json()["job_id"],
        timeout=1850 if os.environ.get("ATR_FINAL_E2E_PROVIDER") == "real" else 120,
    )
    assert job["status"] == "completed", job
    return client.get(f"/api/v1/runs/{job['result']['id']}").json()


@pytest.fixture
def actual_runtime(tmp_path):
    engine = docker.from_env()
    prefix = "atr-final-e2e-" + uuid.uuid4().hex[:8]
    label = {"atr.final_e2e": prefix}
    network = engine.networks.create(prefix, labels=label)
    containers = []
    keys = tmp_path / "fixture"
    keys.mkdir()
    host_key, client_key = paramiko.RSAKey.generate(2048), paramiko.RSAKey.generate(2048)
    host_key.write_private_key_file(str(keys / "host_key"))
    client_key.write_private_key_file(str(keys / "client_key"))
    (keys / "client.pub").write_text(client_key.get_name() + " " + client_key.get_base64())

    def create_api(port):
        result = engine.containers.run(
            "python:3.12-slim",
            ["python", "-B", "/fixture.py"],
            name=prefix + "-catalog-api-" + str(port),
            detach=True,
            network=network.name,
            environment={"DB_HOST": "store", "DB_PORT": str(port)},
            labels={
                **label,
                "com.docker.compose.service": "catalog-api",
                "com.docker.compose.project": prefix,
            },
            volumes={
                str(ROOT / "tests/fixtures/final-e2e/api.py"): {"bind": "/fixture.py", "mode": "ro"}
            },
            ports={"8000/tcp": ("127.0.0.1", 0)},
            user="10002:10002",
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            mem_limit="64m",
            pids_limit=32,
        )
        containers.append(result)
        return result

    def ready_db(database):
        for _ in range(60):
            check = database.exec_run(["pg_isready", "-h", "127.0.0.1", "-U", "postgres"])
            if check.exit_code == 0:
                query = database.exec_run(
                    ["psql", "-h", "127.0.0.1", "-U", "postgres", "-Atc", "SELECT 1"]
                )
                if query.exit_code == 0 and query.output.strip() == b"1":
                    return
            time.sleep(0.25)
        raise AssertionError("Isolated PostgreSQL did not become ready")

    try:
        database = engine.containers.run(
            "postgres:16-alpine",
            detach=True,
            name=prefix + "-store",
            network=network.name,
            hostname="store",
            environment={
                "POSTGRES_HOST_AUTH_METHOD": "trust",
                "PGDATA": "/var/lib/postgresql/data/pgdata",
            },
            labels={
                **label,
                "com.docker.compose.service": "store",
                "com.docker.compose.project": prefix,
            },
            user="postgres",
            tmpfs={
                "/var/lib/postgresql/data": "rw,mode=1777",
                "/var/run/postgresql": "rw,mode=1777",
                "/tmp": "rw",
            },
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            mem_limit="256m",
            pids_limit=64,
        )
        containers.append(database)
        network.disconnect(database)
        network.connect(database, aliases=["store"])
        ready_db(database)
        yield {
            "engine": engine,
            "prefix": prefix,
            "containers": containers,
            "network": network,
            "database": database,
            "keys": keys,
            "host_key": host_key,
            "create_api": create_api,
            "ready_db": ready_db,
        }
    finally:
        for container in reversed(containers):
            container.reload()
            assert (
                container.name.startswith(prefix)
                and container.labels.get("atr.final_e2e") == prefix
            )
            assert all(m["Type"] != "volume" for m in container.attrs["Mounts"])
            container.remove(force=True)
        assert network.attrs["Labels"].get("atr.final_e2e") == prefix
        network.remove()
        engine.close()


def start_gateway(runtime, api):
    keys, database = runtime["keys"], runtime["database"]
    (keys / "policy.json").write_text(
        json.dumps(
            {
                "services": [],
                "containers": [api.id, database.id],
                "allowed_paths": ["/srv/final-reviewed"],
                "docker_reads_enabled": True,
            }
        )
    )
    (keys / "deployment.reviewed.json").write_text(
        json.dumps(
            {
                "source": "administrator-reviewed configuration snapshot",
                "api_dependency_host": "store",
                "api_dependency_port": int(
                    next(x for x in api.attrs["Config"]["Env"] if x.startswith("DB_PORT=")).split(
                        "=", 1
                    )[1]
                ),
                "database_service_port": 5432,
            }
        )
    )
    gateway = runtime["engine"].containers.run(
        "atr-final-e2e-gateway:20261012",
        detach=True,
        name=runtime["prefix"] + "-gateway",
        labels={"atr.final_e2e": runtime["prefix"]},
        ports={"2222/tcp": ("127.0.0.1", 0)},
        network=runtime["network"].name,
        volumes={
            str(keys): {"bind": "/fixture", "mode": "ro"},
            "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "ro"},
        },
        mem_limit="128m",
        pids_limit=64,
    )
    runtime["containers"].append(gateway)
    gateway.reload()
    port = int(gateway.attrs["NetworkSettings"]["Ports"]["2222/tcp"][0]["HostPort"])
    for _ in range(60):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    else:
        raise AssertionError("Isolated OpenSSH did not start")
    return {
        "name": "isolated gateway",
        "connector": "ssh",
        "host": "127.0.0.1",
        "port": port,
        "username": "atrreader",
        "credential_ref": "final-fixture",
        "host_public_key": runtime["host_key"].get_name() + " " + runtime["host_key"].get_base64(),
        "access_authorized": True,
        "permission_boundary": "forced_command_gateway",
        "boundary_attestation": "Verified isolated low-privilege account and pinned ForceCommand gateway",
        "execution_mode": "forced_gateway",
        "allowed_paths": ["/srv/final-reviewed"],
        "docker_reads_enabled": True,
        "max_calls": 100,
        "max_output_bytes": 48000,
        "timeout_seconds": 10,
    }


def observation_driven_reply(context, _):
    # Reads only the same observations visible to a model; never receives the case/truth.
    node = next(n for n in context["system_graph"] if n["name"] == "catalog-api")
    evidence = context["current_evidence"]
    configs = [e for e in evidence if e["tool_id"] == "config.read"]
    actions = context["available_actions"]
    if not configs:
        action = next(
            (
                a
                for tool in ("container.status", "container.logs", "config.list", "config.read")
                for a in actions
                if a["tool_id"] == tool
                and (a.get("service") == "store" or tool.startswith("config."))
            ),
            None,
        )
        return report(action=proposal(action) if action else None)
    observed = json.loads(configs[-1]["payload"]["text"])
    if observed["api_dependency_port"] == observed["database_service_port"]:
        return report(
            summary="No established fault; status and logs alone do not prove complete business health."
        )
    links = [e for e in evidence if e["target_node_id"] == node["id"] or e["tool_id"] == "container.logs"]
    hypothesis = {
        "target_node_id": node["id"],
        "target_role": node["role"],
        "failure_mode": "startup_failure",
        "causal_chain": [
            f"Configured API database port {observed['api_dependency_port']} differs from datastore port {observed['database_service_port']}",
            "API database connection is refused",
            "API startup exits",
        ],
        "prediction": "API remains exited in this incident window",
        "rationale": "Current configuration, startup log and independent container state must corroborate.",
        "evidence_links": [
            {
                "evidence_id": e["evidence_id"],
                "relation": "supports",
                "explanation": "Current acquisition corroborates the startup failure.",
            }
            for e in links
        ],
        "missing_evidence": [],
        "confidence": 0.8,
        "verification_predictions": [
            {
                "target_node_id": node["id"],
                "tool_id": "container.status",
                "field": "state",
                "operator": "eq",
                "value": "exited",
            }
        ],
    }
    return report(
        [hypothesis],
        summary="API startup failed because its configured database port differs from the healthy database service port.",
    )


@pytest.mark.parametrize("dependency_port", [5432, 25432])
def test_actual_docker_ssh_worker_fault_and_recovery(
    client,
    monkeypatch,
    fake_model,  # noqa: F811
    actual_runtime,
    dependency_port,
):  # noqa: F811
    runtime = actual_runtime
    window_start = datetime.now(timezone.utc).isoformat()
    api = runtime["create_api"](dependency_port)
    for _ in range(60):
        api.reload()
        if dependency_port == 25432 and api.status == "exited":
            break
        if dependency_port == 5432 and b"ready:" in api.logs():
            break
        time.sleep(0.1)
    assert api.status == ("exited" if dependency_port == 25432 else "running")
    if dependency_port == 5432:
        port = api.attrs["NetworkSettings"]["Ports"]["8000/tcp"][0]["HostPort"]
        assert httpx.get("http://127.0.0.1:" + port + "/health").status_code == 200
    config = start_gateway(runtime, api)
    settings = get_settings()
    settings.ssh_enabled = True
    settings.investigation_input_tokens = (
        20000  # Same existing production budget, no change to .env.
    )
    settings.ssh_credentials = json.dumps({"final-fixture": str(runtime["keys"] / "client_key")})
    profile = onboard(client, PROFILE)
    base = f"/api/v1/businesses/{profile['business_id']}"
    response = client.post(base + "/targets", json=config)
    assert response.status_code == 201, response.text
    target = response.json()
    inventory = discover(client, profile, target)
    bindings = [
        b
        for b in inventory["matches"]
        if b["kind"] == "container" and b["business_service"] in ("catalog-api", "store")
    ]
    assert len(bindings) == 2, inventory
    for binding in bindings:
        assert (
            client.post(
                base + f"/bindings/{binding['id']}/review",
                json={
                    "decision": "confirm",
                    "reason": "Verified isolated Docker IDs and service labels",
                },
            ).status_code
            == 200
        )
    current = client.get(base).json()
    reviewed = client.post(
        base + "/review",
        json={
            "expected_version": current["profile_version"],
            "decision": "approve",
            "reason": "Reviewed only disposable fixture business and confirmed containers",
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    profile = reviewed.json()
    remote = SSHConnector({**config, "id": target["id"]}, settings)
    listed = remote.read("container.list", {})
    assert {api.id, runtime["database"].id} <= {c["id"] for c in listed["containers"]}
    with pytest.raises(PermissionError):
        remote.read("container.status", {"container_id": "a" * 64})
    with pytest.raises(PermissionError):
        remote.read("config.read", {"path": "/etc/passwd"})
    _, model_settings, provider, _ = fake_model
    real_provider = os.environ.get("ATR_FINAL_E2E_PROVIDER") == "real"
    if real_provider:
        assert dependency_port == 25432, "One explicitly selected isolated fault Run only"
        configured = dotenv_values(ROOT / ".env")
        fields = (
            "MODEL_NAME",
            "MODEL_API_KEY",
            "MODEL_BASE_URL",
            "MODEL_TIMEOUT_SECONDS",
            "MODEL_WIRE_API",
            "MODEL_REASONING_EFFORT",
            "INVESTIGATION_INPUT_TOKENS",
            "BUDGET_TOTAL_TOKENS",
        )
        model_settings = Settings(**{key.lower(): configured[key] for key in fields if configured.get(key)})
        assert (
            model_settings.model_name == "gpt-6.1-sol"
            and model_settings.model_timeout_seconds == 180
        )
        assert model_settings.model_reasoning_effort == "low"
        settings.investigation_input_tokens = model_settings.investigation_input_tokens
        settings.budget_total_tokens = model_settings.budget_total_tokens
    else:
        model_settings.model_timeout_seconds = 15
        model_settings.model_reasoning_effort = "low"
        provider["reply"] = observation_driven_reply
    corrections = []
    correction_enabled = os.environ.get("ATR_FINAL_E2E_CORRECT_ONCE") == "1"
    if correction_enabled:
        from app.context import model
        from bounded_decision_experiment import one_scoped_correction

        monkeypatch.setattr(model, "call_json", one_scoped_correction(model.call_json, corrections))
        if not real_provider:
            injected = False

            def invalid_once(context, number):
                nonlocal injected
                reply = observation_driven_reply(context, number)
                if reply["hypotheses"] and not injected:
                    injected = True
                    item = reply["hypotheses"][0]
                    dependency = next(e for e in context["current_evidence"] if e["target_node_id"] != item["target_node_id"])
                    item["evidence_links"].append({"evidence_id": dependency["evidence_id"],
                        "relation": "refutes", "explanation": "Synthetic deliberately invalid cross-target relation"})
                return reply

            provider["reply"] = invalid_once
    configure_client(model_settings)
    settings.model_wire_api = model_settings.model_wire_api
    settings.model_reasoning_effort = model_settings.model_reasoning_effort
    result = investigate_fixture(client, profile, target["id"], window_start)
    output = os.environ.get("ATR_FINAL_E2E_OUTPUT")
    if output:
        path = Path(output)
        path.mkdir(parents=True, exist_ok=True)
        name = "real-isolated-trial.json" if real_provider else f"isolated-{dependency_port}.json"
        (path / name).write_text(
            json.dumps(
                {
                    "provider": "real gpt-6.1-sol"
                    if real_provider
                    else "Fake Provider; not model-intelligence proof",
                    "truth": {"dependency_port": dependency_port, "database_port": 5432},
                    "run_id": result["id"],
                    "target_id": target["id"],
                    "status": result["status"],
                    "summary": result["summary"],
                    "budget": result["context_budget"],
                    "tools": result["tool_calls"],
                    "experimental_semantic_correction": correction_enabled,
                    "semantic_corrections": corrections,
                    "recovery_http_status": None,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    if dependency_port == 25432:
        recovered = runtime["create_api"](5432)
        for _ in range(60):
            recovered.reload()
            if b"ready:" in recovered.logs():
                break
            time.sleep(0.1)
        port = recovered.attrs["NetworkSettings"]["Ports"]["8000/tcp"][0]["HostPort"]
        assert httpx.get("http://127.0.0.1:" + port + "/health").status_code == 200
        runtime["ready_db"](runtime["database"])
    if output:
        path = Path(output)
        path.mkdir(parents=True, exist_ok=True)
        (
            path
            / ("real-isolated-trial.json" if real_provider else f"isolated-{dependency_port}.json")
        ).write_text(
            json.dumps(
                {
                    "provider": "real gpt-6.1-sol"
                    if real_provider
                    else "Fake Provider; not model-intelligence proof",
                    "truth": {"dependency_port": dependency_port, "database_port": 5432},
                    "run_id": result["id"],
                    "target_id": target["id"],
                    "status": result["status"],
                    "summary": result["summary"],
                    "budget": result["context_budget"],
                    "tools": result["tool_calls"],
                    "experimental_semantic_correction": correction_enabled,
                    "semantic_corrections": corrections,
                    "recovery_http_status": 200,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    assert result["status"] == ("supported" if dependency_port == 25432 else "unresolved"), result[
        "summary"
    ]
    assert len(result["summary"]["iterations"]) >= (2 if real_provider else 3)
    assert result["context_budget"]["calls"] <= 8
    assert len(corrections) <= 1
    assert all(c["status"] == "succeeded" and c["target_id"] == target["id"] for c in result["tool_calls"])
    if not real_provider:
        assert result["context_budget"]["calls"] == provider["requests"]
        assert {"container.status", "container.logs", "config.list", "config.read"} <= {
            c["tool_id"] for c in result["tool_calls"]
        }
    if dependency_port == 25432:
        assert result["summary"]["stop_reason"] == "evidence_supported"
        assert result["summary"]["gate"]["hypotheses"][0]["verification"]["mechanism_verified"]
        root = next(h for h in result["summary"]["gate"]["hypotheses"] if h["status"] == "supported")
        diagnosis = json.dumps(root["causal_chain"] + [root["rationale"]], ensure_ascii=False)
        assert "25432" in diagnosis and "5432" in diagnosis, "Confirmed startup failure alone does not prove the injected port-mismatch root cause"
        assert {"config.read", "container.logs"} <= {t["tool_id"] for t in result["tool_calls"]}
        db_log_calls = {t["id"] for t in result["tool_calls"] if t["tool_id"] == "container.logs"
                        and json.loads(t["args_redacted_json"]).get("container_id") == runtime["database"].id}
        config_calls = {t["id"] for t in result["tool_calls"] if t["tool_id"] == "config.read"}
        for calls in (db_log_calls, config_calls):
            assert any(e["tool_call_id"] in calls and e["id"] in root["evidence_ids"] for e in result["evidence"]), "Root cause must cite both current database logs and reviewed configuration"
