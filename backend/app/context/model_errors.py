"""Safe model diagnostics without provider bodies, credentials or exception text."""

from pydantic import ValidationError

# Schema vocabulary only: provider-controlled extra keys must never enter diagnostics.
SAFE_FIELDS = frozenset("summary hypotheses next_action action scope_requests target_node_id target_role failure_mode causal_chain prediction rationale evidence_links missing_evidence confidence status verification_predictions evidence_id relation explanation tool_id field operator value service reason proposals role candidate_key kind evidence_ids reasons counterevidence uncertainty next_tools action_id next_action_id next_investigation finish finish_reason missing_information reasoning".split())
SAFE_ERROR_TYPES = frozenset("missing extra_forbidden string_type int_type float_type bool_type dict_type list_type literal_error greater_than_equal less_than_equal too_short too_long finite_number value_error unknown_target target_role_mismatch unknown_evidence unknown_prediction_target prediction_tool_not_allowed invalid_prediction action_not_allowed counterevidence_target_mismatch finish_action_conflict".split())


class ModelDecisionError(ValueError):
    def __init__(self, path, error_type):
        self.path, self.error_type = path, error_type
        super().__init__("Invalid model decision")


def safe_validation_errors(errors, prefix=()):
    result = []
    for error in errors[:24]:
        path = ""
        location = tuple(prefix) + tuple(error.get("loc", ()))
        # Pydantic's scalar union branch is not a field supplied by the model.
        if (len(location) >= 6 and location[-4] == "verification_predictions"
                and type(location[-3]) is int and location[-2] == "value"
                and (location[-1], error.get("type")) in {("str", "string_type"), ("int", "int_type"), ("bool", "bool_type")}):
            location = location[:-1]
        for part in location[:16]:
            if type(part) is int and 0 <= part <= 100000:
                path += f"[{part}]"
            else:
                field = part if isinstance(part, str) and part in SAFE_FIELDS else "<extra_field>"
                path += ("." if path else "") + field
        kind = error.get("type")
        result.append({"path": path or "$", "type": kind if kind in SAFE_ERROR_TYPES else "validation_error", "stage": "validate"})
    return result

MESSAGES = {
    "model_import_error": "模型客户端初始化失败",
    "model_timeout": "模型调用超时，已停止本次请求",
    "provider_connection_error": "模型供应商连接失败或服务异常",
    "provider_unavailable": "模型服务暂时不可用",
    "provider_auth_error": "模型供应商认证失败，请检查凭据",
    "provider_rate_limit": "模型供应商限流，请稍后重试",
    "provider_bad_request": "模型供应商拒绝请求，请检查模型与结构化输出兼容性",
    "provider_model_configuration_error": "模型供应商报告模型价格未配置，请联系供应商管理员",
    "model_invalid_json": "模型响应不符合 JSON 结构约定",
    "model_schema_error": "模型输出结构或引用无效，未执行后续工具",
    "model_budget_exceeded": "模型预算已耗尽，本次未发起供应商请求",
    "model_internal_error": "模型客户端内部错误",
}


class ModelCallError(ValueError):
    def __init__(self, diagnostic):
        self.code = diagnostic.get("code", "model_internal_error")
        if self.code not in MESSAGES:
            self.code = "model_internal_error"
        self.diagnostic = {"code": self.code, "message": MESSAGES[self.code],
                           "exception_type": str(diagnostic.get("exception_type", "UnknownError"))[:80],
                           "stage": str(diagnostic.get("stage", "request"))[:32],
                           "status_code": diagnostic.get("status_code"),
                           "frames": diagnostic.get("frames", [])[:12]}
        self.diagnostic.update({key: diagnostic[key] for key in (
            "request_attempt_id", "investigation_run_id", "model", "provider", "request_stage",
            "started_at", "ended_at", "elapsed_ms", "worker_initialize_ms", "process_overhead_ms",
            "network_ms", "parse_ms", "error_category", "http_status", "usage_kind", "usage_status",
            "retry_count", "provider_request_id", "validation_errors", "validation_kind",
            "budget_reason", "estimated_input_tokens", "input_limit", "candidate_count", "candidate_limit") if key in diagnostic})
        if self.code == "provider_model_configuration_error" and diagnostic.get("provider_error_reason") == "model_price_not_configured":
            self.diagnostic["provider_error_reason"] = "model_price_not_configured"
        super().__init__(f"{self.code}: {MESSAGES[self.code]} ({self.diagnostic['exception_type']})")


def diagnostic(exc, stage="request", http_status=..., field_prefix=()):
    import traceback

    name = type(exc).__name__
    status = getattr(exc, "status_code", None) if http_status is ... else http_status
    status = status if isinstance(status, int) else None
    types = {cls.__name__ for cls in type(exc).__mro__}
    if stage == "initialize" or isinstance(exc, ImportError):
        code = "model_import_error"
    elif status in {401, 403}:
        code = "provider_auth_error"
    elif status == 429:
        code = "provider_rate_limit"
    elif http_status is ... and types & {"Timeout", "TimeoutError", "TimeoutExpired", "APITimeoutError", "TimeoutException", "ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout"}:
        # LiteLLM uses synthetic status=500 for transport failures without an HTTP response.
        code = "model_timeout"
        status = None
    elif http_status is ... and types & {"APIConnectionError", "ConnectionError", "ConnectError", "NetworkError", "SSLError", "gaierror"}:
        code = "provider_connection_error"
        status = None
    elif status and status >= 500:
        code = "provider_unavailable"
    elif types & {"Timeout", "TimeoutError", "TimeoutExpired", "APITimeoutError", "TimeoutException", "ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout"}:
        code = "model_timeout"
    elif types & {"APIConnectionError", "ConnectionError", "ConnectError", "NetworkError", "SSLError", "gaierror"}:
        code = "provider_connection_error"
    elif status and 400 <= status < 500:
        code = "provider_bad_request"
    elif stage == "validate":
        code = "model_schema_error"
    elif stage == "parse":
        code = "model_invalid_json"
    else:
        code = "model_internal_error"
    frames = [{"file": frame.filename.replace("\\", "/").rsplit("/", 1)[-1],
               "line": frame.lineno, "function": frame.name}
              for frame in traceback.extract_tb(exc.__traceback__)[-12:]]
    result = {"code": code, "exception_type": name, "stage": stage,
              "status_code": status, "frames": frames}
    if stage == "validate" and isinstance(exc, ValidationError):
        result.update(validation_kind="format", validation_errors=safe_validation_errors(
            exc.errors(include_url=False, include_context=False, include_input=False), field_prefix))
    elif stage == "validate" and isinstance(exc, ModelDecisionError):
        result.update(validation_kind="decision", validation_errors=safe_validation_errors(
            [{"loc": exc.path, "type": exc.error_type}]))
    return result


def usage_counts(usage):
    """Only explicit nonnegative integer counts qualify as provider actual usage."""
    values = [usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
              for key in ("prompt_tokens", "completion_tokens")]
    return tuple(values) if all(type(value) is int and value >= 0 for value in values) else None
