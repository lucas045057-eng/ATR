from app.common import now


class MockConnector:
    """Explicit isolated fixture, never presented as a real server observation."""

    def __init__(self, config):
        self.config = config

    def read(self, tool, args):
        scenario = self.config["mock_scenario"]
        if scenario == "timeout":
            raise TimeoutError("Mock 模拟执行超时")
        if scenario == "permission_denied":
            raise PermissionError("Mock 模拟账号权限不足")
        if scenario == "disconnect":
            raise ConnectionError("Mock 模拟网络断开")
        stamp = now()
        common = {"connector": "mock", "observed_at": stamp}
        if tool == "host.info":
            return {**common, "os": "Linux (isolated mock)", "hostname": "atr-mixed-fixture"}
        if tool == "service.list":
            return {
                **common,
                "services": [
                    {
                        "id": "catalog-api.service",
                        "name": "catalog-api",
                        "kind": "service",
                        "state": "failed",
                        "ports": [8000],
                    },
                    {
                        "id": "unrelated-backup.service",
                        "name": "unrelated-backup",
                        "kind": "service",
                        "state": "active",
                    },
                ],
            }
        if tool == "process.list":
            return {
                **common,
                "processes": [
                    {"id": "1024", "name": "catalog-worker", "kind": "process"},
                    {"id": "1025", "name": "unrelated-monitor", "kind": "process"},
                ],
            }
        if tool == "network.listeners":
            return {**common, "listeners": [{"port": 8000}, {"port": 5432}]}
        if tool == "container.list":
            return {
                **common,
                "containers": [
                    {
                        "id": "a" * 64,
                        "name": "catalog-api",
                        "kind": "container",
                        "image": "catalog:current",
                        "ports": [8000],
                        "environment_keys": ["DB_HOST"],
                    },
                    {
                        "id": "b" * 64,
                        "name": "catalog-store",
                        "kind": "container",
                        "image": "postgres:17",
                    },
                ],
            }
        if tool in {"service.status", "container.status"}:
            return {
                **common,
                "state": "failed",
                "name": args.get("name"),
                "text": "Current observed service state: failed",
            }
        if tool in {"service.logs", "container.logs"}:
            return {
                **common,
                "text": f"{stamp} startup dependency check failed: connection refused\n{stamp} application exited because required datastore was unavailable",
            }
        if tool == "config.read":
            return {
                **common,
                "path": args["path"],
                "text": "DB_ENGINE=postgresql\nDB_PASSWORD=[REDACTED]\n"
                if scenario == "datastore_conflict"
                else "DB_ENGINE=mysql\nDB_PASSWORD=[REDACTED]\n",
            }
        if tool == "config.list":
            return {**common, "files": [args["path"] + "/app.conf"], "recursive": False}
        return {**common, "text": "Mock bounded host summary"}
