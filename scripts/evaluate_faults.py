"""Repeatable 13-case evaluation: actual ATR API/SQLite/Gate with a deterministic Stub.

Optional isolated Docker process observations are replayed by MockConnector. This is
not an evaluation of model intelligence or production SSH/systemd/Redis/database behavior.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "tests")]

import pytest  # noqa: E402
from app.agent.verification import MECHANISMS  # noqa: E402
from app.common import now  # noqa: E402
from app.connectors.mock import MockConnector  # noqa: E402
from app.settings import get_settings  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import SecretStr  # noqa: E402
from test_business import SAMPLE, onboard  # noqa: E402
from test_runtime import discover, target  # noqa: E402
from test_v22_adaptive import investigate  # noqa: E402

SERVICES = ("catalog-api", "store", "cache")
ARMS = ("graph_only", "graph_pattern", "graph_adaptive")
CASES = [
    {"key": "application_exit", "truth": {"catalog-api": "startup_failure"}, "injection": "raise initialization RuntimeError; process exits 1"},
    {"key": "database_unavailable", "truth": {"store": "database_unavailable"}, "injection": "TCP connect to absent 127.0.0.1:15432; store test process exits 1"},
    {"key": "redis_unavailable", "truth": {"cache": "redis_unavailable"}, "injection": "TCP connect to absent 127.0.0.1:16379; cache test process exits 1"},
    {"key": "upstream_unavailable", "truth": {"catalog-api": "upstream_unavailable"}, "injection": "TCP connect to absent upstream 127.0.0.1:18080"},
    {"key": "port_unbound", "truth": {"catalog-api": "port_unbound"}, "injection": "run active process without opening business listener"},
    {"key": "port_mismatch", "truth": {"catalog-api": "port_mismatch"}, "injection": "open 18124 while static configured port is 18123"},
    {"key": "dependency_wrong", "truth": {"catalog-api": "dependency_misconfiguration"}, "injection": "connect to wrong dependency endpoint 25432"},
    {"key": "file_permission", "truth": {"catalog-api": "permission_denied"}, "injection": "UID 10002 attempts open under root-only directory"},
    {"key": "unrelated_error", "truth": {}, "injection": "healthy listener plus unrelated telemetry ERROR"},
    {"key": "multiple_services", "truth": {"store": "database_unavailable", "cache": "redis_unavailable"}, "injection": "both isolated dependency test processes exit 1"},
    {"key": "historical_error", "truth": {}, "injection": "healthy listener plus timestamped three-day-old startup error"},
    {"key": "permission_blocked", "truth": {"store": "database_unavailable"}, "injection": "failed dependency remains unconfirmed; access must be blocked"},
    {"key": "bind_failure", "truth": {"catalog-api": "bind_failure"}, "injection": "bind second socket to occupied 18123"},
]
# Thirteen scenarios include all requested categories and negative controls.


def stub_report(context):
    """Uses only model-visible observations, never case key or ground truth."""
    import re
    nodes = {n["id"]: n for n in context["system_graph"]}
    grouped = {node: [e for e in context["current_evidence"] if e["target_node_id"] == node] for node in nodes}
    hypotheses = []
    for node_id, evidence in grouped.items():
        text = "\n".join(e.get("payload", {}).get("text", "") for e in evidence)
        mechanism = next((name for name in ("permission_denied", "bind_failure", "database_unavailable", "redis_unavailable", "startup_failure") if re.search(MECHANISMS[name], text, re.I)), None)
        if not mechanism:
            continue
        hypotheses.append({
            "target_node_id": node_id, "target_role": nodes[node_id]["role"], "failure_mode": mechanism,
            "prediction": "The affected service is failed during this incident",
            "causal_chain": ["observed mechanism prevents service operation", "required business requests cannot complete"],
            "rationale": "Require separately sampled current failed state and timestamped mechanism log on this exact service.",
            "confidence": .9, "evidence_links": [{"evidence_id": e["evidence_id"], "relation": "supports", "explanation": "Current observation must be checked for causal relevance and independent origin."} for e in evidence],
            "verification_predictions": [{"target_node_id": node_id, "tool_id": "service.status", "field": "state", "operator": "eq", "value": "failed"}],
        })
    actions = context["available_actions"]
    priority_names = ["store", "cache"] if any("connection refused" in e.get("payload", {}).get("text", "") for e in context["current_evidence"]) else []
    action = next((a for name in priority_names for a in actions if a.get("service") == name and a["tool_id"] in {"service.status", "service.logs"}), None)
    action = action or next((a for a in actions if a["tool_id"] in {"service.status", "service.logs"}), None)
    action = action or (actions[0] if actions else None)
    if context["adaptive"]:
        proposal = {"action_id": action["action_id"], "rationale": "Read a distinct bounded observation"} if action else None
    else:
        proposal = {"tool_id": action["tool_id"], "target_node_id": action["target_node_id"], "rationale": "Read a distinct bounded observation"} if action else None
    requests = []
    if priority_names and not any(a.get("service") == "store" for a in actions) and not any(nodes[e["target_node_id"]]["name"] == "store" for e in context["current_evidence"]):
        requests = [{"service": "store", "reason": "Dependency has no authorized current observations"}]
    return {"summary": "Deterministic fixture Stub: causal evidence must pass Gate", "next_action": "Collect current independent observations", "hypotheses": hypotheses[:3], "action": proposal, "scope_requests": requests}


def docker_snapshot(case, prefix, wsl):
    command = ["wsl", "-d", wsl, "--", "docker"] if wsl else ["docker"]
    fixture = ROOT / "tests" / "fixtures" / "fault_workload.py"
    source = "/mnt/" + fixture.drive[0].lower() + fixture.as_posix()[2:] if wsl else str(fixture)

    def docker(*args):
        result = subprocess.run([*command, *args], capture_output=True, timeout=30, check=True)
        return result.stdout.decode("utf-8", errors="replace").strip()

    payloads, containers = {}, []
    try:
        for service in SERVICES:
            name = f"{prefix}-{service}"
            docker("run", "-d", "--name", name, "--label", "atr.v22.evaluation=isolated",
                   "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                   "--user", "10002:10002", "--pids-limit", "32", "--memory", "64m",
                   "--mount", f"type=bind,source={source},target=/fixture.py,readonly",
                   "--entrypoint", "python3", "python:3.12-slim", "/fixture.py", "--case", case["key"], "--service", service)
            containers.append(name)
        time.sleep(1)
        for service, name in zip(SERVICES, containers, strict=True):
            item = json.loads(docker("inspect", name))[0]
            assert item["Config"]["Labels"].get("atr.v22.evaluation") == "isolated"
            state = item["State"]
            payloads[service] = {"state": "active" if state["Running"] else "failed", "text": docker("logs", name),
                                 "captured_at": now(), "created_at": item["Created"], "container_state": state["Status"], "exit_code": state["ExitCode"]}
            assert payloads[service]["text"], f"No workload observation: {name}"
        return payloads
    finally:
        for name in containers:
            item = json.loads(docker("inspect", name))[0]
            assert name.startswith(prefix) and item["Config"]["Labels"].get("atr.v22.evaluation") == "isolated"
            # Only newly created disposable containers; no -v, no volume or history deletion.
            docker("rm", "-f", name)


def evaluate(work_dir, output, *, wsl=None, docker=False, repetitions=1):
    work_dir, output = Path(work_dir), Path(output)
    work_dir.mkdir(parents=True, exist_ok=True)
    db_path = work_dir / "evaluation.sqlite"
    if db_path.exists():
        raise ValueError("Use a new work directory; existing evaluation history is preserved")
    records, payloads = [], {}
    category = "isolated_docker_observations_mock_replay_stub_model" if docker else "mock_observations_stub_model"
    original_read = MockConnector.read

    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Health)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with pytest.MonkeyPatch.context() as patch:
            for key, value in {"DATABASE_PATH": str(db_path), "ARTIFACT_ROOT": str(work_dir / "artifacts"), "REPOSITORY_ROOTS": str(ROOT / "demo"), "OPERATOR_TOKEN": "evaluation-local-" + "x" * 40, "COLLECTOR_TOKEN": "evaluation-local-" + "y" * 40, "MODEL_NAME": "", "MODEL_API_KEY": "", "SSH_ENABLED": "false", "EXTERNAL_MODEL_ENABLED": "false"}.items():
                patch.setenv(key, value)
            get_settings.cache_clear()
            from app.main import app

            def read(self, tool, args):
                if tool == "service.list":
                    return {"connector": "mock", "observed_at": now(), "services": [{"id": name + ".service", "name": name, "kind": "service"} for name in SERVICES]}
                if tool in {"service.status", "service.logs"}:
                    data = payloads[args["name"].removesuffix(".service")]
                    return {"connector": "mock", "fixture_observation_category": category,
                            "state": data["state"]} if tool == "service.status" else {
                                "connector": "mock", "fixture_observation_category": category, "text": now() + " " + data["text"] if data.get("live_timestamp") else data["text"], "captured_at": data.get("captured_at")}
                if tool == "config.list":
                    return {"connector": "mock", "files": ["/srv/catalog/app.conf"]}
                if tool == "config.read":
                    return {"connector": "mock", "text": "PORT=18123", "source_type": "Static"}
                return original_read(self, tool, args)

            def completion(**kwargs):
                body = stub_report(json.loads(kwargs["messages"][1]["content"]))
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(body)))], usage=None)

            patch.setattr(MockConnector, "read", read)
            patch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
            with TestClient(app, headers={"Authorization": "Bearer evaluation-local-" + "x" * 40}) as client:
                settings = get_settings()
                settings.external_model_enabled = True
                settings.model_name = "evaluation-deterministic-stub"
                settings.model_api_key = SecretStr("fixture-no-external-credentials")
                settings.allowed_health_urls = f"http://127.0.0.1:{server.server_port}/health"
                settings.model_max_output_tokens = 1500
                for index, case in enumerate(CASES, 1):
                    files = {**SAMPLE, "compose.yaml": "services:\n  catalog-api: {build: ., ports: ['18123:18123'], depends_on: [store, cache]}\n  store: {image: mysql:8.4}\n  cache: {image: redis:7}"}
                    profile = onboard(client, files)
                    runtime = target(client, profile)
                    discovery = discover(client, profile, runtime)
                    for binding in discovery["matches"]:
                        if binding["kind"] == "service" and not (case["key"] == "permission_blocked" and binding["business_service"] == "store"):
                            response = client.post(f"/api/v1/businesses/{profile['business_id']}/bindings/{binding['id']}/review", json={"decision": "confirm", "reason": "Evaluation fixture operator confirms this isolated binding"})
                            assert response.status_code == 200, response.text
                    latest = client.get(f"/api/v1/businesses/{profile['business_id']}").json()
                    response = client.post(f"/api/v1/businesses/{profile['business_id']}/review", json={"expected_version": latest["profile_version"], "decision": "approve", "reason": "Evaluation fixture operator confirms static scope"})
                    assert response.status_code == 200, response.text
                    profile = response.json()
                    if index == 1:
                        # A separate synthetic training case, verified through the existing repair/review API.
                        payloads = {name: {"state": "active", "text": "ready", "live_timestamp": True} for name in SERVICES}
                        payloads["catalog-api"] = {"state": "failed", "text": "startup failed: required initialization failed", "live_timestamp": True}
                        training = investigate(client, profile, mode="graph_only")
                        assert training["status"] == "supported", training
                        response = client.post(f"/api/v1/incidents/{training['incident_id']}/repair", json={"summary": "Synthetic fixture operator restored test application", "operator_verified": True, "verification_summary": "Actual local verification HTTP returns 200; diagnosis/repair are Mock", "verification_url": settings.allowed_health_urls})
                        assert response.status_code == 200, response.text
                        pattern_id = response.json()["pattern_version_id"]
                        reviewed = client.post(f"/api/v1/patterns/{pattern_id}/review", json={"decision": "approve", "reason": "Fixture operator approves verified synthetic example only"})
                        assert reviewed.status_code == 200, reviewed.text
                    registered = client.post("/api/v1/evaluation-cases", json={"case_key": "v22-" + case["key"], "system_id": profile["system_id"], "fault_class": case["key"], "ground_truth": {**case, "category": category, "allowed_services": [name for name in SERVICES if case["key"] != "permission_blocked" or name != "store"]}, "protocol_version": "three-arm-v22"})
                    assert registered.status_code == 200, registered.text
                    registered = registered.json()
                    for repetition in range(1, repetitions + 1):
                        window = now()
                        if docker:
                            prefix = f"atr-v22-eval-{datetime.now(timezone.utc).strftime('%H%M%S')}-{index}-{repetition}"
                            payloads = docker_snapshot(case, prefix, wsl)
                            # Use the injection host clock, before workloads start; never retimestamp fault logs.
                            window = min(data["created_at"] for data in payloads.values())
                        else:
                            payloads = {name: {"state": "failed" if name in case["truth"] and case["key"] not in {"port_unbound", "port_mismatch"} else "active", "text": now() + " " + (case["truth"].get(name, "ready").replace("_", " ") + " startup failed" if name in case["truth"] else "ready")} for name in SERVICES}
                        for arm in ARMS:
                            response = client.post("/api/v1/incidents", json={"system_id": profile["system_id"], "symptom": "catalog-api unavailable", "mode": arm, "window_start": window})
                            incident = response.json()
                            response = client.post(f"/api/v1/incidents/{incident['id']}/runs")
                            from conftest import wait_job
                            job = wait_job(client, response.json()["job_id"], timeout=60)
                            assert job["status"] == "completed", job
                            run = client.get(f"/api/v1/runs/{job['result']['id']}").json()
                            graph = client.get(f"/api/v1/systems/{profile['system_id']}/graph").json()
                            names = {n["id"]: n["name"] for n in graph["nodes"]}
                            diagnosed = {names.get(h["target_node_id"]): h["failure_mode"] for h in run["summary"]["gate"]["hypotheses"] if h["status"] == "supported"}
                            verdict = "unresolved" if run["status"] == "unresolved" else "correct" if diagnosed == case["truth"] else "incorrect"
                            response = client.post("/api/v1/evaluation-trials", json={"case_id": registered["id"], "run_id": run["id"], "pair_key": "v22-" + case["key"], "arm": arm, "repetition": repetition, "reset_digest": registered["fixture_digest"], "verdict": verdict})
                            assert response.status_code == 200, response.text
                            records.append({"case": case, "repetition": repetition, "arm": arm, "run_id": run["id"], "category": category, "window_start": window, "actual_observations": payloads, "diagnosed": diagnosed, "verdict": verdict, "false_positive": response.json()["metrics"]["false_positive"], "partial_diagnosis": response.json()["metrics"]["partial_diagnosis"], "metrics": response.json()["metrics"], "steps": run["diagnostic_steps"], "tool_calls": run["tool_calls"], "pattern_matches": run["summary"].get("pattern_matches", []), "allowed_services": [name for name in SERVICES if case["key"] != "permission_blocked" or name != "store"], "trusted_pattern": "synthetic operator-reviewed fixture with real loopback HTTP verification"})
                            output.parent.mkdir(parents=True, exist_ok=True)
                            output.write_text(json.dumps({"category": category, "external_model_verified": False, "token_usage": "estimates, Stub supplies no provider usage", "database_cache_edge": "Python TCP test doubles, not MySQL/Redis/Nginx", "trials": records, "comparison": client.get("/api/v1/metrics").json()["three_arm_evaluation"]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                        print(f"{index}/{len(CASES)} {case['key']}: three arms recorded", flush=True)
    finally:
        get_settings.cache_clear()
        server.shutdown()
        server.server_close()
        worker.join(5)
    return json.loads(output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--docker", action="store_true")
    parser.add_argument("--docker-wsl")
    parser.add_argument("--allow-isolated-mutations", action="store_true")
    parser.add_argument("--repetitions", type=int, choices=range(1, 6), default=1)
    args = parser.parse_args()
    if (args.docker or args.docker_wsl) and not args.allow_isolated_mutations:
        parser.error("Docker fixture creation requires --allow-isolated-mutations")
    result = evaluate(args.work_dir, args.output, wsl=args.docker_wsl, docker=args.docker or bool(args.docker_wsl), repetitions=args.repetitions)
    print(json.dumps(result["comparison"], indent=2))
