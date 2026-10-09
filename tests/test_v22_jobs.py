import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from app import jobs
from app.settings import get_settings
from app.storage.database import Database
from test_business import onboard


def incident_for(client):
    profile = onboard(client)
    response = client.post('/api/v1/incidents', json={'system_id': profile['system_id'], 'symptom': 'catalog-api unavailable'})
    assert response.status_code == 200
    return profile, response.json()['id']


def test_concurrent_duplicate_requests_reuse_one_queued_job(client, monkeypatch):
    _, incident = incident_for(client)

    class PausedExecutor:
        def submit(self, function):
            return None

    monkeypatch.setattr(jobs, 'POOL', PausedExecutor())
    barrier = Barrier(8)

    def request():
        barrier.wait()
        return client.post(f'/api/v1/incidents/{incident}/runs')

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: request(), range(8)))
    assert all(r.status_code == 202 for r in responses)
    assert len({r.json()['job_id'] for r in responses}) == 1
    database = Database(get_settings().database_path)
    with database.transaction() as tx:
        assert tx.execute("SELECT COUNT(*) FROM jobs WHERE kind=?", ('investigate:' + incident,)).fetchone()[0] == 1
        assert tx.execute('SELECT COUNT(*) FROM context_budgets WHERE run_id IS NOT NULL').fetchone()[0] == 0


def test_finish_preserves_run_reference_and_cannot_overwrite_interruption(client):
    profile, incident = incident_for(client)
    db = Database(get_settings().database_path)
    job_id = jobs.reserve(db, 'investigate:' + incident, profile['business_id'])
    with db.transaction() as tx:
        tx.execute("UPDATE jobs SET status='interrupted',result_json=?,error='backend_restart' WHERE id=?", (json.dumps({'run_id': 'recorded-run'}), job_id))
    jobs.finish(db, job_id, {'run_id': 'late-run'})
    with db.transaction() as tx:
        row = tx.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        assert row['status'] == 'interrupted'
        assert json.loads(row['result_json'])['run_id'] == 'recorded-run'


def test_failure_does_not_erase_recorded_run_reference(client):
    profile, incident = incident_for(client)
    db = Database(get_settings().database_path)
    job_id = jobs.reserve(db, 'investigate:' + incident, profile['business_id'])
    with db.transaction() as tx:
        tx.execute("UPDATE jobs SET status='running',result_json=? WHERE id=?", (json.dumps({'run_id': 'recorded-run'}), job_id))
    jobs.finish(db, job_id, error='controlled failure')
    with db.transaction() as tx:
        row = tx.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        assert row['status'] == 'failed'
        assert json.loads(row['result_json'])['run_id'] == 'recorded-run'


def test_executor_rejection_finishes_reservation(client, monkeypatch):
    profile, incident = incident_for(client)
    db = Database(get_settings().database_path)
    job_id = jobs.reserve(db, 'investigate:' + incident, profile['business_id'])

    class RejectedExecutor:
        def submit(self, function):
            raise RuntimeError('executor unavailable')

    monkeypatch.setattr(jobs, 'POOL', RejectedExecutor())
    jobs.submit(db, job_id, lambda: {'ok': True})
    with db.transaction() as tx:
        row = tx.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        assert row['status'] == 'failed'
        assert row['ended_at']
    response = client.post(f'/api/v1/incidents/{incident}/runs')
    assert response.status_code == 202


def test_restart_interrupts_malformed_summary_job_and_settles_pending_budget(client):
    from app.context.budget import ContextBudgetManager
    from app.main import app
    from fastapi.testclient import TestClient

    profile, incident = incident_for(client)
    db = Database(get_settings().database_path)
    job_id = jobs.reserve(db, 'investigate:' + incident, profile['business_id'])
    with db.transaction() as tx:
        snapshot = tx.execute('SELECT id FROM graph_snapshots WHERE system_id=? ORDER BY version DESC LIMIT 1', (profile['system_id'],)).fetchone()[0]
        tx.execute("INSERT INTO diagnosis_runs(id,incident_id,graph_snapshot_id,mode,model_config_json,budget_json,started_at,status,summary_json) VALUES('restart-run',?,?, 'normal','{}','{}','2026-10-09','running','[]')", (incident, snapshot))
        tx.execute("UPDATE jobs SET status='running',result_json=? WHERE id=?", (json.dumps({'run_id': 'restart-run'}), job_id))
    manager = ContextBudgetManager(db, get_settings())
    budget_id = manager.create('investigation', profile['business_id'], 'restart-run')
    manager.reserve(budget_id, [{'role': 'user', 'content': 'hello'}], 64)
    with TestClient(app):
        pass
    jobs.finish(db, job_id, {'run_id': 'late-completion'})
    with db.transaction() as tx:
        assert tx.execute("SELECT status FROM diagnosis_runs WHERE id='restart-run'").fetchone()[0] == 'interrupted'
        assert tx.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()[0] == 'interrupted'
        assert tx.execute("SELECT COUNT(*) FROM events WHERE entity_id=? AND event_type='job_interrupted'", (job_id,)).fetchone()[0] == 1
    budget = manager.get(budget_id)
    assert budget['reserved_tokens'] == 0
    assert budget['input_tokens'] == 389
    assert budget['output_tokens'] == 64
    assert budget['usage_records'][0]['usage_kind'] == 'estimate'
