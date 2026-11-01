# Always-on read-only dashboard. No admin, no credentials, no broker access. Safe to re-run.
$ErrorActionPreference='Stop'
$root=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$py=Join-Path $root '.venv/Scripts/pythonw.exe'   # windowless: no console ever appears
if (-not (Test-Path $py)) { throw 'pythonw.exe not found in .venv' }
$owner=[Security.Principal.WindowsIdentity]::GetCurrent().Name
$name='AutoTrader-Dashboard'
$action=New-ScheduledTaskAction -Execute $py -WorkingDirectory $root -Argument ('-I -B "'+(Join-Path $PSScriptRoot 'run_readonly_dashboard.py')+'"')
# Starts at sign-in, and is re-launched every 5 minutes if it is not running (IgnoreNew skips the launch while it is).
$logon=New-ScheduledTaskTrigger -AtLogOn -User $owner
$repeat=New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden
$principal=New-ScheduledTaskPrincipal -UserId $owner -LogonType Interactive -RunLevel Limited
$task=New-ScheduledTask -Action $action -Trigger @($logon,$repeat) -Settings $settings -Principal $principal -Description 'Read-only local dashboard on http://localhost:8777 (restarts itself).'
Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='pythonw.exe'" | Where-Object { $_.CommandLine -like '*run_readonly_dashboard.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Register-ScheduledTask -TaskName $name -InputObject $task -Force | Out-Null
Start-ScheduledTask -TaskName $name
Write-Host 'Dashboard task installed and started: http://localhost:8777 (errors: .local\dashboard\dashboard-errors.log)'
