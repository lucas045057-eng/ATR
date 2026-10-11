from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
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
    env = dict(os.environ)
    env.update(PYTHONPATH=str(Path(__file__).resolve().parents[2]), PYTHONUTF8="1",
               LITELLM_MODE="PRODUCTION", LITELLM_LOCAL_MODEL_COST_MAP="True")
    try:
        result = subprocess.run([sys.executable, "-m", "app.context.model_worker"],
                                input=json.dumps(request, ensure_ascii=False).encode("utf-8"),
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
                                timeout=request["timeout"] + 10)
    except subprocess.TimeoutExpired as exc:
        error = diagnostic(exc, "worker")
        try:
            progress = json.loads((exc.stdout or b'').splitlines()[-1])
            if progress.get('worker_stage') in {'initialize', 'request'}:
                error['stage'] = progress['worker_stage']
        except (ValueError, IndexError, TypeError):
            pass
        raise ModelCallError(error) from None
    except OSError as exc:
        raise ModelCallError(diagnostic(exc, "initialize")) from None
    if result.returncode or len(result.stdout) > request["max_tokens"] * 96 + 16000:
        raise ModelCallError({"code": "model_internal_error", "exception_type": "ModelWorkerError", "stage": "worker"})
    try:
        envelope = json.loads(result.stdout.splitlines()[-1])
        usage = SimpleNamespace(**envelope["usage"]) if envelope.get("usage") else None
        timing = envelope.get("timing", {})
        timing["process_overhead_ms"] = round(max(0, (time.monotonic() - started) * 1000 - timing.get("worker_elapsed_ms", 0)), 3)
        metadata = {"timing": timing, "provider_request_id": envelope.get("provider_request_id")}
        if "error" in envelope:
            error = ModelCallError(envelope["error"])
            error.response = SimpleNamespace(usage=usage, choices=[]) if usage else None
            error.model_metadata = metadata
            raise error
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=envelope["content"]))], usage=usage, model_metadata=metadata)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        raise ModelCallError({"code": "model_internal_error", "exception_type": "ModelWorkerProtocolError", "stage": "worker"}) from None


def capabilities(settings):
    return {
        "configured": bool(settings.model_name and settings.model_api_key.get_secret_value()),
        "external_analysis_enabled": settings.external_model_enabled,
        "text": True,
        "structured_json": settings.model_json_supported,
        "vision_declared": settings.model_vision_supported,
        "vision_adapter": "pending",
        "context_window": settings.model_context_window,
        "usage": "provider_actual_or_explicit_estimate",
        "model": settings.model_name or None,
    }


def model_messages(system_prompt, context):
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(sanitize_payload(context), ensure_ascii=False,
                                               separators=(",", ":"))},
    ]


def call_json(database, settings, budget_id, system_prompt, context, validate=None):
    if (
        not settings.external_model_enabled
        or not settings.model_name
        or not settings.model_api_key.get_secret_value()
    ):
        raise ModelUnavailable("模型未配置或外部分析尚未启用")
    if not settings.model_json_supported:
        raise ModelUnavailable("模型未声明 JSON 结构化输出支持")
    messages = model_messages(system_prompt, context)
    manager = ContextBudgetManager(database, settings)
    remaining = manager.remaining_seconds(budget_id)
    reservation = manager.reserve(budget_id, messages, settings.model_max_output_tokens)
    audit = manager.start_attempt(budget_id, reservation)
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
            max_tokens=settings.model_max_output_tokens,
            num_retries=0,
            response_format={"type": "json_object"},
            messages=messages,
            **protocol,
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
        error = exc if isinstance(exc, ModelCallError) else ModelCallError(diagnostic(exc, stage))
        _finish_audit(audit, started, parse_started, response or exc, error)
        manager.settle(budget_id, reservation, response or getattr(exc, "response", None), "failed", audit)
        manager.record_time(budget_id, time.monotonic() - started)
        error.diagnostic.update(audit)
        logging.getLogger("atr.model").warning("Model failed: attempt=%s run=%s code=%s type=%s stage=%s status=%s validation=%s", reservation[0], audit["investigation_run_id"], error.code, error.diagnostic["exception_type"], error.diagnostic["stage"], error.diagnostic["status_code"], json.dumps(error.diagnostic.get("validation_errors", [])))
        raise error from None
    _finish_audit(audit, started, parse_started, response)
    manager.settle(budget_id, reservation, response, audit=audit)
    manager.record_time(budget_id, time.monotonic() - started)
    return result


def _finish_audit(audit, started, parse_started, result, error=None):
    metadata = getattr(result, "model_metadata", {})
    timing = metadata.get("timing", {})
    audit.update(ended_at=now(), elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                 worker_initialize_ms=timing.get("worker_initialize_ms"), network_ms=timing.get("network_ms"),
                 process_overhead_ms=timing.get("process_overhead_ms"),
                 parse_ms=round((time.monotonic() - parse_started) * 1000, 3) if parse_started else 0,
                 request_stage=error.diagnostic["stage"] if error else "completed",
                 error_category=error.code if error else None,
                 exception_type=error.diagnostic["exception_type"] if error else None,
                 http_status=error.diagnostic["status_code"] if error else None,
                 provider_request_id=metadata.get("provider_request_id"))
    if error:
        audit.update({k: error.diagnostic[k] for k in ("validation_kind", "validation_errors", "provider_error_reason") if k in error.diagnostic})
