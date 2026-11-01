$ErrorActionPreference='Stop'
Unregister-ScheduledTask -TaskName 'AutoTraderFork-Real-Daily' -Confirm:$false
