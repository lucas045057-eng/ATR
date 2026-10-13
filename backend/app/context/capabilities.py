"""Actual vision proof for the chosen model; no provider changes or persisted credentials."""
import hashlib
import hmac
import io
import json
import os
import secrets
import time

from app.common import digest, dumps, now, uid


def configuration_digest(settings):
    return digest({'model':settings.model_name,'provider':settings.model_provider,
                   'base_url':settings.model_base_url,'wire_api':settings.model_wire_api})


def proof_path(settings):
    return settings.database_path.parent/'model-vision-proof.json'


def proof_signature(settings,proof):
    return hmac.new(settings.operator_token.get_secret_value().encode(),dumps(proof).encode(),hashlib.sha256).hexdigest()


def verified_vision(settings):
    try:
        path=proof_path(settings)
        if path.stat().st_size>16384:
            return None
        envelope=json.loads(path.read_text(encoding='utf-8'))
        proof=envelope['proof']
        if (not hmac.compare_digest(envelope['signature'],proof_signature(settings,proof))
                or proof.get('configuration_digest')!=configuration_digest(settings)
                or proof.get('vision_verified') is not True):
            return None
        return proof
    except (OSError,ValueError,KeyError,TypeError):
        return None


def vision_ready(settings):
    return settings.model_vision_supported or verified_vision(settings) is not None


def probe_vision(database,settings):
    from PIL import Image, ImageDraw, ImageFont

    from app.context.budget import BudgetLimits, ContextBudgetManager
    from app.context.model import call_json
    nonce=str(secrets.randbelow(900000)+100000)
    image=Image.new('RGB',(600,260),'white')
    draw=ImageDraw.Draw(image)
    draw.rectangle((30,40,180,190),fill='red')
    draw.text((230,95),nonce,font=ImageFont.load_default(size=48),fill='black')
    stream=io.BytesIO()
    image.save(stream,format='JPEG',quality=95)
    image.close()
    def validate(value):
        if set(value)!={'marker','color'} or value['marker']!=nonce or value['color']!='red':
            raise ValueError('Actual image proof did not match')
        return value
    budget_id=ContextBudgetManager(database,settings).create('capability',limits=BudgetLimits(
        max_calls=1,max_tools=1,max_input_tokens=settings.initial_input_tokens,
        max_total_tokens=min(settings.budget_total_tokens,settings.model_context_window),max_execution_seconds=45))
    receipt={}
    call_json(database,settings.model_copy(update={'model_vision_supported':True}),budget_id,
              'Read the supplied image. Return only JSON with marker (the exact printed digits as a string) and color (the solid square color in lowercase English).',
              {'purpose':'actual visual capability check'},validate=validate,
              images=[{'data':stream.getvalue(),'width':600,'height':260}],deadline=time.monotonic()+45,receipt=receipt)
    proof={'vision_verified':True,'configuration_digest':configuration_digest(settings),'checked_at':now(),
           'model':settings.model_name,'wire_api':settings.model_wire_api,'budget_id':budget_id,
           'request_attempt_id':receipt['request_attempt_id'],'image_count':1,'image_pixels':156000,
           'usage_kind':receipt['usage_kind']}
    path=proof_path(settings)
    temp=path.with_suffix('.'+uid()+'.tmp')
    path.parent.mkdir(parents=True,exist_ok=True)
    descriptor=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w',encoding='utf-8',newline='\n') as output:
        output.write(dumps({'proof':proof,'signature':proof_signature(settings,proof)}))
        output.flush()
        os.fsync(output.fileno())
    os.replace(temp,path)
    return {'status':'verified','vision_verified':True,'proof':proof}
