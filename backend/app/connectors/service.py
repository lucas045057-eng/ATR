from __future__ import annotations

import json

from app.common import digest, dumps, now, uid
from app.connectors.credentials import SSH_SESSIONS
from app.connectors.diagnostics import ToolFailure, failure
from app.connectors.mock import MockConnector
from app.connectors.models import RuntimeTargetCreate, ToolRequest
from app.connectors.policy import check
from app.connectors.ssh import SSHConnector, SSHOutputLimit
from app.connectors.standard_ssh import StandardSSHConnector
from app.tools.registry import sanitize_payload


class RuntimeService:
    def __init__(self, database, settings):
        self.db, self.settings = database, settings

    def create(self, business_id, request: RuntimeTargetCreate):
        if request.docker_reader == "restricted_list" and (request.connector != "ssh" or request.execution_mode != "standard_ssh_readonly"):
            raise PermissionError("受限 Docker reader 需使用普通 SSH 固定命令连接器")
        if request.connector == "ssh" and (
            not request.access_authorized
            or not self.settings.ssh_enabled
            or not request.host_public_key
            or (request.auth_mode == "private_key" and not request.credential_ref)
            or request.permission_boundary != (
                "client_readonly" if request.execution_mode == "standard_ssh_readonly" else "forced_command_gateway")
            or len(request.boundary_attestation) < 10
        ):
            raise PermissionError("SSH 需显式启用、授权、凭据引用、已确认主机公钥与受限账号声明")
        if request.auth_mode == "password" and request.credential_ref:
            raise PermissionError("密码凭据只能通过当前服务器的会话接口提供")
        target_id = uid()
        with self.db.transaction() as tx:
            tx.execute(
                "INSERT INTO runtime_targets VALUES(?,?,?,?)",
                (target_id, business_id, request.model_dump_json(), now()),
            )
        return self.public({"id": target_id, **request.model_dump()})

    def public(self, config):
        config.setdefault("auth_mode", "private_key")
        config.setdefault("execution_mode", "forced_gateway")
        config.setdefault("docker_reader", "direct_cli")
        config["docker_capabilities"] = (["container.list"] if config["docker_reader"] == "restricted_list"
                                         else ["container.list", "container.status", "container.logs"]) if config["docker_reads_enabled"] else []
        if config["connector"] == "ssh" and config["auth_mode"] == "password":
            config["credential_status"] = SSH_SESSIONS.status(config, self.settings.operator_id)
        return config

    def investigation_readiness(self, config):
        if config.get("connector") == "mock":
            return {"eligible": True, "status": "mock", "reason": None}
        if config.get("connector") != "ssh":
            return {"eligible": False, "status": "unsupported", "reason": "连接器不支持故障调查"}
        if not self.settings.ssh_enabled:
            return {"eligible": False, "status": "disabled", "reason": "SSH 连接功能未启用"}
        if not config.get("access_authorized"):
            return {"eligible": False, "status": "revoked", "reason": "SSH 目标授权未确认或已撤销"}
        if not config.get("host_public_key"):
            return {"eligible": False, "status": "invalid", "reason": "缺少已确认的 SSH 主机公钥"}
        if config.get("username", "").casefold() == "root":
            return {"eligible": False, "status": "invalid", "reason": "禁止使用 root 账号执行调查"}
        expected = "client_readonly" if config.get("execution_mode") == "standard_ssh_readonly" else "forced_command_gateway"
        if config.get("permission_boundary") != expected or len(config.get("boundary_attestation", "")) < 10:
            return {"eligible": False, "status": "invalid", "reason": "SSH 只读权限边界声明无效"}
        credential_state = config.get("credential_status")
        if isinstance(credential_state, dict):
            credential_state = credential_state.get("state")
        if config.get("auth_mode") == "password" and credential_state != "ready":
            return {"eligible": False, "status": "credential_missing", "reason": "密码会话凭据缺失或已过期"}
        if config.get("auth_mode") == "private_key" and not config.get("credential_ref"):
            return {"eligible": False, "status": "credential_missing", "reason": "未配置 SSH 私钥凭据引用"}
        return {"eligible": True, "status": "authorized", "reason": None}

    def provide_password(self, business_id, target_id, password):
        config = self.get(business_id, target_id)
        if (not self.settings.ssh_enabled or config["connector"] != "ssh"
                or config.get("auth_mode") != "password" or config["username"].casefold() == "root"
                or not config["access_authorized"]
                or config["permission_boundary"] != (
                    "client_readonly" if config.get("execution_mode") == "standard_ssh_readonly" else "forced_command_gateway")
                or len(config["boundary_attestation"]) < 10):
            raise PermissionError("此环境未授权使用 SSH 密码连接")
        try:
            with self.db.transaction() as tx:
                ref = SSH_SESSIONS.put(config, self.settings.operator_id, password)
                config["credential_ref"] = ref
                stored = {k: v for k, v in config.items() if k not in {"id", "credential_status", "docker_capabilities"}}
                tx.execute("UPDATE runtime_targets SET config_json=? WHERE id=? AND business_id=?",
                           (dumps(stored), target_id, business_id))
        except BaseException:
            SSH_SESSIONS.revoke(config, self.settings.operator_id)
            raise
        return self.public(config)

    def select_docker_reader(self, business_id, target_id, reader):
        config = self.get(business_id, target_id)
        if config["connector"] != "ssh" or config["execution_mode"] != "standard_ssh_readonly":
            raise PermissionError("Docker reader 策略仅用于普通 SSH")
        if config["docker_reader"] != reader:
            config["docker_reader"] = reader
            stored = {k: v for k, v in config.items() if k not in {"id", "credential_status", "docker_capabilities"}}
            with self.db.transaction() as tx:
                tx.execute("UPDATE runtime_targets SET config_json=? WHERE id=? AND business_id=?", (dumps(stored), target_id, business_id))
                tx.execute("UPDATE service_bindings SET freshness_status='stale' WHERE target_id=? AND kind='container'", (target_id,))
        return self.public(config)

    def select_health_endpoints(self, business_id, target_id, request):
        config = self.get(business_id, target_id)
        endpoints = RuntimeTargetCreate(name='health-location', host=config['host'],
                                        health_endpoints=request.health_endpoints).model_dump()['health_endpoints']
        with self.db.transaction() as tx:
            row = tx.execute('SELECT config_json FROM runtime_targets WHERE id=? AND business_id=?',
                             (target_id, business_id)).fetchone()
            stored = json.loads(row['config_json'])
            stored['health_endpoints'] = endpoints
            tx.execute('UPDATE runtime_targets SET config_json=? WHERE id=? AND business_id=?',
                       (dumps(stored), target_id, business_id))
        return self.get(business_id, target_id)

    def require_credentials(self, business_id, target_id):
        config = self.get(business_id, target_id)
        if config["connector"] == "ssh" and config.get("auth_mode") == "password":
            SSH_SESSIONS.resolve(config, self.settings.operator_id)

    def get(self, business_id, target_id):
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM runtime_targets WHERE id=? AND business_id=?",
                (target_id, business_id),
            ).fetchone()
            if not row:
                raise KeyError("运行环境不属于当前业务")
            return self.public({"id": target_id, **json.loads(row["config_json"])})
        finally:
            conn.close()

    def execute(self, business_id, target_id, request: ToolRequest, timeout_cap=None, *, supervision=None, read_protocol=None):
        config = self.get(business_id, target_id)
        if read_protocol is not None or request.read_protocol is not None:
            config['_read_protocol'] = read_protocol or request.read_protocol
        if timeout_cap is not None:
            config["timeout_seconds"] = min(config["timeout_seconds"], timeout_cap)
        observation_id = uid()
        with self.db.transaction() as tx:
            if supervision is not None:
                supervision['authorize'](tx)
            count = tx.execute(
                "SELECT COUNT(*) FROM runtime_observations WHERE target_id=?", (target_id,)
            ).fetchone()[0]
            from app.system.scope import binding_is_current
            approved = {
                (r["kind"], r["runtime_id"])
                for r in tx.execute(
                    "SELECT * FROM service_bindings WHERE target_id=? AND status='confirmed'",
                    (target_id,),
                ) if binding_is_current(r)
            }
            tx.execute(
                "INSERT INTO runtime_observations(id,target_id,tool_id,args_json,status,payload_json,content_digest,observed_at,bytes,error) VALUES(?,?,?,?,?,?,?,?,?,?)",
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
        stage = "parameters"
        try:
            if config["connector"] == "ssh" and not config["access_authorized"]:
                exc = PermissionError("目标访问授权已撤销")
                exc.code = "authorization_revoked"
                exc.stage = "authorization"
                raise exc
            if count >= config["max_calls"]:
                exc = PermissionError("目标累计调用预算已用尽")
                exc.code = "target_call_limit"
                exc.stage = "budget"
                raise exc
            args = check(config, request.tool_id, request.arguments, approved)
            connector = (
                MockConnector(config)
                if config["connector"] == "mock"
                else SSHConnector(config, self.settings)
                if config.get("execution_mode", "forced_gateway") == "forced_gateway"
                else StandardSSHConnector({**config, "_approved": approved}, self.settings)
            )
            stage = "request"
            if config['connector'] == 'ssh' and (supervision is not None or config.get('_read_protocol') == 'ab_v1'):
                from app.runtime.ssh_channel import run_ssh_action
                payload = sanitize_payload(run_ssh_action(config, self.settings, request.tool_id, args,
                                                         approved, **{k: v for k, v in (supervision or {}).items() if k != 'authorize'}))
            else:
                payload = sanitize_payload(connector.read(request.tool_id, args))
            stage = "output"
            serialized = dumps(payload)
            if len(serialized.encode()) > config["max_output_bytes"]:
                exc = SSHOutputLimit("读取输出超过大小限制")
                exc.output_bytes = len(serialized.encode())
                raise exc
            diagnostic = {}
            if payload.get("partial"):
                diagnostic = ToolFailure(request.tool_id, config.get("execution_mode", "forced_gateway"),
                    "tool_incomplete", "parse" if payload.get("invalid_records") else "pagination", "partial",
                    next(iter(payload.get("invalid_line_numbers", [])), None),
                    payload.get("output_bytes", len(serialized.encode())), payload.get("invalid_records", 0),
                    partial=True, observation_id=observation_id).public()
            stamp = now()  # Local acquisition time, remote clock is untrusted.
            with self.db.transaction() as tx:
                if supervision is not None:
                    supervision['authorize'](tx)
                tx.execute(
                    "UPDATE runtime_observations SET status='succeeded',payload_json=?,content_digest=?,observed_at=?,bytes=?,diagnostics_json=? WHERE id=?",
                    (serialized, digest(payload), stamp, len(serialized.encode()), dumps(diagnostic), observation_id),
                )
            return {
                "observation_id": observation_id,
                "tool_id": request.tool_id,
                "payload": payload,
                "observed_at": stamp,
                "bytes": len(serialized.encode()),
                "source_type": "Runtime",
                "connector": config["connector"],
                "diagnostic": diagnostic,
            }
        except Exception as exc:
            if supervision is not None and supervision['cancelled'].is_set():
                raise InterruptedError('调查已终止') from None
            diagnostic = failure(exc, request.tool_id, config.get("execution_mode", "forced_gateway"), observation_id, stage)
            with self.db.transaction() as tx:
                tx.execute(
                    "UPDATE runtime_observations SET status=?,error=?,diagnostics_json=?,bytes=? WHERE id=?",
                    (diagnostic["status"], type(exc).__name__, dumps(diagnostic), diagnostic["output_bytes"], observation_id),
                )
            result = {
                "observation_id": observation_id,
                "tool_id": request.tool_id,
                **diagnostic,
                "error": type(exc).__name__,
                "bytes": diagnostic["output_bytes"],
                "diagnostic": diagnostic,
            }
            return result
