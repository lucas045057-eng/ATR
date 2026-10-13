from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.business import router as business_router
from app.api.health import router as health_router
from app.api.v1 import router as v1_router
from app.connectors.credentials import SSH_SESSIONS
from app.context.budget import interrupt_usage
from app.runtime.coordinator import get_coordinator
from app.settings import get_settings
from app.storage.database import Database


@asynccontextmanager
async def lifespan(_: FastAPI):
    SSH_SESSIONS.clear()
    settings = get_settings()
    database = Database(settings.database_path)
    database.initialize()
    coordinator = get_coordinator(database)
    coordinator.recover()
    timestamp = datetime.now(timezone.utc).isoformat()
    with database.transaction() as tx:
        interrupted = tx.execute("""SELECT r.id,r.summary_json,i.system_id FROM diagnosis_runs r
          JOIN incidents i ON i.id=r.incident_id WHERE r.status='running'""").fetchall()
        for run in interrupted:
            try:
                summary = json.loads(run["summary_json"] or "{}")
            except json.JSONDecodeError:
                summary = {}
            if not isinstance(summary, dict):
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
        for job in tx.execute("SELECT * FROM jobs WHERE status IN ('queued','running') AND kind LIKE 'investigate:%'").fetchall():
            incident = tx.execute("SELECT system_id FROM incidents WHERE id=?", (job['kind'].split(':', 1)[1],)).fetchone()
            if not incident:
                continue
            try:
                result = json.loads(job['result_json'])
                run_id = result.get('run_id') if isinstance(result, dict) else None
            except (ValueError, TypeError):
                run_id = None
            if run_id and not tx.execute("SELECT 1 FROM diagnosis_runs WHERE id=?", (run_id,)).fetchone():
                run_id = None
            sequence = tx.execute("SELECT COALESCE(MAX(sequence_no),0)+1 FROM events WHERE run_id=?", (run_id,)).fetchone()[0]
            tx.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), incident['system_id'], run_id, sequence, 'job', job['id'], 'job_interrupted', 'runtime', json.dumps({'reason':'backend_restart'}), timestamp))
        tx.execute("UPDATE jobs SET status='interrupted',error='backend_restart',ended_at=? WHERE status IN ('queued','running')", (timestamp,))
        tx.execute("UPDATE uploaded_artifacts SET status='failed',error='backend_restart' WHERE status='queued'")
        tx.execute("UPDATE discovery_runs SET status='interrupted' WHERE status='running'")
        tx.execute("UPDATE runtime_observations SET status='interrupted',error='backend_restart' WHERE status='pending'")
        for usage in tx.execute("SELECT * FROM model_usage_records WHERE status='pending'").fetchall():
            interrupt_usage(tx, usage)
    async def purge_passwords():
        while True:
            await asyncio.sleep(30)
            SSH_SESSIONS.purge()

    async def supervise_runs():
        from app.agent.ab_investigation import dispatch_ready
        iteration = 0
        while True:
            await asyncio.sleep(1)
            iteration += 1
            if iteration % 5 == 0:
                await asyncio.to_thread(coordinator.tick)
            await asyncio.to_thread(dispatch_ready, database, settings)

    purge_task = asyncio.create_task(purge_passwords())
    supervisor_task = asyncio.create_task(supervise_runs())
    try:
        yield
    finally:
        purge_task.cancel()
        supervisor_task.cancel()
        with suppress(asyncio.CancelledError):
            await purge_task
        with suppress(asyncio.CancelledError):
            await supervisor_task
        SSH_SESSIONS.clear()


app = FastAPI(
    title="Adaptive Troubleshooting Runtime",
    version="2.2.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)
app.include_router(health_router, prefix="/api/v1")
app.include_router(v1_router)
app.include_router(business_router)
frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if frontend_dist.is_dir():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="workbench")
