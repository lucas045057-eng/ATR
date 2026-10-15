import json

import pytest
from app.agent.ab_investigation import InvestigationBroker, prepare_run
from app.common import dumps
from app.jobs import reserve_investigation
from app.runtime.coordinator import get_coordinator
from app.settings import get_settings
from app.storage.database import Database
from conftest import wait_job
from test_investigation_broker import wait_reaped
from test_run_target_binding import add_target, incident


def waiting(client):
    # Persisted pre-P0.5 native runs still use the original independent approval flow.
    profile = client.post('/api/v1/businesses', json={'name':'Empty local question'}).json()
    assert profile['cognition_state'] == 'empty'
    target = add_target(client, profile, 'Authorized target')
    database = Database(get_settings().database_path)
    event = incident(client, profile)
    job, _, _ = reserve_investigation(database, event, profile['business_id'], target['id'])
    run_id = prepare_run(database, get_settings(), event, profile['business_id'], target['id'], job, 'test')
    with database.transaction() as tx:
        configuration = json.loads(tx.execute('SELECT model_config_json FROM diagnosis_runs WHERE id=?', (run_id,)).fetchone()[0])
        configuration.pop('scope_identity_required')
        configuration.pop('scope_grants')
        tx.execute('UPDATE diagnosis_runs SET model_config_json=? WHERE id=?', (dumps(configuration), run_id))
    coordinator = get_coordinator(database)
    lease = coordinator.claim(run_id)
    broker = InvestigationBroker(database, get_settings(), lease)
    for _ in range(8):
        if not broker.advance()['continue']:
            break
    else:
        raise AssertionError('Historical independent authorization did not wait finitely')
    coordinator.reaped(run_id, lease['lease_token'])
    assert wait_job(client, job)['status'] == 'waiting_input'
    run = client.get('/api/v1/runs/'+run_id).json()
    wait_reaped(run['id'])
    question = run['clarification']
    option = question['options'][0]
    return profile, run, {'question_id':question['question_id'],'version':question['version'],
                         'selections':[{'option_id':option['option_id']}], 'confirm_owned':True}


@pytest.mark.parametrize('change', ['missing_confirmation','unknown_option','stale_version','duplicate_option'])
def test_bad_answer_cannot_add_a_mapping(client, change):
    profile, run, answer = waiting(client)
    if change == 'missing_confirmation':
        answer['confirm_owned'] = False
    elif change == 'unknown_option':
        answer['selections'][0]['option_id'] = 'not-current-candidate'
    elif change == 'stale_version':
        answer['version'] += 1
    else:
        answer['selections'] *= 2
    response = client.post('/api/v1/runs/'+run['id']+'/resume', json=answer)
    assert response.status_code in {409,422}, response.text
    with Database(get_settings().database_path).connect() as tx:
        assert tx.execute('SELECT COUNT(*) FROM service_bindings WHERE business_id=?', (profile['business_id'],)).fetchone()[0] == 0
    assert client.get('/api/v1/runs/'+run['id']).json()['execution_status'] == 'waiting_input'


def test_empty_profile_confirmed_object_is_readable_without_approving_profile(client, monkeypatch):
    profile, run, answer = waiting(client)
    before = run['context_budget']['tool_calls']
    def choose(database, settings, budget_id, prompt, context, validate, **kwargs):
        return validate({'summary':'检查当前已授权的对象。','hypotheses':[], 'next_investigation':'读取状态与日志。',
                         'next_action_id':context['available_actions'][0]['action_id'], 'finish':False, 'finish_reason':None})
    monkeypatch.setattr('app.agent.ab_investigation.call_json', choose)
    resumed = client.post('/api/v1/runs/'+run['id']+'/resume', json=answer)
    assert resumed.status_code == 202, resumed.text
    assert resumed.json()['run_id'] == run['id']
    assert wait_job(client, resumed.json()['job_id'])['status'] == 'completed'
    final = client.get('/api/v1/runs/'+run['id']).json()
    assert final['context_budget']['tool_calls'] >= before+2
    assert {'host.info','service.list','container.list'} <= {row['tool_id'] for row in final['tool_calls']}
    profile_after = client.get('/api/v1/businesses/'+profile['business_id']).json()
    assert profile_after['review_status'] == 'pending' and profile_after['services'] == []
    assert profile_after['cognition_state'] == 'partial'
    bindings = client.get('/api/v1/businesses/'+profile['business_id']+'/bindings').json()
    assert all(row['authorization_valid'] and row['authorization']['identity_mode']=='exact_instance'
               for row in bindings if row['status']=='confirmed')
    assert not final['completion_satisfied']
    wait_reaped(run['id'])
