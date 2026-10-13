import json
import threading
import time

import pytest
from app.agent.ab_investigation import clarification_candidates, effective_limits
from app.agent.decision_runtime import adapt_minimal_decision
from app.runtime.coordinator import get_coordinator
from app.settings import Settings, get_settings
from app.storage.database import Database
from conftest import wait_job
from test_business import onboard
from test_run_target_binding import add_target, confirm_catalog, incident


def test_actual_limits_do_not_hide_missing_supplement_room():
    settings = Settings(_env_file=None, investigation_model_rounds=6)
    with pytest.raises(ValueError, match='余量'):
        effective_limits(settings)
    assert effective_limits(settings.model_copy(update={'ab_supplement_enabled': False}))['hard_model_calls'] == 6
    limits = effective_limits(Settings(_env_file=None))
    assert (limits['soft_model_calls'], limits['soft_tool_calls'], limits['hard_model_calls'], limits['hard_tool_calls']) == (6, 16, 8, 20)


def test_clarification_keeps_actual_previous_container_in_large_service_inventory():
    evidence = [{'evidence_id': 'service-list', 'payload': {'services': [
        {'name': 'noise-' + str(i), 'id': 'noise-' + str(i)} for i in range(60)]}},
        {'evidence_id': 'container-list', 'payload': {'containers': [{'name': 'business-api', 'id': 'a'*64}]}}]
    selected, omitted = clarification_candidates(evidence, [{'kind': 'container', 'runtime_id': 'a'*64}])
    assert selected[0]['runtime_id'] == 'a'*64 and selected[0]['previously_mapped']
    assert len(selected) == 20 and omitted == 41


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
    assert job['status'] == 'waiting_input', job
    run = client.get(f"/api/v1/runs/{started['run_id']}").json()
    assert run['diagnosis_status'] == 'unresolved'
    assert not run['completion_satisfied']
    assert run['clarification']['question_id']
    assert {c['tool_id'] for c in run['tool_calls']} == {'host.info', 'service.list', 'container.list'}
    assert all(c['status'] == 'succeeded' for c in run['tool_calls'])
    assert client.get(f"/api/v1/businesses/{profile['business_id']}").json()['review_status'] == 'pending'
    assert json.loads(run['budget_json'])['max_seconds'] == 600
    wait_reaped(run['id'])
    repeated = client.post(f'/api/v1/incidents/{event_id}/runs', json={'target_id': target['id'], 'decision_protocol': 'ab_v1'}).json()
    assert repeated['reused'] and repeated['job_id'] == started['job_id']
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
    assert run['evidence'] == [] and run['tool_calls'] == []
    assert run['execution_status'] == 'cancelled'
    assert client.get('/api/v1/health').status_code == 200


def test_model_cannot_make_an_uncited_chain_authoritative():
    state = {'decision_protocol': 'ab_v1', 'graph': {'nodes': [], 'edges': []}, 'available_actions': []}
    result = adapt_minimal_decision({'summary': '根因已经定位', 'hypotheses': [], 'next_investigation': '',
                                    'next_action_id': None, 'finish': True, 'finish_reason': '完毕'}, state)
    assert result['finish'] and 'completion_satisfied' not in result
