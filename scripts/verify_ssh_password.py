"""Local-only OpenSSH + Docker Mock acceptance; never contacts production or a model provider.

Build fixture/API/web images first (see report). Creates a unique evidence directory,
keeps data, and stops only its own Compose services. Plaintext test password is memory-only.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import paramiko
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "tests"), str(ROOT / "tests/integration")]
from conftest import wait_job  # noqa: E402


def command(args, *, input=None):
    # Binary stdin prevents Windows CRLF translation from changing the test password hash.
    result = subprocess.run(args, input=input.encode() if input is not None else None,
                            capture_output=True, cwd=ROOT, timeout=120)
    if result.returncode:
        raise RuntimeError("Isolation command failed: " + args[0] + " (" + str(result.returncode) + ")")
    return result.stdout.decode("utf-8")


def main(standard=False, discovery=False):
    image = "atr-real-discovery" if discovery else "atr-standard-ssh" if standard else "atr-ssh-password"
    tag = "20261011" if discovery else "20261010"
    fixture_tag = tag + "-cached" if "--cached-fixture" in sys.argv[1:] else tag
    evidence_root = ROOT / "test-results" / ("real-discovery-ssh" if discovery else "standard-ssh" if standard else "ssh-password")
    evidence_root.mkdir(parents=True, exist_ok=True)
    for port in (*range(32022, 32029 if standard else 32026), 29080):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
    folder = evidence_root / ("acceptance-" + secrets.token_hex(4))
    keys, data = folder / "keys", folder / "docker-data"
    keys.mkdir(parents=True)
    data.mkdir()
    host, client = paramiko.RSAKey.generate(2048), paramiko.RSAKey.generate(2048)
    host.write_private_key_file(str(keys / "host_key"))
    client.write_private_key_file(str(keys / "client"))
    (keys / "client.pub").write_text(f"{client.get_name()} {client.get_base64()}\n")
    password = secrets.token_urlsafe(32)
    hashed = command(["docker", "run", "--rm", "-i", image + "-fixture:" + fixture_tag,
                      "openssl", "passwd", "-6", "-stdin"], input=password + "\n")
    (keys / "password_hash").write_text(hashed.strip())
    from app.connectors.models import RuntimeTargetCreate
    cfg = RuntimeTargetCreate(**{"name": "isolated OpenSSH", "connector": "ssh", "host": "127.0.0.1", "port": 32022,
           "username": "atrreader", "credential_ref": "v22-fixture",
           "host_public_key": f"{host.get_name()} {host.get_base64()}", "access_authorized": True,
           "permission_boundary": "forced_command_gateway", "boundary_attestation": "隔离容器固定只读网关已核验",
           "allowed_paths": ["/srv/atr-fixture"], "timeout_seconds": 3,
           "docker_reads_enabled": False}).model_dump()
    config_path = folder / "fixture-config.json"
    config_path.write_text(json.dumps({"key_path": str(keys / "client"), "config": cfg}), encoding="utf-8")
    compose = {"name": image + "-test", "services": {
        "ssh": {"image": image + "-fixture:" + fixture_tag, "pids_limit": 768,
            "volumes": [str(keys.as_posix()) + ":/keys:ro"], "networks": ["isolated", "published"],
            "ports": [f"127.0.0.1:{32022 + i}:{2222 + i}" for i in range(7 if standard else 4)]},
        "api": {"image": image + "-api:" + tag, "networks": ["isolated"],
            "security_opt": ["no-new-privileges:true"], "cap_drop": ["ALL"],
            "volumes": [str(data.as_posix()) + ":/data", str(keys.as_posix()) + ":/keys:ro",
                        str((ROOT / "demo").as_posix()) + ":/demo:ro"],
            "environment": {"DATABASE_PATH": "/data/mock.sqlite3", "ARTIFACT_ROOT": "/data/artifacts",
                "REPOSITORY_ROOTS": "/app,/demo", "OPERATOR_TOKEN": "browser-test-operator-" + "x" * 40,
                "COLLECTOR_TOKEN": "test-collector-" + "y" * 40, "EXTERNAL_MODEL_ENABLED": "false",
                "MODEL_API_KEY": "", "MODEL_NAME": "", "SSH_ENABLED": "true",
                "SSH_CREDENTIALS": json.dumps({"v22-fixture": "/keys/client"})}},
        "web": {"image": image + "-web:" + tag, "networks": ["isolated", "published"],
            "ports": ["127.0.0.1:29080:80"]}},
        "networks": {"isolated": {"internal": True}, "published": {}}}
    if discovery:
        compose["services"]["model"] = {"image": "python:3.12-slim", "networks": ["isolated"],
            "volumes": [str((ROOT / "tests/fixtures/model_fixture.py").as_posix()) + ":/fixture.py:ro"],
            "command": ["python", "/fixture.py"], "security_opt": ["no-new-privileges:true"], "cap_drop": ["ALL"]}
    compose_path = folder / "compose.yaml"
    compose_path.write_text(yaml.safe_dump(compose), encoding="utf-8")
    dc = ["docker", "compose", "-f", str(compose_path)]
    summary = {"status": "running", "production_contacted": False, "paid_model_calls": 0,
               "password_on_disk": False, "original_data_touched": False, "git_pushed": False}
    try:
        command(dc + ["up", "-d"])
        base = "http://127.0.0.1:29080"
        for _ in range(100):
            try:
                if httpx.get(base + "/api/v1/health", timeout=1).status_code == 200:
                    with socket.create_connection(("127.0.0.1", 32025), timeout=1):
                        break
            except (httpx.HTTPError, OSError):
                pass
            time.sleep(0.2)
        else:
            raise RuntimeError("Isolated fixture not healthy")
        ssh_id = command(dc + ["ps", "-q", "ssh"]).strip()
        permissions = []
        for account_uid in (10002, 10003):
            proof = command(["docker", "exec", "-i", "--user", str(account_uid), ssh_id, "python3", "-"],
                input=(ROOT / "tests/fixtures/linux-ssh/check_permissions.py").read_text(encoding="utf-8"))
            permissions.append(json.loads(proof))
        summary["permission_checks"] = permissions
        os.environ.update(ATR_LINUX_SSH_CONFIG=str(config_path), ATR_E2E_BASE_URL=base,
                          ATR_E2E_OPERATOR_TOKEN="browser-test-operator-" + "x" * 40,
                          ATR_BROWSER_CHANNEL="chrome", MODEL_API_KEY="", EXTERNAL_MODEL_ENABLED="false")
        import ssh_password_acceptance
        ssh_password_acceptance.PASSWORD = password
        test_files = [str(ROOT / "tests/integration/linux_ssh_acceptance.py"),
                      str(ROOT / "tests/integration/ssh_password_acceptance.py"),
                      str(ROOT / "tests/e2e/test_browser.py"), str(ROOT / "tests/e2e/test_ssh_password_ui.py")]
        if standard:
            import standard_ssh_acceptance
            standard_ssh_acceptance.PASSWORD = password
            test_files.append(str(ROOT / "tests/integration/standard_ssh_acceptance.py"))
            test_files.append(str(ROOT / "tests/e2e/test_standard_ssh_ui.py"))
        if discovery:
            test_files.append(str(ROOT / "tests/integration/discovery_acceptance.py"))
            test_files.append(str(ROOT / "tests/e2e/test_discovery_fixes_ui.py") + "::test_browser_optional_attachment_undo_and_process_groups")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            exit_code = pytest.main([*test_files,
                "-q", "--basetemp=" + str(folder / "pytest"), "--junitxml=" + str(folder / "junit.xml")])
        (folder / "tests.log").write_text(output.getvalue().replace(password, "[REDACTED]"), encoding="utf-8")
        assert exit_code == 0, "OpenSSH / browser acceptance failed (see sanitized log)"
        with httpx.Client(base_url=base, headers={"Authorization": "Bearer browser-test-operator-" + "x" * 40}, timeout=10) as api:
            business = api.post("/api/v1/businesses", json={"name": "Docker password acceptance", "description": "合成隔离接入"}).json()
            path = "/api/v1/businesses/" + business["business_id"] + "/targets"
            target_config = {**cfg, "host": "ssh", "port": 2225, "username": "atrpassword",
                             "credential_ref": "", "auth_mode": "password"}
            response = api.post(path, json=target_config)
            response.raise_for_status()
            target = response.json()
            target_path = path + "/" + target["id"]

            def provide(value):
                response = api.post(target_path + "/credential", json={"password": value})
                response.raise_for_status()
                assert value not in response.text

            def read():
                response = api.post(target_path + "/tools", json={"tool_id": "host.info"})
                response.raise_for_status()
                return wait_job(api, response.json()["job_id"], timeout=15)["result"]

            provide(password)
            assert "Linux" in read()["payload"]["text"]
            provide("invalid-synthetic-password")
            assert read()["error"] == "AuthenticationException"
            provide(password)
            denied = api.post(target_path + "/tools", json={"tool_id": "shell.exec", "arguments": {"command": "id"}})
            assert denied.status_code == 202
            assert wait_job(api, denied.json()["job_id"])["result"]["status"] == "denied"
            assert "payload" in read()
            api_id = command(dc + ["ps", "-q", "api"]).strip()
            dump = command(["docker", "exec", api_id, "python", "-c",
                "import sqlite3; c=sqlite3.connect('/data/mock.sqlite3'); print(list(c.iterdump()))"])
            assert password not in dump and "invalid-synthetic-password" not in dump
            logs = command(dc + ["logs", "--no-color"])
            assert password not in logs
            env = command(["docker", "inspect", api_id, "--format", "{{json .Config.Env}}"])
            assert password not in env
            before = command(["docker", "exec", api_id, "python", "-c",
                "import sqlite3; c=sqlite3.connect('/data/mock.sqlite3'); print(c.execute('SELECT count(*) FROM runtime_targets').fetchone()[0])"])
            command(dc + ["restart", "api"])
            for _ in range(100):
                try:
                    listed = api.get(path)
                    if listed.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("Isolated API did not restart")
            assert listed.json()[0]["credential_status"]["state"] == "required"
            assert api.post(target_path + "/tools", json={"tool_id": "host.info"}).status_code == 403
            after = command(["docker", "exec", api_id, "python", "-c",
                "import sqlite3; c=sqlite3.connect('/data/mock.sqlite3'); print(c.execute('SELECT count(*) FROM runtime_targets').fetchone()[0])"])
            assert before == after
            provide(password)
            assert "payload" in read()
            api.delete(target_path + "/credential").raise_for_status()
            assert api.post(target_path + "/discover").status_code == 403
            summary.update(password_login=True, wrong_password_denied=True, async_jobs=True,
                           restart_requires_password=True, database_no_password=True, logs_no_password=True,
                           environment_no_password=True, clear_denies_new_jobs=True)
            if standard:
                standard_config = {**cfg, "execution_mode": "standard_ssh_readonly", "permission_boundary": "client_readonly",
                    "host": "ssh", "port": 2226, "username": "atrstandard", "timeout_seconds": 10,
                    "docker_reads_enabled": True}
                targets = []
                for auth in ("password", "private_key"):
                    response = api.post(path, json={**standard_config, "auth_mode": auth,
                        "credential_ref": "" if auth == "password" else "v22-fixture"})
                    response.raise_for_status()
                    t = response.json()
                    targets.append(t)
                    target_url = path + "/" + t["id"]
                    if auth == "password":
                        api.post(target_url + "/credential", json={"password": password}).raise_for_status()
                    discovery = api.post(target_url + "/discover")
                    discovery.raise_for_status()
                    result = wait_job(api, discovery.json()["job_id"], timeout=30)["result"]
                    assert result["collection_status"] in {"completed", "partial"} and not result["errors"], "Ordinary SSH discovery failed"
                    assert any(o["payload"].get("identity", {}).get("uid") == 10004 for o in result["observations"])
                    shell = api.post(target_url + "/tools", json={"tool_id": "shell.exec", "arguments": {"command": "id"}})
                    shell.raise_for_status()
                    assert wait_job(api, shell.json()["job_id"])["result"]["status"] == "denied"
                    for match in result["matches"]:
                        if match["kind"] == "service":
                            api.post("/api/v1/businesses/" + business["business_id"] + "/bindings/" + match["id"] + "/review",
                                json={"decision": "confirm", "reason": "隔离合成 CLI 对象人工审核"}).raise_for_status()
                    for tool in ("host.memory", "host.disk"):
                        read_job = api.post(target_url + "/tools", json={"tool_id": tool})
                        read_job.raise_for_status()
                        assert "payload" in wait_job(api, read_job.json()["job_id"], timeout=20)["result"]
                dump = command(["docker", "exec", api_id, "python", "-c",
                    "import sqlite3; c=sqlite3.connect('/data/mock.sqlite3'); print(list(c.iterdump()))"])
                assert password not in dump
                profile = api.get("/api/v1/businesses/" + business["business_id"]).json()
                graph = api.get("/api/v1/systems/" + profile["system_id"] + "/graph").json()
                hosts = [n for n in graph["nodes"] if n["kind"] == "host" and n["name"] in {t["id"] for t in targets}]
                assert len(hosts) == 2 and all(n["attributes"]["server_enforced_readonly"] is False for n in hosts)
                summary["standard_observations_in_graph"] = True
                command(dc + ["restart", "api"])
                for _ in range(100):
                    try:
                        listed = api.get(path)
                        if listed.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Standard API did not restart")
                pw_target = next(t for t in listed.json() if t["id"] == targets[0]["id"])
                assert pw_target["credential_status"]["state"] == "required"
                assert api.post(path + "/" + targets[0]["id"] + "/tools", json={"tool_id": "host.info"}).status_code == 403
                job = api.post(path + "/" + targets[1]["id"] + "/tools", json={"tool_id": "host.info"})
                job.raise_for_status()
                assert "payload" in wait_job(api, job.json()["job_id"])["result"]
                summary.update(standard_password=True, standard_private_key=True, standard_discovery=True,
                               standard_restart_password_lost=True, standard_restart_key_preserved=True,
                               all_registered_read_tools=True, docker_cli="synthetic_no_daemon", arbitrary_shell_denied=True)
            if discovery:
                override = folder / "fake-model-compose.yaml"
                override.write_text(yaml.safe_dump({"services": {"api": {"environment": {
                    "EXTERNAL_MODEL_ENABLED": "true", "MODEL_API_KEY": "synthetic-local-fixture", "MODEL_NAME": "gpt-6.1-sol",
                    "MODEL_BASE_URL": "http://model:8080/v1", "MODEL_WIRE_API": "chat_completions", "MODEL_REASONING_EFFORT": "low"}}}}), encoding="utf8")
                command(dc + ["-f", str(override), "up", "-d", "--no-deps", "api"])
                for _ in range(100):
                    try:
                        if api.get("/api/v1/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                from test_business import onboard
                profile = onboard(api)
                business_path = "/api/v1/businesses/" + profile["business_id"]
                new_config = {**cfg, "name": "Zero binding OpenSSH", "host": "ssh", "port": 2226, "username": "atrstandard",
                    "execution_mode": "standard_ssh_readonly", "permission_boundary": "client_readonly", "timeout_seconds": 10,
                    "docker_reads_enabled": True, "docker_reader": "restricted_list"}
                ssh_target = api.post(business_path + "/targets", json=new_config).json()
                api.post(business_path + "/review", json={"expected_version": profile["profile_version"], "decision": "approve", "reason": "隔离画像已审核，运行范围仍未确认"}).raise_for_status()
                incident = api.post("/api/v1/incidents", json={"system_id": profile["system_id"], "symptom": "catalog-api synthetic discovery"}).json()
                started = api.post("/api/v1/incidents/" + incident["id"] + "/runs")
                started.raise_for_status()
                job = wait_job(api, started.json()["job_id"], timeout=40)
                assert job["status"] == "completed", "Discovery job failed"
                run_path = "/api/v1/runs/" + job["result"]["run_id"]
                waiting = api.get(run_path).json()
                assert waiting["status"] == "awaiting_review" and waiting["summary"]["model_status"] == "completed"
                assert waiting["context_budget"]["calls"] == 1 and waiting["context_budget"]["tool_calls"] == 7
                assert api.post(run_path + "/resume").status_code == 409
                denied = api.post(business_path + "/targets/" + ssh_target["id"] + "/tools", json={"tool_id": "service.logs", "arguments": {"name": "catalog-api.service"}})
                assert wait_job(api, denied.json()["job_id"])["result"]["status"] == "denied"
                proposal = waiting["summary"]["mapping_proposals"][0]
                api.post(business_path + "/bindings/" + proposal["binding_id"] + "/review", json={"decision": "confirm", "reason": "人工审核隔离 systemd 服务身份及授权"}).raise_for_status()
                resumed = api.post(run_path + "/resume")
                resumed.raise_for_status()
                job = wait_job(api, resumed.json()["job_id"], timeout=40)
                assert job["status"] == "completed", "Resume job failed"
                final = api.get(run_path).json()
                assert final["status"] == "unresolved" and final["context_budget"]["calls"] == 2 and final["context_budget"]["tool_calls"] == 9
                assert final["summary"]["budget_id"] == waiting["summary"]["budget_id"]
                assert not set(final["summary"]["evidence_ids"]) & set(waiting["summary"]["discovery_evidence_ids"])
                (folder / "discovery-workflow-proof.json").write_text(json.dumps({"run_id": final["id"], "status": final["status"],
                    "model_calls": final["context_budget"]["calls"], "tool_calls": final["context_budget"]["tool_calls"],
                    "preserved_budget_id": final["summary"]["budget_id"], "provider": "isolated_docker_fake_http",
                    "restricted_list_source": True, "unauthorized_logs_denied": True}, indent=2), encoding="utf8")
                summary.update(zero_binding_agent=True, fake_worker_requests=2, restricted_sudo_list=True,
                    manual_review_resume_same_budget=True, unauthorized_logs_denied=True, root_gate_unresolved=True)
        if standard:
            logs = command(dc + ["logs", "--no-color"])
            assert password not in logs
        for file in folder.rglob("*"):
            if file.is_file():
                assert password.encode() not in file.read_bytes(), "Test plaintext entered evidence files"
        summary["status"] = "passed"
    except BaseException:
        summary["status"] = "failed"
        raise
    finally:
        command(dc + ["stop"])
        (folder / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        (evidence_root / "latest-acceptance.txt").write_text(str(folder), encoding="utf-8")
        password = ""
    print(json.dumps({"status": summary["status"], "evidence": str(folder)}, ensure_ascii=False))


if __name__ == "__main__":
    main(standard="--standard" in sys.argv[1:] or "--discovery" in sys.argv[1:], discovery="--discovery" in sys.argv[1:])
