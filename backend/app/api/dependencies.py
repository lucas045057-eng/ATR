from __future__ import annotations

from fastapi import Header, HTTPException

from app.settings import get_settings


def require_operator(authorization: str | None = Header(default=None, alias="Authorization")) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    settings = get_settings()
    if scheme.casefold() != "bearer" or not settings.authenticate_operator(token):
        raise HTTPException(status_code=401, detail="Operator authentication required")
    return settings.operator_id
