from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import yaml


def parse_compose(path: Path) -> tuple[dict[str, Any], str]:
    with path.open("rb") as stream:
        raw = stream.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("Compose file exceeds the 2 MiB onboarding limit")
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Compose YAML is invalid: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
        raise ValueError("Compose file must define a services mapping")
    if not 1 <= len(data["services"]) <= 200:
        raise ValueError("Compose must contain between 1 and 200 services")
    if any(not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", str(name))
           or not isinstance(config, dict) for name, config in data["services"].items()):
        raise ValueError("Compose service names or definitions are invalid")
    if data.get("name") and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}", str(data["name"])):
        raise ValueError("Compose project name contains unsupported characters")
    return data, hashlib.sha256(raw).hexdigest()
