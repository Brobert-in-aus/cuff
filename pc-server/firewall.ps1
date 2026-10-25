# Firewall rules for Cuff for PC (run as admin; install.ps1 does this for you).
#   Home networks (Private): TCP 8765, the headset's dictation and stats requests.
#   The Steam Frame's dongle link: Windows classes it as a Public network, so it's opened only
#   for the Frame's own subnet there (10.35.78.0/24), not on every public network.
# The headset hears the app's UDP announcements (port 8766) without an inbound rule here.
$ErrorActionPreference = "Stop"
Remove-NetFirewallRule -DisplayName "Cuff for PC*" -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName "Cuff for PC (home network)" -Direction Inbound -Protocol TCP `
    -LocalPort 8765 -Profile Private -Action Allow | Out-Null
New-NetFirewallRule -DisplayName "Cuff for PC (Steam Frame dongle)" -Direction Inbound -Protocol TCP `
    -LocalPort 8765 -Profile Public -RemoteAddress 10.35.78.0/24 -Action Allow | Out-Null
Write-Host "Firewall rules added."
