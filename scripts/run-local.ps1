$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
if (-not (Test-Path -LiteralPath '.env')) {
    throw '请先复制 .env.example 为 .env，并设置两个不同的私密令牌。'
}
if (-not (Test-Path -LiteralPath 'frontend/dist/index.html')) {
    throw '请先在 frontend 目录运行 npm ci 和 npm run build。'
}
python -m uv run --locked uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000 --workers 1
exit $LASTEXITCODE
