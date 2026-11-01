# Read-only watcher: no admin, no credentials, no broker/database access. Safe to re-run.
# Creates a private ntfy topic once (printed at the end: subscribe to it in the ntfy phone app).
$ErrorActionPreference='Stop'
$root=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$py=Join-Path $root '.venv/Scripts/pythonw.exe'
if (-not (Test-Path $py)) { throw 'pythonw.exe not found in .venv' }
$dir=Join-Path $root '.local/watcher'; New-Item -ItemType Directory -Force $dir | Out-Null
$cfg=Join-Path $dir 'config.json'
if (-not (Test-Path $cfg)) {
  $bytes=New-Object byte[] 12; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $topic='autotrader-'+(($bytes | ForEach-Object { $_.ToString('x2') }) -join '')
  (@{ntfy_topic=$topic} | ConvertTo-Json) | Set-Content -Encoding UTF8 $cfg
}
$topic=(Get-Content $cfg -Raw | ConvertFrom-Json).ntfy_topic
$owner=[Security.Principal.WindowsIdentity]::GetCurrent().Name
$name='AutoTrader-Watcher'
$action=New-ScheduledTaskAction -Execute $py -WorkingDirectory $root -Argument ('-I -B "'+(Join-Path $PSScriptRoot 'trade_watcher.py')+'"')
$logon=New-ScheduledTaskTrigger -AtLogOn -User $owner
$repeat=New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden
$principal=New-ScheduledTaskPrincipal -UserId $owner -LogonType Interactive -RunLevel Limited
$task=New-ScheduledTask -Action $action -Trigger @($logon,$repeat) -Settings $settings -Principal $principal -Description 'Read-only trader watcher; sends event alerts (restarts, stale heartbeat, blocks, fills, close).'
Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='pythonw.exe'" | Where-Object { $_.CommandLine -like '*trade_watcher.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Register-ScheduledTask -TaskName $name -InputObject $task -Force | Out-Null
Start-ScheduledTask -TaskName $name
try { Invoke-RestMethod -Method Post -Uri ('https://ntfy.sh/'+$topic) -Headers @{Title='Auto Trader'} -Body 'Watcher installed. You will get alerts here.' | Out-Null } catch {}
Write-Host ''
Write-Host 'Watcher installed and started.'
Write-Host ('In the ntfy phone app, subscribe to this topic:  '+$topic)
