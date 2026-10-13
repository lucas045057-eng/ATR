"""Protocol-aware consumers of persisted results; execution and diagnosis stay separate."""
from __future__ import annotations

import json

from app.agent.result_evaluator import REQUIRED_CHAIN


def root_supported(result):
    if (not isinstance(result,dict) or result.get('schema_version')!='ab-result-1'
            or result.get('diagnosis_status')!='root_cause_supported' or result.get('completion_satisfied') is not True
            or any(result.get(key) for key in ('required_gaps','counterevidence','critical_ambiguities'))):
        return False
    items=result.get('verified_segments',[])
    if not isinstance(items,list):
        return False
    segments={segment.get('rule_id'):segment for segment in items if isinstance(segment,dict)}
    if not set(REQUIRED_CHAIN)<=segments.keys():
        return False
    for rule in REQUIRED_CHAIN:
        segment=segments[rule]
        participants=segment.get('participants',{})
        citations=segment.get('supports',[])
        if segment.get('status')!='supported' or not isinstance(participants,dict) or not isinstance(citations,list) or not citations:
            return False
        if any(not isinstance(item,dict) or not all(isinstance(item.get(key),str) and item[key] for key in ('fact_id','call_id','evidence_id','observed_on'))
               or item.get('asserts_about')!=rule or item['observed_on'] not in participants.values() for item in citations):
            return False
    first,last=(segments[rule]['participants'] for rule in REQUIRED_CHAIN)
    return (all(isinstance(item,str) and item for item in [first.get('consumer'),first.get('dependency'),last.get('proxy'),last.get('upstream')])
            and first['consumer']==last['upstream'] and first['consumer']!=first['dependency'] and last['proxy']!=last['upstream'])


def outcome(connection,run):
    run=dict(run)
    try:
        configuration=json.loads(run.get('model_config_json') or '{}')
    except (ValueError,TypeError):
        configuration={}
    if not isinstance(configuration,dict):
        configuration={}
    execution=connection.execute('SELECT execution_status,result_json FROM run_executions WHERE run_id=?',(run['id'],)).fetchone()
    if configuration.get('decision_protocol')!='ab_v1' and not execution:
        return {'native':False,'status':run['status'],'completed':run['status'] in {'supported','unresolved'},
                'root_supported':run['status']=='supported','healthy':False,'result':None}
    try:
        result=json.loads(execution['result_json']) if execution else {}
    except (ValueError,TypeError):
        result={}
    if not isinstance(result,dict) or result.get('schema_version')!='ab-result-1':
        result={}
    completed=bool(execution and execution['execution_status']=='completed')
    supported=completed and root_supported(result)
    healthy=(completed and result.get('diagnosis_status')=='no_fault_observed' and result.get('completion_satisfied') is True
             and not any(result.get(key) for key in ('required_gaps','counterevidence','critical_ambiguities','direct_faults')))
    return {'native':True,'completed':completed,'root_supported':supported,'healthy':healthy,
            'status':'supported' if supported else 'no_fault_observed' if healthy else 'unresolved',
            'execution_status':execution['execution_status'] if execution else run['status'],
            'diagnosis_status':result.get('diagnosis_status','unresolved'),'result':result}


def supported_checks(connection,run,result):
    citations={item['evidence_id']:item for segment in result['verified_segments'] for item in segment['supports']}
    checks=[]
    seen=set()
    for citation in citations.values():
        row=connection.execute("""SELECT t.id AS source_call_id,t.tool_id,t.purpose,t.started_at,
            n.name AS target_service,n.role AS target_role FROM evidence e JOIN tool_calls t ON t.id=e.tool_call_id
            JOIN graph_nodes n ON n.id=e.target_node_id WHERE e.id=? AND t.id=? AND e.target_node_id=?
            AND t.run_id=? AND t.snapshot_id=? AND n.snapshot_id=? AND t.target_id=?
            AND t.status='succeeded' AND t.purpose='diagnosis' AND n.kind='service'""",
            (citation['evidence_id'],citation['call_id'],citation['observed_on'],run['id'],run['graph_snapshot_id'],run['graph_snapshot_id'],run['target_id'])).fetchone()
        if not row:
            raise ValueError('根因引用缺少同一调查、图版本和目标的成功原始取证记录')
        checks.append(dict(row))
    ordered=[]
    for check in sorted(checks,key=lambda item:(item['started_at'],item['source_call_id'])):
        identity=(check['tool_id'],check['target_service'])
        if identity not in seen:
            ordered.append({**{key:value for key,value in check.items() if key!='started_at'},'sequence_no':len(ordered)+1})
            seen.add(identity)
    return ordered


def native_verification_valid(connection,run,call_id,expected_endpoint=None):
    if not isinstance(expected_endpoint,dict) or not expected_endpoint.get('endpoint_id') or not expected_endpoint.get('url'):
        return False
    row=connection.execute("""SELECT t.tool_id,t.status,t.purpose,t.target_id,t.args_redacted_json,t.snapshot_id,
        e.target_node_id,e.payload_redacted_json,o.target_id AS observation_target,o.status AS observation_status,
        o.payload_json FROM tool_calls t JOIN evidence e ON e.tool_call_id=t.id
        JOIN runtime_observations o ON o.id=t.request_id WHERE t.id=? AND t.run_id=?""",(call_id,run['id'])).fetchone()
    if not row or row['status']!='succeeded' or row['purpose']!='verification' or row['tool_id']!='health.get' or row['snapshot_id']!=run['graph_snapshot_id']:
        return False
    payload=json.loads(row['payload_redacted_json'])
    observed=json.loads(row['payload_json'])
    args=json.loads(row['args_redacted_json'])
    measured=outcome(connection,run)
    proxies={segment['participants'].get('proxy') for segment in measured['result'].get('verified_segments',[]) if segment.get('rule_id')=='upstream_unavailable'}
    return (row['target_id']==row['observation_target']==run['target_id'] and row['observation_status']=='succeeded'
            and row['target_node_id'] in proxies and payload.get('endpoint_id')==args.get('endpoint_id')
            and payload.get('endpoint_id')==expected_endpoint['endpoint_id'] and payload.get('url')==expected_endpoint['url']
            and payload.get('endpoint_id')==observed.get('endpoint_id') and payload.get('url')==observed.get('url')
            and payload.get('status_code')==observed.get('status_code')==200
            and payload.get('observation_location')==observed.get('observation_location')=='remote_ssh'
            and payload.get('protocol')==observed.get('protocol')=='atr-read-ab-v1' and not any(payload.get(key) or observed.get(key) for key in ('partial','truncated','timeout','redirected')))
