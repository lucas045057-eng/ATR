from copy import deepcopy

import pytest
from app.run_outcomes import root_supported
from test_native_result_consumers import native_run


@pytest.mark.parametrize('rule', ['bind_failure', 'permission_denied', 'dependency_unavailable_at_startup', 'upstream_unavailable'])
def test_verified_single_target_root_reaches_metrics_and_exact_recovery(client, monkeypatch, rule):
    from app.connectors.mock import MockConnector
    original = MockConnector.read
    reads = []
    def remote(self, tool, args):
        if tool == 'health.get':
            reads.append(args)
            return {'protocol':'atr-read-ab-v1', 'tool_version':1, 'observation_location':'remote_ssh',
                    'status_code':200, 'endpoint_id':args['endpoint_id'],
                    'url':'http://127.0.0.1:18000/health', 'redirected':False}
        return original(self, tool, args)
    monkeypatch.setattr(MockConnector, 'read', remote)
    run, database, _, runtime = native_run(client, single_rule=rule)
    assert run['repair_eligible'] and root_supported(run['diagnosis_result'])
    assert run['supported_at'] and run['verification_endpoints'][0]['endpoint_id'] == 'proxy-health'
    metrics = client.get('/api/v1/metrics').json()
    assert metrics['supported_runs'] == metrics['goal_completed_runs'] == 1
    assert metrics['direct_cause_runs'] == 0
    response = client.post(f"/api/v1/incidents/{run['incident_id']}/repair", json={
        'summary':'Manual repair', 'operator_verified':True, 'verification_summary':'Checked original authorized endpoint',
        'verification_endpoint_id':'proxy-health'})
    assert response.status_code == 200, response.text
    assert reads == [{'endpoint_id':'proxy-health'}]
    with database.connect() as tx:
        call = tx.execute("SELECT * FROM tool_calls WHERE run_id=? AND purpose='verification'", (run['id'],)).fetchone()
        assert call['target_id'] == runtime['id']
    reviewed = client.post('/api/v1/patterns/' + response.json()['pattern_version_id'] + '/review',
                           json={'decision':'approve', 'reason':'Verified current cause and independent recovery'})
    assert reviewed.status_code == 200, reviewed.text


@pytest.mark.parametrize('change', ['historical', 'direct', 'missing_citation', 'wrong_target', 'wrong_rule', 'gap'])
def test_scoped_root_never_relaxes_history_or_citation_and_grade_guards(client, change):
    run, _, _, _ = native_run(client, single_rule='bind_failure')
    result = deepcopy(run['diagnosis_result'])
    if change == 'historical':
        result.pop('investigation_goal', None)
    elif change == 'direct':
        result['diagnosis_status'] = 'direct_cause_supported'
    elif change == 'missing_citation':
        result['verified_segments'][0]['supports'] = []
    elif change == 'wrong_target':
        result['verified_segments'][0]['supports'][0]['observed_on'] = 'another-service'
    elif change == 'wrong_rule':
        result['verified_segments'][0]['supports'][0]['asserts_about'] = 'unverified-model-claim'
    else:
        result['required_gaps'] = [{'requirement_id':'current_logs'}]
    assert not root_supported(result)


@pytest.mark.parametrize('rule', ['dependency_unavailable_at_startup', 'upstream_unavailable'])
@pytest.mark.parametrize('change', ['missing_role', 'same_participant', 'missing_observation', 'copied_call', 'historical'])
def test_single_cross_segment_requires_complete_independent_participants_and_new_goal(rule, change):
    from test_result_evaluator import complete_state, evaluate
    state, assessments = complete_state()
    state.update(investigation_goal='trigger_cause', required_rule_ids=[rule])
    result = evaluate(state, [a for a in assessments if a['rule_id'] == rule])
    assert result['diagnosis_status'] == 'root_cause_supported'
    segment = result['verified_segments'][0]
    roles = ['consumer', 'dependency'] if rule == 'dependency_unavailable_at_startup' else ['proxy', 'upstream']
    if change == 'missing_role':
        segment['participants'].pop(roles[-1])
    elif change == 'same_participant':
        segment['participants'][roles[-1]] = segment['participants'][roles[0]]
    elif change == 'missing_observation':
        segment['supports'] = [c for c in segment['supports'] if c['observed_on'] == segment['participants'][roles[0]]]
    elif change == 'copied_call':
        for citation in segment['supports']:
            citation['call_id'] = 'one-acquisition'
    else:
        result.pop('investigation_goal')
    assert not root_supported(result)


def test_new_goal_verification_targets_include_consumer_but_history_keeps_proxy_only():
    from app.run_outcomes import verification_node_ids
    from test_result_evaluator import complete_state, evaluate
    state, assessments = complete_state()
    historical = evaluate(state, assessments)
    assert verification_node_ids(historical) == {'proxy'}
    state['investigation_goal'] = 'trigger_cause'
    assert verification_node_ids(evaluate(state, assessments)) == {'app', 'proxy'}


def test_cross_root_verification_cannot_relabel_endpoint_to_another_supported_service(client, monkeypatch):
    from app.connectors.mock import MockConnector
    original = MockConnector.read
    def remote(self, tool, args):
        if tool == 'health.get':
            return {'protocol':'atr-read-ab-v1', 'tool_version':1, 'observation_location':'remote_ssh',
                    'status_code':200, 'endpoint_id':args['endpoint_id'],
                    'url':'http://127.0.0.1:18000/health', 'redirected':False}
        return original(self, tool, args)
    monkeypatch.setattr(MockConnector, 'read', remote)
    run, database, _, _ = native_run(client, single_rule='upstream_unavailable')
    response = client.post(f"/api/v1/incidents/{run['incident_id']}/repair", json={
        'summary':'Manual repair', 'operator_verified':True, 'verification_summary':'Checked endpoint',
        'verification_endpoint_id':'proxy-health'})
    assert response.status_code == 200, response.text
    upstream = run['diagnosis_result']['verified_segments'][0]['participants']['upstream']
    with database.transaction() as tx:
        tx.execute("UPDATE evidence SET target_node_id=? WHERE tool_call_id IN (SELECT id FROM tool_calls WHERE run_id=? AND purpose='verification')", (upstream, run['id']))
    reviewed = client.post('/api/v1/patterns/' + response.json()['pattern_version_id'] + '/review',
                           json={'decision':'approve', 'reason':'Recheck exact endpoint ownership'})
    assert reviewed.status_code == 409
