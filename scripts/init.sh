#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
command -v uv >/dev/null
command -v python3 >/dev/null
python3 -c 'import sys; assert sys.version_info >= (3, 11)'
node -e 'if(Number(process.versions.node.split(".")[0]) < 22) process.exit(1)'
make deps build
node scripts/features.mjs validate
if [ "${1:-}" != "--no-serve" ]; then
  exec node scripts/serve.mjs dist-site "${PORT:-8080}"
fi
