"""Operator-only fault/recovery checks for the dedicated atr-v22-system-a/b stacks."""

import argparse
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:28080")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--docker-via-wsl", action="store_true")
    parser.add_argument("--allow-isolated-mutations", action="store_true", required=True)
    args = parser.parse_args()
    assert urlsplit(args.url).hostname in {"127.0.0.1", "localhost"}
    config = dict(line.split("=", 1) for line in args.env_file.read_text().splitlines()
                  if line and not line.startswith("#") and "=" in line)
    cli = ["wsl", "-d", "Ubuntu", "--", "docker"] if args.docker_via_wsl else ["docker"]

    def docker(*command):
        result = subprocess.run(cli + list(command), check=True, capture_output=True)
        return result.stdout.decode("utf-8", errors="replace").strip()

    def endpoints(port):
        return {path: httpx.get(f"http://127.0.0.1:{port}{path}", timeout=5).status_code for path in ("/health", "/record")}

    results = []
    with httpx.Client(base_url=args.url, headers={"Authorization": "Bearer " + config["OPERATOR_TOKEN"]}, timeout=10) as api:
        for side, port, datastore, app in (("a", 18080, "mysql", "python_api"), ("b", 18081, "postgres_store", "catalog_api")):
            prefix = f"atr-v22-system-{side}"
            # These names are deliberately fixed; this script is absent from the Agent catalog.
            db_name, app_name = prefix + "_" + datastore + "_1", prefix + "_" + app + "_1"
            assert docker("inspect", "--format", '{{index .Config.Labels "com.docker.compose.project"}}', db_name) == prefix
            baseline = endpoints(port)
            assert baseline == {"/health": 200, "/record": 200}
            response = api.post("/api/v1/systems/onboard", json={"source_key": f"v22-system-{side}", "display_name": f"V22 isolated {side}"})
            response.raise_for_status()
            system_id = response.json()["system_id"]
            started, window = time.monotonic(), datetime.now(timezone.utc).isoformat()
            record = {"system": side, "category": "isolated_actual_docker", "injection": "stop datastore and restart application", "baseline": baseline}
            results.append(record)
            try:
                docker("stop", "-t", "3", db_name)
                docker("restart", app_name)
                for _ in range(80):
                    fault_status = endpoints(port)["/health"]
                    if fault_status == 502:
                        break
                    time.sleep(0.25)
                assert fault_status == 502
                incident = api.post("/api/v1/incidents", json={"system_id": system_id, "symptom": "startup failure produces health 502 after datastore outage", "window_start": window, "mode": "graph_only"})
                incident.raise_for_status()
                response = api.post(f"/api/v1/incidents/{incident.json()['id']}/runs")
                response.raise_for_status()
                for _ in range(200):
                    job = api.get("/api/v1/jobs/" + response.json()["job_id"]).json()
                    if job["status"] not in {"queued", "running"}:
                        break
                    time.sleep(0.1)
                assert job["status"] == "completed"
                run = api.get("/api/v1/runs/" + job["result"]["id"]).json()
                record.update(fault_health_status=fault_status, run_id=run["id"], diagnosis=run["status"],
                              model_calls=run["context_budget"]["calls"], tools=[{"tool": c["tool_id"], "status": c["status"]} for c in run["tool_calls"]],
                              elapsed_seconds=round(time.monotonic() - started, 3), model="disabled")
                assert run["status"] == "unresolved" and record["model_calls"] == 0
            finally:
                docker("start", db_name)
                for _ in range(120):
                    if docker("inspect", "--format", "{{.State.Health.Status}}", db_name) == "healthy":
                        break
                    time.sleep(0.25)
                docker("start", app_name)
                for _ in range(120):
                    restored = endpoints(port)
                    if restored == {"/health": 200, "/record": 200}:
                        break
                    time.sleep(0.25)
                record["restored"] = restored
                args.output.write_text(json.dumps(results, indent=2))
                assert restored == {"/health": 200, "/record": 200}
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
