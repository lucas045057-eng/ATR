import json

import pytest
from app.context.capabilities import probe_vision, proof_path, verified_vision, vision_ready
from pydantic import SecretStr
from test_model_isolation import fake_model  # noqa: F401


@pytest.mark.parametrize('correct',[True,False])
def test_actual_wire_proof_is_persisted_only_after_visual_match(fake_model,monkeypatch,correct):  # noqa: F811
    database,settings,state,_=fake_model
    settings.database_path=database.path
    settings.operator_token=SecretStr('private-proof-key-32-characters-synthetic')
    monkeypatch.setattr('app.context.capabilities.secrets.randbelow',lambda size:0)
    body={'marker':'100000' if correct else '999999','color':'red'}
    state['envelope']={'id':'local-proof','object':'chat.completion','created':1,'model':settings.model_name,
                       'choices':[{'index':0,'message':{'role':'assistant','content':json.dumps(body)},'finish_reason':'stop'}],
                       'usage':{'prompt_tokens':11,'completion_tokens':5}}
    if not correct:
        with pytest.raises(ValueError):
            probe_vision(database,settings)
        assert not proof_path(settings).exists() and not vision_ready(settings)
        return
    result=probe_vision(database,settings)
    assert result['vision_verified'] and not settings.model_vision_supported
    assert verified_vision(settings)['model']==settings.model_name and vision_ready(settings)
    stored=proof_path(settings).read_text()
    assert 'private-proof-key' not in stored and 'synthetic-secret' not in stored
    assert state['requests']==1 and state['wire']['messages'][1]['content'][1]['type']=='image_url'
    envelope=json.loads(stored)
    envelope['proof']['model']='tampered'
    proof_path(settings).write_text(json.dumps(envelope))
    assert verified_vision(settings) is None
    proof_path(settings).write_text(stored)
    settings.model_name='another-model'
    assert verified_vision(settings) is None
