from __future__ import annotations

from app.settings import get_settings


def current_actor() -> str:
    """Return the local audit actor; the demo has no administrator login."""
    return get_settings().operator_id
