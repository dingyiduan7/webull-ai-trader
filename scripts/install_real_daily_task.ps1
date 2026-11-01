param([switch]$Preview,[switch]$Enable)
$ErrorActionPreference='Stop'
$taskRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskPython=Join-Path $taskRoot '.venv/Scripts/python.exe'
$taskOwner=[Security.Principal.WindowsIdentity]::GetCurrent().Name
# Validate reviewed persistent scope before changing OS configuration. No credentials.
Push-Location $taskRoot
try {
    & $taskPython -I -B -c 'import sys; from pathlib import Path; sys.path.insert(0,str(Path.cwd()/"src")); from datetime import datetime,timezone; from autonomous_trader.real_daily import load_authority; load_authority(Path.cwd(),datetime.now(timezone.utc))'
    if ($LASTEXITCODE -ne 0) { throw 'Owner charter and bootstrap evidence must verify first (scripts\owner_sign_authorization.py, scripts\real_bootstrap.py).' }
} finally { Pop-Location }
$taskAction=New-ScheduledTaskAction -Execute $taskPython -WorkingDirectory $taskRoot -Argument ('-I -B "'+(Join-Path $PSScriptRoot 'run_real_daily.py')+'"')
$taskTriggers=@()
# Use the installation date to avoid the observed winter-boundary scheduling offset.
$taskStartDate=[DateTime]::UtcNow.ToString('yyyy-MM-dd')
foreach($taskHour in @('13','14')) {
    $taskTrigger=New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '09:30'
    $taskTrigger.StartBoundary="${taskStartDate}T${taskHour}:30:00Z"
    $taskTriggers+=$taskTrigger
}
$taskTriggers+=New-ScheduledTaskTrigger -AtLogOn -User $taskOwner
$taskSettings=New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$taskPrincipal=New-ScheduledTaskPrincipal -UserId $taskOwner -LogonType Interactive -RunLevel Limited
$taskDefinition=New-ScheduledTask -Action $taskAction -Trigger $taskTriggers -Settings $taskSettings -Principal $taskPrincipal -Description 'REAL exact-build approval and fresh daily certificate required. DPAPI owner token reuse. Exchange session gates. No implicit approval.'
$taskDefinition.Settings.Enabled=[bool]$Enable
if ($Preview) { Export-ScheduledTask -InputObject $taskDefinition; exit 0 }
$existingTask=Get-ScheduledTask -TaskName 'AutoTraderFork-Real-Daily' -TaskPath '\' -ErrorAction SilentlyContinue
if ($existingTask) {
    $ownerSid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $principalId=$existingTask.Principal.UserId
    if ($principalId -match '^S-1-') {
        $principalSid=([Security.Principal.SecurityIdentifier]::new($principalId)).Value
    } else {
        $principalSid=([Security.Principal.NTAccount]::new($principalId)).Translate([Security.Principal.SecurityIdentifier]).Value
    }
    $actions=@($existingTask.Actions)
    if ($principalSid -ne $ownerSid -or
        $existingTask.Principal.LogonType -ne $taskPrincipal.LogonType -or
        $existingTask.Principal.RunLevel -ne $taskPrincipal.RunLevel -or
        $actions.Count -ne 1 -or $actions[0].Execute -ne $taskAction.Execute -or
        $actions[0].Arguments -cne $taskAction.Arguments -or
        $actions[0].WorkingDirectory -ne $taskAction.WorkingDirectory) {
        throw 'Existing REAL daily task identity or action mismatch.'
    }
    if ($Enable) {
        Enable-ScheduledTask -TaskName 'AutoTraderFork-Real-Daily' -TaskPath '\' | Out-Null
    } else {
        Disable-ScheduledTask -TaskName 'AutoTraderFork-Real-Daily' -TaskPath '\' | Out-Null
    }
} else {
    Register-ScheduledTask -TaskName 'AutoTraderFork-Real-Daily' -InputObject $taskDefinition | Out-Null
}
if ($Enable) {
    $enabledTask=Get-ScheduledTask -TaskName 'AutoTraderFork-Real-Daily' -TaskPath '\'
    if ($enabledTask.State -ne 'Running') {
        Start-ScheduledTask -TaskName 'AutoTraderFork-Real-Daily' -TaskPath '\'
    }
}
if ($Enable) { Write-Host 'REAL daily task enabled. Windows owner must remain signed in; wake requires permitted wake timers.' }
else { Write-Host 'REAL daily task installed disabled.' }
