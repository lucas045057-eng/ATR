from __future__ import annotations

import json

from app.common import digest, dumps, now, uid
from app.connectors.mock import MockConnector
from app.connectors.models import RuntimeTargetCreate, ToolRequest
from app.connectors.policy import check
from app.connectors.ssh import SSHConnector
from app.tools.registry import sanitize_payload


class RuntimeService:
    def __init__(self, database, settings):
        self.db, self.settings = database, settings

    def create(self, business_id, request: RuntimeTargetCreate):
        if request.connector == "ssh" and (
            not request.access_authorized
            or not self.settings.ssh_enabled
            or not request.host_public_key
            or not request.credential_ref
            or request.permission_boundary != "forced_command_gateway"
            or len(request.boundary_attestation) < 10
        ):
            raise PermissionError("SSH 需显式启用、授权、凭据引用、已确认主机公钥与受限账号声明")
        target_id = uid()
        with self.db.transaction() as tx:
            tx.execute(
                "INSERT INTO runtime_targets VALUES(?,?,?,?)",
                (target_id, business_id, request.model_dump_json(), now()),
            )
        return {"id": target_id, **request.model_dump()}

    def get(self, business_id, target_id):
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM runtime_targets WHERE id=? AND business_id=?",
                (target_id, business_id),
            ).fetchone()
            if not row:
                raise KeyError("运行环境不属于当前业务")
            return {"id": target_id, **json.loads(row["config_json"])}
        finally:
            conn.close()

    def execute(self, business_id, target_id, request: ToolRequest):
        config = self.get(business_id, target_id)
        observation_id = uid()
        with self.db.transaction() as tx:
            count = tx.execute(
                "SELECT COUNT(*) FROM runtime_observations WHERE target_id=?", (target_id,)
            ).fetchone()[0]
            approved = {
                (r["kind"], r["runtime_id"])
                for r in tx.execute(
                    "SELECT kind,runtime_id FROM service_bindings WHERE target_id=? AND status='confirmed'",
                    (target_id,),
                )
            }
            tx.execute(
                "INSERT INTO runtime_observations VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    observation_id,
                    target_id,
                    request.tool_id,
                    dumps(sanitize_payload(request.arguments)),
                    "pending",
                    "{}",
                    digest({}),
                    now(),
                    0,
                    None,
                ),
            )
        try:
            if count >= config["max_calls"]:
                raise PermissionError("运行环境工具调用总数已达硬上限")
            args = check(config, request.tool_id, request.arguments, approved)
            connector = (
                MockConnector(config)
                if config["connector"] == "mock"
                else SSHConnector(config, self.settings)
            )
            payload = sanitize_payload(connector.read(request.tool_id, args))
            serialized = dumps(payload)
            if len(serialized.encode()) > config["max_output_bytes"]:
                raise ValueError("读取输出超过大小限制")
            stamp = now()  # Local acquisition time, remote clock is untrusted.
            with self.db.transaction() as tx:
                tx.execute(
                    "UPDATE runtime_observations SET status='succeeded',payload_json=?,content_digest=?,observed_at=?,bytes=? WHERE id=?",
                    (serialized, digest(payload), stamp, len(serialized.encode()), observation_id),
                )
            return {
                "observation_id": observation_id,
                "tool_id": request.tool_id,
                "payload": payload,
                "observed_at": stamp,
                "bytes": len(serialized.encode()),
                "source_type": "Runtime",
                "connector": config["connector"],
            }
        except Exception as exc:
            status = (
                "denied"
                if isinstance(exc, PermissionError)
                else "timed_out"
                if isinstance(exc, TimeoutError)
                else "failed"
            )
            with self.db.transaction() as tx:
                tx.execute(
                    "UPDATE runtime_observations SET status=?,error=? WHERE id=?",
                    (status, type(exc).__name__, observation_id),
                )
            return {
                "observation_id": observation_id,
                "tool_id": request.tool_id,
                "status": status,
                "error": type(exc).__name__,
                "bytes": 0,
            }
