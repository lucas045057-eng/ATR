from __future__ import annotations

import re

from app.common import digest
from app.tools.registry import sanitize_payload


def safe_label(value):
    value=str(value)
    if re.search(r'<\s*(?:script|iframe|object|embed)|javascript:',value,re.I):
        raise ValueError('图中的脚本或主动内容被拒绝')
    return value[:120]


def project_snapshot(snapshot: dict, view: str) -> dict:
    if view not in {'structure','flow'}:
        raise ValueError('未知图视图')
    profile=snapshot['profile']
    identity=snapshot['snapshot_id']
    nodes,edges,sources={},[],{}

    def add(key,label,kind='backend',refs=(),certainty='待核验'):
        identifier='n'+digest(key)[:18]
        if identifier not in nodes:
            nodes[identifier]={'id':identifier,'type':kind,'label':safe_label(label),'sublabel':safe_label(certainty)}
        sources[identifier]=[sanitize_payload(ref) for ref in refs]
        return identifier

    def connect(first,last,relation,refs=()):
        if first not in nodes or last not in nodes:
            return
        identifier='e'+digest([first,last,relation,len(edges)])[:18]
        edges.append({'id':identifier,'from':first,'to':last,'label':safe_label(relation)[:70]})
        sources[identifier]=[sanitize_payload(ref) for ref in refs]

    unknowns=[]
    if view=='structure':
        graph=snapshot.get('graph',{})
        mapped={}
        for node in graph.get('nodes',[]):
            attributes=node.get('attributes',{})
            provenance=attributes.get('provenance') or {}
            kind='database' if node.get('role') in {'datastore','cache'} or node['kind']=='datastore' else 'backend'
            refs=provenance.get('source_refs',[]) or [ {'schema_version':'observation-ref-1','observation_id':item,
                   'snapshot_id':identity,'source_type':'Runtime'} for item in attributes.get('evidence_ids',[]) ]
            mapped[node['id']]=add(node['canonical_key'],node['name'],kind,refs,
                                    provenance.get('source_type') or attributes.get('source_type','待核验'))
        for edge in graph.get('edges',[]):
            if edge['from_node_id'] in mapped and edge['to_node_id'] in mapped:
                attributes=edge.get('attributes',{})
                connect(mapped[edge['from_node_id']],mapped[edge['to_node_id']],edge['relation'],
                        (attributes.get('provenance') or {}).get('source_refs',[]))
        for document in profile.get('documents',[]):
            if document.get('vision_state')!='completed' or document.get('external_paused'):
                continue
            visual=document.get('visual') or {}
            prefix=f"document:{document['file_id']}:{document['page_number']}:"
            mapped={}
            for component in visual.get('components',[]):
                refs=[{**ref,'region':component['region']} for ref in document['source_refs']]
                mapped[component['id']]=add(prefix+component['id'],component['name'],
                                          'database' if component['kind']=='datastore' else 'backend',refs,'资料描述 · 待审核')
            for edge in visual.get('relations',[]):
                refs=[{**ref,'region':edge['region']} for ref in document['source_refs']]
                connect(mapped.get(edge['source']),mapped.get(edge['target']),edge['relation'],refs)
            unknowns.extend(visual.get('unknowns',[]))
    else:
        selected=snapshot.get('flow_id')
        flows=[flow for flow in profile.get('business_flow_graphs',[]) if not selected or flow['flow_id']==selected]
        for flow in flows:
            mapped={}
            for index,step in enumerate(flow.get('steps',[])):
                conditions='; '.join(str(region.get('condition') or region.get('arm')) for region in step.get('syntax_branch_regions',[]))
                mapped[step['step_id']]=add(f"flow:{flow['flow_id']}:{index}",f"{index+1}. "+str(step.get('title') or step['kind']),
                         'backend',step.get('source_refs',[]),conditions or ('启动路径' if flow['kind']=='startup' else '源码路径 · 未验证执行'))
            for edge in flow.get('edges',[]):
                connect(mapped.get(edge['from']),mapped.get(edge['to']),edge['relation'])
            unknowns.extend(flow.get('unknowns',[]))
    if not nodes:
        raise ValueError('此版本没有可绘制的结构或源码流程，请先导入相应资料')
    for index,node in enumerate(nodes.values()):
        node.update(pos=[60+index%3*390,100+index//3*170],size=[240,78])
    title=f"{profile['name']} · {'业务结构' if view=='structure' else '源码流程'} · v{profile['profile_version']}"
    candidate={'schema_version':1,'diagram_type':'architecture','meta':{'title':safe_label(title),'locale':'zh-CN',
               'output':'diagram.html','quality_profile':'standard'},'components':list(nodes.values()),'connections':edges}
    return {'snapshot_id':identity,'profile_version':profile['profile_version'],'view':view,'candidate':candidate,
            'sources':sources,'unknowns':unknowns,'node_count':len(nodes),'edge_count':len(edges),'omitted_nodes':0,'omitted_edges':0}
