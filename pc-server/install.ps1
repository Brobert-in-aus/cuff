# Install or update Cuff for PC on Windows: dictation for the headset (on the PC's GPU), the
# game's side of a Steam Link stream for the Performance tab, and an announcement on the local
# networks so the headset finds it.
#   powershell -ExecutionPolicy Bypass -File pc-server\install.ps1
# It needs uv (https://docs.astral.sh/uv/): winget install --id=astral-sh.uv -e
# Steps: uv sync; a logon task that starts it hidden (install-autostart.ps1); in one admin
# prompt, the firewall rules (firewall.ps1); then it starts the app and pairs the headset
# (pair.ps1: type the code the wrist panel shows).
# Remove: Unregister-ScheduledTask -TaskName "Cuff for PC" -Confirm:$false
#         Remove-NetFirewallRule -DisplayName "Cuff for PC*"    (as admin)
param([switch]$NoFirewall)
$ErrorActionPreference = "Stop"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Cuff for PC needs uv. Install it with:  winget install --id=astral-sh.uv -e"
    exit 1
}
Set-Location $PSScriptRoot
uv sync
& (Join-Path $PSScriptRoot "install-autostart.ps1")
if (-not $NoFirewall) {
    $rules = Get-NetFirewallRule -DisplayName "Cuff for PC*" -ErrorAction SilentlyContinue
    if (-not $rules) {
        Write-Host "Adding the firewall rules (Windows will ask for admin)..."
        Start-Process powershell.exe -Verb RunAs -Wait -ArgumentList `
            "-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $PSScriptRoot 'firewall.ps1')`""
    }
}
Stop-ScheduledTask -TaskName "Cuff for PC" -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName "Cuff for PC"
Write-Host "Cuff for PC is starting; its log is $env:LOCALAPPDATA\cuff-pc\server.log"
# Pair the headset, once the app is up (the first start downloads the speech model).
$up = $false
foreach ($i in 1..120) {
    try { Invoke-RestMethod "http://127.0.0.1:8765/health" -TimeoutSec 2 | Out-Null; $up = $true; break } catch { Start-Sleep 2 }
}
if ($up) { & (Join-Path $PSScriptRoot "pair.ps1") } else { Write-Host "Still starting; pair later with pair.ps1." }
