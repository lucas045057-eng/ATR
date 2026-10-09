from __future__ import annotations

import json

from app.context.budget import ContextBudgetManager
from app.tools.redaction import redact_text


class ModelUnavailable(ValueError):
    pass


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


def call_json(database, settings, budget_id, system_prompt, context):
    if (
        not settings.external_model_enabled
        or not settings.model_name
        or not settings.model_api_key.get_secret_value()
    ):
        raise ModelUnavailable("模型未配置或外部分析尚未启用")
    if not settings.model_json_supported:
        raise ModelUnavailable("模型未声明 JSON 结构化输出支持")
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": redact_text(json.dumps(context, ensure_ascii=False))},
    ]
    manager = ContextBudgetManager(database, settings)
    reservation = manager.reserve(budget_id, messages, settings.model_max_output_tokens)
    response = None
    try:
        from litellm import completion

        response = completion(
            model=settings.model_name
            if "/" in settings.model_name
            else f"openai/{settings.model_name}",
            api_base=settings.model_base_url or None,
            api_key=settings.model_api_key.get_secret_value(),
            timeout=settings.model_timeout_seconds,
            max_tokens=settings.model_max_output_tokens,
            num_retries=0,
            response_format={"type": "json_object"},
            messages=messages,
        )
        content = response.choices[0].message.content or "{}"
        if len(content.encode()) > settings.model_max_output_tokens * 16:
            raise ValueError("Model output exceeded size limit")
        result = json.loads(redact_text(content))
        if not isinstance(result, dict):
            raise ValueError("Model must return an object")
    except Exception:
        manager.settle(budget_id, reservation, response, "failed")
        raise
    manager.settle(budget_id, reservation, response)
    return result
