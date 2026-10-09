from __future__ import annotations

import json
import uuid
from typing import Any


def ident() -> str:
    return str(uuid.uuid4())


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def role_for(name: str, service: dict[str, Any]) -> str:
    text = " ".join((name, str(service.get("image", "")))).casefold()
    if any(x in text for x in ("nginx", "proxy", "gateway")):
        return "proxy"
    if any(x in text for x in ("mysql", "postgres", "mariadb", "database", "db")):
        return "datastore"
    if "redis" in text:
        return "cache"
    if any(x in text for x in ("api", "app", "web", "server")):
        return "application"
    return "unknown"


def node(kind: str, name: str, role: str, attributes: dict[str, Any], snapshot_id: str) -> dict[str, Any]:
    canonical = f"{kind}:{name}"
    return {"id": ident(), "snapshot_id": snapshot_id, "canonical_key": canonical, "kind": kind,
            "name": name, "role": role, "attributes": attributes}


def build_graph(snapshot_id: str, services: dict[str, dict[str, Any]], networks: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    nodes: list[dict] = []
    edges: list[dict] = []
    service_nodes: dict[str, dict] = {}
    for name, config in services.items():
        role = role_for(name, config)
        config = config if isinstance(config, dict) else {}
        item = node("service", name, role, {
            "image": config.get("image"), "build": bool(config.get("build")),
            "command_declared": "command" in config, "healthcheck_declared": "healthcheck" in config,
            "environment_keys": sorted((config.get("environment") or {}).keys()) if isinstance(config.get("environment"), dict) else [],
            "ports": config.get("ports", []) if isinstance(config.get("ports", []), list) else [],
            "depends_on": list((config.get("depends_on") or {}).keys()) if isinstance(config.get("depends_on"), dict) else list(config.get("depends_on") or []),
            "networks": list((config.get("networks") or {}).keys()) if isinstance(config.get("networks"), dict) else list(config.get("networks") or []),
        }, snapshot_id)
        nodes.append(item)
        service_nodes[name] = item
    for name, config in services.items():
        config = config if isinstance(config, dict) else {}
        for dependency in service_nodes[name]["attributes"]["depends_on"]:
            if dependency in service_nodes:
                edges.append({"id": ident(), "snapshot_id": snapshot_id, "from_node_id": service_nodes[name]["id"],
                              "to_node_id": service_nodes[dependency]["id"], "relation": "depends_on",
                              "certainty": "declared", "attributes": {"source": "compose"}})
        for network_name in service_nodes[name]["attributes"]["networks"]:
            if network_name not in networks:
                continue
            key = f"network:{network_name}"
            network_node = next((n for n in nodes if n["canonical_key"] == key), None)
            if network_node is None:
                network_node = node("network", network_name, "network", {}, snapshot_id)
                nodes.append(network_node)
            edges.append({"id": ident(), "snapshot_id": snapshot_id, "from_node_id": service_nodes[name]["id"],
                          "to_node_id": network_node["id"], "relation": "connected_to", "certainty": "declared", "attributes": {}})
    return nodes, edges
