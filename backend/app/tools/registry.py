from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import HTTPException

from app.settings import Settings
from app.storage.database import Database
from app.tools.redaction import redact_text as sanitize


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_payload(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize(value)
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]
    if isinstance(value, dict):
        return {key: "[REDACTED]" if re.search(r"(?i)password|passwd|secret|token|api.?key|private.?key|authorization|cookie", str(key))
                else sanitize_payload(item) for key, item in value.items()}
    return value


class CollectorClient:
    def __init__(self, settings: Settings):
        self.base = settings.collector_url.rstrip("/")
        self.token = settings.collector_token.get_secret_value()

    def get(self, path: str, timeout: float = 12) -> Any:
        response = httpx.get(self.base + path, headers={"Authorization": f"Bearer {self.token}"}, timeout=timeout)
        response.raise_for_status()
        return response.json()


class ToolRegistry:
    """Only exposes bounded status and log reads through the isolated collector."""
    version = "read-tools-v1"
    policy_version = "low-read-only-v1"

    def __init__(self, db: Database, settings: Settings):
        self.db, self.settings = db, settings
        self.collector = CollectorClient(settings)

    def execute(self, run_id: str, snapshot_id: str, project: str, tool_id: str,
                arguments: dict[str, Any], target_node_id: str | None = None,
                purpose: str = "diagnosis") -> dict[str, Any]:
        allowed = {"compose.status", "container.logs", "container.processes", "health.get"}
        call_id, request_id = str(uuid.uuid4()), str(uuid.uuid4())
        started, tick = utcnow(), time.monotonic()
        denied = tool_id not in allowed
        args_redacted = {key: value for key, value in arguments.items() if key not in {"authorization", "token", "secret"}}
        with self.db.transaction() as tx:
            tx.execute("INSERT INTO tool_calls(id,run_id,snapshot_id,purpose,tool_id,adapter_version,args_redacted_json,risk,policy_version,status,request_id,started_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (call_id, run_id, snapshot_id, purpose, tool_id, self.version,
                        json.dumps(args_redacted, sort_keys=True), "LOW", self.policy_version,
                        "denied" if denied else "pending", request_id, started))
        if denied:
            raise HTTPException(status_code=403, detail="Tool denied by the fixed read-only catalog")
        try:
            if tool_id != "health.get" and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}", project):
                raise ValueError("Invalid authorized Compose project")
            if purpose == "diagnosis":
                from app.context.budget import ContextBudgetManager
                conn = self.db.connect()
                try:
                    budget = conn.execute("SELECT id FROM context_budgets WHERE run_id=? ORDER BY created_at DESC LIMIT 1", (run_id,)).fetchone()
                finally:
                    conn.close()
                if budget:
                    ContextBudgetManager(self.db, self.settings).consume_tool(budget["id"])
            arg_keys = {"compose.status": set(), "health.get": {"url"}, "container.logs": {"container_id", "tail"}, "container.processes": {"container_id"}}
            if set(arguments) - arg_keys[tool_id]:
                raise ValueError("Unsupported tool parameters")
            if tool_id == "compose.status":
                data = self.collector.get(f"/internal/v1/projects/{project}/containers")[:64]
                payload = {"containers": data}
            elif tool_id == "health.get":
                url = str(arguments.get("url", ""))
                if url not in self.settings.health_url_allowlist():
                    raise ValueError("Health URL is not in the configured exact-URL allowlist")
                with httpx.stream("GET", url, timeout=3, follow_redirects=False,
                                  headers={"Accept": "application/json,text/plain;q=0.8"}) as response:
                    raw = bytearray()
                    truncated = False
                    for chunk in response.iter_bytes(chunk_size=4096):
                        raw.extend(chunk[:8001-len(raw)])
                        if len(raw) > 8000:
                            truncated = True
                            break
                    payload = {"url": url, "status_code": response.status_code,
                               "body": sanitize(bytes(raw[:8000]).decode("utf-8", errors="replace")),
                               "redirected": response.is_redirect, "truncated": truncated}
            else:
                container_id = str(arguments.get("container_id", ""))
                if not re.fullmatch(r"[a-f0-9]{12,64}", container_id):
                    raise ValueError("A collector-issued container identifier is required")
                if tool_id == "container.logs":
                    tail = int(arguments.get("tail", 100))
                    if not 1 <= tail <= 400:
                        raise ValueError("Log tail must be from 1 to 400 lines")
                    payload = self.collector.get(f"/internal/v1/projects/{project}/containers/{container_id}/logs?tail={tail}")
                    payload["text"] = sanitize(str(payload.get("text", "")))[:48_000]
                else:
                    payload = self.collector.get(f"/internal/v1/projects/{project}/containers/{container_id}/processes")
            payload = sanitize_payload(payload)
            serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            if purpose == "diagnosis" and budget:
                ContextBudgetManager(self.db, self.settings).retrieval(budget["id"], len(serialized.encode()))
            content_hash = hashlib.sha256(serialized.encode()).hexdigest()
            ended = utcnow()
            evidence_id = str(uuid.uuid4())
            with self.db.transaction() as tx:
                tx.execute("UPDATE tool_calls SET status='succeeded',ended_at=?,duration_ms=? WHERE id=?",
                           (ended, int((time.monotonic()-tick)*1000), call_id))
                tx.execute("INSERT INTO evidence(id,tool_call_id,target_node_id,observed_at,payload_redacted_json,content_hash,truncated,freshness_state) VALUES(?,?,?,?,?,?,?,?)",
                           (evidence_id, call_id, target_node_id, ended, serialized, content_hash,
                            int(bool(payload.get("truncated"))), "current"))
            return {"call_id": call_id, "evidence_id": evidence_id, "tool_id": tool_id,
                    "payload": payload, "observed_at": ended, "content_hash": content_hash,
                    "duration_ms": int((time.monotonic()-tick)*1000)}
        except Exception as exc:
            with self.db.transaction() as tx:
                tx.execute("UPDATE tool_calls SET status=?,ended_at=?,duration_ms=?,error_code=? WHERE id=?",
                           ("timed_out" if isinstance(exc, httpx.TimeoutException) else "failed", utcnow(),
                            int((time.monotonic()-tick)*1000), type(exc).__name__[:80], call_id))
            if isinstance(exc, HTTPException):
                raise
            return {"call_id": call_id, "evidence_id": None, "tool_id": tool_id,
                    "error": "read_failed", "detail": type(exc).__name__}
