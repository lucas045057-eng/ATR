import os

from fastapi import APIRouter

from app import FRONTEND_DIST, __version__
from app.settings import get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, object]:
    body: dict[str, object] = {
        "status": "ready",
        "service": "adaptive-troubleshooting-runtime",
        "version": __version__,
        "build": os.environ.get("ATR_BUILD", ""),
    }
    try:
        settings = get_settings()
    except Exception as exc:
        body["status"] = "degraded"
        body["config_error"] = type(exc).__name__
        return body
    body["decision_protocol"] = settings.agent_decision_protocol
    body["data_root"] = str(settings.database_path.resolve().parent)
    body["ui"] = FRONTEND_DIST.is_dir()
    return body
