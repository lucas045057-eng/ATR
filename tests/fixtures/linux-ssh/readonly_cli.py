#!/usr/local/bin/python3
"""Synthetic CLI output on a real OpenSSH host; no systemd or Docker daemon."""
import json
import sys
from pathlib import Path

tool, args = Path(sys.argv[0]).name, sys.argv[1:]
with open("/tmp/atr-readonly-cli-audit.jsonl", "a") as audit:
    audit.write(json.dumps({"tool": tool, "args": args}) + "\n")
if tool == "systemctl" and args[:2] == ["list-units", "--type=service"]:
    print("catalog-api.service loaded active running Synthetic catalog")
    print("unrelated-backup.service loaded active running Synthetic unrelated")
elif tool == "systemctl" and args == ["show", "--no-pager", "--property=ActiveState,SubState,Id", "--", "catalog-api.service"]:
    print("Id=catalog-api.service\nActiveState=active\nSubState=running")
elif tool == "journalctl" and args == ["--no-pager", "--output=short-iso", "-n", "80", "-u", "catalog-api.service"]:
    print("2026-10-10T10:00:00+00:00 synthetic catalog unavailable DB_PASSWORD=synthetic-cli-secret")
elif tool == "docker" and args == ["ps", "--all", "--no-trunc", "--format", "{{json .}}"]:
    print(json.dumps({"ID": "a" * 64, "Names": "catalog-api", "Image": "catalog:fixture", "State": "running"}))
elif tool == "docker" and args == ["inspect", "--format", "{{.State.Status}}", "a" * 64]:
    print("running")
elif tool == "docker" and args == ["logs", "--timestamps", "--tail", "80", "a" * 64]:
    print("2026-10-10T10:00:00Z synthetic catalog log")
    print("2026-10-10T10:00:01Z synthetic catalog stderr", file=sys.stderr)
else:
    sys.exit(1)
