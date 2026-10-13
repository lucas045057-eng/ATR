import hashlib
import json

import pytest
from app.business.flows import build_flows
from app.business.languages import describe_language
from app.context.model import model_messages


@pytest.mark.parametrize('path,source',[
    ('api.py','from fastapi import FastAPI\nimport hmac\napp=FastAPI()\n@app.get("/check")\ndef check(api_key):\n    if hmac.compare_digest(api_key, "SYNTHETIC_CREDENTIAL_ARGUMENT_7Q"):\n        allow()\n    query("SELECT id FROM catalog_items", ("demo-1",))\n    return False\n'),
    ('api.js','import express from "express"; const app=express();\nfunction check(apiKey){if(compare(apiKey,"SYNTHETIC_CREDENTIAL_ARGUMENT_7Q")){allow();} return false;}\napp.get("/check",check);'),
    ('api.c','int main(){if(compare(api_key,"SYNTHETIC_CREDENTIAL_ARGUMENT_7Q")){allow();}return 0;}'),
])
def test_secret_argument_never_reaches_structured_source_or_model_message(path,source):
    info=describe_language(path,source)
    assert info['parse_state']=='complete' and info['branches']
    row={'id':'original-file','artifact_id':'original-archive','path':path,'media_type':'code',
         'content_digest':hashlib.sha256(source.encode()).hexdigest(),'analysis':info}
    flows=build_flows([row],1)
    assert flows
    packet=json.dumps(model_messages('source',{'flows':flows}),ensure_ascii=False)
    assert 'SYNTHETIC_CREDENTIAL_ARGUMENT_7Q' not in packet
    assert 'REDACTED' in packet
    if path.endswith('.py'):
        assert info['data_operations'][0]['literal_parameters']==['demo-1']


def test_historical_cached_condition_is_redacted_at_model_boundary():
    message=model_messages('source',{'steps':[{'details':{'condition':'compare(api_key,"SYNTHETIC_OLD_CACHE_SECRET_7Q")'}}]})
    assert 'SYNTHETIC_OLD_CACHE_SECRET_7Q' not in json.dumps(message)


@pytest.mark.parametrize('path,source',[
    ('api.py','from fastapi import FastAPI\napp=FastAPI()\n@app.get("/check")\ndef check(api_key):\n    if api_key == """SYNTHETIC_FIRST_LINE\nSYNTHETIC_MIDDLE_SECRET\nSYNTHETIC_LAST_LINE""":\n        allow()\n    return False\n'),
    ('api.js','import express from "express"; const app=express();\nfunction check(apiKey){ if (apiKey === `SYNTHETIC_FIRST_LINE\nSYNTHETIC_MIDDLE_SECRET\nSYNTHETIC_LAST_LINE`) {allow();} return false;}\napp.get("/check",check);\n'),
])
def test_multiline_credential_condition_context_preserves_lines_without_secret_fragments(client,path,source):
    from test_business import onboard
    profile=onboard(client,{path:source})
    file=next(reference for reference in profile['source_references'] if reference['path']==path)
    response=client.post(f"/api/v1/businesses/{profile['business_id']}/code/context",json={'file_id':file['id'],'start_line':1,'max_lines':80})
    assert response.status_code==200,response.text
    context=response.json()
    assert len(context['text'].splitlines())==len(source.splitlines())
    for fragment in ('SYNTHETIC_FIRST_LINE','SYNTHETIC_MIDDLE_SECRET','SYNTHETIC_LAST_LINE'):
        assert fragment not in json.dumps(model_messages('source',{'context':context}))


@pytest.mark.parametrize('comment',["", "# user's settings\n", "// user's settings\n", "/* user's settings */\n"])
def test_source_window_inside_multiline_assignment_cannot_lose_credential_context(client,comment):
    from test_business import onboard
    source=comment+'api_key = """SYNTHETIC_ASSIGNMENT_FIRST\nSYNTHETIC_ASSIGNMENT_MIDDLE\nSYNTHETIC_ASSIGNMENT_LAST"""\ndef check():\n    return True\n'
    profile=onboard(client,{'api.py':source})
    file=next(reference for reference in profile['source_references'] if reference['path']=='api.py')
    line=2+comment.count('\n')
    response=client.post(f"/api/v1/businesses/{profile['business_id']}/code/context",json={'file_id':file['id'],'start_line':line,'max_lines':1})
    assert response.status_code==200,response.text
    context=response.json()
    assert context['start_line']==line and context['end_line']==line
    assert 'SYNTHETIC_ASSIGNMENT_MIDDLE' not in json.dumps(model_messages('source',{'context':context}))


def test_unterminated_conditional_literal_is_hidden_in_partial_parser_result():
    source='import express from "express"; const app=express();\napp.get("/check", function check(apiKey){ if(compare(apiKey,"SYNTHETIC_UNTERMINATED_SECRET\n )) {return true;} return false; });'
    info=describe_language('api.js',source)
    assert info['parse_state']=='partial_parser_error' and info['branches']
    row={'id':'original-file','artifact_id':'original-archive','path':'api.js','media_type':'code','content_digest':hashlib.sha256(source.encode()).hexdigest(),'analysis':info}
    assert 'SYNTHETIC_UNTERMINATED_SECRET' not in json.dumps(model_messages('source',{'flows':build_flows([row],1)}))


@pytest.mark.parametrize('path',['api.c','api.cpp'])
@pytest.mark.parametrize('continuation',['','\\\n  ','\\\r\n  ','\\\n \\\n  '])
def test_preprocessor_credential_literal_does_not_bypass_source_masking(path,continuation):
    from app.tools.redaction import redact_source_text
    source='#define API_KEY '+continuation+'"SYNTHETIC_MACRO_SECRET"\nint main(){return 0;}\n'
    assert describe_language(path,source)['parse_state']=='complete'
    assert 'SYNTHETIC_MACRO_SECRET' not in json.dumps(model_messages('source',{'text':redact_source_text(source)}))


@pytest.mark.parametrize('prefix',['#define CREDENTIALS ','const char *credentials = '])
@pytest.mark.parametrize('delimiter',['','ATR'])
def test_cpp_raw_credential_literal_is_hidden_as_a_whole(prefix,delimiter):
    from app.tools.redaction import redact_source_text
    source=prefix+'R"'+delimiter+'({"api_key":"SYNTHETIC_RAW_CREDENTIAL_SECRET"})'+delimiter+'";\nint main(){return 0;}\n'
    assert describe_language('api.cpp',source)['parse_state']=='complete'
    assert 'SYNTHETIC_RAW_CREDENTIAL_SECRET' not in json.dumps(model_messages('source',{'text':redact_source_text(source)}))


def test_partial_multiline_raw_directive_remains_fail_closed():
    from app.tools.redaction import redact_source_text
    source='#define API_KEY R"ATR(\nSYNTHETIC_MULTILINE_RAW_SECRET\n)ATR"\nint main(){return 0;}\n'
    assert describe_language('api.cpp',source)['parse_state']=='partial_parser_error'
    masked=redact_source_text(source)
    assert masked.count('\n')==source.count('\n')
    assert 'SYNTHETIC_MULTILINE_RAW_SECRET' not in json.dumps(model_messages('source',{'text':masked}))
