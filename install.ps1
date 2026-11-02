$ErrorActionPreference = 'Stop'

function Fail([string]$message) {
    Write-Host "安装失败：$message" -ForegroundColor Red
    exit 1
}

function Test-PythonModule([string]$module) {
    $saved = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & python -m $module --version 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
    finally {
        $ErrorActionPreference = $saved
    }
}

$projectRoot = $PSScriptRoot
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Fail "未找到 python 命令。请安装 Python 3.11 或更高版本，并在安装时勾选 Add python.exe to PATH：https://www.python.org/downloads/"
}

$versionText = ''
$versionExit = 1
$saved = $ErrorActionPreference
try {
    $ErrorActionPreference = 'Continue'
    $versionText = (& python --version 2>&1 | Out-String).Trim()
    $versionExit = $LASTEXITCODE
}
finally {
    $ErrorActionPreference = $saved
}
if ($versionExit -ne 0 -or $versionText -notmatch 'Python (\d+)\.(\d+)') {
    Fail "无法读取 Python 版本（$versionText）。请确认 python 可正常执行。"
}
$major = [int]$Matches[1]
$minor = [int]$Matches[2]
if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 11)) {
    Fail "需要 Python 3.11 或更高版本，当前为 $versionText。请升级 Python 后重试：https://www.python.org/downloads/"
}

if (-not (Test-PythonModule 'pip')) {
    Fail "当前 Python 缺少 pip。请运行 python -m ensurepip --upgrade 后重试。"
}

if (-not (Test-PythonModule 'uv')) {
    Write-Host "未找到 uv，正在安装..."
    & python -m pip install uv
    if ($LASTEXITCODE -ne 0) {
        Fail "uv 安装失败。请检查网络，或手动运行 python -m pip install uv。"
    }
}

Push-Location $projectRoot
try {
    Write-Host "正在安装 ATR 依赖（首次需要联网）..."
    & python -m uv sync --locked
    if ($LASTEXITCODE -ne 0) {
        Fail "依赖安装失败。请检查网络连接和 uv.lock 是否存在。"
    }

    Write-Host "正在初始化 ATR..."
    & (Join-Path $projectRoot 'scripts\atr.ps1') init
    if ($LASTEXITCODE -ne 0) {
        Fail "初始化失败。请根据上面的错误修复后重试。"
    }
}
finally {
    Pop-Location
}

Write-Host ""
Write-Host "安装完成。下一步："
Write-Host "  .\scripts\atr.ps1 serve"
Write-Host "健康检查：http://127.0.0.1:8000/api/v1/health"
Write-Host "已存在的 .env 不会被覆盖；如需修改配置，请参考 docs/deployment-new-machine-runbook.md。"
