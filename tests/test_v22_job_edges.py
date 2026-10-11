import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

from app import jobs
from app.common import now, uid
from app.context.budget import ContextBudgetManager
from app.settings import get_settings
from app.storage.database import Database
from conftest import wait_job
from test_investigation import approved_scope
from test_v22_jobs import incident_for


def database_for_client():
    return Database(get_settings().database_path)


def insert_run(database, profile, incident, job_id, summary=None):
    run_id = uid()
    with database.transaction() as tx:
        snapshot = tx.execute(
            "SELECT id FROM graph_snapshots WHERE system_id=? ORDER BY version DESC LIMIT 1",
            (profile["system_id"],),
        ).fetchone()[0]
        tx.execute(
            "INSERT INTO diagnosis_runs(id,incident_id,graph_snapshot_id,mode,"
            "model_config_json,budget_json,started_at,status,summary_json) "
            "VALUES(?,?,?,'normal','{}','{}',?,'running',?)",
            (run_id, incident, snapshot, now(), json.dumps(summary or {})),
        )
        tx.execute(
            "UPDATE jobs SET result_json=? WHERE id=?",
            (json.dumps({"run_id": run_id}), job_id),
        )
    return run_id


def wait_terminal(database, job_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with database.transaction() as tx:
            row = dict(tx.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        if row["status"] not in {"queued", "running"}:
            return row
        time.sleep(0.01)
    raise AssertionError("Job did not reach a terminal status")


def test_real_concurrent_requests_produce_one_run_and_budget(client, monkeypatch):
    profile, incident = incident_for(client)
    release = Event()
    blocked = Event()
    database = database_for_client()

    def hold_worker():
        blocked.set()
        assert release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as workers:
        monkeypatch.setattr(jobs, "POOL", workers)
        workers.submit(hold_worker)
        assert blocked.wait(3)
        barrier = Barrier(8)

        def request():
            barrier.wait(timeout=5)
            return client.post(f"/api/v1/incidents/{incident}/runs")

        try:
            with ThreadPoolExecutor(max_workers=8) as callers:
                responses = list(callers.map(lambda _: request(), range(8)))
            assert [r.status_code for r in responses] == [202] * 8
            job_ids = {r.json()["job_id"] for r in responses}
            assert len(job_ids) == 1
            job_id = job_ids.pop()
            assert sum(not r.json()["reused"] for r in responses) == 1
            with database.transaction() as tx:
                assert tx.execute(
                    "SELECT COUNT(*) FROM diagnosis_runs WHERE incident_id=?", (incident,)
                ).fetchone()[0] == 0
                assert tx.execute(
                    "SELECT COUNT(*) FROM jobs WHERE kind=?", ("investigate:" + incident,)
                ).fetchone()[0] == 1
        finally:
            release.set()
        job = wait_job(client, job_id)
        assert job["status"] == "completed", job

    with database.transaction() as tx:
        assert tx.execute(
            "SELECT COUNT(*) FROM diagnosis_runs WHERE incident_id=?", (incident,)
        ).fetchone()[0] == 1
        budgets = tx.execute(
            "SELECT b.* FROM context_budgets b JOIN diagnosis_runs r ON r.id=b.run_id "
            "WHERE r.incident_id=?", (incident,),
        ).fetchall()
        assert len(budgets) == 1
        assert budgets[0]["business_id"] == profile["business_id"]
        assert budgets[0]["calls"] == 0


def test_running_duplicate_reuses_existing_job_and_budget(client, monkeypatch):
    from app.tools.business import BusinessToolRegistry

    profile, _ = approved_scope(client)
    incident = client.post(
        "/api/v1/incidents",
        json={"system_id": profile["system_id"], "symptom": "catalog-api unavailable"},
    ).json()["id"]
    entered, release = Event(), Event()
    original = BusinessToolRegistry.execute

    def controlled_read(self, action):
        entered.set()
        assert release.wait(10)
        return original(self, action)

    monkeypatch.setattr(BusinessToolRegistry, "execute", controlled_read)
    try:
        first = client.post(f"/api/v1/incidents/{incident}/runs")
        assert first.status_code == 202
        assert entered.wait(3)
        repeated = client.post(f"/api/v1/incidents/{incident}/runs")
        assert repeated.status_code == 202
        assert repeated.json() == {
            "job_id": first.json()["job_id"], "status": "running", "reused": True,
        }
        with database_for_client().transaction() as tx:
            assert tx.execute(
                "SELECT COUNT(*) FROM diagnosis_runs WHERE incident_id=?", (incident,)
            ).fetchone()[0] == 1
            assert tx.execute(
                "SELECT COUNT(*) FROM context_budgets b JOIN diagnosis_runs r "
                "ON r.id=b.run_id WHERE r.incident_id=?", (incident,),
            ).fetchone()[0] == 1
    finally:
        release.set()
    assert wait_job(client, first.json()["job_id"])["status"] == "completed"


def test_failure_settles_reserved_usage_without_charging_another_run(client, monkeypatch):
    profile, incident = incident_for(client)
    database = database_for_client()
    manager = ContextBudgetManager(database, get_settings())
    job_id = jobs.reserve(database, "investigate:" + incident, profile["business_id"])
    run_id = insert_run(database, profile, incident, job_id, {"checkpoint": "evidence_kept"})
    budget_id = manager.create("investigation", profile["business_id"], run_id)
    reservation = manager.reserve(budget_id, [{"role": "user", "content": "hello"}], 64)
    manager.start_attempt(budget_id, reservation)
    other_budget = manager.create("investigation")
    manager.reserve(other_budget, [{"role": "user", "content": "other"}], 20)
    with database.transaction() as tx:
        tx.execute(
            "INSERT INTO tool_calls(id,run_id,purpose,tool_id,adapter_version,"
            "args_redacted_json,risk,policy_version,status,request_id,started_at) "
            "VALUES(?,?,'diagnosis','service.status','test','{}','read','test','pending',?,?)",
            (uid(), run_id, uid(), now()),
        )

    def fail():
        raise RuntimeError("api_key=private-test-secret")

    with ThreadPoolExecutor(max_workers=1) as workers:
        monkeypatch.setattr(jobs, "POOL", workers)
        jobs.submit(database, job_id, fail)
        row = wait_terminal(database, job_id)
    assert row["status"] == "failed"
    assert json.loads(row["result_json"]) == {"run_id": run_id}
    assert "private-test-secret" not in row["error"]
    budget = manager.get(budget_id)
    assert budget["calls"] == 1
    assert budget["reserved_tokens"] == 0
    assert budget["input_tokens"] == reservation[1]
    assert budget["output_tokens"] == 64
    assert budget["usage_records"][0]["usage_kind"] == "estimate"
    assert budget["usage_records"][0]["status"] == "interrupted"
    audit = budget["usage_records"][0]["diagnostic"]
    assert audit['usage_status'] == 'interrupted' and audit['usage_kind'] == 'estimate'
    assert audit['ended_at'] is None and audit['recovered_at']
    untouched = manager.get(other_budget)
    assert untouched["reserved_tokens"] > 0
    assert untouched["input_tokens"] == untouched["output_tokens"] == 0
    assert untouched["usage_records"][0]["status"] == "pending"
    with database.transaction() as tx:
        run = tx.execute("SELECT * FROM diagnosis_runs WHERE id=?", (run_id,)).fetchone()
        assert run["status"] == "failed"
        assert run["ended_at"]
        assert json.loads(run["summary_json"])["checkpoint"] == "evidence_kept"
        assert tx.execute(
            "SELECT status FROM tool_calls WHERE run_id=?", (run_id,)
        ).fetchone()[0] == "interrupted"
        event = tx.execute(
            "SELECT * FROM events WHERE entity_id=? AND event_type='job_failed'", (job_id,),
        ).fetchone()
        assert event["run_id"] == run_id
        assert "private-test-secret" not in event["payload_redacted_json"]
    jobs.finish(database, job_id, error="late failure")
    assert manager.get(budget_id)["input_tokens"] == reservation[1]


def test_duplicate_worker_submission_executes_only_once(client, monkeypatch):
    profile, incident = incident_for(client)
    database = database_for_client()
    job_id = jobs.reserve(database, "investigate:" + incident, profile["business_id"])
    manager = ContextBudgetManager(database, get_settings())
    release, entered = Event(), Event()

    def execute():
        run_id = insert_run(database, profile, incident, job_id)
        manager.create("investigation", profile["business_id"], run_id)
        entered.set()
        assert release.wait(10)
        with database.transaction() as tx:
            tx.execute(
                "UPDATE diagnosis_runs SET status='unresolved',ended_at=? WHERE id=?",
                (now(), run_id),
            )
        return {"id": run_id, "status": "unresolved"}

    with ThreadPoolExecutor(max_workers=2) as workers:
        monkeypatch.setattr(jobs, "POOL", workers)
        try:
            jobs.submit(database, job_id, execute)
            assert entered.wait(3)
            jobs.submit(database, job_id, execute)
        finally:
            release.set()
    with database.transaction() as tx:
        assert tx.execute(
            "SELECT COUNT(*) FROM diagnosis_runs WHERE incident_id=?", (incident,)
        ).fetchone()[0] == 1
        assert tx.execute(
            "SELECT COUNT(*) FROM context_budgets WHERE run_id IS NOT NULL"
        ).fetchone()[0] == 1
        assert tx.execute(
            "SELECT status FROM jobs WHERE id=?", (job_id,)
        ).fetchone()[0] == "completed"


def test_duplicate_reservation_survives_full_queue(client):
    profile, incident = incident_for(client)
    database = database_for_client()
    with database.connect() as connection:
        target_id = connection.execute("SELECT id FROM runtime_targets WHERE business_id=? LIMIT 1", (profile["business_id"],)).fetchone()["id"]
    existing = jobs.reserve(database, "investigate:" + incident, profile["business_id"], target_id)
    for i in range(11):
        jobs.reserve(database, f"other:{i}")
    response = client.post(f"/api/v1/incidents/{incident}/runs")
    assert response.status_code == 202
    assert response.json() == {"job_id": existing, "status": "queued", "reused": True}
    with database.transaction() as tx:
        assert tx.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0] == 12


def test_api_executor_rejection_returns_terminal_status_matching_poll(client, monkeypatch):
    _, incident = incident_for(client)
    workers = ThreadPoolExecutor(max_workers=1)
    workers.shutdown(wait=True)
    monkeypatch.setattr(jobs, "POOL", workers)
    response = client.post(f"/api/v1/incidents/{incident}/runs")
    assert response.status_code == 202
    polled = client.get(f"/api/v1/jobs/{response.json()['job_id']}").json()
    assert polled["status"] == "failed"
    assert response.json()["status"] == polled["status"]
    assert polled["ended_at"]


def test_queued_restart_retains_audit_and_never_runs_stale_submission(client, monkeypatch):
    from app.main import app
    from fastapi.testclient import TestClient

    profile, incident = incident_for(client)
    database = database_for_client()
    job_id = jobs.reserve(database, "investigate:" + incident, profile["business_id"])
    for _ in range(2):
        with TestClient(app):
            pass
    with ThreadPoolExecutor(max_workers=1) as workers:
        monkeypatch.setattr(jobs, "POOL", workers)
        jobs.submit(database, job_id, lambda: insert_run(database, profile, incident, job_id))
    with database.transaction() as tx:
        job = tx.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["status"] == "interrupted"
        assert job["error"] == "backend_restart"
        assert job["ended_at"]
        assert tx.execute(
            "SELECT COUNT(*) FROM diagnosis_runs WHERE incident_id=?", (incident,)
        ).fetchone()[0] == 0
        event = tx.execute(
            "SELECT * FROM events WHERE entity_id=? AND event_type='job_interrupted'", (job_id,),
        ).fetchall()
        assert len(event) == 1
        assert event[0]["system_id"] == profile["system_id"]
        assert event[0]["run_id"] is None
