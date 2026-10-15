import pytest
from app.business.models import BusinessReview, MediaBatchReview
from app.connectors.models import (
    AuthorizationRuleReview,
    BindingReview,
    EndpointMappingReview,
    InventoryAuthorization,
    ScopeRevocation,
)
from app.connectors.service import RuntimeService
from app.connectors.ssh import SSHConnector
from app.connectors.standard_ssh import StandardSSHConnector
from app.domain import ConflictReviewRequest, ReviewRequest
from app.settings import get_settings
from app.storage.database import Database
from pydantic import ValidationError
from test_business import onboard
from test_runtime import discover, target
from test_ssh_password import config


@pytest.mark.parametrize('model,payload', [
    (BindingReview, {'decision': 'confirm'}),
    (ScopeRevocation, {}),
    (AuthorizationRuleReview, {'identity_mode': 'stable_object'}),
    (InventoryAuthorization, {'observation_id': 'observation', 'runtime_id': 'api.service',
                              'kind': 'service', 'service': 'api'}),
    (EndpointMappingReview, {'expected_version': 1, 'dependency_fact_id': 'fact',
                             'candidate_id': 'a' * 64}),
    (BusinessReview, {'expected_version': 1, 'decision': 'approve'}),
    (MediaBatchReview, {'expected_version': 1, 'file_ids': ['file'], 'action': 'optional'}),
    (ReviewRequest, {'decision': 'approve'}),
])
@pytest.mark.parametrize('note', [None, '', 'ok'])
def test_audit_note_is_optional_bounded_text(model, payload, note):
    request = model.model_validate({**payload, **({'reason': note} if note is not None else {})})
    if note is not None:
        assert request.reason == note
    with pytest.raises(ValidationError):
        model.model_validate({**payload, 'reason': 'x' * 1001})


@pytest.mark.parametrize('mode,boundary,connector', [
    ('forced_gateway', 'forced_command_gateway', SSHConnector),
    ('standard_ssh_readonly', 'client_readonly', StandardSSHConnector),
])
def test_ssh_authorization_does_not_require_a_written_attestation(client, mode, boundary, connector):
    settings = get_settings()
    settings.ssh_enabled = True
    business = client.post('/api/v1/businesses', json={'name': 'Optional SSH note'}).json()
    payload = config(execution_mode=mode, permission_boundary=boundary)
    payload.pop('id')
    payload.pop('boundary_attestation')
    base = f"/api/v1/businesses/{business['business_id']}/targets"
    response = client.post(base, json=payload)
    assert response.status_code == 201, response.text
    runtime = response.json()
    assert runtime['boundary_attestation'] == ''
    connector(runtime, settings).check_boundary()
    password = client.post(base + '/' + runtime['id'] + '/credential', json={'password': 'fixture-only'})
    assert password.status_code == 200, password.text
    assert RuntimeService(Database(settings.database_path), settings).investigation_readiness(password.json())['eligible']
    assert client.post(base, json={**payload, 'access_authorized': False}).status_code == 403
    assert client.post(base, json={**payload, 'permission_boundary': 'mock'}).status_code == 403
    assert client.post(base, json={**payload, 'host_public_key': ''}).status_code == 403


def test_blank_binding_notes_still_record_actor_decision_and_scope(client):
    profile = onboard(client)
    runtime = target(client, profile)
    binding = next(row for row in discover(client, profile, runtime)['matches'] if row['kind'] == 'service')
    base = f"/api/v1/businesses/{profile['business_id']}"
    path = base + '/bindings/' + binding['id']
    assert client.post(path + '/review', json={'decision': 'confirm'}).status_code == 200
    rows = client.get(base + '/bindings').json()
    approved = next(row for row in rows if row['id'] == binding['id'])
    assert approved['authorization_valid']
    assert approved['authorization']['identity_mode'] == 'exact_instance'
    assert approved['authorization']['confirmed_by']
    assert client.put(path + '/authorization', json={'identity_mode': 'stable_object', 'reason': ''}).status_code == 200
    assert client.post(path + '/revoke', json={}).status_code == 200
    with Database(get_settings().database_path).connect() as connection:
        history = connection.execute('SELECT decision,reason,actor,created_at FROM business_reviews WHERE subject_id=?',
                                     (binding['id'],)).fetchall()
    assert len(history) == 3
    assert all(row['reason'] == '' and row['actor'] and row['created_at'] for row in history)


def test_profile_approval_without_note_keeps_version_check(client):
    profile = onboard(client)
    path = f"/api/v1/businesses/{profile['business_id']}/review"
    payload = {'expected_version': profile['profile_version'], 'decision': 'approve'}
    response = client.post(path, json=payload)
    assert response.status_code == 200, response.text
    assert client.post(path, json=payload).status_code == 422


def test_conflict_resolution_still_needs_an_actual_explanation():
    with pytest.raises(ValidationError):
        BusinessReview(expected_version=1, decision='resolve_conflict')
    with pytest.raises(ValidationError):
        ConflictReviewRequest(decision='approve', reason='')
