# Register (or update) a per-user logon task that starts Cuff for PC hidden.
#   powershell -ExecutionPolicy Bypass -File pc-server\install-autostart.ps1
# Remove: Unregister-ScheduledTask -TaskName "Cuff for PC" -Confirm:$false
$ErrorActionPreference = "Stop"
$script = Join-Path $PSScriptRoot "run-server.ps1"
$action = New-ScheduledTaskAction -Execute "conhost.exe" `
    -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "Cuff for PC" -Action $action -Trigger $trigger `
    -Settings $settings -Description "Cuff for PC: dictation and stream stats for the Cuff wrist panel (pc-server)" -Force |
    Select-Object TaskName, State
