from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from app.common import now, uid
from app.storage.database import Database

POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix="atr-job")


def reserve(database: Database, kind: str, business_id: str | None = None) -> str:
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
            "INSERT INTO jobs(id,business_id,kind,status,created_at) VALUES(?,?,?,'queued',?)",
            (job_id, business_id, kind, now()),
        )
    return job_id


def finish(database, job_id, result=None, error=None):
    with database.transaction() as tx:
        tx.execute(
            "UPDATE jobs SET status=?,result_json=?,error=?,ended_at=? WHERE id=?",
            (
                "failed" if error else "completed",
                json.dumps(result or {}, ensure_ascii=False),
                error,
                now(),
                job_id,
            ),
        )


def submit(database, job_id, function):
    def run():
        with database.transaction() as tx:
            tx.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
        try:
            finish(database, job_id, function())
        except Exception as exc:
            # Exception type + sanitized bounded message; never trace uploaded contents.
            from app.tools.redaction import redact_text

            finish(database, job_id, error=redact_text(f"{type(exc).__name__}: {exc}")[:500])

    POOL.submit(run)
