"""Adaptive Troubleshooting Runtime API."""
from pathlib import Path

# Keep in sync with pyproject.toml; the runtime line (V2.3) is a separate label.
__version__ = "2.2.0"

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
