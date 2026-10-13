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
