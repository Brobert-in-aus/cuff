# Start Cuff for PC, logging to %LOCALAPPDATA%\cuff-pc\server.log.
# Run at logon by the "Cuff for PC" scheduled task (see install-autostart.ps1),
# or by hand: powershell -File pc-server\run-server.ps1
$ErrorActionPreference = "Stop"
$logDir = Join-Path $env:LOCALAPPDATA "cuff-pc"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir "server.log"
if ((Test-Path $log) -and (Get-Item $log).Length -gt 5MB) { Move-Item -Force $log "$log.1" }

$env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"
Set-Location $PSScriptRoot
# server.py writes the log itself; PowerShell 5.1 holds back redirected native output.
& uv run server.py --log $log
