from pathlib import PurePosixPath

from app.connectors.models import (
    ConfigArgs,
    ContainerArgs,
    ContainerLogArgs,
    EmptyArgs,
    ListArgs,
    ServiceArgs,
    ServiceLogArgs,
)

SCHEMAS = {
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
    if tool not in SCHEMAS:
        raise PermissionError("工具不在固定只读注册表")
    args = SCHEMAS[tool].model_validate(arguments).model_dump()
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
