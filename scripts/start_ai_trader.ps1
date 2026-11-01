param([ValidateSet('DRY_RUN','SANDBOX')][string]$Mode='DRY_RUN',[string]$KeyFile,[int]$IntervalSeconds=300,[switch]$NoBrowser,[switch]$Wait,[switch]$Scheduled)
$ErrorActionPreference='Stop'
$taskRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskLocal=Join-Path $taskRoot '.local/mvp'
New-Item -ItemType Directory -Force -Path $taskLocal | Out-Null
try {
    if ($Scheduled) {
        $taskZone=[TimeZoneInfo]::FindSystemTimeZoneById('Mountain Standard Time')
        $taskDenver=[TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow,$taskZone)
        if ($taskDenver.DayOfWeek -in @('Saturday','Sunday') -or $taskDenver.TimeOfDay -lt [TimeSpan]::Parse('07:15:00')) { exit 0 }
        $Mode='SANDBOX'
    }
    if ($IntervalSeconds -lt 30 -or $IntervalSeconds -gt 86400) { throw 'Invalid interval' }
    $taskConfig=Join-Path $taskLocal 'owner-config.json'
    if ($KeyFile) {
        $taskFullKeyPath=[IO.Path]::GetFullPath($KeyFile)
        if (-not (Test-Path -LiteralPath $taskFullKeyPath -PathType Leaf)) { throw 'Key file missing' }
        @{openai_key_file=$taskFullKeyPath} | ConvertTo-Json | Set-Content -LiteralPath $taskConfig -Encoding UTF8
    }
    if (-not $env:OPENAI_API_KEY -and -not $env:OPENAI_API_KEY_FILE -and (Test-Path -LiteralPath $taskConfig)) {
        $env:OPENAI_API_KEY_FILE=(Get-Content -Raw -LiteralPath $taskConfig | ConvertFrom-Json).openai_key_file
    }
    $taskPg=Join-Path $taskRoot '.local/postgresql/pgsql/bin/pg_ctl.exe'
    $taskData=Join-Path $taskRoot '.local/pgdata-m2'
    & $taskPg -D $taskData status *> $null
    if ($LASTEXITCODE -ne 0) { & (Join-Path $PSScriptRoot 'local_postgres.ps1') start }
    try {
        $taskExisting=Invoke-RestMethod -Uri 'http://127.0.0.1:8766/api/snapshot' -TimeoutSec 15
        if ($taskExisting.system.mode -ne $Mode) { throw 'MODE_CONFLICT' }
        Write-Host 'AITrader already serves http://127.0.0.1:8766'
        if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:8766' }
        exit 0
    } catch { if ($_.Exception.Message -eq 'MODE_CONFLICT') { throw 'Existing runner uses another mode; stop it explicitly first.' } }
    $env:AI_TRADER_MODE=$Mode;$env:AI_TRADER_INTERVAL_SECONDS=[string]$IntervalSeconds
    $taskPython=Join-Path $taskRoot '.venv/Scripts/python.exe';$taskRunner=Join-Path $PSScriptRoot 'run_ai_trader.py'
    do {
    & $taskPg -D $taskData status *> $null
    if ($LASTEXITCODE -ne 0) { & (Join-Path $PSScriptRoot 'local_postgres.ps1') start }
    $taskProcess=Start-Process -FilePath $taskPython -ArgumentList @('-u',('"'+$taskRunner+'"')) -WorkingDirectory $taskRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskLocal 'process.log') -RedirectStandardError (Join-Path $taskLocal 'process-error.log')
    $taskReady=$false
    for($taskTry=0;$taskTry -lt 20;$taskTry++) {
        Start-Sleep -Seconds 1
        if ($taskProcess.HasExited) { throw 'Runner exited during startup' }
        try { $null=Invoke-RestMethod -Uri 'http://127.0.0.1:8766/api/snapshot' -TimeoutSec 15; $taskReady=$true; break } catch {}
    }
    if (-not $taskReady) { throw 'Dashboard startup timeout' }
    $taskProcess.Id | Set-Content -LiteralPath (Join-Path $taskLocal 'runner.pid')
    Write-Host "AITrader $Mode dashboard: http://127.0.0.1:8766"
    Write-Host "Process ID: $($taskProcess.Id). Local logs: .local/mvp"
    if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:8766' }
    if ($Wait) {
        $taskProcess.WaitForExit()
        ('{0:o} TRADER_EXITED: supervised child ended; restarting after 60 seconds.' -f [DateTime]::UtcNow) | Add-Content -LiteralPath (Join-Path $taskLocal 'startup-errors.log')
        # Task Scheduler exit-code restart was not reliable in the local crash test.
        # Keep its failure setting, and supervise the trader child explicitly.
        Start-Sleep -Seconds 60
    }
    } while ($Wait)
} catch {
    ('{0:o} STARTUP_FAILED: check local configuration, PostgreSQL and process logs.' -f [DateTime]::UtcNow) | Add-Content -LiteralPath (Join-Path $taskLocal 'startup-errors.log')
    Write-Error 'AITrader startup failed. See .local/mvp/startup-errors.log and process-error.log. No credentials are logged.'
    exit 1
}
