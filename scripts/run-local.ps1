$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'atr.ps1') serve @args
exit $LASTEXITCODE
