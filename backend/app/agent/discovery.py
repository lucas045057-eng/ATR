"""Basic metadata can suggest scope; only the operator can authorize deep reads."""
import json

from pydantic import Field

from app.agent.model_schema import StrictModel
from app.common import digest
from app.context.budget import BudgetExceeded, ContextBudgetManager, estimate_model_input
from app.context.model import ModelUnavailable, call_json, model_messages
from app.context.model_errors import ModelCallError, ModelDecisionError
from app.system.scope import INVENTORY_FIELDS, ScopeResolver, discovery_requests, valid_candidate


class MappingProposal(StrictModel):
    service: str = Field(min_length=1, max_length=255)
    role: str = Field(min_length=1, max_length=80)
    candidate_key: str = Field(min_length=1, max_length=400)
    kind: str = Field(pattern="^(service|container|process)$")
    evidence_ids: list[str] = Field(min_length=1, max_length=8)
    reasons: list[str] = Field(min_length=1, max_length=6)
    counterevidence: list[str] = Field(max_length=6)
    uncertainty: str = Field(min_length=1, max_length=1000)
    next_tools: list[str] = Field(max_length=6)


class MappingReport(StrictModel):
    summary: str = Field(min_length=1, max_length=2000)
    proposals: list[MappingProposal] = Field(max_length=12)


def authorization_digest(target):
    target = {"auth_mode": "private_key", "execution_mode": "forced_gateway", "docker_reader": "direct_cli", **target}
    excluded = {"credential_status", "docker_capabilities"}
    if target.get("auth_mode") == "password":
        excluded.add("credential_ref")
    return digest({k: v for k, v in target.items() if k not in excluded})


def validate_mapping(data, context):
    report = MappingReport.model_validate(data)
    services = {s["name"]: s["role"] for s in context["services"]}
    candidates = {c["candidate_key"]: c for c in context["candidates"]}
    seen = set()
    for i, proposal in enumerate(report.proposals):
        path = ("proposals", i)
        candidate = candidates.get(proposal.candidate_key)
        if not candidate or candidate["kind"] != proposal.kind:
            raise ModelDecisionError(path + ("candidate_key",), "unknown_target")
        if services.get(proposal.service) != proposal.role:
            raise ModelDecisionError(path + ("service",), "unknown_target")
        if not set(proposal.evidence_ids) <= set(candidate["evidence_ids"]):
            raise ModelDecisionError(path + ("evidence_ids",), "unknown_evidence")
        allowed_tools = {proposal.kind + ".list"}
        if proposal.kind != "process":
            allowed_tools |= {proposal.kind + ".status", proposal.kind + ".logs"}
        if not set(proposal.next_tools) <= allowed_tools:
            raise ModelDecisionError(path + ("next_tools",), "unknown_action")
        # A PID/name match is insufficient for a business identity, even if the model says otherwise.
        if proposal.kind == "process" and not candidate.get("business_identity_evidence"):
            raise ModelDecisionError(path + ("kind",), "unknown_target")
        key = (proposal.service, proposal.candidate_key)
        if key in seen:
            raise ModelDecisionError(path + ("candidate_key",), "unknown_target")
        seen.add(key)
    return report.model_dump()


def discover_for_run(database, settings, profile, targets, registry, service_node_id, budget_id):
    manager = ContextBudgetManager(database, settings)
    resolver = ScopeResolver(database, settings)
    observations, candidates, discoveries, errors = [], [], [], []
    terminal = None
    authorization = {}
    for target_id, stored in targets.items():
        target = {"id": target_id, **stored}
        authorization[target_id] = authorization_digest(target)
        collected = []
        for request in discovery_requests(profile, target):
            r = registry.execute({"tool_id": request.tool_id, "arguments": request.arguments, "target_id": target_id,
                                  "target_node_id": service_node_id, "service": "基础发现", "role": "discovery"})
            collected.append(r)
            observations.append(r)
            if r.get("error_code"):
                errors.append(r.get("diagnostic") or {"tool_id": request.tool_id, "error_code": r["error_code"]})
            from app.connectors.diagnostics import TERMINAL_CODES
            if r.get("error_code") in TERMINAL_CODES:
                terminal = r["error_code"]
                break
            if manager.get(budget_id)["stop_reason"]:
                terminal = "global_budget_exhausted"
                break
        discovery = resolver.discover(profile["business_id"], target_id, observations=collected)
        discoveries.append(discovery["id"])
        for o in collected:
            field = INVENTORY_FIELDS.get(o["tool_id"])
            for item in o.get("payload", {}).get(field, []) if field else []:
                if not valid_candidate(item, o["tool_id"]) or not o.get("evidence_id"):
                    continue
                candidates.append({**item, "target_id": target_id, "observation_id": o["observation_id"],
                                   "observed_at": o["observed_at"], "evidence_ids": [o["evidence_id"]],
                                   "candidate_key": f"{target_id}:{item['kind']}:{item['id']}", "discovery_id": discovery["id"]})
        if terminal:
            break
    candidates.sort(key=lambda c: ({"service": 0, "container": 1, "process": 2}[c["kind"]], c["id"]))
    context = {"phase": "discovery", "profile_version": profile["profile_version"],
               "services": [{"name": s["name"], "role": s["role"]} for s in profile["services"][:20]],
               "candidates": candidates[:80], "collection_errors": errors,
               "boundary": "候选元数据仅用于范围建议；未确认对象禁止读取日志、业务配置或作根因结论"}
    prompt = "根据脱敏基础证据提出候选映射。名称相似只是线索；说明反证和不确定性。不得确认权限、执行工具或诊断根因。只返回契约中的 JSON：" + json.dumps(MappingReport.model_json_schema(), ensure_ascii=False)
    limit = manager.get(budget_id)["limits"]["max_input_tokens"]
    while context["candidates"] and estimate_model_input(model_messages(prompt, context)) > limit:
        context["candidates"].pop()
    model_status, model_error, proposals = "not_called", None, []
    summary = "已采集基础证据，需要人工审核运行范围"
    if not terminal and context["candidates"]:
        try:
            report = call_json(database, settings, budget_id, prompt, context, validate=lambda data: validate_mapping(data, context))
            model_status, summary = "completed", report["summary"]
            lookup = {c["candidate_key"]: c for c in context["candidates"]}
            for proposal in report["proposals"]:
                candidate = lookup[proposal["candidate_key"]]
                proposals.append(resolver.propose(profile["business_id"], proposal, candidate, candidate["discovery_id"], profile["profile_version"]))
        except ModelUnavailable:
            model_status, summary = "offline", "基础发现已完成；模型未配置，仍需人工审核候选范围"
        except ModelCallError as exc:
            model_status, model_error, terminal = "failed", exc.diagnostic, exc.code
        except BudgetExceeded as exc:
            model_status = "failed"
            model_error = ModelCallError({"code": "model_budget_exceeded", "exception_type": "BudgetExceeded",
                                          "stage": "budget", **exc.diagnostic}).diagnostic
            terminal = "global_budget_exhausted"
    gate = {"status": "unresolved", "hypotheses": [], "rationale": "候选清单尚未经过人工范围确认，不构成根因证据"}
    return {"stage": "stopped" if terminal else "awaiting_review", "model_status": model_status,
            "model_error": model_error, "business_id": profile["business_id"], "profile_version": profile["profile_version"],
            "budget_id": budget_id, "summary": summary, "unresolved_reason": gate["rationale"], "gate": gate,
            "stop_reason": terminal or "awaiting_scope_review", "discovery_ids": discoveries, "mapping_proposals": proposals,
            "authorization_digests": authorization, "discovery_evidence_ids": [o["evidence_id"] for o in observations if o.get("evidence_id")],
            "evidence_ids": [], "iterations": [], "hypotheses": [], "scope_requests": [], "tool_errors": errors,
            "connector_types": sorted({t["connector"] for t in targets.values()}),
            "collection_status": "failed" if not candidates else "partial" if errors or any(o.get("payload", {}).get("partial") for o in observations) else "completed"}
