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
    LHM tag to fetch. Default: v0.9.6 (released 2026-02-14). Picked
    over v0.9.4 because it:
      * Adds the NvAPI workaround for the RTX 5090 memory-junction
        sensor that NVIDIA removed from the public NVML / nvidia-smi
        surface in the 50-series driver (LHM PR #1878 / issue #1686).
      * Adds NVIDIA GPU core voltage as a first-class signal (PR #2175).
      * Adds foundational support for MSI B840/B850, X870(E), and Z890
        motherboards (PR #2216), plus ASUS Astral 50-series GPUs.
      * Adds Thermal Grizzly WireView Pro 2 + Arctic Fan Controller
        coverage for users with those PSU/fan monitors inline.

.PARAMETER Force
    Overwrite an existing LibreHardwareMonitor.exe at the destination.

.EXAMPLE
    pwsh scripts/fetch_lhm.ps1
    pwsh scripts/fetch_lhm.ps1 -Destination dist/atfield -Version v0.9.6
#>

[CmdletBinding()]
param(
    [string]$Destination = (Join-Path (Get-Location) "dist/atfield"),
    [string]$Version     = "v0.9.6",
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

# LHM publishes two zips per release as of v0.9.5+:
#   * LibreHardwareMonitor.zip           -- .NET Framework 4.7.2 build
#                                          (in-box on Windows 10 1803+ / 11)
#   * LibreHardwareMonitor.NET.10.zip    -- .NET 10 build, smaller but
#                                          requires the .NET 10 runtime
# We grab the .NET Framework build for the same in-box compatibility
# reasoning we used in v0.2 -- it runs on stock Windows 10/11 without
# a separate runtime install.
#
# (Pre-0.9.5 the file was named LibreHardwareMonitor-net472.zip; the
# rename happened upstream in v0.9.5.)
$asset = "LibreHardwareMonitor.zip"
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

# Note: we used to ship a pre-baked LibreHardwareMonitor.config here
# (in v0.2.0). That approach broke between LHM v0.9.4 and v0.9.6 because
# LHM rewrites the file from its in-memory settings on first boot,
# overwriting our keys before the supervisor could connect.
#
# The robust pattern lives in atfield.lhm_config: the supervisor
# re-asserts the config on every spawn, merging our required keys
# (web server enabled, port = 8085, start minimized, no auto-update)
# into whatever LHM / the user left on disk. So we don't ship a
# config file at build time anymore -- the supervisor materializes
# one on first run.

Write-Host ""
Write-Host "Vendored LHM $Version into $Destination"
Write-Host "  $(Resolve-Path $lhmExe)"
Write-Host ""
Write-Host "Note: LHM is MPL 2.0. Vendor unmodified. See vendor/lhm/README.md."
