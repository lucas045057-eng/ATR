from __future__ import annotations

import hashlib
import os
import re
import secrets
from functools import lru_cache
from typing import Any

import docker
from docker.errors import APIError, DockerException
from fastapi import FastAPI, Header, HTTPException, Query
from requests.exceptions import RequestException

from .redaction import redact_text, safe_text

app = FastAPI(title="ATR Read-only Collector", docs_url=None, redoc_url=None)
TOKEN = os.environ.get("COLLECTOR_TOKEN", "")
PROJECT_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")
CONTAINER_RE = re.compile(r"^[a-f0-9]{12,64}$")
MAX_LOG_BYTES = 48_000


@lru_cache(maxsize=1)
def client() -> docker.DockerClient:
    return docker.from_env(timeout=8)


def authorize(authorization: str | None) -> None:
    if len(TOKEN) < 32 or not authorization or not secrets.compare_digest(authorization, f"Bearer {TOKEN}"):
        raise HTTPException(status_code=401, detail="Collector authentication required")


def project_containers(project: str) -> list[Any]:
    if not PROJECT_RE.fullmatch(project):
        raise HTTPException(status_code=400, detail="Invalid project name")
    try:
        return client().containers.list(
            all=True, filters={"label": f"com.docker.compose.project={project}"}
        )
    except DockerException as exc:
        raise HTTPException(status_code=503, detail="Docker read unavailable") from exc


@app.get("/internal/v1/health")
def health() -> dict[str, str]:
    return {"status": "ready", "service": "read-only-collector"}


@app.get("/internal/v1/projects/{project}/containers")
def list_containers(project: str, authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
    authorize(authorization)
    result = []
    for container in project_containers(project):
        labels = container.labels or {}
        result.append({
            "id": container.id[:64],
            "name": redact_text((container.name or "")[:128]),
            "service": redact_text(labels.get("com.docker.compose.service", "")[:128]),
            "project": redact_text(labels.get("com.docker.compose.project", "")[:80]),
            "state": redact_text((container.status or "unknown")[:32]),
            "image": redact_text((container.image.tags[0] if container.image.tags else "")[:160]),
            "observed_at": redact_text(container.attrs.get("State", {}).get("StartedAt", "")),
        })
    return result[:64]


def resolve_container(project: str, container_id: str) -> Any:
    if not CONTAINER_RE.fullmatch(container_id):
        raise HTTPException(status_code=400, detail="Invalid container identifier")
    matches = [c for c in project_containers(project) if c.id.startswith(container_id)]
    if len(matches) != 1:
        raise HTTPException(status_code=404, detail="Container is not in the authorized project")
    return matches[0]


@app.get("/internal/v1/projects/{project}/containers/{container_id}/logs")
def logs(project: str, container_id: str, tail: int = Query(default=120, ge=1, le=400),
         authorization: str | None = Header(default=None)) -> dict[str, Any]:
    authorize(authorization)
    container = resolve_container(project, container_id)
    log_stream = None
    buffer = bytearray()
    truncated = False
    try:
        log_stream = container.logs(tail=tail, timestamps=True, stdout=True, stderr=True,
                                    stream=True, follow=False)
        for chunk in log_stream:
            if not chunk:
                continue
            if len(chunk) >= MAX_LOG_BYTES:
                buffer[:] = chunk[-MAX_LOG_BYTES:]
                truncated = True
                continue
            buffer.extend(chunk)
            if len(buffer) > MAX_LOG_BYTES:
                del buffer[:-MAX_LOG_BYTES]
                truncated = True
    except (APIError, DockerException, RequestException) as exc:
        raise HTTPException(status_code=503, detail="Container logs unavailable") from exc
    finally:
        close_stream = getattr(log_stream, "close", None)
        if callable(close_stream):
            try:
                close_stream()
            except Exception:
                pass
    text, clipped = safe_text(bytes(buffer).decode("utf-8", errors="replace"), MAX_LOG_BYTES)
    return {"container_id": container.id[:64], "project": redact_text(project),
            "observed_at": redact_text(container.attrs.get("State", {}).get("StartedAt", "")),
            "text": text, "truncated": truncated or clipped,
            "content_hash": hashlib.sha256(text.encode()).hexdigest()}


@app.get("/internal/v1/projects/{project}/containers/{container_id}/processes")
def processes(project: str, container_id: str,
              authorization: str | None = Header(default=None)) -> dict[str, Any]:
    authorize(authorization)
    container = resolve_container(project, container_id)
    try:
        data = container.top(ps_args="aux")
    except (APIError, DockerException) as exc:
        raise HTTPException(status_code=503, detail="Process summary unavailable") from exc
    processes_out = []
    titles = data.get("Titles", [])[:16]
    for process in data.get("Processes", [])[:80]:
        values = process if isinstance(process, dict) else dict(zip(titles, process))
        row = {key: redact_text(str(value)[:256]) for key, value in values.items()
               if key in {"PID", "USER", "%CPU", "%MEM", "STAT", "COMMAND"}}
        processes_out.append(row)
    return {"container_id": container.id[:64], "project": redact_text(project),
            "titles": [redact_text(str(title)[:128]) for title in data.get("Titles", [])[:16]],
            "processes": processes_out}
