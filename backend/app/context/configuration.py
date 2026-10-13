"""Operator-owned model settings; the environment remains the initial configuration."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.common import digest, dumps, uid

CONFIGURATION_LOCK = threading.RLock()


def revision(settings):
    from app.context.capabilities import configuration_digest
    return digest({'configuration':configuration_digest(settings),'enabled':settings.external_model_enabled,
                   'json_supported':settings.model_json_supported,'reasoning_effort':settings.model_reasoning_effort,
                   'context_window':settings.model_context_window,'max_output':settings.model_max_output_tokens})


class ModelConfiguration(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider: str = Field(min_length=1,max_length=80)
    base_url: str = Field(min_length=1,max_length=2048)
    model: str = Field(min_length=1,max_length=200)
    api_key: SecretStr = SecretStr('')
    wire_api: str = 'chat_completions'
    external_analysis_enabled: bool

    @field_validator('api_key')
    @classmethod
    def bounded_key(cls,value):
        if len(value.get_secret_value())>8192:
            raise ValueError('模型密钥过长')
        return value

    @field_validator('provider')
    @classmethod
    def supported_provider(cls,value):
        if value not in {'openai','openai-compatible'}:
            raise ValueError('此入口仅支持 OpenAI 或兼容服务，请保留原服务类型')
        return value

    @field_validator('wire_api')
    @classmethod
    def supported_protocol(cls,value):
        if value not in {'chat_completions','responses'}:
            raise ValueError('仅支持 Chat Completions 或 Responses 协议')
        return value

    @field_validator('base_url')
    @classmethod
    def safe_address(cls,value):
        parts=urlsplit(value)
        if (parts.scheme not in {'https','http'} or not parts.hostname or parts.username
                or parts.password or parts.query or parts.fragment):
            raise ValueError('模型地址须为不含密钥、账号、查询参数的 HTTP(S) 地址')
        return value.rstrip('/')


def path(settings):
    return settings.database_path.parent/'model-configuration.enc'


def cipher(settings):
    key=hashlib.sha256(b'ATR:model-configuration:v1\0'+settings.operator_token.get_secret_value().encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def load(settings):
    with CONFIGURATION_LOCK:
        location=path(settings)
        if not location.exists():
            return settings
        try:
            if location.stat().st_size>32768:
                raise ValueError()
            values=json.loads(cipher(settings).decrypt(location.read_bytes()))
            request=ModelConfiguration.model_validate(values)
            return settings.model_copy(update=updates(request))
        except (OSError,ValueError,InvalidToken):
            raise ValueError('加密模型设置无法读取；请恢复匹配的操作员配置和备份') from None


def updates(request):
    return {'model_provider':request.provider,'model_base_url':request.base_url,'model_name':request.model,
            'model_api_key':request.api_key,'model_wire_api':request.wire_api,
            'external_model_enabled':request.external_analysis_enabled,'model_vision_supported':False,
            'model_json_supported':True}


def public(settings):
    from app.context.model import capabilities
    return {'provider':settings.model_provider,'base_url':settings.model_base_url,'model':settings.model_name,
            'wire_api':settings.model_wire_api,'api_key_configured':bool(settings.model_api_key.get_secret_value()),
            'external_analysis_enabled':settings.external_model_enabled,
            'source':'encrypted_operator_configuration' if path(settings).exists() else 'existing_environment',
            'capabilities':capabilities(settings)}


def save(database,settings,request):
    with CONFIGURATION_LOCK, database.transaction() as tx:
        settings=load(settings)
        if tx.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running','waiting_input') LIMIT 1").fetchone():
            raise ValueError('仍有进行中或等待确认的任务，请先结束任务再保存模型设置')
        key=request.api_key.get_secret_value()
        if not key:
            if (request.provider,request.base_url)!=(settings.model_provider,settings.model_base_url.rstrip('/')):
                raise ValueError('更换模型服务地址或类型时，请填写该服务自己的密钥')
            key=settings.model_api_key.get_secret_value()
        if not key:
            raise ValueError('请填写模型服务密钥')
        request=request.model_copy(update={'api_key':SecretStr(key)})
        values=request.model_dump(mode='json')
        values['api_key']=key
        encrypted=cipher(settings).encrypt(dumps(values).encode())
        location=path(settings)
        location.parent.mkdir(parents=True,exist_ok=True)
        temporary=location.with_suffix('.'+uid()+'.tmp')
        descriptor=os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        try:
            with os.fdopen(descriptor,'wb') as output:
                output.write(encrypted)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary,location)
        finally:
            temporary.unlink(missing_ok=True)
        from app.settings import get_settings
        get_settings.cache_clear()
        return public(settings.model_copy(update=updates(request)))
