from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from app.api.dependencies import require_operator
from app.business.ingestion import safe_name
from app.business.models import (
    BusinessCreate,
    BusinessReview,
    CodeContext,
    CodeSearch,
    ManualMediaReview,
    MediaBatchReview,
    MediaContinuation,
)
from app.business.service import BusinessService
from app.business.source_index import SourceIndex
from app.common import digest, now, uid
from app.connectors.credentials import SSH_SESSIONS
from app.connectors.models import (
    BindingReview,
    DockerReaderSelection,
    HealthEndpointSelection,
    ProcessLogSelection,
    RuntimeTargetCreate,
    SSHPassword,
    ToolRequest,
)
from app.connectors.service import RuntimeService
from app.context.budget import BudgetLimits, ContextBudgetManager
from app.context.model import capabilities
from app.jobs import finish, reserve, submit
from app.settings import get_settings
from app.storage.database import Database
from app.system.scope import ScopeResolver
from app.tools.redaction import redact_text


class BusinessRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                return await original(request)
            except RequestValidationError as exc:
                # Never echo submitted private credentials or file contents in validation errors.
                if request.url.path.endswith("/credential"):
                    raise HTTPException(422, detail="SSH 密码请求格式无效；请提交非空且有界的 password 字段") from exc
                detail = "; ".join(
                    f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()
                )
                raise HTTPException(422, detail=detail[:1000]) from exc
            except KeyError as exc:
                raise HTTPException(404, detail=redact_text(str(exc))) from exc
            except PermissionError as exc:
                raise HTTPException(403, detail=redact_text(str(exc))) from exc
            except sqlite3.IntegrityError as exc:
                raise HTTPException(
                    409, detail="同一业务已有后台任务或记录冲突，请等待完成后重试"
                ) from exc
            except ValueError as exc:
                raise HTTPException(422, detail=redact_text(str(exc))[:500]) from exc

        return handler


router = APIRouter(
    prefix="/api/v1", dependencies=[Depends(require_operator)], route_class=BusinessRoute
)


def service():
    settings = get_settings()
    return BusinessService(Database(settings.database_path), settings)


def ensure_idle(business_id):
    s = service()
    conn = s.db.connect()
    try:
        if conn.execute(
            "SELECT 1 FROM jobs WHERE business_id=? AND status IN ('queued','running')",
            (business_id,),
        ).fetchone():
            raise HTTPException(409, "业务正在处理，请等待后台任务完成")
    finally:
        conn.close()


@router.get("/businesses")
def list_businesses():
    return service().list()


@router.post("/businesses", status_code=201)
def create_business(request: BusinessCreate):
    return service().create(request)


@router.get("/businesses/{business_id}")
def get_business(business_id: str, version: int | None = None):
    return service().get(business_id, version)


@router.get("/businesses/{business_id}/versions")
def versions(business_id: str):
    return service().versions(business_id)


@router.get("/businesses/{business_id}/artifacts")
def list_artifacts(business_id: str):
    s = service()
    s.get(business_id)
    conn = s.db.connect()
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM uploaded_artifacts WHERE business_id=? ORDER BY created_at DESC",
                (business_id,),
            )
        ]
    finally:
        conn.close()


@router.post("/businesses/{business_id}/artifacts", status_code=202)
async def upload(
    business_id: str,
    request: Request,
    filename: str,
    mode: Literal["append", "replace"] = "append",
    slot: str = "source",
):
    s = service()
    s.get(business_id)
    filename = safe_name(filename)
    if "/" in filename or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", slot):
        raise ValueError("上传文件名或资料集名称不合法")
    if int(request.headers.get("content-length", "0")) > s.settings.upload_max_bytes:
        raise HTTPException(413, "单次上传大小超限")
    job_id = reserve(s.db, "ingest", business_id)
    artifact_id = uid()
    folder = s.root / business_id / artifact_id
    folder.mkdir(parents=True, exist_ok=False)
    sha, size = hashlib.sha256(), 0
    try:
        # The browser streams File directly. No multipart parser buffers the entire upload.
        with (folder / "upload").open("xb") as stream:
            async for chunk in request.stream():
                size += len(chunk)
                if size > s.settings.upload_max_bytes:
                    raise HTTPException(413, "单次上传大小超限")
                stream.write(chunk)
                sha.update(chunk)
        if not size:
            raise ValueError("资料文件为空")
        with s.db.transaction() as tx:
            tx.execute(
                "INSERT INTO uploaded_artifacts VALUES(?,?,?,?,?,?,?,?,NULL,?)",
                (
                    artifact_id,
                    business_id,
                    filename,
                    sha.hexdigest(),
                    size,
                    "queued",
                    mode,
                    slot,
                    now(),
                ),
            )
        submit(s.db, job_id, lambda: s.process_artifact(artifact_id))
        return {"job_id": job_id, "artifact_id": artifact_id, "status": "queued", "bytes": size}
    except BaseException as exc:
        finish(s.db, job_id, error=type(exc).__name__)
        raise


@router.delete("/businesses/{business_id}/artifacts/{artifact_id}")
def remove_artifact(business_id: str, artifact_id: str):
    ensure_idle(business_id)
    return service().remove(business_id, artifact_id)


@router.post("/businesses/{business_id}/analyze", status_code=202)
def analyze(business_id: str):
    s = service()
    s.get(business_id)
    job_id = reserve(s.db, "analyze", business_id)
    submit(s.db, job_id, lambda: s.analyze(business_id))
    return {"job_id": job_id, "status": "queued"}


@router.post('/businesses/{business_id}/files/{file_id}/media-continue',status_code=202)
def continue_media(business_id: str,file_id: str,request: MediaContinuation):
    ensure_idle(business_id)
    s=service()
    if s.get(business_id)['profile_version']!=request.expected_version:
        raise ValueError('媒体来源版本已变化，请重新加载')
    job_id=reserve(s.db,'media',business_id)
    submit(s.db,job_id,lambda:s.continue_media(business_id,file_id,request.expected_version))
    return {'job_id':job_id,'status':'queued'}


@router.post("/businesses/{business_id}/review")
def review(business_id: str, request: BusinessReview, actor: str = Depends(require_operator)):
    ensure_idle(business_id)
    return service().review(business_id, request, actor)


@router.post("/businesses/{business_id}/code/search")
def search_code(business_id: str, request: CodeSearch):
    s = service()
    return SourceIndex(s.db, s.settings).search(business_id, request)


@router.post("/businesses/{business_id}/code/context")
def code_context(business_id: str, request: CodeContext):
    s = service()
    return SourceIndex(s.db, s.settings).context(business_id, request)


@router.post("/businesses/{business_id}/files/{file_id}/media-review")
def media_review(business_id: str, file_id: str, request: ManualMediaReview):
    ensure_idle(business_id)
    s = service()
    profile = s.get(business_id)
    if request.expected_version != profile["profile_version"]:
        raise ValueError("资料审核版本过期")
    with s.db.transaction() as tx:
        row = tx.execute(
            "SELECT * FROM artifact_files WHERE id=? AND business_id=? AND active=1",
            (file_id, business_id),
        ).fetchone()
        if not row or row["media_type"] not in {"image", "pdf"}:
            raise KeyError("没有可人工录入的图片/PDF")
        parsed = json.loads(row["parse_json"])
        parsed.update(manual_text=redact_text(request.text), parse_state="complete")
    return s.rebuild(business_id, {file_id: parsed}, request.expected_version)


@router.post("/businesses/{business_id}/files/media-batch-review")
def media_batch_review(business_id: str, request: MediaBatchReview, actor: str = Depends(require_operator)):
    from app.business.media import optional_image
    ensure_idle(business_id)
    s = service()
    profile = s.get(business_id)
    if request.expected_version != profile["profile_version"]:
        raise ValueError("资料审核版本过期")
    updates = {}
    with s.db.connect() as c:
        for file_id in set(request.file_ids):
            row = c.execute("SELECT * FROM artifact_files WHERE id=? AND business_id=? AND active=1", (file_id, business_id)).fetchone()
            if not row or row["media_type"] != "image":
                raise ValueError("批量分类仅接受当前业务的图片附件")
            parsed = json.loads(row["parse_json"])
            if request.action == "optional":
                if not optional_image(row["path"]):
                    raise ValueError("架构图和用途不明图片需单独审核，不能批量设为可选附件")
                if parsed.get("parse_state") == "optional_static_attachment":
                    continue
                parsed["optional_previous"] = {k: v for k, v in parsed.items() if k not in {"optional_history", "optional_previous"}}
                parsed.update(parse_state="optional_static_attachment", media_role="optional_static")
            else:
                if parsed.get("parse_state") != "optional_static_attachment" or not isinstance(parsed.get("optional_previous"), dict):
                    raise ValueError("此文件没有可撤销的静态附件分类")
                history = parsed.get("optional_history", [])
                parsed = {**parsed["optional_previous"], "optional_history": history}
            parsed["optional_history"] = [*parsed.get("optional_history", [])[-15:],
                {"action": request.action, "actor": actor, "reason": redact_text(request.reason), "at": now(), "profile_version": request.expected_version}]
            updates[file_id] = parsed
    if not updates:
        return profile
    record = (uid(), business_id, request.expected_version + 1, "media_batch", digest(sorted(updates)), request.action,
              redact_text(request.reason), actor, now())
    return s.rebuild(business_id, updates, request.expected_version, record)


@router.get("/businesses/{business_id}/budgets")
def budgets(business_id: str):
    s = service()
    s.get(business_id)
    conn = s.db.connect()
    try:
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM context_budgets WHERE business_id=? ORDER BY created_at DESC",
                (business_id,),
            )
        ]
    finally:
        conn.close()
    return [ContextBudgetManager(s.db, s.settings).get(i) for i in ids]


@router.put("/businesses/{business_id}/budgets/{budget_id}")
def update_budget(business_id: str, budget_id: str, request: BudgetLimits):
    ensure_idle(business_id)
    s = service()
    manager = ContextBudgetManager(s.db, s.settings)
    if manager.get(budget_id)["business_id"] != business_id:
        raise KeyError("预算不属于当前业务")
    manager.update(budget_id, request)
    return manager.get(budget_id)


@router.get("/model/capabilities")
def model_capabilities():
    return capabilities(get_settings())


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    conn = service().db.connect()
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError("后台任务不存在")
        item = dict(row)
        item["result"] = json.loads(item.pop("result_json"))
        return item
    finally:
        conn.close()


@router.get("/businesses/{business_id}/targets")
def targets(business_id: str):
    s = service()
    s.get(business_id)
    conn = s.db.connect()
    try:
        runtime = RuntimeService(s.db, s.settings)
        result = []
        for row in conn.execute("SELECT * FROM runtime_targets WHERE business_id=? ORDER BY created_at,id", (business_id,)):
            target = runtime.public({"id": row["id"], **json.loads(row["config_json"])})
            target["investigation_readiness"] = runtime.investigation_readiness(target)
            result.append(target)
        return result
    finally:
        conn.close()


@router.post("/businesses/{business_id}/targets", status_code=201)
def create_target(business_id: str, request: RuntimeTargetCreate):
    s = service()
    s.get(business_id)
    return RuntimeService(s.db, s.settings).create(business_id, request)


@router.patch("/businesses/{business_id}/targets/{target_id}/docker-reader")
def select_docker_reader(business_id: str, target_id: str, request: DockerReaderSelection):
    ensure_idle(business_id)
    s = service()
    return RuntimeService(s.db, s.settings).select_docker_reader(business_id, target_id, request.docker_reader)


@router.post("/businesses/{business_id}/targets/{target_id}/credential")
def provide_ssh_password(business_id: str, target_id: str, request: SSHPassword):
    s = service()
    return RuntimeService(s.db, s.settings).provide_password(business_id, target_id, request.password)


@router.patch('/businesses/{business_id}/targets/{target_id}/health-endpoints')
def select_health_endpoints(business_id: str, target_id: str, request: HealthEndpointSelection):
    ensure_idle(business_id)
    s = service()
    return RuntimeService(s.db, s.settings).select_health_endpoints(business_id, target_id, request)


@router.delete("/businesses/{business_id}/targets/{target_id}/credential")
def clear_ssh_password(business_id: str, target_id: str):
    s = service()
    runtime = RuntimeService(s.db, s.settings)
    config = runtime.get(business_id, target_id)
    SSH_SESSIONS.clear_target(config, s.settings.operator_id)
    return runtime.public(config)


@router.patch('/businesses/{business_id}/targets/{target_id}/log-files')
def select_log_files(business_id: str, target_id: str, request: ProcessLogSelection):
    ensure_idle(business_id)
    s=service()
    return RuntimeService(s.db,s.settings).select_log_files(business_id,target_id,request)


@router.post("/businesses/{business_id}/targets/{target_id}/discover", status_code=202)
def discover(business_id: str, target_id: str):
    s = service()
    RuntimeService(s.db, s.settings).require_credentials(business_id, target_id)
    job_id = reserve(s.db, "discover", business_id)
    submit(s.db, job_id, lambda: ScopeResolver(s.db, s.settings).discover(business_id, target_id))
    return {"job_id": job_id, "status": "queued"}


@router.get("/businesses/{business_id}/discoveries")
def discoveries(business_id: str):
    s = service()
    s.get(business_id)
    conn = s.db.connect()
    try:
        return [
            {
                "id": r["id"],
                "status": r["status"],
                "target_id": r["target_id"],
                "created_at": r["created_at"],
                **json.loads(r["summary_json"]),
            }
            for r in conn.execute(
                "SELECT * FROM discovery_runs WHERE business_id=? ORDER BY created_at DESC LIMIT 20",
                (business_id,),
            )
        ]
    finally:
        conn.close()


@router.get("/businesses/{business_id}/bindings")
def bindings(business_id: str):
    from app.system.scope import binding_is_current
    s = service()
    s.get(business_id)
    conn = s.db.connect()
    try:
        return [
            {
                **dict(r),
                "reasons": json.loads(r["reasons_json"]),
                "evidence_ids": json.loads(r["evidence_ids_json"]),
                "source": json.loads(r["source_json"]),
                "runtime_current": binding_is_current(r),
            }
            for r in conn.execute(
                "SELECT * FROM service_bindings WHERE business_id=? ORDER BY score DESC",
                (business_id,),
            )
        ]
    finally:
        conn.close()


@router.post("/businesses/{business_id}/bindings/{binding_id}/review")
def review_binding(
    business_id: str,
    binding_id: str,
    request: BindingReview,
    actor: str = Depends(require_operator),
):
    ensure_idle(business_id)
    s = service()
    return ScopeResolver(s.db, s.settings).confirm(
        business_id, binding_id, request.decision, redact_text(request.reason), actor
    )


@router.post("/businesses/{business_id}/targets/{target_id}/tools", status_code=202)
def runtime_tool(business_id: str, target_id: str, request: ToolRequest):
    s = service()
    RuntimeService(s.db, s.settings).require_credentials(business_id, target_id)
    job_id = reserve(s.db, "runtime_read", business_id)

    def execute():
        observation = RuntimeService(s.db, s.settings).execute(business_id, target_id, request)
        ScopeResolver(s.db, s.settings).reconcile(business_id, observation)
        return observation

    submit(s.db, job_id, execute)
    return {"job_id": job_id, "status": "queued"}
