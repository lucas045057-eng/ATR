import time

import pytest
from app.visualization.bridge import bridge_message, embed_bridge
from app.visualization.projection import project_snapshot
from app.visualization.renderer import render_snapshot, verify_package


def snapshot():
    reference={'schema_version':'source-ref-1','file_id':'owned-file','original_artifact_id':'owned-archive',
               'content_digest':'original-sha','profile_version':2,'page_number':1,'path':'diagram.png','source_type':'Documented'}
    visual={'components':[{'id':'api','name':'API','kind':'component','region':[.1,.1,.3,.3]},
                          {'id':'db','name':'Database','kind':'datastore','region':[.6,.1,.8,.3]}],
            'relations':[{'source':'api','target':'db','relation':'flow','region':[.3,.15,.6,.25]}],
            'unknowns':['A lower relationship has no clear arrowhead']}
    return {'snapshot_id':'frozen-snapshot','profile':{'name':'Frozen document structure','profile_version':2,
            'documents':[{'vision_state':'completed','file_id':'owned-file','page_number':1,'source_refs':[reference],'visual':visual}]}}


def test_projection_preserves_source_regions_and_rejects_active_content():
    data=snapshot()
    projected=project_snapshot(data,'structure')
    assert projected['node_count']==2 and projected['edge_count']==1
    assert projected['snapshot_id']=='frozen-snapshot' and projected['unknowns']
    edge=projected['candidate']['connections'][0]
    assert projected['sources'][edge['id']][0]['region']==[.3,.15,.6,.25]
    data['profile']['documents'][0]['visual']['components'][0]['name']='<script>alert(1)</script>'
    with pytest.raises(ValueError):
        project_snapshot(data,'structure')


def test_bridge_fails_closed_for_wrong_snapshot_nonce_and_unknown_id():
    message={'protocol':'atr-visualization-1','snapshot_id':'frozen','nonce':'fresh','type':'source','id':'owned'}
    assert bridge_message(message,'frozen','fresh',{'owned'})
    for field,value in [('nonce','expired'),('snapshot_id','old'),('id','foreign'),('protocol','other'),('type','execute')]:
        assert not bridge_message({**message,field:value},'frozen','fresh',{'owned'})
    html=embed_bridge('<html><head></head><body><svg></svg></body></html>','frozen','fresh')
    assert "connect-src 'none'" in html and 'postMessage' in html


def test_vendored_renderer_runs_without_skill_paths_or_npm_and_cache_is_frozen(tmp_path):
    package=verify_package()
    assert 'third_party' in str(package) and '.codex' not in str(package)
    projected=project_snapshot(snapshot(),'structure')
    rendered=render_snapshot(projected,time.monotonic()+30,tmp_path)
    assert '<svg' in rendered['html'] and not rendered['cached']
    cached=render_snapshot(projected,time.monotonic()+30,tmp_path)
    assert cached['cached'] and cached['html_sha256']==rendered['html_sha256']
    assert cached['snapshot_id']==projected['snapshot_id']
    light=render_snapshot(projected,time.monotonic()+30,tmp_path,theme='light')
    assert not light['cached'] and light['theme']=='light'
    assert light['snapshot_id']==cached['snapshot_id'] and light['view']==cached['view']
    assert render_snapshot(projected,time.monotonic()+30,tmp_path,theme='dark')['cached']
    embedded=embed_bridge(light['html'],light['snapshot_id'],'fresh',theme='light')
    assert 'const theme="light"' in embedded and 'attributeFilter' in embedded
    with pytest.raises(ValueError):
        embed_bridge(light['html'],light['snapshot_id'],'fresh',theme='untrusted')
