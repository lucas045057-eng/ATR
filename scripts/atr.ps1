$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $projectRoot 'backend'
python -m uv run --locked --project $projectRoot python -m app.cli @args
exit $LASTEXITCODE
