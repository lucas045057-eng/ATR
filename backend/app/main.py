from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.v1 import router as v1_router
from app.settings import get_settings
from app.storage.database import Database


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    database = Database(settings.database_path)
    database.initialize()
    timestamp = datetime.now(timezone.utc).isoformat()
    with database.transaction() as tx:
        interrupted = tx.execute("""SELECT r.id,r.summary_json,i.system_id FROM diagnosis_runs r
          JOIN incidents i ON i.id=r.incident_id WHERE r.status='running'""").fetchall()
        for run in interrupted:
            try:
                summary = json.loads(run["summary_json"] or "{}")
            except json.JSONDecodeError:
                summary = {}
            summary["interrupted_reason"] = "backend_restarted_before_run_completed"
            tx.execute("UPDATE diagnosis_runs SET status='interrupted',ended_at=?,summary_json=? WHERE id=?",
                       (timestamp, json.dumps(summary), run["id"]))
            sequence = tx.execute("SELECT COALESCE(MAX(sequence_no),0)+1 AS n FROM events WHERE run_id=?",
                                  (run["id"],)).fetchone()["n"]
            tx.execute("INSERT INTO events(id,system_id,run_id,sequence_no,entity_type,entity_id,event_type,actor,payload_redacted_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (str(uuid.uuid4()), run["system_id"], run["id"], sequence, "run", run["id"],
                        "run_interrupted", "runtime", json.dumps({"reason": "backend_restart"}), timestamp))
        tx.execute("UPDATE tool_calls SET status='interrupted',ended_at=?,error_code='backend_restart' WHERE status='pending'", (timestamp,))
    yield


app = FastAPI(
    title="Adaptive Troubleshooting Runtime",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)
app.include_router(health_router, prefix="/api/v1")
app.include_router(v1_router)
