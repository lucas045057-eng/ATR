"""Append-only acceptance evidence; never turn regressions into product verdicts."""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
from pathlib import Path

FIELDS = {"case_id", "subcase", "SHA", "environment", "model_fingerprint", "run_id",
          "expected", "actual", "verdict", "evidence_path", "failure_reason"}
VERDICTS = {"PASS", "FAIL", "BLOCKED", "NOT_RUN"}
SENSITIVE = re.compile(
    r"(?i)(?:password|api[_-]?key|authorization|secret|access[_-]?token)\s*[:=]"
    r"|\bBearer\s+\S+|-----BEGIN .*PRIVATE KEY-----"
)


def record_case(record: dict, destination: Path) -> None:
    if not isinstance(record, dict) or set(record) != FIELDS:
        raise ValueError("Acceptance record fields do not match the contract")
    if not isinstance(record["verdict"], str) or record["verdict"] not in VERDICTS:
        raise ValueError("Invalid acceptance verdict")
    for key in ("case_id", "subcase", "SHA", "environment"):
        if not isinstance(record[key], str) or not record[key].strip():
            raise ValueError("Acceptance identity is required")
    encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    if SENSITIVE.search(encoded):
        raise ValueError("Sensitive content is forbidden in acceptance records")
    if record["verdict"] in {"PASS", "FAIL"} and (
        not isinstance(record["evidence_path"], str) or not Path(record["evidence_path"]).is_file()
    ):
        raise ValueError("Executed acceptance records require an existing evidence file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("a", encoding="utf-8") as stream:
        stream.write(encoded + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def environment_fingerprint() -> dict:
    result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                            cwd=Path(__file__).resolve().parents[1], timeout=5, check=True)
    return {"SHA": result.stdout.strip(), "os": platform.platform(),
            "python": platform.python_version(), "machine": platform.machine()}
