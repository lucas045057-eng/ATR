"""One request per process: LiteLLM cannot modify the API's logging/import state."""

import contextlib
import json
import os
import re
import sys
import time
from collections.abc import Mapping
from urllib.parse import urlsplit

from app.context.model_errors import diagnostic, usage_counts


def provider_request_id(response, api_key):
    hidden = getattr(response, "_hidden_params", None)
    headers = getattr(response, "headers", None) or (hidden.get("additional_headers") if isinstance(hidden, Mapping) else None)
    if not isinstance(headers, Mapping):
        return None
    for key in ("x-request-id", "request-id"):
        value = headers.get(key)
        if isinstance(value, str) and not (api_key and api_key in value) and re.fullmatch(r"(?:req[-_][A-Za-z0-9_-]{4,100}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})", value):
            return value
    return None


def responses_text(body):
    if body.get('status') != 'completed' or body.get('error') or body.get('incomplete_details') is not None:
        raise ValueError('Incomplete model response')
    parts = []
    for item in body.get('output', []):
        if item.get('type') == 'reasoning':
            continue
        if item.get('type') != 'message' or item.get('role') != 'assistant' or item.get('status') != 'completed':
            raise ValueError('Unexpected model output')
        for chunk in item.get('content', []):
            if chunk.get('type') != 'output_text' or not isinstance(chunk.get('text'), str):
                raise ValueError('Unexpected model content')
            parts.append(chunk['text'])
    if not parts or not ''.join(parts).strip():
        raise ValueError('Missing model output')
    return ''.join(parts)


def responses_input(messages):
    converted=[]
    for message in messages:
        if isinstance(message['content'],str):
            converted.append(message)
            continue
        parts=[]
        for part in message['content']:
            if part['type']=='text':
                parts.append({'type':'input_text','text':part['text']})
            elif part['type']=='image_url':
                parts.append({'type':'input_image','image_url':part['image_url']['url'],'detail':part['image_url']['detail']})
            else:
                raise ValueError('Unsupported Responses input')
        converted.append({'role':message['role'],'content':parts})
    return converted


def main():
    output = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    stage, usage_data = "initialize", None
    started = time.monotonic()
    request_started = None
    timing = {"network_ms": None, "worker_initialize_ms": None}
    trace_id = None
    raw_status, raw_body, raw_counts = None, None, None
    pricing_unconfigured = False
    wire_api = 'chat_completions'

    def progress():
        output.write(json.dumps({"worker_stage": stage, "worker_initialize_ms": timing["worker_initialize_ms"]}) + "\n")
        output.flush()

    def capture_raw(response):
        nonlocal raw_status, raw_body, raw_counts, trace_id, usage_data, pricing_unconfigured
        raw_status = response.status_code
        trace_id = provider_request_id(response, request['api_key'])
        response.read()
        try:
            body = response.json()
        except (ValueError, UnicodeError):
            raw_body = False
            return
        raw_body = isinstance(body, dict)
        provider_error = body.get('error') if raw_body else None
        if raw_status == 400 and isinstance(provider_error, dict) and provider_error.get('type') == 'invalid_request_error':
            message = provider_error.get('message')
            pricing_unconfigured = isinstance(message, str) and any(phrase in message for phrase in (
                'has not been priced by the administrator yet', '价格尚未由管理员配置'))
        raw_usage = body.get('usage') if raw_body else None
        if wire_api == 'responses' and isinstance(raw_usage, dict):
            raw_usage = {'prompt_tokens': raw_usage.get('input_tokens'), 'completion_tokens': raw_usage.get('output_tokens')}
        raw_counts = usage_counts(raw_usage) if raw_status == 200 else None
        usage_data = {"prompt_tokens": raw_counts[0], "completion_tokens": raw_counts[1]} if raw_counts else None

    progress()
    try:
        request = json.loads(sys.stdin.buffer.read(2 * 1024 * 1024))
        if request.get('wire_api') == 'responses' and (not request['model'].startswith('openai/')
                or '/' in request['model'].removeprefix('openai/')):
            raise ValueError('Unsupported native Responses provider')
        # Only safe errors and the response envelope cross the pipe, never dependency logs.
        with contextlib.redirect_stdout(sys.stderr):
            from litellm import completion

            stage = "request"
            timing["worker_initialize_ms"] = round((time.monotonic() - started) * 1000, 3)
            progress()
            request_started = time.monotonic()
            with contextlib.ExitStack() as resources:
                options = dict(request)
                wire_api = options.pop('wire_api', 'chat_completions')
                effort = options.pop('reasoning_effort', None)
                if wire_api == 'responses':
                    from litellm import responses
                    from litellm.llms.custom_httpx.http_handler import HTTPHandler
                    from openai import DefaultHttpxClient
                    http_client = resources.enter_context(DefaultHttpxClient(event_hooks={'response': [capture_raw]}))
                    base = (request['api_base'] or 'https://api.openai.com/v1').rstrip('/')
                    # A bare origin is a site root; versioned/custom prefixes remain explicit.
                    if not urlsplit(base).path:
                        base += '/v1'
                    response_options = dict(model=request['model'], api_base=base, api_key=request['api_key'],
                                            input=responses_input(request['messages']), timeout=request['timeout'], num_retries=0,
                                            max_output_tokens=request['max_tokens'], store=False, stream=False,
                                            text={'format': request['response_format']}, client=HTTPHandler(client=http_client))
                    if effort:
                        response_options['reasoning'] = {'effort': effort}
                    response = responses(**response_options)
                    timing['network_ms'] = round((time.monotonic() - request_started) * 1000, 3)
                    stage = 'validate'
                    content = responses_text(response.model_dump())
                elif request['model'].startswith('openai/'):
                    from openai import DefaultHttpxClient, OpenAI
                    http_client = resources.enter_context(DefaultHttpxClient(event_hooks={'response': [capture_raw]}))
                    options['client'] = resources.enter_context(OpenAI(api_key=request['api_key'], base_url=request['api_base'] or None,
                                                                    max_retries=0, http_client=http_client))
                    if effort:
                        options['reasoning_effort'] = effort
                    response = completion(**options)
                    content = response.choices[0].message.content or ""
                else:
                    if effort:
                        options['reasoning_effort'] = effort
                    response = completion(**options)
                    content = response.choices[0].message.content or ""
            timing["network_ms"] = round((time.monotonic() - request_started) * 1000, 3)
            trace_id = trace_id or provider_request_id(response, request["api_key"])
            # LiteLLM coerces missing/string/bool usage. Only original wire counts are actual.
            usage_data = {"prompt_tokens": raw_counts[0], "completion_tokens": raw_counts[1]} if raw_counts else None
            if len(content.encode("utf-8")) > request["max_tokens"] * 16:
                stage = "parse"
                raise ValueError("Response size limit")
            envelope = {"content": content, "usage": usage_data}
    except Exception as exc:
        if stage == "request" and request_started is not None:
            timing["network_ms"] = round((time.monotonic() - request_started) * 1000, 3)
            trace_id = trace_id or provider_request_id(getattr(exc, "response", None), request["api_key"])
        error = diagnostic(exc, stage, http_status=raw_status) if raw_status is not None else diagnostic(exc, stage)
        if pricing_unconfigured and error['code'] == 'provider_bad_request':
            error.update(code='provider_model_configuration_error', provider_error_reason='model_price_not_configured')
        if raw_status == 200 and raw_body is not None and stage == 'request' and error['code'] not in {'model_timeout', 'provider_connection_error'}:
            error.update(code='model_schema_error' if raw_body else 'model_invalid_json', stage='validate' if raw_body else 'parse', status_code=raw_status)
        envelope = {"error": error, "usage": usage_data}
    timing["worker_elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
    envelope.update(timing=timing, provider_request_id=trace_id)
    output.write(json.dumps(envelope, ensure_ascii=False))
    output.flush()
    # Callback threads must not keep a completed/failed request alive.
    os._exit(0)


if __name__ == "__main__":
    main()
