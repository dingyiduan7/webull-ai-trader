param([switch]$Preview,[switch]$LoggedOnOnly)
$ErrorActionPreference='Stop'
$taskName='AutoTraderFork-Sandbox'
$taskRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskOwner=[Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskLogon=if($LoggedOnOnly){'Interactive'}else{'Password'}
$taskPrincipal=New-ScheduledTaskPrincipal -UserId $taskOwner -LogonType $taskLogon -RunLevel Limited
$taskLauncher=Join-Path $PSScriptRoot 'start_ai_trader.ps1'
$taskAction=New-ScheduledTaskAction -Execute (Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe') -WorkingDirectory $taskRoot -Argument ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "'+$taskLauncher+'" -Mode SANDBOX -Scheduled -Wait -NoBrowser')
# Explicit UTC schedules cover both Denver offsets, independent of the PC timezone.
# The launcher rejects the early winter wake; a second wake is ignored while running.
$taskTriggers=@()
foreach($taskHour in @('13','14')) {
    $taskTrigger=New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '07:15'
    $taskTrigger.StartBoundary="2026-01-01T${taskHour}:15:00Z"
    $taskTriggers+=$taskTrigger
}
$taskSettings=New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$taskDefinition=New-ScheduledTask -Action $taskAction -Trigger $taskTriggers -Settings $taskSettings -Principal $taskPrincipal -Description 'AITrader explicit SANDBOX. Denver 07:15 weekday start; exchange calendar gates trading. Read-only local dashboard. No production.'
if($Preview) {
    $taskDir=Join-Path $taskRoot '.local/mvp';New-Item -ItemType Directory -Force -Path $taskDir | Out-Null
    Export-ScheduledTask -InputObject $taskDefinition | Set-Content -LiteralPath (Join-Path $taskDir 'task-preview.xml') -Encoding UTF8
    Write-Host 'Preview only; no task registered. XML: .local/mvp/task-preview.xml'
    exit 0
}
if($LoggedOnOnly) {
    Register-ScheduledTask -TaskName $taskName -InputObject $taskDefinition -Force | Out-Null
    Write-Host 'Task installed for logged-on sessions only. Locking the screen is fine; signing out prevents startup.'
} else {
    # Windows Task Scheduler stores its owner logon credential. This is NOT an API key.
    # Enter it locally. Do not use a PIN, chat, command-line password, or transcript.
    $taskCredential=Get-Credential -UserName $taskOwner -Message 'Windows sign-in password for AITrader unattended task (not Webull/OpenAI credentials)'
    if($taskCredential.UserName -ne $taskOwner) { throw 'Use the same Windows owner that encrypted the local Webull credentials.' }
    try { Register-ScheduledTask -TaskName $taskName -InputObject $taskDefinition -User $taskOwner -Password $taskCredential.GetNetworkCredential().Password -Force | Out-Null }
    finally { $taskCredential=$null }
}
Write-Host "Installed $taskName. Denver weekday startup at 07:15; wake enabled; crash retry every minute; no per-trade prompts."
Write-Host 'Task uses existing local key-path/environment configuration. It does not store API keys. Test with Start-ScheduledTask -TaskName AutoTraderFork-Sandbox.'
Write-Host 'PC firmware and power policy must permit wake timers. Do not power the PC off.'
