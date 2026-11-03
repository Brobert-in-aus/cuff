# Build Cuff for PC's Windows installer: installer\output\CuffForPC-Setup-<version>.exe, and
# its SHA-256 next to it (CuffForPC-Setup-<version>.exe.sha256, as sha256sum writes it).
#   powershell -ExecutionPolicy Bypass -File pc-server\installer\build.ps1 [-Owner <github owner>] [-NoCompile]
#
# 1. Stages the public copy of this repo's current commit (tools/publish.sh --stage: no
#    private files, checked), with a VERSION file (git describe).
# 2. Adds uv.exe (the one on PATH) and cuff.ico (drawn by app.py's icon_image).
# 3. Compiles cuff-pc.iss with Inno Setup 6 (winget install JRSoftware.InnoSetup), unless
#    -NoCompile.
# The version: a release is a commit tagged vX.Y.Z (git describe gives exactly that), and
# anything else is refused, so an untagged or tag-plus-commits build can't be released by
# mistake. Test builds:
#   -Version vX.Y.Z-rcN   a release candidate (older than vX.Y.Z to an installed copy)
#   -AllowDev             any version at all (git describe's own, or -Version's)
# For an update: publish a GitHub release on the public repo, tagged with the version, with
# both the .exe and the .exe.sha256 attached. Installed copies offer it within a day
# (updates.py), and refuse it without the .sha256.
param([string]$Owner = "Brobert-in-aus", [string]$Version = "", [switch]$NoCompile, [switch]$AllowDev)
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$root = (git -C $here rev-parse --show-toplevel).Trim()
$version = $Version
if (-not $version) { $version = (git -C $root describe --tags --always).Trim() }
$release = $version -cmatch '^v\d+\.\d+\.\d+$'
$candidate = $Version -and ($version -cmatch '^v\d+\.\d+\.\d+-rc\d+$')
if (-not ($release -or $candidate -or $AllowDev)) {
    throw ("Version '$version' isn't a release (vX.Y.Z: tag the commit) or, with -Version, a release " +
           "candidate (vX.Y.Z-rcN). For a test build of anything else, add -AllowDev.")
}
$stage = Join-Path $here "stage"
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory $stage | Out-Null

# publish.sh needs a POSIX sh: the one on PATH, else Git for Windows' own.
$sh = (Get-Command sh -ErrorAction SilentlyContinue).Source
if (-not $sh) {
    $gitRoot = Split-Path (Split-Path (Get-Command git).Source)
    $sh = Join-Path $gitRoot "bin\sh.exe"
    if (-not (Test-Path $sh)) { throw "No sh found: install Git for Windows, or put sh on PATH" }
}
Push-Location $root
try {
    & $sh tools/publish.sh --stage $stage.Replace("\", "/") $Owner
    if ($LASTEXITCODE -ne 0) { throw "staging failed (commit your changes, and check the private-file report)" }
} finally { Pop-Location }
# (The version checked above, whatever publish.sh's own git describe said meanwhile.)
Set-Content -NoNewline -Encoding ascii (Join-Path $stage "VERSION") $version

$uv = (Get-Command uv).Source
Copy-Item $uv (Join-Path $stage "uv.exe")
Push-Location (Join-Path $root "pc-server")
try {
    uv run --no-sync python -c "import app; app.icon_image(True).save(r'$stage\cuff.ico', sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])"
} finally { Pop-Location }
# Drawing the icon imports app.py, so it needs pc-server's packages already synced (--no-sync).
if ($LASTEXITCODE -ne 0 -or -not (Test-Path (Join-Path $stage "cuff.ico"))) {
    throw "Couldn't draw the icon: run Cuff for PC once from this checkout (uv sync in pc-server), or set UV_PROJECT_ENVIRONMENT to a synced .venv"
}
Write-Host "Staged Cuff for PC $version in $stage"

if ($NoCompile) { exit 0 }
$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
          "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 isn't installed: winget install --id JRSoftware.InnoSetup -e" }
& $iscc "/DVersion=$version" "/DStage=$stage" (Join-Path $here "cuff-pc.iss")
if ($LASTEXITCODE -ne 0) { throw "ISCC failed" }
# The checksum updates.py checks the download against: "<sha256>  <file name>", ASCII.
$name = "CuffForPC-Setup-$version.exe"
$exe = Join-Path (Join-Path $here "output") $name
$hash = (Get-FileHash -Algorithm SHA256 $exe).Hash.ToLowerInvariant()
[IO.File]::WriteAllText("$exe.sha256", "$hash  $name`n", [Text.Encoding]::ASCII)
Get-ChildItem (Join-Path $here "output") | Where-Object { $_.Name -eq $name -or $_.Name -eq "$name.sha256" }
