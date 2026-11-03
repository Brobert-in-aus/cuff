# Register (or update) a per-user logon task that starts Cuff for PC hidden (its tray icon
# shows), and a Start menu shortcut that opens it (or starts it, if it isn't running).
#   powershell -ExecutionPolicy Bypass -File pc-server\install-autostart.ps1
# Remove: Unregister-ScheduledTask -TaskName "Cuff for PC" -Confirm:$false
# -NoShortcut: the Windows installer makes its own Start menu shortcut.
# -Start: then start Cuff for PC through the task (the installer's last step), even while an
#   instance of the task is still running.
param([switch]$NoShortcut, [switch]$Start)
$ErrorActionPreference = "Stop"
$task = "Cuff for PC"
$script = Join-Path $PSScriptRoot "run-server.ps1"
$action = New-ScheduledTaskAction -Execute "conhost.exe" `
    -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
function Register-Task([string]$instances) {
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -MultipleInstances $instances
    Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Settings $settings `
        -Description "Cuff for PC: dictation and stream stats for the Cuff wrist panel (pc-server)" -Force
}
Register-Task "IgnoreNew" | Select-Object TaskName, State

if ($Start) {
    # An instance still running (what's left of the copy the installer stopped, or the
    # installer itself when an older Cuff for PC started it inside the task) makes Task
    # Scheduler ignore a start (IgnoreNew). Give it a moment; if it's still there, allow a
    # second instance just for this start. (Only one copy of the app runs whatever happens:
    # app.py checks.)
    for ($i = 0; $i -lt 20 -and (Get-ScheduledTask -TaskName $task).State -eq "Running"; $i++) { Start-Sleep 1 }
    if ((Get-ScheduledTask -TaskName $task).State -eq "Running") {
        Register-Task "Parallel" | Out-Null
        Start-ScheduledTask -TaskName $task
        Start-Sleep 2
        Register-Task "IgnoreNew" | Out-Null
    } else {
        Start-ScheduledTask -TaskName $task
    }
    Write-Host "Started: $task"
}

if ($NoShortcut) { exit 0 }
$programs = [Environment]::GetFolderPath("Programs")
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut((Join-Path $programs "Cuff for PC.lnk"))
$link.TargetPath = "conhost.exe"
$link.Arguments = "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$script`""
$link.WorkingDirectory = $PSScriptRoot
$link.Description = "Open Cuff for PC"
$link.Save()
Write-Host "Start menu: Cuff for PC"
