import json

import pytest
from app.context.budget import ContextBudgetManager
from app.context.capabilities import probe_text, verified_text
from app.context.configuration import ModelConfiguration, load, path, public, save
from app.context.model import call_json
from app.context.model_errors import ModelCallError
from app.jobs import reserve
from pydantic import SecretStr
from test_model_isolation import fake_model  # noqa: F401


def request(settings,**changes):
    return ModelConfiguration(provider=settings.model_provider,base_url=settings.model_base_url,
                              model=changes.pop('model',settings.model_name),external_analysis_enabled=True,**changes)


def owner(settings,database):
    settings.database_path=database.path


def test_encrypted_save_survives_reload_with_local_key(fake_model):  # noqa: F811
    database,settings,_,_=fake_model
    owner(settings,database)
    result=save(database,settings,request(settings,model='chosen-model'))
    assert result['model']=='chosen-model' and result['api_key_configured']
    assert 'api_key' not in result and 'synthetic-secret' not in json.dumps(result)
    assert b'synthetic-secret' not in path(settings).read_bytes()
    reloaded=load(settings)
    assert reloaded.model_name=='chosen-model' and reloaded.model_api_key==settings.model_api_key
    assert load(settings).model_name == 'chosen-model'


def test_destination_change_requires_own_key_and_active_task_blocks_save(fake_model):  # noqa: F811
    database,settings,_,_=fake_model
    owner(settings,database)
    changed=request(settings).model_copy(update={'base_url':'https://another.example/v1'})
    with pytest.raises(ValueError,match='自己的密钥'):
        save(database,settings,changed)
    assert not path(settings).exists()
    reserve(database,'model_text_probe')
    with pytest.raises(ValueError,match='进行中'):
        save(database,settings,request(settings))
    assert not path(settings).exists() and public(settings)['source']=='existing_environment'


def test_configuration_api_is_open_and_invalid_secret_is_not_returned(client):
    assert client.get('/api/v1/model/configuration').status_code==200
    value=client.get('/api/v1/model/configuration').json()
    assert 'api_key' not in value
    payload={'provider':'openai-compatible','base_url':'https://model.example/v1','model':'chosen-model',
             'api_key':'SYNTHETIC-DO-NOT-ECHO','wire_api':'unsupported','external_analysis_enabled':False}
    bad=client.put('/api/v1/model/configuration',json=payload)
    assert bad.status_code==422 and 'SYNTHETIC-DO-NOT-ECHO' not in bad.text
    payload['wire_api']='chat_completions'
    response=client.put('/api/v1/model/configuration',json=payload)
    assert response.status_code==200 and 'SYNTHETIC-DO-NOT-ECHO' not in response.text
    assert client.get('/api/v1/model/configuration').json()['source']=='encrypted_operator_configuration'


def test_text_probe_requires_actual_valid_json_and_invalid_json_keeps_http_status(fake_model):  # noqa: F811
    database,settings,state,_=fake_model
    owner(settings,database)
    state['reply']=lambda context,_:{'marker':context['marker'],'result':42}
    result=probe_text(database,settings)
    assert result['text_verified'] and result['json_verified'] and verified_text(settings)
    settings.model_api_key=SecretStr('different-synthetic-key')
    assert verified_text(settings) is None
    state.pop('reply')
    state['invalid']=True
    manager=ContextBudgetManager(database,settings)
    budget_id=manager.create('initial')
    with pytest.raises(ModelCallError) as caught:
        call_json(database,settings,budget_id,'bounded',{})
    assert caught.value.diagnostic['status_code']==200
    usage=manager.get(budget_id)['usage_records'][0]
    assert usage['diagnostic']['http_status']==200 and usage['usage_kind']=='actual'


def test_captured_settings_cannot_send_after_authorization_or_configuration_changes(fake_model,monkeypatch):  # noqa: F811
    database,settings,state,_=fake_model
    object.__setattr__(settings,'model_configuration_revision','captured-revision')
    current=settings.model_copy(update={'external_model_enabled':False,'model_configuration_revision':'disabled-revision'})
    monkeypatch.setattr('app.settings.get_settings',lambda:current)
    budget_id=ContextBudgetManager(database,settings).create('initial')
    with pytest.raises(ValueError,match='模型设置已变化|外部分析已关闭'):
        call_json(database,settings,budget_id,'source',{'private_business_material':'synthetic'})
    assert state['requests']==0


def test_serialized_saves_cannot_restore_a_captured_old_key(fake_model):  # noqa: F811
    database,settings,_,_=fake_model
    owner(settings,database)
    captured=settings.model_copy(deep=True)
    rotated=request(settings,api_key=SecretStr('rotated-synthetic-key'))
    save(database,settings,rotated)
    save(database,captured,request(captured,model='another-model'))
    assert load(settings).model_api_key.get_secret_value()=='rotated-synthetic-key'
