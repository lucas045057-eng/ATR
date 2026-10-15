"""Daily native investigations verify existing grants without another approval."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from app.connectors.mock import MockConnector
from app.settings import get_settings
from app.storage.database import Database
from conftest import wait_job
from test_business import onboard
from test_run_target_binding import add_target, incident
from test_runtime import discover, target
from test_v22_scope import current_binding, setup_binding


def start_native(client, profile, runtime):
    event = incident(client, profile)
    response = client.post(f'/api/v1/incidents/{event}/runs', json={
        'target_id': runtime['id'], 'decision_protocol': 'ab_v1',
    })
    assert response.status_code == 202, response.text
    started = response.json()
    job = wait_job(client, started['job_id'])
    run = client.get('/api/v1/runs/' + started['run_id']).json()
    return job, run


@pytest.mark.parametrize('days', [2, 31])
def test_old_approval_survives_restart_and_daily_run_revalidates_identity(client, days):
    profile, runtime, binding = setup_binding(client)
    database = Database(get_settings().database_path)
    stamp = datetime.now(timezone.utc) - timedelta(days=days)
    with database.transaction() as tx:
        grant = json.loads(tx.execute('SELECT authorization_json FROM service_bindings WHERE id=?', (binding['id'],)).fetchone()[0])
        grant['confirmed_at'] = stamp.isoformat()
        tx.execute('UPDATE service_bindings SET observed_at=?,expires_at=?,authorization_json=? WHERE id=?',
                   (stamp.isoformat(), (stamp + timedelta(days=1)).isoformat(), json.dumps(grant), binding['id']))
        tx.execute("UPDATE business_reviews SET created_at=? WHERE subject_id=? AND decision='confirm'", (stamp.isoformat(), binding['id']))
    database.initialize()
    old = current_binding(client, profile, binding)
    assert old['authorization_valid'] is True
    assert old['runtime_current'] is False  # Old identity observation is still old.
    job, run = start_native(client, profile, runtime)
    assert job['status'] == 'completed', job
    assert not run.get('clarification') and run['execution_status'] != 'waiting_input'
    tools = [call['tool_id'] for call in run['tool_calls']]
    assert tools.index('service.list') < tools.index('service.status')
    assert 'service.logs' not in tools  # A stopped instance already satisfies the direct goal.
    assert all(call['status'] == 'succeeded' for call in run['tool_calls'] if call['tool_id'] in {'service.status','service.logs'})
    assert current_binding(client, profile, binding)['authorization']['confirmed_at'] == old['authorization']['confirmed_at']
    assert run['summary']['scope_validation']['verified_count'] == 1
    assert all(e['observed_at'] >= run['started_at'] for e in run['evidence'])


def test_unknown_objects_do_not_pause_authorized_checks_or_get_deep_reads(client):
    profile, runtime, _ = setup_binding(client)
    job, run = start_native(client, profile, runtime)
    assert job['status'] == 'completed', job
    assert any(call['tool_id'] == 'service.status' for call in run['tool_calls'])
    assert any(call['tool_id'] == 'service.status' and call['status'] == 'succeeded' for call in run['tool_calls'])
    unknown = run['summary']['unauthorized_objects']
    assert any(row['name'] == 'unrelated-backup' and row['evidence_id'] for row in unknown)
    assert all('unrelated-backup' not in call['args_redacted_json']
               for call in run['tool_calls'] if call['tool_id'] in {'service.status','service.logs'})
    assert not run.get('clarification')
    report = run['summary']['report']['fact_report']
    assert report['fault_directions'] and all(d['evidence_level']=='direct_observation' for d in report['fault_directions'])
    assert report['repair_advice'] and report['root_cause_verified'] is False


def test_no_approved_objects_finishes_with_scope_gap_and_releases_worker(client):
    profile = client.post('/api/v1/businesses', json={'name':'Unfamiliar empty business'}).json()
    runtime = add_target(client, profile, 'Explicit local Mock target')
    job, run = start_native(client, profile, runtime)
    assert job['status'] == 'completed', job
    assert run['diagnosis_status'] == 'unresolved' and not run['repair_eligible']
    assert run['summary']['stop_reason'] == 'no_verified_authorized_objects'
    assert run['summary']['unauthorized_objects'] and run['summary']['scope_gaps']
    assert not run.get('clarification')
    assert any(g.get('reason') and g['requirement_id']=='business_object_authorization_required'
               for g in run['summary']['report']['fact_report']['missing_evidence'])


def test_explicit_stable_rule_accepts_only_the_same_object_identity(client, monkeypatch):
    profile = onboard(client)
    runtime = target(client, profile, docker=True)
    found = discover(client, profile, runtime)
    binding = next(row for row in found['matches'] if row['kind'] == 'container' and row['business_service'] == 'catalog-api')
    response = client.post(f"/api/v1/businesses/{profile['business_id']}/bindings/{binding['id']}/review", json={
        'decision':'confirm', 'reason':'Approve exact name image ports and environment-key replacement rule',
        'identity_mode':'stable_object',
    })
    assert response.status_code == 200, response.text
    original = MockConnector.read
    reads = []

    def replaced(self, tool, args):
        reads.append((tool, dict(args)))
        payload = original(self, tool, args)
        if tool == 'container.list':
            payload['containers'][0]['id'] = 'c' * 64
        return payload

    monkeypatch.setattr(MockConnector, 'read', replaced)
    job, run = start_native(client, profile, runtime)
    assert job['status'] == 'completed', job
    assert ('container.status', {'container_id':'c' * 64}) in reads
    assert run['summary']['scope_validation']['verified_count'] == 1
    assert not run.get('clarification')
    rows = client.get(f"/api/v1/businesses/{profile['business_id']}/bindings").json()
    current = next(row for row in rows if row['status'] == 'confirmed')
    assert current['authorization']['grant_id'] == binding['id']


def test_revocation_persists_and_prevents_daily_deep_reads(client):
    profile, runtime, binding = setup_binding(client)
    response = client.post(f"/api/v1/businesses/{profile['business_id']}/bindings/{binding['id']}/revoke", json={
        'reason':'Owner explicitly revoked this business object',
    })
    assert response.status_code == 200, response.text
    Database(get_settings().database_path).initialize()
    job, run = start_native(client, profile, runtime)
    assert job['status'] == 'completed', job
    assert not any(call['tool_id'] in {'service.status','service.logs'} for call in run['tool_calls'])
    assert current_binding(client, profile, binding)['authorization_valid'] is False
    assert current_binding(client, profile, binding)['authorization']['revoked_at']


@pytest.mark.parametrize('change', [{'image':'another-business:latest'}, {'name':'another-business-api'}, {'ports':[9999]}])
def test_stable_rule_does_not_authorize_a_changed_identity(client, monkeypatch, change):
    profile = onboard(client)
    runtime = target(client, profile, docker=True)
    found = discover(client, profile, runtime)
    binding = next(row for row in found['matches'] if row['kind'] == 'container' and row['business_service'] == 'catalog-api')
    assert client.post(f"/api/v1/businesses/{profile['business_id']}/bindings/{binding['id']}/review", json={
        'decision':'confirm','identity_mode':'stable_object','reason':'Approve only the precisely recorded business identity',
    }).status_code == 200
    original = MockConnector.read
    reads = []

    def changed(self, tool, args):
        reads.append(tool)
        payload = original(self, tool, args)
        if tool == 'container.list':
            payload['containers'][0].update(id='c'*64, **change)
        return payload

    monkeypatch.setattr(MockConnector, 'read', changed)
    job, run = start_native(client, profile, runtime)
    assert job['status'] == 'completed' and run['diagnosis_status'] == 'unresolved'
    assert 'container.status' not in reads and 'container.logs' not in reads
    assert run['summary']['scope_gaps'] and not run.get('clarification')


def test_revoking_during_read_discards_late_evidence_and_stops_remaining_reads(client, monkeypatch):
    profile, runtime, binding = setup_binding(client)
    original = MockConnector.read
    reads = []

    def revoke(self, tool, args):
        reads.append(tool)
        if tool == 'service.status':
            assert client.post(f"/api/v1/businesses/{profile['business_id']}/bindings/{binding['id']}/revoke", json={
                'reason':'Owner revoked scope while a read was in progress',
            }).status_code == 200
        return original(self, tool, args)

    monkeypatch.setattr(MockConnector, 'read', revoke)
    job, run = start_native(client, profile, runtime)
    assert job['status'] == 'completed', job
    assert 'service.logs' not in reads
    assert run['summary']['stop_reason'] == 'business_authorization_changed'
    assert not any(e['tool_id'] == 'service.status' for e in run['evidence'])
    assert not run['completion_satisfied'] and run['summary']['scope_gaps']


def test_configured_target_is_used_without_selection_and_never_fails_over(client, monkeypatch):
    from app.connectors.service import RuntimeService
    profile, runtime, _ = setup_binding(client)
    alternate = add_target(client, profile, 'Another approved SSH target')
    response = client.put(f"/api/v1/businesses/{profile['business_id']}/investigation-defaults", json={
        'target_id':runtime['id'],'services':['catalog-api'],'primary_service':'catalog-api',
    })
    assert response.status_code == 200, response.text
    event = incident(client, profile)
    response = client.post(f'/api/v1/incidents/{event}/runs', json={'decision_protocol':'ab_v1'})
    assert response.status_code == 202, response.text
    started = response.json()
    assert wait_job(client, started['job_id'])['status'] == 'completed'
    assert client.get('/api/v1/runs/'+started['run_id']).json()['target_id'] == runtime['id']
    original = RuntimeService.investigation_readiness

    def readiness(self, config):
        return {'eligible':False,'status':'credential_missing','reason':'Synthetic expired credentials'} if config['id'] == runtime['id'] else original(self, config)

    monkeypatch.setattr(RuntimeService, 'investigation_readiness', readiness)
    event = incident(client, profile)
    response = client.post(f'/api/v1/incidents/{event}/runs', json={'decision_protocol':'ab_v1'})
    assert response.status_code == 409 and '默认' in response.text
    assert alternate['id'] not in response.text


def test_independent_authorization_supports_a_business_without_material(client):
    profile = client.post('/api/v1/businesses', json={'name':'No-material local business'}).json()
    runtime = add_target(client, profile, 'Explicit local Mock target')
    found = discover(client, profile, runtime)
    candidate = next(row for row in found['runtime_objects'] if row['kind'] == 'service' and row['name'] == 'catalog-api')
    response = client.post(f"/api/v1/businesses/{profile['business_id']}/targets/{runtime['id']}/authorize-object", json={
        'observation_id':candidate['observation_id'],'runtime_id':candidate['id'],'kind':'service',
        'service':'unfamiliar-api','role':'unknown','identity_mode':'exact_instance',
        'reason':'Operator independently approves only the observed exact object',
    })
    assert response.status_code == 200, response.text
    job, run = start_native(client, profile, runtime)
    assert job['status'] == 'completed', job
    assert any(call['tool_id']=='service.status' and call['status']=='succeeded' for call in run['tool_calls'])
    assert not run.get('clarification')


def test_unknown_scope_cannot_be_saved_as_default(client):
    profile, runtime, _ = setup_binding(client)
    response = client.put(f"/api/v1/businesses/{profile['business_id']}/investigation-defaults", json={
        'target_id':runtime['id'],'services':['unapproved-object'],
    })
    assert response.status_code == 422, response.text


def test_duplicate_stable_identities_require_review_instead_of_choosing_an_instance(client, monkeypatch):
    profile = onboard(client)
    runtime = target(client, profile, docker=True)
    found = discover(client, profile, runtime)
    binding = next(row for row in found['matches'] if row['kind']=='container' and row['business_service']=='catalog-api')
    assert client.post(f"/api/v1/businesses/{profile['business_id']}/bindings/{binding['id']}/review", json={
        'decision':'confirm','identity_mode':'stable_object','reason':'Approve an unambiguous precise object identity',
    }).status_code == 200
    original = MockConnector.read
    def duplicates(self, tool, args):
        payload = original(self, tool, args)
        if tool == 'container.list':
            payload['containers'].append({**payload['containers'][0], 'id':'c'*64})
        return payload
    monkeypatch.setattr(MockConnector, 'read', duplicates)
    job, run = start_native(client, profile, runtime)
    assert job['status']=='completed' and run['diagnosis_status']=='unresolved'
    assert not any(call['tool_id'] in {'container.status','container.logs'} for call in run['tool_calls'])
    assert run['summary']['scope_gaps']


def test_independent_authorization_rejects_old_observation_after_target_config_change(client):
    from app.common import dumps
    profile = client.post('/api/v1/businesses', json={'name':'Target mutation fixture'}).json()
    runtime = add_target(client, profile, 'Explicit Mock target')
    found = discover(client, profile, runtime)
    candidate = next(c for c in found['runtime_objects'] if c['kind']=='service')
    database = Database(get_settings().database_path)
    with database.transaction() as tx:
        config = dict(runtime)
        config.pop('id')
        config['allowed_paths'] = ['/changed-scope']
        tx.execute('UPDATE runtime_targets SET config_json=? WHERE id=?', (dumps(config), runtime['id']))
    response = client.post(f"/api/v1/businesses/{profile['business_id']}/targets/{runtime['id']}/authorize-object", json={
        'observation_id':candidate['observation_id'],'runtime_id':candidate['id'],'kind':'service',
        'service':'generic-api','reason':'Owner approves an exact newly observed object',
    })
    assert response.status_code==422 and '目标授权已变化' in response.text


@pytest.mark.parametrize('field', ['ports','environment_keys','image'])
def test_incomplete_container_identity_cannot_grant_stable_replacement(client, monkeypatch, field):
    profile = onboard(client)
    runtime = target(client, profile, docker=True)
    original = MockConnector.read
    def incomplete(self, tool, args):
        payload = original(self, tool, args)
        if tool == 'container.list':
            payload['containers'][0].pop(field)
        return payload
    monkeypatch.setattr(MockConnector, 'read', incomplete)
    found = discover(client, profile, runtime)
    binding = next(row for row in found['matches'] if row['kind']=='container' and row['business_service']=='catalog-api')
    response = client.post(f"/api/v1/businesses/{profile['business_id']}/bindings/{binding['id']}/review", json={
        'decision':'confirm','identity_mode':'stable_object','reason':'Approve replacement only if the full identity is present',
    })
    assert response.status_code==422 and '完整身份' in response.text


def test_manual_discovery_during_a_run_cannot_clear_its_identity_marker(client, monkeypatch):
    profile, runtime, _ = setup_binding(client)
    original = MockConnector.read
    attempts = []
    def discovered(self, tool, args):
        if tool == 'service.status':
            attempts.append(client.post(f"/api/v1/businesses/{profile['business_id']}/targets/{runtime['id']}/discover"))
        return original(self, tool, args)
    monkeypatch.setattr(MockConnector, 'read', discovered)
    job, run = start_native(client, profile, runtime)
    assert job['status']=='completed'
    assert attempts and all(r.status_code==409 for r in attempts)
    assert run['summary']['stop_reason']!='business_authorization_changed'
    assert any(call['tool_id']=='service.status' and call['status']=='succeeded' for call in run['tool_calls'])


def test_internal_identity_refresh_preserves_the_current_run_marker(client, monkeypatch):
    from app.system.scope import ScopeResolver
    profile, runtime, binding = setup_binding(client)
    original = MockConnector.read
    def refresh(self, tool, args):
        if tool == 'service.status':
            resolver = ScopeResolver(Database(get_settings().database_path), get_settings())
            resolver.discover(profile['business_id'], runtime['id'], observations=[{
                'observation_id':'refresh-fixture','tool_id':'service.list','observed_at':datetime.now(timezone.utc).isoformat(),
                'payload':original(self, 'service.list', {'limit':100}),
            }])
        return original(self, tool, args)
    monkeypatch.setattr(MockConnector, 'read', refresh)
    job, run = start_native(client, profile, runtime)
    assert job['status']=='completed'
    assert current_binding(client, profile, binding)['verified_run_id']==run['id']
    assert any(call['tool_id']=='service.status' and call['status']=='succeeded' for call in run['tool_calls'])
