from pathlib import PurePosixPath

from app.connectors.diagnostics import CapabilityUnavailable
from app.connectors.models import (
    ConfigArgs,
    ContainerArgs,
    ContainerLogArgs,
    EmptyArgs,
    HealthArgs,
    ListArgs,
    LogFileArgs,
    ProcessArgs,
    ServiceArgs,
    ServiceLogArgs,
)

SCHEMAS = {
    'capabilities': EmptyArgs,
    'health.get': HealthArgs,
    'process.status': ProcessArgs,
    'log.tail': LogFileArgs,
    "host.info": EmptyArgs,
    "host.memory": EmptyArgs,
    "host.disk": EmptyArgs,
    "process.list": ListArgs,
    "network.listeners": EmptyArgs,
    "service.list": ListArgs,
    "container.list": ListArgs,
    "config.list": ConfigArgs,
    "service.status": ServiceArgs,
    "service.logs": ServiceLogArgs,
    "container.status": ContainerArgs,
    "container.logs": ContainerLogArgs,
    "config.read": ConfigArgs,
}
DISCOVERY_TOOLS = {
    "host.info",
    "host.memory",
    "host.disk",
    "process.list",
    "network.listeners",
    "service.list",
    "container.list",
    "config.list",
}


def check(config, tool, arguments, approved=()):
    if config.get("connector") == "ssh" and config.get("execution_mode", "forced_gateway") == "standard_ssh_readonly":
        # Reject ambiguous whitespace rather than silently normalizing remote parameters.
        strings = [v for v in arguments.values() if isinstance(v, str)]
        strings += arguments.get("name_hints", []) if isinstance(arguments.get("name_hints"), list) else []
        if any(not isinstance(v, str) or v != v.strip() for v in strings):
            raise ValueError("普通 SSH 参数不接受首尾空白")
        if any(key in arguments and type(arguments[key]) is not int for key in ("limit", "tail")):
            raise ValueError("普通 SSH 数量参数必须为整数")
    if tool not in SCHEMAS:
        raise PermissionError("工具不在固定只读注册表")
    if (tool in {"container.status", "container.logs"} and config.get("docker_reader") == "restricted_list"):
        raise CapabilityUnavailable("受限 Docker 网关仅提供 list")
    args = SCHEMAS[tool].model_validate(arguments).model_dump()
    if tool == 'health.get' and not any(row['endpoint_id'] == args['endpoint_id']
                                       for row in config.get('health_endpoints', [])):
        raise PermissionError('HTTP 端点未在当前目标的精确授权清单内')
    if tool == 'process.status' and ('process', str(args['pid']) + ':' + args['start_time']) not in approved:
        raise PermissionError('进程 PID 与启动时间尚未确认')
    if tool == 'log.tail':
        source = next((row for row in config.get('log_files', []) if row['file_id'] == args['file_id']), None)
        if not source or ('process', str(source['pid']) + ':' + source['start_time']) not in approved:
            raise PermissionError('日志文件标识或对应进程身份尚未授权')
    if tool.startswith("container.") and not config["docker_reads_enabled"]:
        raise PermissionError("没有授权 Docker 元数据读取；不访问 Docker socket")
    if tool.startswith("config."):
        path = PurePosixPath(args["path"])
        if not any(path.is_relative_to(PurePosixPath(root)) for root in config["allowed_paths"]):
            raise PermissionError("文件或目录不在授权资料范围内")
    if tool in {"service.status", "service.logs"} and ("service", args["name"]) not in approved:
        raise PermissionError("服务尚未通过范围确认")
    if (
        tool in {"container.status", "container.logs"}
        and ("container", args["container_id"]) not in approved
    ):
        raise PermissionError("容器尚未通过范围确认")
    return args
