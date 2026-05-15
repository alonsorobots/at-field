<#
.SYNOPSIS
    Vendor LibreHardwareMonitor (LHM) into the AT-Field bundle directory.

.DESCRIPTION
    Downloads a pinned LibreHardwareMonitor release from GitHub and
    extracts the contents into the target directory so that
    `LibreHardwareMonitor.exe` lives next to AT-Field's own binaries.

    AT-Field's lhm_supervisor.find_lhm_executable() looks first next to
    the running atfield-service.exe (Path(sys.executable).parent), which
    in the installed product is `<InstallDir>\resources\atfield\`. By
    staging LHM there at build time, the installed AT-Field service
    finds and supervises it automatically -- no separate download or
    UAC step for the end user.

    LHM is licensed under the Mozilla Public License 2.0. We vendor it
    UNMODIFIED, as the MPL requires; see vendor/lhm/README.md for
    redistribution notes.

.PARAMETER Destination
    Where to extract LHM. Default: dist/atfield/ (the PyInstaller bundle
    dir, where atfield-service.exe lives).

.PARAMETER Version
    LHM tag to fetch. Default: v0.9.4 (stable as of 2026-Q1, ships the
    --server CLI we depend on).

.PARAMETER Force
    Overwrite an existing LibreHardwareMonitor.exe at the destination.

.EXAMPLE
    pwsh scripts/fetch_lhm.ps1
    pwsh scripts/fetch_lhm.ps1 -Destination dist/atfield -Version v0.9.4
#>

[CmdletBinding()]
param(
    [string]$Destination = (Join-Path (Get-Location) "dist/atfield"),
    [string]$Version     = "v0.9.4",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$lhmExe = Join-Path $Destination "LibreHardwareMonitor.exe"
if ((Test-Path $lhmExe) -and -not $Force.IsPresent) {
    Write-Host "LibreHardwareMonitor already vendored at $lhmExe (use -Force to re-download)."
    exit 0
}

if (-not (Test-Path $Destination)) {
    Write-Host "Creating destination directory: $Destination"
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
}

# LHM publishes a `LibreHardwareMonitor-net472.zip` under each release
# (the .NET 4.7.2 build is the one with the lowest install footprint --
# .NET Framework 4.7.2 is in-box on Windows 10 1803+ / 11). The CLI
# `--server`/`--port` flags we depend on land in v0.9.x.
$asset = "LibreHardwareMonitor-net472.zip"
$url = "https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/download/$Version/$asset"

$zipPath = Join-Path $env:TEMP "lhm-$Version.zip"
$extractDir = Join-Path $env:TEMP "lhm-$Version-extract"

Write-Host "Downloading LHM $Version from $url ..."
Invoke-WebRequest -Uri $url -OutFile $zipPath -UseBasicParsing

if (Test-Path $extractDir) { Remove-Item -Recurse -Force $extractDir }
Write-Host "Extracting to $extractDir ..."
Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force

# LHM ships at the zip root. Copy every file (exe, DLLs, config) flat
# into the destination -- LHM expects its sibling DLLs in the same dir.
Get-ChildItem -Path $extractDir -File | Copy-Item -Destination $Destination -Force
Get-ChildItem -Path $extractDir -Directory | Copy-Item -Destination $Destination -Recurse -Force

Remove-Item -Force $zipPath
Remove-Item -Recurse -Force $extractDir

if (-not (Test-Path $lhmExe)) {
    Write-Error "LibreHardwareMonitor.exe missing at $lhmExe after extraction. Aborting."
    exit 1
}

# Drop our pre-baked LibreHardwareMonitor.config alongside the binary so
# the web server starts on the AT-Field-expected port (8085) without
# the user clicking through LHM's Options menu. LHM rewrites this file
# when the user changes settings, but our defaults are what they get on
# first boot. A user-tweaked file is preserved (we don't overwrite if
# present, unless -Force was supplied).
$configSrc = Join-Path $PSScriptRoot "../vendor/lhm/LibreHardwareMonitor.config"
$configDst = Join-Path $Destination "LibreHardwareMonitor.config"
if ((Test-Path $configSrc) -and (-not (Test-Path $configDst) -or $Force.IsPresent)) {
    Copy-Item -Path $configSrc -Destination $configDst -Force
    Write-Host "Wrote AT-Field LHM config: $configDst"
}

Write-Host ""
Write-Host "Vendored LHM $Version into $Destination"
Write-Host "  $(Resolve-Path $lhmExe)"
Write-Host ""
Write-Host "Note: LHM is MPL 2.0. Vendor unmodified. See vendor/lhm/README.md."
