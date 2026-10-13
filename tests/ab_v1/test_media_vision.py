import hashlib
import io
import json
import time

import pytest
from app.business.media import validate_visual_page
from app.business.profile import build_static
from app.business.service import BusinessService
from app.context.budget import ContextBudgetManager
from app.context.model import call_json
from PIL import Image
from test_model_isolation import fake_model  # noqa: F401


def image_bytes():
    stream=io.BytesIO()
    Image.new('RGB',(400,300),'white').save(stream,format='JPEG')
    return stream.getvalue()


def visual_result():
    return {'summary':'Catalog API writes to database','components':[
        {'id':'api','name':'Catalog API','kind':'component','region':[.1,.1,.4,.4]},
        {'id':'db','name':'Database','kind':'datastore','region':[.6,.1,.9,.4]}],
        'relations':[{'source':'api','target':'db','relation':'writes','direction':'forward',
                      'certainty':'explicit','confidence':.95,'region':[.4,.15,.6,.25]},
                     {'source':'db','target':'api','relation':'flow','direction':'unknown',
                      'certainty':'uncertain','confidence':.3,'region':[.4,.3,.6,.4]}],
        'unknowns':[]}


def test_ambiguous_direction_never_becomes_an_edge_and_regions_are_validated():
    value=validate_visual_page(visual_result())
    assert len(value['relations'])==1 and value['relations'][0]['source']=='api'
    assert value['unknowns']
    for mutation in ('unknown_id','nan_region','reversed_region','text_confidence','object_unknown'):
        value=visual_result()
        if mutation=='unknown_id':
            value['relations'][0]['target']='other'
        elif mutation=='nan_region':
            value['components'][0]['region'][0]=float('nan')
        elif mutation=='reversed_region':
            value['components'][0]['region']=[.7,.1,.2,.8]
        elif mutation=='text_confidence':
            value['relations'][0]['confidence']='high'
        else:
            value['unknowns']=[{'reason':'unclear'}]
        with pytest.raises(ValueError):
            validate_visual_page(value)


@pytest.mark.parametrize('wire_api',['chat_completions','responses'])
def test_vision_wire_and_usage_keep_images_out_of_plain_text_and_diagnostics(fake_model,wire_api):  # noqa: F811
    database,settings,state,_=fake_model
    settings.model_vision_supported=True
    settings.model_wire_api=wire_api
    content=json.dumps(visual_result())
    state['envelope']=({'id':'local-vision','object':'chat.completion','created':1,'model':'gpt-6.1-sol',
                       'choices':[{'index':0,'message':{'role':'assistant','content':content},'finish_reason':'stop'}],
                       'usage':{'prompt_tokens':123,'completion_tokens':45}} if wire_api=='chat_completions' else
                      {'id':'resp_local','object':'response','created_at':1,'status':'completed','model':'gpt-6.1-sol',
                       'error':None,'incomplete_details':None,
                       'output':[{'type':'message','id':'msg_local','role':'assistant','status':'completed',
                                  'content':[{'type':'output_text','text':content,'annotations':[]}]}],
                       'usage':{'input_tokens':123,'output_tokens':45,'total_tokens':168}})
    manager=ContextBudgetManager(database,settings)
    budget=manager.create('initial')
    receipt={}
    result=call_json(database,settings,budget,'Interpret as evidence',{'source':'same-page'},validate=validate_visual_page,
                     images=[{'data':image_bytes(),'width':400,'height':300}],receipt=receipt)
    assert len(result['relations'])==1
    parts=state['wire']['messages' if wire_api=='chat_completions' else 'input'][1]['content']
    assert parts[0]['type']==('text' if wire_api=='chat_completions' else 'input_text')
    assert parts[1]['type']==('image_url' if wire_api=='chat_completions' else 'input_image')
    diagnostic=manager.get(budget)['usage_records'][0]['diagnostic']
    assert diagnostic['image_count']==1 and diagnostic['image_pixels']==120000
    assert diagnostic['usage_kind']=='actual' and manager.get(budget)['input_tokens']==123
    assert 'data:image' not in json.dumps(diagnostic) and 'synthetic-secret' not in json.dumps(diagnostic)
    assert receipt['request_attempt_id']==diagnostic['request_attempt_id']


def test_page_only_pause_and_documented_provenance_survive_vision_enrichment(fake_model,tmp_path,monkeypatch):  # noqa: F811
    database,settings,_,_=fake_model
    settings.model_vision_supported=True
    output=tmp_path/'derived'
    output.mkdir()
    raw=image_bytes()
    (output/'vision-1.jpg').write_bytes(raw)
    source={'schema_version':'source-ref-1','path':'diagram.png','page_number':1,'content_digest':'original-sha'}
    parsed={'media_type':'image','parse_state':'local_media_complete','derived_path':'derived','total_pages':2,'next_page':2,
            'pages':[{'page_number':1,'text':'Catalog API','source_refs':[source],'vision_image_path':'vision-1.jpg',
                      'vision_image_size':[400,300],'vision_image_digest':hashlib.sha256(raw).hexdigest()},
                     {'page_number':2,'text':'[REDACTED_SENSITIVE_PAGE]','source_refs':[{**source,'page_number':2}],
                      'external_paused':True}]}
    calls=[]
    def provider(*args,**kwargs):
        calls.append(args[4])
        kwargs['receipt'].update(request_attempt_id='actual-attempt',image_count=1,image_pixels=120000)
        return validate_visual_page(visual_result())
    monkeypatch.setattr('app.business.service.call_json',provider)
    row={'id':'file-original','artifact_id':'archive-original','path':'diagram.png',
         'content_digest':'original-sha','bytes':len(raw),'media_type':'image','analysis':parsed}
    service=BusinessService(database,settings)
    budget=ContextBudgetManager(database,settings).create('initial')
    result=service.process_media_vision(tmp_path,row,parsed,budget,time.monotonic()+60)
    assert len(calls)==1 and result['pages'][1]['vision_state']=='paused_sensitive_or_local_failure'
    assert result['parse_state']=='partial_media'
    row['analysis']=result
    profile=build_static({'id':'business','name':'same-business','description':'','current_version':1},[row],None)
    edges=[fact for fact in profile.facts if fact.predicate=='writes']
    assert len(edges)==1 and edges[0].source_type=='Documented' and edges[0].verification_status=='pending'
    reference=edges[0].source_refs[0]
    assert reference['region']==[.4,.15,.6,.25] and reference['page_number']==1
    assert reference['file_id']=='file-original' and reference['original_artifact_id']=='archive-original'
    assert reference['content_digest']=='original-sha' and reference['profile_version']==2
    service.process_media_vision(tmp_path,row,result,budget,time.monotonic()+60)
    assert len(calls)==1, 'completed pages must not be sent to the provider again'
