from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from app.common import now, uid
from app.context.budget import interrupt_usage
from app.storage.database import Database

POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix="atr-job")


def reserve(database: Database, kind: str, business_id: str | None = None, target_id: str | None = None) -> str:
    job_id = uid()
    with database.transaction() as tx:
        # Global bounded queue; no unbounded upload/job fanout.
        if (
            tx.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[
                0
            ]
            >= 12
        ):
            raise ValueError("后台任务队列已满，请稍后重试")
        tx.execute(
            "INSERT INTO jobs(id,business_id,kind,status,created_at,target_id) VALUES(?,?,?,'queued',?,?)",
            (job_id, business_id, kind, now(), target_id),
        )
    return job_id


def reserve_investigation(database, incident_id, business_id=None, target_id=None):
    kind = "investigate:" + incident_id
    with database.transaction() as tx:
        active = tx.execute("SELECT id,status,target_id FROM jobs WHERE kind=? AND status IN ('queued','running')", (kind,)).fetchone()
        if active:
            if active["target_id"] == target_id:
                return active['id'], active['status'], False
            raise ValueError("此事件已有绑定到其他或来源未知 SSH 目标的活动任务，请等待结束")
        if tx.execute("SELECT 1 FROM diagnosis_runs WHERE incident_id=? AND status='running'", (incident_id,)).fetchone():
            raise ValueError("调查仍在进行，需等待当前调查结束")
        if business_id and tx.execute("SELECT 1 FROM jobs WHERE business_id=? AND status IN ('queued','running')", (business_id,)).fetchone():
            raise ValueError("当前业务已有活动任务")
        if tx.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] >= 12:
            raise ValueError("后台任务队列已满")
        job_id = uid()
        tx.execute("INSERT INTO jobs(id,business_id,kind,status,created_at,target_id) VALUES(?,?,?,'queued',?,?)", (job_id, business_id, kind, now(), target_id))
        return job_id, 'queued', True


def _fail_investigation(tx, job, error):
    if not job['kind'].startswith('investigate:'):
        return
    incident_id = job['kind'].split(':', 1)[1]
    incident = tx.execute("SELECT system_id FROM incidents WHERE id=?", (incident_id,)).fetchone()
    if not incident:
        return
    try:
        result = json.loads(job['result_json'])
        run_id = result.get('run_id') if isinstance(result, dict) else None
    except (ValueError, TypeError):
        run_id = None
    run = tx.execute("SELECT * FROM diagnosis_runs WHERE id=? AND incident_id=?", (run_id, incident_id)).fetchone() if run_id else tx.execute("SELECT * FROM diagnosis_runs WHERE incident_id=? AND status='running'", (incident_id,)).fetchone()
    stamp = now()
    if run and run['status'] == 'running':
        try:
            summary = json.loads(run['summary_json'])
        except (ValueError, TypeError):
            summary = {}
        if not isinstance(summary, dict):
            summary = {}
        summary['unresolved_reason'] = error
        summary['stop_reason'] = 'background_task_failed'
        tx.execute("UPDATE diagnosis_runs SET status='failed',ended_at=?,summary_json=? WHERE id=? AND status='running'", (stamp, json.dumps(summary), run['id']))
        tx.execute("UPDATE tool_calls SET status='interrupted',ended_at=?,error_code='background_task_failed' WHERE run_id=? AND status='pending'", (stamp, run['id']))
        for usage in tx.execute("SELECT u.* FROM model_usage_records u JOIN context_budgets b ON b.id=u.budget_id WHERE b.run_id=? AND u.status='pending'", (run['id'],)).fetchall():
            interrupt_usage(tx, usage)
    audit_run_id = run['id'] if run else None
    sequence = tx.execute("SELECT COALESCE(MAX(sequence_no),0)+1 FROM events WHERE run_id=?", (audit_run_id,)).fetchone()[0]
    tx.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?)", (uid(), incident['system_id'], audit_run_id, sequence, 'job', job['id'], 'job_failed', 'runtime', json.dumps({'reason': error}), stamp))


def finish(database, job_id, result=None, error=None):
    with database.transaction() as tx:
        job = tx.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job or job['status'] not in {'queued', 'running'}:
            return
        from app.tools.redaction import redact_text
        error = redact_text(str(error))[:500] if error else None
        if error:
            _fail_investigation(tx, job, error)
        tx.execute(
            "UPDATE jobs SET status=?,result_json=?,error=?,ended_at=? WHERE id=?",
            (
                "failed" if error else "completed",
                json.dumps(result, ensure_ascii=False) if result is not None else job['result_json'],
                error,
                now(),
                job_id,
            ),
        )


def submit(database, job_id, function):
    def run():
        try:
            with database.transaction() as tx:
                job = tx.execute("SELECT kind FROM jobs WHERE id=?", (job_id,)).fetchone()
                claimed = tx.execute("UPDATE jobs SET status='running' WHERE id=? AND status='queued'", (job_id,)).rowcount
            if not claimed:
                return
            result = function()
            error = None
            if job['kind'] == 'analyze' and isinstance(result, dict) and result.get('model_error'):
                details = result['model_error']
                error = f"{details['code']}: {details['message']} ({details['exception_type']})"
            finish(database, job_id, result, error)
        except Exception as exc:
            # Exception type + sanitized bounded message; never trace uploaded contents.
            from app.tools.redaction import redact_text

            finish(database, job_id, error=redact_text(f"{type(exc).__name__}: {exc}")[:500])

    try:
        POOL.submit(run)
    except Exception as exc:
        finish(database, job_id, error=f"{type(exc).__name__}: {exc}")
