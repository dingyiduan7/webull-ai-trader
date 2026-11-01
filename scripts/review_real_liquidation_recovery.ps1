# Manual dry run only. This script never activates or schedules recovery.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
& (Join-Path $root '.venv\Scripts\python.exe') (Join-Path $PSScriptRoot 'webull_real_liquidation_recovery.py') --dry-run
exit $LASTEXITCODE
