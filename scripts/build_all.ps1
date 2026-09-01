<#
.SYNOPSIS
    Build a COMPLETE AT-Field bundle: Python layer + native sensor stack.

.DESCRIPTION
    THE BUG THIS EXISTS TO PREVENT, measured on Chronos 2026-09-01.

    `pyinstaller --clean` deletes dist\ and rebuilds it. But PyInstaller only
    produces the PYTHON layer. Two things AT-Field cannot read a temperature
    without are produced separately and live next to the exe:

        LibreHardwareMonitorLib.dll + friends   scripts\fetch_lhm.ps1
        atfield-sensors.exe (C# helper)         scripts\build_helper.ps1

    So a plain `pyinstaller --clean` silently STRIPS the sensor stack. Deploying
    that bundle produces a service that starts, reports healthy, and publishes
    2 thermal signals instead of 5 -- no CPU package temperature, which is the
    only signal `cpu-pkg-hot` can fire on. The installer prints "LHM DLLs: not
    found nearby" as an ordinary line and carries on. Nothing fails. The machine
    is simply no longer protected.

    This script does all three steps and then REFUSES to finish if the bundle is
    missing any of them, so an incomplete bundle can never reach a machine.

.PARAMETER SkipPyInstaller
    Reuse the existing dist\atfield Python layer and only top up the native
    artifacts. Useful when iterating on packaging.
#>
param(
    [switch]$SkipPyInstaller,
    [string]$Python = 'C:\Users\admin\Desktop\RESEARCH\at-field\.venv\Scripts\python.exe'
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$dist = Join-Path $repo 'dist\atfield'
Push-Location $repo
try {
    if (-not $SkipPyInstaller) {
        Write-Host "[1/4] PyInstaller ..."
        & $Python -m PyInstaller --noconfirm --clean packaging\pyinstaller\atfield.spec
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }
    } else {
        Write-Host "[1/4] PyInstaller SKIPPED"
    }

    Write-Host "[2/4] LibreHardwareMonitor DLLs ..."
    if (-not (Test-Path (Join-Path $dist 'LibreHardwareMonitorLib.dll'))) {
        & (Join-Path $PSScriptRoot 'fetch_lhm.ps1') -Destination $dist
    } else {
        Write-Host "      already present"
    }

    Write-Host "[3/4] C# sensor helper ..."
    if (-not (Test-Path (Join-Path $dist 'atfield-sensors.exe'))) {
        & (Join-Path $PSScriptRoot 'build_helper.ps1') -OutDir $dist
    } else {
        Write-Host "      already present"
    }

    Write-Host "[4/4] completeness gate ..."
    # An incomplete bundle must not be installable. These three are exactly the
    # pieces whose absence produces a service that LOOKS healthy and is blind.
    $required = @(
        'atfield-service.exe',          # the Python layer
        'LibreHardwareMonitorLib.dll',  # CPU package + VRAM junction come from here
        'atfield-sensors.exe'           # the helper that reads them
    )
    $missing = $required | Where-Object { -not (Test-Path (Join-Path $dist $_)) }
    if ($missing) {
        throw ("INCOMPLETE BUNDLE -- refusing. Missing: " + ($missing -join ', ') +
               ". Installing this would yield a watchdog with no CPU temperature.")
    }
    foreach ($r in $required) {
        "      OK  {0,-30} {1}" -f $r, (Get-Item (Join-Path $dist $r)).LastWriteTime
    }
    Write-Host "BUILD COMPLETE: $dist"
}
finally { Pop-Location }
