from __future__ import annotations

import base64
import json
import logging
import math
import re
import sys
import time
from types import SimpleNamespace

from app.common import now
from app.context.budget import ContextBudgetManager
from app.context.model_errors import ModelCallError, diagnostic
from app.tools.registry import sanitize_payload


class ModelUnavailable(ValueError):
    pass


def _parse_content(content):
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Empty model output")
    content = content.strip()
    fence = re.fullmatch(r"```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```", content)
    if fence:
        content = fence[1]

    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Ambiguous duplicate JSON field")
            value[key] = item
        return value

    result = json.loads(content, object_pairs_hook=unique_object)
    if not isinstance(result, dict):
        raise ValueError("Model must return an object")
    return result


def _completion(**request):
    started = time.monotonic()
    supervision = request.pop('_supervision', None)
    process_timeout=request.pop('_process_timeout',request['timeout']+(0 if supervision is not None else 10))
    progress = {'worker_stage': 'initialize', 'worker_initialize_ms': None}

    def record_progress(value):
        if isinstance(value, dict) and value.get('worker_stage') in {'initialize', 'request'}:
            progress['worker_stage'] = value['worker_stage']
            elapsed = value.get('worker_initialize_ms')
            if type(elapsed) in {float, int} and 0 <= elapsed <= process_timeout * 1000:
                progress['worker_initialize_ms'] = elapsed
    try:
        from app.runtime.processes import run_bounded
        result = SimpleNamespace(**run_bounded(
            [sys.executable, '-m', 'app.context.model_worker'],
            json.dumps(request, ensure_ascii=False).encode('utf-8'),
            timeout=process_timeout, max_output=request['max_tokens'] * 96 + 16000,
            on_progress=record_progress,
            **(supervision or {})))
    except InterruptedError:
        raise
    except TimeoutError as exc:
        details=diagnostic(exc, progress['worker_stage'])
        details.update(code='model_timeout',worker_initialize_ms=progress['worker_initialize_ms'])
        error=ModelCallError(details)
        error.model_metadata={'timing': {'worker_initialize_ms': progress['worker_initialize_ms']}}
        raise error from None
    except OSError as exc:
        raise ModelCallError(diagnostic(exc, "initialize")) from None
    if result.returncode or len(result.stdout) > request["max_tokens"] * 96 + 16000:
        raise ModelCallError({"code": "model_internal_error", "exception_type": "ModelWorkerError", "stage": "worker"})
    try:
        envelope = json.loads(result.stdout.splitlines()[-1])
        usage = SimpleNamespace(**envelope["usage"]) if envelope.get("usage") else None
        timing = envelope.get("timing", {})
        timing["process_overhead_ms"] = round(max(0, (time.monotonic() - started) * 1000 - timing.get("worker_elapsed_ms", 0)), 3)
        status=envelope.get('http_status')
        metadata = {"timing": timing, "provider_request_id": envelope.get("provider_request_id"),
                    "http_status":status if type(status) is int and 100<=status<=599 else None}
        if "error" in envelope:
            error = ModelCallError(envelope["error"])
            error.response = SimpleNamespace(usage=usage, choices=[]) if usage else None
            error.model_metadata = metadata
            raise error
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=envelope["content"]))], usage=usage, model_metadata=metadata)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        raise ModelCallError({"code": "model_internal_error", "exception_type": "ModelWorkerProtocolError", "stage": "worker"}) from None


def capabilities(settings):
    from app.context.capabilities import verified_text, verified_vision, vision_ready
    proof=verified_vision(settings)
    text_proof=verified_text(settings)
    return {
        "configured": bool(settings.model_name and settings.model_api_key.get_secret_value()),
        "external_analysis_enabled": settings.external_model_enabled,
        "text": True,
        "structured_json": settings.model_json_supported,
        "text_verified":text_proof is not None,
        "json_verified":text_proof is not None,
        "text_checked_at":text_proof.get('checked_at') if text_proof else None,
        "vision_declared": settings.model_vision_supported,
        "vision_verified":proof is not None,
        "vision_available":vision_ready(settings),
        "vision_checked_at":proof.get('checked_at') if proof else None,
        "vision_adapter": "chat_completions_and_native_responses",
        "context_window": settings.model_context_window,
        "usage": "provider_actual_or_explicit_estimate",
        "model": settings.model_name or None,
    }


def model_messages(system_prompt, context, images=None):
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(sanitize_payload(context), ensure_ascii=False,
                                               separators=(",", ":"))},
    ]
    if images:
        if len(images) != 1:
            raise ValueError('One bounded page per vision request')
        parts=[{'type':'text','text':messages[1]['content']}]
        for image in images:
            data=image['data']
            if (not isinstance(data,bytes) or not data.startswith(b'\xff\xd8\xff') or len(data)>900*1024
                    or type(image['width']) is not int or type(image['height']) is not int
                    or not 0<image['width']<=1024 or not 0<image['height']<=1024):
                raise ValueError('Invalid bounded local image')
            parts.append({'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+base64.b64encode(data).decode('ascii'),
                                                        'detail':'high'}})
        messages[1]['content']=parts
    return messages


def call_json(database, settings, budget_id, system_prompt, context, validate=None, *, supervision=None,
              images=None, deadline=None, receipt=None):
    if settings.model_configuration_revision is not None:
        from app.context.configuration import CONFIGURATION_LOCK
        from app.settings import get_settings
        with CONFIGURATION_LOCK:
            current=get_settings()
            if (not current.external_model_enabled
                    or current.model_configuration_revision!=settings.model_configuration_revision):
                raise ModelUnavailable('模型设置已变化或外部分析已关闭，请重新发起本次任务')
    if (
        not settings.external_model_enabled
        or not settings.model_name
        or not settings.model_api_key.get_secret_value()
    ):
        raise ModelUnavailable("模型未配置或外部分析尚未启用")
    if not settings.model_json_supported:
        raise ModelUnavailable("模型未声明 JSON 结构化输出支持")
    from app.context.capabilities import vision_ready
    if images and not vision_ready(settings):
        raise ModelUnavailable('模型未声明视觉能力')
    messages = model_messages(system_prompt, context, images)
    if supervision is not None:
        with database.connect() as tx:
            supervision['authorize'](tx)
    manager = ContextBudgetManager(database, settings)
    remaining = manager.remaining_seconds(budget_id)
    if deadline is not None:
        from app.context.budget import BudgetExceeded
        remaining=min(remaining,deadline-time.monotonic())
        if remaining<=0:
            raise BudgetExceeded('execution_time_limit')
    image_usage = {'image_count':len(images or []),'image_pixels':sum(image['width']*image['height'] for image in images or []),
                   'image_estimated_tokens':sum(1024+4*math.ceil(image['width']/32)*math.ceil(image['height']/32) for image in images or []),
                   'image_usage_kind':'pixel_estimate_provider_usage_separate'}
    reservation = manager.reserve(budget_id, messages, settings.model_max_output_tokens,
                                  **({'authorize': supervision['authorize']} if supervision else {}))
    audit = manager.start_attempt(budget_id, reservation)
    if images:
        audit.update(image_usage)
    started = time.monotonic()
    parse_started = None
    response = None
    stage = "request"
    try:
        protocol = {'wire_api': 'responses'} if settings.model_wire_api == 'responses' else {}
        if settings.model_reasoning_effort:
            protocol['reasoning_effort'] = settings.model_reasoning_effort
        response = _completion(
            model=settings.model_name
            if "/" in settings.model_name
            else f"openai/{settings.model_name}",
            api_base=settings.model_base_url or None,
            api_key=settings.model_api_key.get_secret_value(),
            timeout=min(settings.model_timeout_seconds, remaining),
            _process_timeout=min(settings.model_timeout_seconds+(0 if supervision else 10),remaining),
            max_tokens=settings.model_max_output_tokens,
            num_retries=0,
            response_format={"type": "json_object"},
            messages=messages,
            **protocol,
            **({'_supervision': {k: v for k, v in supervision.items() if k != 'authorize'}} if supervision is not None else {}),
        )
        stage = "parse"
        parse_started = time.monotonic()
        content = response.choices[0].message.content
        if isinstance(content, str) and len(content.encode()) > settings.model_max_output_tokens * 16:
            raise ValueError("Model output exceeded size limit")
        result = _parse_content(content)
        result = sanitize_payload(result)
        if validate is not None:
            stage = "validate"
            validated = validate(result)
            if validated is not None:
                result = validated
    except Exception as exc:
        if supervision is not None and supervision['cancelled'].is_set():
            # The coordinator owns settlement after lease revocation.
            raise InterruptedError('调查已终止') from None
        error = exc if isinstance(exc, ModelCallError) else ModelCallError(diagnostic(exc, stage))
        _finish_audit(audit, started, parse_started, response or exc, error)
        manager.settle(budget_id, reservation, response or getattr(exc, "response", None), "failed", audit)
        manager.record_time(budget_id, time.monotonic() - started)
        error.diagnostic.update(audit)
        logging.getLogger("atr.model").warning("Model failed: attempt=%s run=%s code=%s type=%s stage=%s status=%s validation=%s", reservation[0], audit["investigation_run_id"], error.code, error.diagnostic["exception_type"], error.diagnostic["stage"], error.diagnostic["status_code"], json.dumps(error.diagnostic.get("validation_errors", [])))
        raise error from None
    _finish_audit(audit, started, parse_started, response)
    if supervision is not None:
        with database.connect() as tx:
            supervision['authorize'](tx)
    manager.settle(budget_id, reservation, response, audit=audit)
    manager.record_time(budget_id, time.monotonic() - started)
    if receipt is not None:
        receipt.update(audit)
    return result


def _finish_audit(audit, started, parse_started, result, error=None):
    metadata = getattr(result, "model_metadata", {})
    timing = metadata.get("timing", {})
    status=(error.diagnostic['status_code'] if error else None) or metadata.get('http_status')
    if error and error.diagnostic['status_code'] is None:
        error.diagnostic['status_code']=status
    audit.update(ended_at=now(), elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                 worker_initialize_ms=timing.get("worker_initialize_ms"), network_ms=timing.get("network_ms"),
                 process_overhead_ms=timing.get("process_overhead_ms"),
                 parse_ms=round((time.monotonic() - parse_started) * 1000, 3) if parse_started else 0,
                 request_stage=error.diagnostic["stage"] if error else "completed",
                 error_category=error.code if error else None,
                 exception_type=error.diagnostic["exception_type"] if error else None,
                 http_status=status,
                 provider_request_id=metadata.get("provider_request_id"))
    if error:
        audit.update({k: error.diagnostic[k] for k in ("validation_kind", "validation_errors", "provider_error_reason") if k in error.diagnostic})
