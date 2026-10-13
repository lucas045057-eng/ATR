from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import yaml

from app.business.ingestion import UnsafeArtifact
from app.business.languages import LANGUAGES
from app.business.media import parse_image, parse_pdf
from app.system.graph import role_for
from app.system.source_parser import describe_source
from app.tools.registry import sanitize_payload

CODE = set(LANGUAGES)
TEXT = CODE | {".yaml", ".yml", ".json", ".toml", ".md", ".txt", ".conf", ".cfg", ".ini", ".env"}
IGNORED = {"node_modules", ".git", ".venv", "venv", "__pycache__", "dist", "build"}


def safe_yaml(text: str):
    # Aliases/anchors are unnecessary for the skeleton and can amplify tiny inputs.
    for count, token in enumerate(yaml.scan(text)):
        if count > 50000 or isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)):
            raise ValueError("YAML aliases or token limit")
    return yaml.safe_load(text)


def inspect_file(path: Path, relative: str, read_limit: int, *, defer_media=False) -> dict:
    suffix = path.suffix.lower()
    base = path.name.lower()
    if suffix in {'.png','.jpg','.jpeg','.webp','.pdf'} and defer_media:
        size=path.stat().st_size
        limit=(50 if suffix=='.pdf' else 20)*1024*1024
        if size>limit:
            raise UnsafeArtifact('媒体原件超出独立大小上限')
        with path.open('rb') as stream:
            signature=stream.read(16)
        valid=(signature.startswith(b'%PDF-') if suffix=='.pdf' else
               signature.startswith(b'\x89PNG\r\n\x1a\n') if suffix=='.png' else
               signature.startswith(b'\xff\xd8\xff') if suffix in {'.jpg','.jpeg'} else
               signature.startswith(b'RIFF') and signature[8:12]==b'WEBP')
        if not valid:
            raise UnsafeArtifact('媒体签名与文件类型不匹配')
        return {'media_type':'pdf' if suffix=='.pdf' else 'image','parse_state':'pending_media_local'}
    with path.open("rb") as stream:
        raw = stream.read(read_limit + 1)
    info = {
        "parse_state": "complete",
        "language": LANGUAGES.get(suffix, ''),
        "services": [],
        "keys": [],
        "datastores": [],
    }
    if suffix in {".png", ".jpg", ".jpeg",'.webp'}:
        return {**info, "media_type": "image", **parse_image(raw, suffix, relative)}
    if suffix == ".pdf":
        return {**info, "media_type": "pdf", **parse_pdf(raw)}
    info["media_type"] = (
        "code"
        if suffix in CODE
        else "configuration"
        if suffix in TEXT - {".md", ".txt"} or base == "dockerfile"
        else "document"
        if suffix in {".md", ".txt"}
        else "unsupported"
    )
    if any(part in IGNORED for part in Path(relative).parts):
        return {**info, "parse_state": "excluded_irrelevant"}
    if info["media_type"] == "unsupported":
        return {**info, "parse_state": "unsupported"}
    if len(raw) > read_limit:
        return {
            **info,
            "parse_state": "read_limit",
            "reason": "文件已建立哈希，但内容超出静态读取上限",
        }
    if b"\0" in raw or raw.startswith((b"PK\x03\x04", b"\x7fELF", b"MZ", b"%PDF-", b"\x89PNG")):
        raise UnsafeArtifact("文本文件包含不匹配的二进制签名")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return {**info, "parse_state": "unsupported_encoding"}
    try:
        if suffix in CODE:
            info.update(describe_source(relative, text, protocol='ab_source_v1'))
        elif suffix in {".yaml", ".yml", ".json", ".toml"}:
            obj = (
                safe_yaml(text)
                if suffix in {".yaml", ".yml"}
                else json.loads(text)
                if suffix == ".json"
                else tomllib.loads(text)
            )
            if isinstance(obj, dict):
                info["keys"] = list(str(key)[:120] for key in obj)[:200]
                services = obj.get("services", {})
                if isinstance(services, dict):
                    if len(services) > 200:
                        raise ValueError("Compose service count exceeds 200")
                    for name, config in services.items():
                        if not isinstance(config, dict) or not re.fullmatch(
                            r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", str(name)
                        ):
                            raise ValueError("Invalid service definition")
                        env = config.get("environment", {}) or {}
                        keys = (
                            sorted(env)
                            if isinstance(env, dict)
                            else [str(x).partition("=")[0] for x in env]
                            if isinstance(env, list)
                            else []
                        )
                        depends = config.get("depends_on", {}) or {}
                        info["services"].append(
                            {
                                "name": name,
                                "image": str(config.get("image", ""))[:160],
                                "role": role_for(name, config),
                                "dependencies": list(depends)[:100],
                                "ports": (config.get("ports") or [])[:30],
                                "environment_keys": keys[:100],
                                "healthcheck_declared": bool(config.get("healthcheck")),
                                "container_name": str(config.get("container_name", ""))[:120],
                            }
                        )
                dependencies = obj.get("dependencies", {})
                if isinstance(dependencies, dict):
                    info["package_dependencies"] = list(dependencies)[:200]
                project = obj.get("project", {})
                if isinstance(project, dict) and isinstance(project.get("dependencies"), list):
                    info["package_dependencies"] = [
                        re.split(r"[<>=!\[]", str(x))[0] for x in project["dependencies"][:200]
                    ]
        elif base == "dockerfile":
            info["base_images"] = re.findall(r"(?im)^FROM\s+([^\s]+)", text)[:20]
            info["ports"] = re.findall(r"(?im)^EXPOSE\s+(\d+)", text)[:20]
        elif suffix in {".md", ".txt"}:
            info["headings"] = re.findall(r"(?m)^#{1,3}\s+(.+)$", text)[:12]
        else:
            info["keys"] = re.findall(r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]{0,100})\s*[=:]", text)[:200]
            info["upstreams"] = re.findall(r"proxy_pass\s+(https?://[a-zA-Z0-9_.:-]+)", text)[:30]
        if info["media_type"] in {"configuration", "code"}:
            # Only dependency names/schemes; never persist connection strings or values.
            info["datastores"] = sorted(
                {
                    name
                    for name, pattern in {
                        "MySQL": r"(?i)mysql|pymysql",
                        "PostgreSQL": r"(?i)postgres(?:ql)?|\bPGHOST\b|\bpsycopg",
                        "Redis": r"(?i)redis",
                    }.items()
                    if re.search(pattern, text)
                }
            )
    except (ValueError, TypeError, AttributeError, RecursionError, yaml.YAMLError, MemoryError):
        info["parse_state"] = "partial_parser_error"
    return sanitize_payload(info)
