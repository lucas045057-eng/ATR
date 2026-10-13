"""Human-reviewed directed endpoints, grounded in current bounded configuration reads."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath

from app.business.profile import claim
from app.business.service import BusinessService
from app.common import digest, now, uid
from app.connectors.service import RuntimeService
from app.tools.redaction import redact_text

TTL_SECONDS=86400
HOST=r'(?:[A-Za-z][A-Za-z0-9_.-]{0,252}|\d{1,3}(?:\.\d{1,3}){3})'


def configuration_path(row):
    payload=json.loads(row['payload_json'])
    path=payload.get('path') or json.loads(row['args_json']).get('path')
    return str(PurePosixPath(path)) if isinstance(path,str) and path else None


def latest_configuration(tx,target_id,path):
    return next((row for row in tx.execute("SELECT * FROM runtime_observations WHERE target_id=? AND tool_id='config.read' AND status='succeeded' ORDER BY observed_at DESC,id DESC LIMIT 100",(target_id,))
                 if configuration_path(row)==path),None)


def configuration_endpoints(text):
    values={}
    variables={}
    for line_number,line in enumerate(text.splitlines(),1):
        if line.lstrip().startswith(('#','//',';')) or re.search(r'(?i)\b\w*(?:password|passwd|secret|token|api_key|private_key)\w*\s*[:=]',line):
            continue
        for host,port in re.findall(r'(?<![A-Za-z0-9_.-])('+HOST+r'):(\d{1,5})(?![\d@])',line):
            if 0<int(port)<=65535:
                values.setdefault(f'{host.lower()}:{int(port)}',[]).append(line_number)
        assignment=re.fullmatch(r'\s*(?:export\s+)?([A-Z][A-Z0-9_]*_(?:HOST|PORT))\s*=\s*[\"\']?([^\"\'\s#]+)[\"\']?\s*',line)
        if assignment:
            variables[assignment[1]]=(assignment[2],line_number)
    for key,(host,line_number) in variables.items():
        if not key.endswith('_HOST') or not re.fullmatch(HOST,host):
            continue
        port,port_line=variables.get(key[:-5]+'_PORT',('',0))
        if port.isdecimal() and 0<int(port)<=65535:
            values.setdefault(f'{host.lower()}:{int(port)}',[]).extend([line_number,port_line])
    return [{'endpoint_id':endpoint,'lines':sorted(set(lines))} for endpoint,lines in sorted(values.items())][:40]


def candidates(database,business_id,target_id):
    with database.connect() as tx:
        rows=tx.execute("SELECT o.* FROM runtime_observations o JOIN runtime_targets t ON t.id=o.target_id WHERE t.business_id=? AND o.target_id=? AND o.tool_id='config.read' AND o.status='succeeded' ORDER BY o.observed_at DESC,o.id DESC LIMIT 100",(business_id,target_id)).fetchall()
    result=[]
    seen=set()
    instant=datetime.now(timezone.utc)
    for row in rows:
        payload=json.loads(row['payload_json'])
        path=configuration_path(row)
        if not path or path in seen:
            continue
        seen.add(path)
        observed=datetime.fromisoformat(row['observed_at'])
        expires=observed+timedelta(seconds=TTL_SECONDS)
        if observed.tzinfo is None or not observed<=instant<expires or any(payload.get(key) for key in ('partial','truncated','timeout')) or not isinstance(payload.get('text'),str):
            continue
        for value in configuration_endpoints(payload['text']):
            result.append({**value,'candidate_id':digest([row['id'],row['content_digest'],value]),
                           'observation_id':row['id'],'target_id':target_id,'path':path,
                           'content_digest':row['content_digest'],'observed_at':row['observed_at'],
                           'expires_at':expires.isoformat(),'verification_status':'pending'})
    return result[:100]


def confirm(database,settings,business_id,target_id,request,actor):
    from app.system.scope import binding_is_current
    RuntimeService(database,settings).get(business_id,target_id)
    service=BusinessService(database,settings)
    profile=service._profile(business_id)
    if profile.profile_version!=request.expected_version:
        raise ValueError('资料版本已变化，请重新核对连接关系')
    relation=next((item for item in profile.dependencies if item['fact_id']==request.dependency_fact_id
                   and item['relation']=='depends_on' and item['from'].startswith('service:') and item['to'].startswith('service:')),None)
    if not relation:
        raise ValueError('需要当前业务中有方向的服务依赖关系')
    source=next((fact for fact in profile.facts if fact.id==relation['fact_id']),None)
    if not source or source.verification_status in {'rejected','stale'}:
        raise ValueError('已否决或失效的依赖关系不能确认端点')
    selected=next((item for item in candidates(database,business_id,target_id) if item['candidate_id']==request.candidate_id),None)
    if not selected:
        raise ValueError('配置连接候选不属于当前目标，已过期或读取不完整，请重新读取')
    with database.connect() as tx:
        bindings=[dict(row) for row in tx.execute('SELECT * FROM service_bindings WHERE business_id=? AND target_id=?',(business_id,target_id)) if binding_is_current(row)]
    chosen=[]
    for name in (relation['from'][8:],relation['to'][8:]):
        matching=[binding for binding in bindings if binding['service']==name and binding['kind'] in {'service','container','process'}]
        if len(matching)!=1:
            raise ValueError('消费者和依赖都需要同一目标中唯一、当前已确认的运行对象')
        chosen.append(matching[0]['id'])
    reviewed=digest(profile.model_dump())
    for fact in profile.facts:
        if fact.predicate=='verified_endpoint' and isinstance(fact.object,dict) and fact.object.get('dependency_fact_id')==source.id and fact.object.get('target_id')==target_id:
            fact.verification_status='stale'
    refs=[{'schema_version':'observation-ref-1','source_type':'Runtime','observation_id':selected['observation_id'],
           'target_id':target_id,'path':selected['path'],'start_line':line,'end_line':line,
           'content_digest':selected['content_digest']} for line in selected['lines']]
    fact=claim(relation['from'],'verified_endpoint',{'dependency_fact_id':source.id,'dependency':relation['to'],
               'target_id':target_id,'endpoint_id':selected['endpoint_id'],'binding_ids':chosen,
               'observation_id':selected['observation_id'],'expires_at':selected['expires_at']},
               'Human Verified',f'operator:{actor}',selected['content_digest'],'confirmed')
    fact.source_refs=refs
    profile.facts.append(fact)
    source.verification_status='confirmed'
    return service.save(profile,request.expected_version,(uid(),business_id,request.expected_version,source.id,
                        reviewed,'confirm_endpoint',redact_text(request.reason),actor,now()))
