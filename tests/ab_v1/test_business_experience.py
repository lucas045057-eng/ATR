import json

from app.common import dumps, now, uid
from app.knowledge import compatible_patterns, refresh_business_ownership
from app.storage.database import Database
from test_regressions import seed


def system_business(tx,name):
    system,snapshot,business=uid(),uid(),uid()
    tx.execute('INSERT INTO systems VALUES(?,?,?,?,?,?,?,?)',(system,name,'/same-source','same','compose.yaml','same',now(),'{}'))
    tx.execute('INSERT INTO graph_snapshots VALUES(?,?,?,?,?,?,?,?,?)',
               (snapshot,system,1,'same','same',now(),now(),dumps({'source_digest':'same-source-digest'}),'current'))
    tx.execute('INSERT INTO business_profiles VALUES(?,?,?,?,?,?)',(business,name,'',0,system,now()))
    return system,snapshot,business


def test_source_digest_alone_cannot_reuse_cross_business_and_history_is_immutable(tmp_path):
    db=Database(tmp_path/'ownership.sqlite')
    db.initialize()
    with db.transaction() as tx:
        first=system_business(tx,'A')
        second=system_business(tx,'B')
    run,patterns=seed(db,first[1],first[0])
    with db.transaction() as tx:
        before=[tuple(row) for row in tx.execute('SELECT * FROM pattern_versions')]
        refresh_business_ownership(tx)
        assert len(compatible_patterns(tx,first[2],first[1]))==2
        assert compatible_patterns(tx,second[2],second[1])==[]
        owners=[tuple(row) for row in tx.execute('SELECT * FROM experience_ownership')]
        refresh_business_ownership(tx)
        assert owners==[tuple(row) for row in tx.execute('SELECT * FROM experience_ownership')]
        assert before==[tuple(row) for row in tx.execute('SELECT * FROM pattern_versions')]
        changed=uid()
        tx.execute('INSERT INTO graph_snapshots VALUES(?,?,?,?,?,?,?,?,?)',
                   (changed,first[0],2,'same','new-deployment',now(),now(),dumps({'source_digest':'same-source-digest'}),'current'))
        assert compatible_patterns(tx,first[2],changed)==[]


def test_ambiguous_legacy_owner_stays_historical_until_explicit_run_proof(tmp_path):
    db=Database(tmp_path/'ambiguous.sqlite')
    db.initialize()
    with db.transaction() as tx:
        system,snapshot,business=system_business(tx,'A')
    run,_=seed(db,snapshot,system)
    with db.transaction() as tx:
        other=uid()
        tx.execute('INSERT INTO business_profiles VALUES(?,?,?,?,?,?)',(other,'other','',0,system,now()))
        refresh_business_ownership(tx)
        assert compatible_patterns(tx,business,snapshot)==[]
        assert all(row['business_id'] is None for row in tx.execute('SELECT * FROM experience_ownership'))
        tx.execute('UPDATE diagnosis_runs SET summary_json=? WHERE id=?',(dumps({'business_id':business}),run))
        refresh_business_ownership(tx)
        assert len(compatible_patterns(tx,business,snapshot))==2
        assert compatible_patterns(tx,other,snapshot)==[]
        proof=json.loads(tx.execute('SELECT proof_json FROM experience_ownership').fetchone()[0])
        assert proof['basis']=='run_and_target_business'


def test_import_profile_reviews_confirm_reject_roles_and_stale_version(client):
    from test_business import onboard

    profile = onboard(client)
    base = f"/api/v1/businesses/{profile['business_id']}"
    relation = next(d for d in profile['dependencies'] if d['relation'] == 'depends_on')
    fact_id = relation['fact_id']
    assert next(f for f in profile['facts'] if f['id'] == fact_id)['verification_status'] == 'pending'
    assert profile['review_status'] == 'pending'

    def review(current, **payload):
        response = client.post(base + '/review', json={
            'expected_version': current['profile_version'], **payload,
        })
        assert response.status_code == 200, response.text
        return response.json()

    confirmed = review(profile, subject_id=fact_id, decision='approve')
    assert next(f for f in confirmed['facts'] if f['id'] == fact_id)['verification_status'] == 'confirmed'
    assert confirmed['review_status'] == 'pending'
    rejected = review(confirmed, subject_id=fact_id, decision='reject')
    assert next(f for f in rejected['facts'] if f['id'] == fact_id)['verification_status'] == 'rejected'
    assert all(d['fact_id'] != fact_id for d in rejected['dependencies'])
    service = rejected['services'][0]
    corrected = review(rejected, subject_id='service:' + service['name'], decision='set_role', role='worker')
    assert next(s for s in corrected['services'] if s['name'] == service['name'])['role'] == 'worker'
    assert any(f['source_type'] == 'Human Verified' and f['predicate'] == 'role'
               and f['object'] == 'worker' and f['verification_status'] == 'confirmed'
               for f in corrected['facts'])
    approved = review(corrected, decision='approve')
    assert approved['review_status'] == 'approved'
    assert client.post(base + '/review', json={
        'expected_version': profile['profile_version'], 'decision': 'approve',
    }).status_code == 422
