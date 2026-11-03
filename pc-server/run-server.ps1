# Start Cuff for PC, logging to %LOCALAPPDATA%\cuff-pc\server.log.
# Run at logon by the "Cuff for PC" scheduled task (see install-autostart.ps1), by the Start
# menu shortcut, or by hand: powershell -File pc-server\run-server.ps1
$ErrorActionPreference = "Stop"
$logDir = Join-Path $env:LOCALAPPDATA "cuff-pc"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir "server.log"
# (Not while a running copy holds it open: then this run only opens that copy's window.)
try { if ((Test-Path $log) -and (Get-Item $log).Length -gt 5MB) { Move-Item -Force $log "$log.1" } } catch {}

$env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"
Set-Location $PSScriptRoot
$appRoot = Split-Path $PSScriptRoot
# An installed copy (a VERSION file, no .git) keeps what it downloads in its own folder, so
# uninstalling removes it: uv's cache, the Python uv fetches, and the speech model
# (cache\uv, cache\python, cache\huggingface). A checkout uses uv's and Hugging Face's usual
# places.
if ((Test-Path (Join-Path $appRoot "VERSION")) -and -not (Test-Path (Join-Path $appRoot ".git"))) {
    $cache = Join-Path $appRoot "cache"
    $env:UV_CACHE_DIR = Join-Path $cache "uv"
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $cache "python"
    $env:HF_HOME = Join-Path $cache "huggingface"
}
# The installer carries uv next to pc-server; a checkout uses the one on PATH.
$uv = Join-Path $appRoot "uv.exe"
if (-not (Test-Path $uv)) { $uv = "uv" }
# The packages, the first time and after an update: bootstrap.py shows a window with progress
# when it's slow, and does nothing when they already match uv.lock.
# (bootstrap.py logs to setup.log; uv's own messages on stderr mustn't count as failures here.)
$ErrorActionPreference = "Continue"
$setupLog = Join-Path $logDir "setup.log"
& $uv run --no-project --python 3.13 bootstrap.py
if ($LASTEXITCODE -ne 0) { "run-server: setup failed ($LASTEXITCODE)" | Out-File -Append -Encoding utf8 $setupLog; exit 1 }
# app.py: the server with its tray icon and window (it opens the window if Cuff for PC is
# already running). server.py writes the log itself; PowerShell 5.1 holds back redirected
# native output.
& $uv run --no-sync app.py --log $log
