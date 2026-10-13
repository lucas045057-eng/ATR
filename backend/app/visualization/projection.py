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
    categories={'Static':('backend','静态解析'),'Documented':('frontend','资料描述'),
                'Runtime':('security','运行观测'),'Inferred':('cloud','模型推断'),
                'Human Verified':('messagebus','人工确认'),'Conflict':('database','冲突'),
                'Unknown':('external','未知 / 待核验')}

    def add(key,label,kind='backend',refs=(),certainty='待核验'):
        identifier='n'+digest(key)[:18]
        if identifier not in nodes:
            category=next((key for key,(_,title) in categories.items() if key in str(certainty) or title in str(certainty)),'Unknown')
            color,title=categories[category]
            detail=' · '+str(certainty).removeprefix('Static').strip(' ·') if category=='Static' and certainty!='Static' else ''
            nodes[identifier]={'id':identifier,'type':color,'label':safe_label(label),
                               'sublabel':safe_label(title+detail+(' · 数据组件' if kind=='database' else '')+(' · 已审核' if '已审核' in certainty else ' · 待审核' if category=='Documented' else ''))}
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

        def graph_sources(attributes):
            provenance=attributes.get('provenance') or {}
            refs=attributes.get('source_refs') or (provenance.get('source_refs') if isinstance(provenance,dict) else []) or []
            identifiers=list(attributes.get('evidence_ids',[]))
            if attributes.get('observation_id'):
                identifiers.append(attributes['observation_id'])
            return refs or [{'schema_version':'observation-ref-1','observation_id':item,
                             'snapshot_id':identity,'source_type':'Runtime'} for item in dict.fromkeys(identifiers)]

        for node in graph.get('nodes',[]):
            attributes=node.get('attributes',{})
            provenance=attributes.get('provenance') or {}
            if not isinstance(provenance,dict):
                provenance={}
            if provenance.get('verification_status') in {'rejected','stale'}:
                unknowns.append('已按审核结果排除节点：'+safe_label(node['name']))
                continue
            kind='database' if node.get('role') in {'datastore','cache'} or node['kind']=='datastore' else 'backend'
            refs=graph_sources(attributes)
            mapped[node['id']]=add(node['canonical_key'],node['name'],kind,refs,
                                    provenance.get('source_type') or attributes.get('source_type','待核验'))
        for edge in graph.get('edges',[]):
            if edge['from_node_id'] in mapped and edge['to_node_id'] in mapped:
                attributes=edge.get('attributes',{})
                connect(mapped[edge['from_node_id']],mapped[edge['to_node_id']],edge['relation'],
                        graph_sources(attributes))
        facts=profile.get('facts',[])
        for document in profile.get('documents',[]):
            if document.get('vision_state')!='completed' or document.get('external_paused'):
                continue
            visual=document.get('visual') or {}
            prefix=f"document:{document['file_id']}:page:{document['page_number']}:"
            mapped={}
            for component in visual.get('components',[]):
                fact=next((fact for fact in facts if fact['subject']==prefix+component['id'] and fact['predicate']=='depicts_component'),{})
                if fact.get('verification_status') in {'rejected','stale'}:
                    unknowns.append('已按审核结果排除图片组件：'+safe_label(component['name']))
                    continue
                refs=fact.get('source_refs') or [{**ref,'region':component['region']} for ref in document['source_refs']]
                mapped[component['id']]=add(prefix+component['id'],component['name'],
                                          'database' if component['kind']=='datastore' else 'backend',refs,
                                          'Documented · 已审核' if fact.get('verification_status')=='confirmed' else 'Documented · 待审核')
            for edge in visual.get('relations',[]):
                fact=next((fact for fact in facts if fact['subject']==prefix+edge['source'] and fact['predicate']==edge['relation']
                           and isinstance(fact.get('object'),dict) and fact['object'].get('target')==prefix+edge['target']),{})
                if fact.get('verification_status') in {'rejected','stale'}:
                    unknowns.append('已按审核结果排除图片关系：'+safe_label(edge['relation']))
                    continue
                refs=fact.get('source_refs') or [{**ref,'region':edge['region']} for ref in document['source_refs']]
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
                         'backend',step.get('source_refs',[]),'Static · '+(conditions or ('启动路径' if flow['kind']=='startup' else '源码路径 · 未验证执行')))
            for edge in flow.get('edges',[]):
                connect(mapped.get(edge['from']),mapped.get(edge['to']),edge['relation'])
            unknowns.extend(flow.get('unknowns',[]))
    if not nodes:
        raise ValueError('此版本没有可绘制的结构或源码流程，请先导入相应资料')
    for index,node in enumerate(nodes.values()):
        node.update(pos=[60+index%3*390,100+index//3*170],size=[240,78])
    # Archify rejects labels covering a node; place each label in the nearest clear gap.
    occupied=[(node['pos'][0]-8,node['pos'][1]-8,node['size'][0]+16,node['size'][1]+16) for node in nodes.values()]
    for edge in edges:
        first,last=nodes[edge['from']],nodes[edge['to']]
        x=(first['pos'][0]+last['pos'][0])/2+120
        y=(first['pos'][1]+last['pos'][1])/2+39
        width=max(40,sum(13 if ord(character)>127 else 9 for character in edge['label']))
        placed=False
        for distance in range(0,2020,20):
            for dx,dy in ((0,distance),(0,-distance),(distance,0),(-distance,0),
                          (distance,distance),(-distance,distance),(distance,-distance),(-distance,-distance)):
                left,top=x+dx-width/2,y+dy-12
                if left<30 or top<50 or any(left<ox+ow and left+width>ox and top<oy+oh and top+24>oy for ox,oy,ow,oh in occupied):
                    continue
                edge['labelAt']=[x+dx,y+dy]
                occupied.append((left,top,width,24))
                placed=True
                break
            if placed:
                break
        if not placed:
            bottom=max(top+height for _,top,_,height in occupied)+24
            edge['labelAt']=[30+width/2,bottom]
            occupied.append((30,bottom-12,width,24))
    title=f"{profile['name']} · {'业务结构' if view=='structure' else '源码流程'} · v{profile['profile_version']}"
    candidate={'schema_version':1,'diagram_type':'architecture','meta':{'title':safe_label(title),'locale':'zh-CN',
               'output':'diagram.html','quality_profile':'standard','subtitle':identity[:16]+' · '+view,
               'legend':{'mode':'all','entries':{kind:{'label':title} for kind,title in categories.values()}}},
               'components':list(nodes.values()),'connections':edges}
    return {'snapshot_id':identity,'profile_version':profile['profile_version'],'view':view,'candidate':candidate,
            'sources':sources,'unknowns':unknowns,'node_count':len(nodes),'edge_count':len(edges),'omitted_nodes':0,'omitted_edges':0}
