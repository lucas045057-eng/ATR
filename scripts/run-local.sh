#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
test -f .env || { echo 'Copy .env.example to .env and configure private tokens first.'; exit 1; }
test -f frontend/dist/index.html || { echo 'Build frontend with npm ci && npm run build first.'; exit 1; }
exec python3 -m uv run --locked uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000 --workers 1
