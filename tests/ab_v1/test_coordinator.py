import json

import pytest
from app.common import now
from app.runtime.coordinator import RunCoordinator
from app.storage.database import Database


@pytest.fixture
def coordinator(tmp_path):
    db = Database(tmp_path / 'coordinator.sqlite3')
    db.initialize()
    with db.transaction() as tx:
        tx.execute("INSERT INTO systems VALUES('s','s','','','','',?,'{}')", (now(),))
        tx.execute("INSERT INTO graph_snapshots VALUES('g','s',1,'','','','','{}','current')")
        for number in range(5):
            tx.execute("INSERT INTO incidents VALUES(?, 's','{}',?,NULL,'open',?)", (f'i{number}', now(), now()))
            tx.execute("INSERT INTO diagnosis_runs(id,incident_id,graph_snapshot_id,mode,model_config_json,budget_json,started_at,status) VALUES(?,?,'g','normal','{\"decision_protocol\":\"ab_v1\"}','{}',?,'queued')", (f'r{number}', f'i{number}', now()))
            tx.execute("INSERT INTO jobs(id,kind,status,created_at) VALUES(?,?,'queued',?)", (f'j{number}', f'investigate:i{number}', now()))
    ticks = [100.0]
    return RunCoordinator(db, clock=lambda: ticks[0]), ticks


def enlist(c, number=0):
    return c.register(f'r{number}', f'j{number}', max_seconds=600)


def message(lease, sequence, kind='heartbeat', payload=None):
    return {'run_id': lease['run_id'], 'lease_token': lease['lease_token'],
            'sequence': sequence, 'type': kind, 'payload': payload or {}}


def result(status='partial'):
    return {'schema_version': 'ab-result-1', 'diagnosis_status': status,
            'completion_satisfied': status in {'root_cause_supported', 'direct_cause_supported', 'no_fault_observed'},
            'verified_segments': [], 'required_gaps': []}


def test_reject_duplicate_out_of_order_and_stale_lease(coordinator):
    c, _ = coordinator
    enlist(c)
    lease = c.claim('r0')
    assert c.accept(message(lease, 1))
    assert not c.accept(message(lease, 1))
    assert not c.accept(message(lease, 3))
    forged = {**message(lease, 2), 'lease_token': 'wrong'}
    assert not c.accept(forged)
    assert c.accept(message(lease, 2, 'progress', {'stage': 'collect'}))


def test_cancel_revokes_lease_and_is_idempotent(coordinator):
    c, _ = coordinator
    enlist(c)
    lease = c.claim('r0')
    c.cancel('r0')
    assert c.cancellation('r0').is_set()
    assert not c.accept(message(lease, 1))
    assert c.cancel('r0')['execution_status'] == 'cancelled'
    assert not c.finalize('r0', 'completed', result('root_cause_supported'), lease_token=lease['lease_token'])
    assert c.public('r0')['diagnosis_status'] == 'unresolved'


def test_capacity_not_released_before_process_reaped(coordinator):
    c, _ = coordinator
    for n in range(4):
        enlist(c, n)
    leases = [c.claim(f'r{n}') for n in range(3)]
    assert c.claim('r3') is None
    c.cancel('r0')
    assert c.claim('r3') is None
    assert c.reaped('r0', leases[0]['lease_token'])
    assert c.claim('r3') is not None


def test_queue_timeout_is_terminal_without_consuming_worker(coordinator):
    c, ticks = coordinator
    enlist(c)
    ticks[0] += 30
    c.tick(ticks[0])
    assert c.public('r0')['execution_status'] == 'timed_out'
    assert c.public('r0')['stop_reason'] == 'queue_timeout'
    assert c.claim('r0') is None


def test_elapsed_time_survives_legacy_total_budget_without_timeout(coordinator):
    c, ticks = coordinator
    enlist(c)
    lease = c.claim('r0')
    for sequence in range(1, 62):
        ticks[0] += 10
        with c.db.transaction() as tx:
            current = result()
            current['verified_segments'] = [{'rule_id': 'check', 'status': 'supported',
                                             'participants': {'service': str(n)}} for n in range(sequence)]
            tx.execute('UPDATE run_executions SET result_json=? WHERE run_id=?', (json.dumps(current), 'r0'))
        assert c.accept(message(lease, sequence, 'progress', {'operation': 'advance'}))
        c.tick(ticks[0])
        assert c.public('r0')['execution_status'] == 'running'
    assert c.public('r0')['elapsed_seconds'] == 610
    assert c.public('r0')['max_seconds'] is None


def test_direct_completion_is_accepted_without_root_timestamp(coordinator):
    c, _ = coordinator
    enlist(c)
    lease = c.claim('r0')
    assert c.finalize('r0', 'completed', result('direct_cause_supported'), lease_token=lease['lease_token'])
    assert c.public('r0')['completion_satisfied'] is True
    with c.db.connect() as tx:
        assert tx.execute("SELECT supported_at FROM diagnosis_runs WHERE id='r0'").fetchone()[0] is None


def test_runtime_finalization_preserves_broker_failure_stage_and_check(coordinator):
    c, _ = coordinator
    enlist(c)
    lease = c.claim('r0')
    diagnostic = {'code':'process_timeout', 'stage':'exchange', 'check_id':'step:7', 'error_category':'deadline'}
    with c.db.transaction() as tx:
        tx.execute("UPDATE diagnosis_runs SET summary_json=? WHERE id='r0'", (json.dumps({'execution_error':diagnostic}),))
    assert c.finalize('r0', 'timed_out', result(), lease_token=lease['lease_token'], reason='process_timeout')
    with c.db.connect() as tx:
        saved = json.loads(tx.execute("SELECT summary_json FROM diagnosis_runs WHERE id='r0'").fetchone()[0])
    assert saved['execution_error'] == diagnostic
    assert saved['runtime_error']['failure_stage'] == saved['runtime_error']['stage'] == 'exchange'
    assert saved['runtime_error']['check_id'] == 'step:7'


def test_completion_requires_a_boolean(coordinator):
    c, _ = coordinator
    enlist(c)
    lease = c.claim('r0')
    invalid = {**result('no_fault_observed'), 'completion_satisfied': 1}
    with pytest.raises(ValueError):
        c.finalize('r0', 'completed', invalid, lease_token=lease['lease_token'])


def test_live_worker_without_semantic_step_progress_times_out(coordinator):
    c, ticks = coordinator
    enlist(c)
    lease = c.claim('r0')
    start = c.public('r0')['progress_at']
    sequence = 0
    for _ in range(20):
        ticks[0] += 10
        sequence += 1
        if not c.accept(message(lease, sequence)):
            break
        sequence += 1
        # Worker text and operation counts cannot certify diagnostic progress.
        c.accept(message(lease, sequence, 'checkpoint', {'summary': str(sequence)}))
        c.tick(ticks[0])
        if c.public('r0')['execution_status'] != 'running':
            break
    final = c.public('r0')
    assert final['execution_status'] == 'timed_out'
    assert final['stop_reason'] == 'step_stagnation_timeout'
    assert final['progress_at'] == start
    assert not c.finalize('r0', 'completed', result('direct_cause_supported'), lease_token=lease['lease_token'])


def test_late_message_cannot_revive_expired_heartbeat(coordinator):
    c, ticks = coordinator
    enlist(c)
    lease = c.claim('r0')
    ticks[0] += 16
    assert not c.accept(message(lease, 1))
    assert c.public('r0')['execution_status'] == 'timed_out'


def test_late_final_result_is_rejected_before_next_supervisor_tick(coordinator):
    c, ticks = coordinator
    enlist(c)
    lease = c.claim('r0')
    ticks[0] += 16
    assert not c.finalize('r0', 'completed', result('no_fault_observed'), lease_token=lease['lease_token'])
    assert c.public('r0')['stop_reason'] == 'worker_liveness_timeout'


def test_live_heartbeats_do_not_extend_an_inflight_step_deadline(coordinator):
    c, ticks = coordinator
    enlist(c)
    lease = c.claim('r0')
    assert c.accept(message(lease, 1, 'progress', {'operation': 'advance'}))
    for sequence in range(2, 10):
        ticks[0] += 10
        assert c.accept(message(lease, sequence))
    ticks[0] += 10
    assert not c.finalize('r0', 'completed', result('direct_cause_supported'), lease_token=lease['lease_token'])
    final = c.public('r0')
    assert final['execution_status'] == 'timed_out' and final['stop_reason'] == 'operation_timeout'
    with c.db.connect() as tx:
        summary = json.loads(tx.execute("SELECT summary_json FROM diagnosis_runs WHERE id='r0'").fetchone()[0])
    assert summary['runtime_error']['check_id'] == 'step:1'
    assert summary['runtime_error']['error_category'] == 'deadline'


def test_persisted_step_resolution_is_observed_before_stagnation_boundary(coordinator):
    c, ticks = coordinator
    enlist(c)
    lease = c.claim('r0')
    for sequence in range(1, 18):
        ticks[0] += 10
        assert c.accept(message(lease, sequence))
    with c.db.transaction() as tx:
        tx.execute("UPDATE run_executions SET result_json=? WHERE run_id='r0'", (json.dumps(result()),))
    ticks[0] += 10
    assert c.accept(message(lease, 18, 'progress', {'operation':'advance'}))
    assert c.public('r0')['progress_at'] == ticks[0]


def test_repeated_evidence_timestamps_and_model_wording_are_not_progress(coordinator):
    c, ticks = coordinator
    enlist(c)
    lease = c.claim('r0')
    for sequence in range(1, 20):
        ticks[0] += 10
        with c.db.transaction() as tx:
            tx.execute("UPDATE diagnosis_runs SET summary_json=? WHERE id='r0'", (json.dumps({
                'evidence':[{'evidence_id':str(sequence), 'observed_at':str(ticks[0])}],
                'report':{'summary':str(sequence)}}),))
            current = result()
            current['model_unknowns'] = [str(sequence)]
            tx.execute("UPDATE run_executions SET result_json=? WHERE run_id='r0'", (json.dumps(current),))
        accepted = c.accept(message(lease, sequence, 'progress', {'operation':'advance'}))
        if not accepted:
            break
    assert c.public('r0')['stop_reason'] == 'step_stagnation_timeout'


def test_execution_and_diagnosis_are_orthogonal(coordinator):
    c, _ = coordinator
    enlist(c)
    lease = c.claim('r0')
    assert c.finalize('r0', 'completed', result(), lease_token=lease['lease_token'])
    assert c.public('r0')['execution_status'] == 'completed'
    assert c.public('r0')['diagnosis_status'] == 'partial'
    assert not c.public('r0')['completion_satisfied']


def test_waiting_input_releases_on_reap_and_preserves_used_budget(coordinator):
    c, ticks = coordinator
    enlist(c)
    lease = c.claim('r0')
    for sequence in range(1, 5):
        ticks[0] += 10
        assert c.accept(message(lease, sequence))
    assert c.wait_for_input('r0', {'question_id': 'q1', 'version': 1}, lease_token=lease['lease_token'])
    assert c.reaped('r0', lease['lease_token'])
    ticks[0] += 300
    fresh = c.resume('r0', 'j0', question_id='q1', version=1)
    assert fresh['elapsed_seconds'] == 40
    next_lease = c.claim('r0')
    assert next_lease['lease_token'] != lease['lease_token']
    ticks[0] += 560
    c.tick(ticks[0])
    assert c.public('r0')['execution_status'] == 'timed_out'


def test_restart_closes_pending_resources_and_does_not_reset_budget(coordinator):
    c, ticks = coordinator
    enlist(c)
    c.claim('r0')
    with c.db.transaction() as tx:
        tx.execute("INSERT INTO context_budgets(id,run_id,phase,limits_json,created_at,reserved_tokens,calls,elapsed_ms) VALUES('b','r0','investigation','{}',?,30,2,1234)", (now(),))
        tx.execute("INSERT INTO model_usage_records(id,budget_id,model,input_tokens,output_tokens,usage_kind,cost_kind,status,created_at) VALUES('u','b','m',10,20,'estimate','unknown','pending',?)", (now(),))
    restarted = RunCoordinator(c.db, clock=lambda: ticks[0])
    assert restarted.recover() == 1
    assert restarted.public('r0')['execution_status'] == 'interrupted'
    with c.db.connect() as tx:
        budget = dict(tx.execute("SELECT * FROM context_budgets WHERE id='b'").fetchone())
        usage = dict(tx.execute("SELECT * FROM model_usage_records WHERE id='u'").fetchone())
    assert budget['reserved_tokens'] == 0
    assert budget['calls'] == 2 and budget['elapsed_ms'] == 1234
    assert usage['status'] == 'interrupted'
    assert json.loads(restarted.public('r0')['result_json'])['diagnosis_status'] == 'unresolved'
    with c.db.connect() as tx:
        diagnostic = json.loads(tx.execute("SELECT summary_json FROM diagnosis_runs WHERE id='r0'").fetchone()[0])['runtime_error']
    assert diagnostic['failure_stage'] == 'model' and diagnostic['check_id'] == 'u'


def test_resume_rejects_stale_answer_and_active_child(coordinator):
    c, _ = coordinator
    enlist(c)
    lease = c.claim('r0')
    c.wait_for_input('r0', {'question_id': 'q', 'version': 1}, lease_token=lease['lease_token'])
    with pytest.raises(ValueError):
        c.resume('r0', 'j0', question_id='q', version=1)
    c.reaped('r0', lease['lease_token'])
    with pytest.raises(ValueError):
        c.resume('r0', 'j0', question_id='q', version=2)
