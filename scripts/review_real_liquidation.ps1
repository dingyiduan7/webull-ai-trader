# Manual dry run only. This script never creates a task or enables execution.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
& (Join-Path $root '.venv\Scripts\python.exe') (Join-Path $PSScriptRoot 'webull_real_liquidation.py') --dry-run
exit $LASTEXITCODE
