import json
import threading
import time

import pytest
from app.agent.ab_investigation import (
    InvestigationBroker,
    clarification_candidates,
    effective_limits,
)
from app.agent.decision_runtime import adapt_minimal_decision
from app.runtime.coordinator import get_coordinator
from app.settings import Settings, get_settings
from app.storage.database import Database
from conftest import wait_job
from test_business import onboard
from test_run_target_binding import add_target, confirm_catalog, incident


def test_legacy_soft_and_hard_budgets_do_not_restrict_investigation():
    settings = Settings(_env_file=None, investigation_model_rounds=6)
    limits = effective_limits(settings)
    assert limits['budget_mode'] == 'accounting_only' and limits['max_seconds'] is None
    assert not any(key.startswith(('soft_', 'hard_')) for key in limits)
    assert limits['model_timeout_seconds'] == 45 and limits['ssh_timeout_seconds'] == 10


def test_preparation_failure_cannot_leave_an_orphan_queued_run(client,monkeypatch):
    from app.agent.ab_investigation import prepare_run
    from app.jobs import reserve_investigation
    profile=onboard(client)
    target=add_target(client,profile,'Preparation target')
    event=incident(client,profile)
    database=Database(get_settings().database_path)
    job,_,_=reserve_investigation(database,event,profile['business_id'],target['id'])
    coordinator=get_coordinator(database)
    def fail(tx,run_id,kind,payload):
        raise RuntimeError('synthetic durable-registration failure')
    monkeypatch.setattr(coordinator,'_event',fail)
    with pytest.raises(RuntimeError,match='durable-registration'):
        prepare_run(database,get_settings(),event,profile['business_id'],target['id'],job,'test-operator')
    with database.connect() as tx:
        assert tx.execute('SELECT COUNT(*) FROM diagnosis_runs WHERE incident_id=?',(event,)).fetchone()[0]==0
        assert tx.execute('SELECT COUNT(*) FROM context_budgets WHERE run_id IS NOT NULL').fetchone()[0]==0
        assert tx.execute('SELECT COUNT(*) FROM run_executions').fetchone()[0]==0


def test_clarification_keeps_actual_previous_container_in_large_service_inventory():
    evidence = [{'evidence_id': 'service-list', 'payload': {'services': [
        {'name': 'noise-' + str(i), 'id': 'noise-' + str(i)} for i in range(60)]}},
        {'evidence_id': 'container-list', 'payload': {'containers': [{'name': 'business-api', 'id': 'a'*64}]}}]
    selected, omitted = clarification_candidates(evidence, [{'kind': 'container', 'runtime_id': 'a'*64}])
    assert selected[0]['runtime_id'] == 'a'*64 and selected[0]['previously_mapped']
    assert len(selected) == 20 and omitted == 41


def test_supplement_selects_the_specific_missing_runtime_check():
    broker = object.__new__(InvestigationBroker)
    broker.result = {'required_gaps': [
        {'requirement_id':'current_logs','target_ids':['db']},
        {'requirement_id':'authorized_directed_relationship','target_ids':[]}]}
    actions = [{'action_id':'read-api','target_node_id':'api','tool_id':'container.logs'},
               {'action_id':'read-db','target_node_id':'db','tool_id':'container.logs'}]
    gap, action = broker.gap_action(actions)
    assert gap['target_ids'] == ['db'] and action['action_id'] == 'read-db'


def start(client, profile, target):
    event_id = incident(client, profile)
    response = client.post(f'/api/v1/incidents/{event_id}/runs', json={'target_id': target['id'], 'decision_protocol': 'ab_v1'})
    assert response.status_code == 202, response.text
    return event_id, response.json()


def wait_reaped(run_id):
    coordinator = get_coordinator(Database(get_settings().database_path))
    deadline = time.monotonic() + 15
    while coordinator.public(run_id)['worker_alive'] and time.monotonic() < deadline:
        time.sleep(.05)
    assert not coordinator.public(run_id)['worker_alive']


def test_empty_pending_profile_discovers_and_releases_process(client):
    profile = client.post('/api/v1/businesses', json={'name': 'Empty'}).json()
    target = add_target(client, profile, 'Empty target')
    event_id, started = start(client, profile, target)
    job = wait_job(client, started['job_id'])
    assert job['status'] == 'completed', job
    run = client.get(f"/api/v1/runs/{started['run_id']}").json()
    assert run['diagnosis_status'] == 'unresolved'
    assert not run['completion_satisfied']
    assert not run.get('clarification') and run['summary']['scope_gaps']
    assert run['summary']['task_type'] == 'root_cause_investigation'
    assert {c['tool_id'] for c in run['tool_calls']} == {'host.info', 'service.list', 'container.list','process.list'}
    assert all(c['status'] == 'succeeded' for c in run['tool_calls'])
    assert client.get(f"/api/v1/businesses/{profile['business_id']}").json()['review_status'] == 'pending'
    assert json.loads(run['budget_json'])['max_seconds'] is None
    assert run['summary']['effective_limits']['budget_mode'] == 'accounting_only'
    wait_reaped(run['id'])
    repeated = client.post(f'/api/v1/incidents/{event_id}/runs', json={'target_id': target['id'], 'decision_protocol': 'ab_v1'}).json()
    assert not repeated['reused'] and repeated['job_id'] != started['job_id']
    assert wait_job(client, repeated['job_id'])['status'] == 'completed'
    wait_reaped(repeated['run_id'])
    stale = client.post(f"/api/v1/runs/{run['id']}/resume", json={'question_id':'stale','version':1})
    assert stale.status_code == 409


def test_pending_material_does_not_prevent_authorized_detailed_reads(client, monkeypatch):
    profile = onboard(client)
    target = add_target(client, profile, 'Authorized runtime')
    confirm_catalog(client, profile, target)

    def choose(database, settings, budget_id, prompt, context, validate, **kwargs):
        return validate({'summary': '继续读取当前授权对象。', 'hypotheses': [], 'next_investigation': '读取当前状态和日志。',
                         'next_action_id': context['available_actions'][0]['action_id'], 'finish': False, 'finish_reason': None})
    monkeypatch.setattr('app.agent.ab_investigation.call_json', choose)
    _, started = start(client, profile, target)
    job = wait_job(client, started['job_id'])
    assert job['status'] == 'completed', job
    run = client.get(f"/api/v1/runs/{started['run_id']}").json()
    assert {'service.status', 'service.logs'} <= {c['tool_id'] for c in run['tool_calls']}
    assert run['diagnosis_status'] in {'partial', 'unresolved'}
    assert run['summary']['required_gaps']
    assert not run['completion_satisfied']
    wait_reaped(run['id'])


def test_cancel_during_model_blocks_late_evidence_and_reuses_same_job(client, monkeypatch):
    profile = onboard(client)
    target = add_target(client, profile, 'One target')
    confirm_catalog(client, profile, target)

    entered = threading.Event()

    def pending(*args, supervision=None, **kwargs):
        entered.set()
        while not supervision['cancelled'].wait(.02):
            pass
        raise InterruptedError('cancelled')
    monkeypatch.setattr('app.agent.ab_investigation.call_json', pending)
    event_id, started = start(client, profile, target)
    duplicate = client.post(f'/api/v1/incidents/{event_id}/runs', json={'target_id': target['id'], 'decision_protocol': 'ab_v1'}).json()
    assert duplicate['job_id'] == started['job_id'] and duplicate['reused']
    run_id = started['run_id']
    assert entered.wait(5)
    assert client.post(f'/api/v1/runs/{run_id}/cancel').json()['execution_status'] == 'cancelled'
    assert client.post(f'/api/v1/runs/{run_id}/cancel').json()['execution_status'] == 'cancelled'
    wait_reaped(run_id)
    run = client.get(f'/api/v1/runs/{run_id}').json()
    assert len(run['evidence']) == len(run['tool_calls']) == 3
    assert {call['tool_id'] for call in run['tool_calls']} == {'service.list', 'service.status', 'service.logs'}
    assert all(call['status'] == 'succeeded' for call in run['tool_calls'])
    assert run['execution_status'] == 'cancelled'
    assert client.get('/api/v1/health').status_code == 200


def test_model_cannot_make_an_uncited_chain_authoritative():
    state = {'decision_protocol': 'ab_v1', 'graph': {'nodes': [], 'edges': []}, 'available_actions': []}
    result = adapt_minimal_decision({'summary': '根因已经定位', 'hypotheses': [], 'next_investigation': '',
                                    'next_action_id': None, 'finish': True, 'finish_reason': '完毕'}, state)
    assert result['finish'] and 'completion_satisfied' not in result


def test_model_outage_finishes_fact_collection_without_retry_or_claiming_root_cause(client, monkeypatch):
    from app.context.model import ModelUnavailable
    profile = onboard(client)
    target = add_target(client, profile, 'Existing authorized object')
    confirm_catalog(client, profile, target)
    attempted = []
    def unavailable(*args, **kwargs):
        attempted.append(True)
        raise ModelUnavailable('unavailable')
    monkeypatch.setattr('app.agent.ab_investigation.call_json', unavailable)
    _, begun = start(client, profile, target)
    assert wait_job(client, begun['job_id'])['status'] == 'completed'
    run = client.get('/api/v1/runs/'+begun['run_id']).json()
    assert len(attempted) == 1
    assert {call['tool_id'] for call in run['tool_calls']} == {'service.list', 'service.status', 'service.logs'}
    assert run['execution_status'] == 'completed' and run['summary']['stop_reason'] == 'offline_checks_complete'
    assert run['summary']['model_status'] == 'failed' and run['summary']['analysis_status'] == 'offline'
    assert not run['completion_satisfied'] and not run['repair_eligible']
    wait_reaped(run['id'])
