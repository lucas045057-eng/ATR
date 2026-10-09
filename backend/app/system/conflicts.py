from __future__ import annotations

from typing import Any


def compose_conflicts(services: dict[str, dict[str, Any]]) -> list[dict]:
    claims: dict[tuple[str, int], list[dict]] = {}
    for service_name, config in services.items():
        for port in (config or {}).get("ports", []) or []:
            if isinstance(port, int):
                container_port, published = port, None
            else:
                parts = str(port).split(":")
                try:
                    container_port = int(parts[-1].split("/")[0])
                    published = int(parts[-2]) if len(parts) >= 2 else None
                except ValueError:
                    continue
            claims.setdefault((service_name, container_port), []).append({"published_port": published, "source": "compose"})
    result = []
    for (service, port), evidence in claims.items():
        if len(evidence) > 1 and len({x["published_port"] for x in evidence}) > 1:
            result.append({"subject_key": f"service:{service}", "property": "port_mapping", "scope": "compose",
                           "claims": evidence, "severity": "medium", "status": "open"})
    return result
