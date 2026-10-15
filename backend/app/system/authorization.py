"""Persistent operator grants; inventory observations remain short lived."""
import json

from app.common import dumps, now


def identity_rule(candidate, mode='exact_instance'):
    from app.system.scope import candidate_digest
    if mode == 'stable_object':
        if candidate['kind'] not in {'service', 'container'}:
            raise ValueError('进程只能授权精确 PID 和启动身份，不能按名称自动接续')
        if candidate['kind'] == 'container' and (not candidate.get('image')
                or any(key not in candidate or not isinstance(candidate[key], list) for key in ('ports', 'environment_keys'))):
            raise ValueError('容器缺少镜像、端口和环境键的完整身份；只能授权精确实例')
        identity = {key: candidate.get(key) for key in ('kind', 'name', 'image')}
        if candidate['kind'] == 'service':
            identity['id'] = candidate['id']
        for key in ('ports', 'environment_keys'):
            identity[key] = sorted(set(candidate.get(key) or []), key=str)
        return {'kind': candidate['kind'], 'identity': identity}
    return {'kind': candidate['kind'], 'runtime_id': candidate['id'], 'candidate_digest': candidate_digest(candidate)}


def authorization(binding):
    return json.loads(dict(binding).get('authorization_json') or '{}')


def authorized(binding, target):
    from app.agent.discovery import authorization_digest
    grant = authorization(binding)
    return bool(binding['status'] == 'confirmed' and grant.get('grant_id') and not grant.get('revoked_at')
                and grant.get('target_signature') == authorization_digest(target))


def matches(grant, candidate):
    try:
        return grant.get('rule') == identity_rule(candidate, grant.get('identity_mode', 'exact_instance'))
    except ValueError:
        return False


def create_grant(binding_id, target, candidate, actor, mode='exact_instance', stamp=None):
    from app.agent.discovery import authorization_digest
    return {'schema_version': 'scope-authorization-1', 'grant_id': binding_id,
            'target_signature': authorization_digest(target), 'rule': identity_rule(candidate, mode),
            'identity_mode': mode, 'confirmed_by': actor, 'confirmed_at': stamp or now(), 'revoked_at': None}


def adopt_legacy_grants(connection):
    """An existing confirmed row grants only its original exact instance."""
    from app.agent.discovery import authorization_digest
    rows = connection.execute("SELECT b.*,t.config_json FROM service_bindings b JOIN runtime_targets t ON t.id=b.target_id WHERE b.status='confirmed' AND b.authorization_json='{}'").fetchall()
    for row in rows:
        if not row['candidate_digest']:
            continue
        review = connection.execute("SELECT actor,created_at FROM business_reviews WHERE subject_id=? AND business_id=? AND decision='confirm' ORDER BY created_at DESC LIMIT 1", (row['id'], row['business_id'])).fetchone()
        source = json.loads(row['source_json'])
        grant = {'schema_version': 'scope-authorization-1', 'grant_id': row['id'],
                 'identity_mode': 'exact_instance', 'rule': {'kind': row['kind'], 'runtime_id': row['runtime_id'], 'candidate_digest': row['candidate_digest']},
                 'target_signature': authorization_digest({'id': row['target_id'], **json.loads(row['config_json'])}),
                 'confirmed_by': review['actor'] if review else source.get('actor'),
                 'confirmed_at': review['created_at'] if review else source.get('confirmed_at'), 'revoked_at': None,
                 'origin': 'existing_operator_confirmed_instance'}
        connection.execute('UPDATE service_bindings SET authorization_json=? WHERE id=?', (dumps(grant), row['id']))


def defaults(connection, business_id):
    row = connection.execute('SELECT * FROM investigation_defaults WHERE business_id=?', (business_id,)).fetchone()
    return ({**dict(row), 'services': json.loads(row['services_json'])} if row else
            {'business_id': business_id, 'target_id': None, 'services': [], 'primary_service': None})
