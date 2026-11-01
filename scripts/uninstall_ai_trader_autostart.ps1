param([switch]$DisableOnly)
$ErrorActionPreference='Stop'
$taskName='AutoTraderFork-Sandbox'
if($DisableOnly) { Disable-ScheduledTask -TaskName $taskName | Out-Null; Write-Host 'AITrader automatic startup disabled.' }
else { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false; Write-Host 'AITrader scheduled task removed.' }
Write-Host 'This does not liquidate positions or kill a running trader. Stop a running trader separately only after checking its position state.'
