<#
.SYNOPSIS
    Put back the native sensor artifacts a PyInstaller rebuild does not produce.

.DESCRIPTION
    PyInstaller bundles ONLY the Python layer. Two things AT-Field needs are
    built or fetched separately and live next to the exe:

      * LibreHardwareMonitorLib.dll and friends  (scripts\fetch_lhm.ps1)
      * atfield-sensors.exe, the C# helper       (scripts\build_helper.ps1)

    `pyinstaller --clean` deletes dist\ before building, so a rebuild silently
    removes both. Installing that bundle then produces a service that STARTS
    and reports healthy while publishing only 2 thermal signals instead of 5 --
    no CPU package temperature, which is the signal `cpu-pkg-hot` fires on.
    Measured on Chronos 2026-09-01: the installer printed "LHM DLLs: not found
    nearby" and "Sensors: helper not found" as ordinary lines and carried on.

    A watchdog that runs blind is worse than one that is out of date, so this
    copies the missing native artifacts from the most recent atfield.bak-*
    into the live install and restarts the service. It copies ONLY files the
    new install lacks, so the freshly built Python layer is never overwritten.
#>
param(
    [string]$InstallRoot = 'C:\Program Files\AT-Field',
    [string]$ServiceName = 'ATFieldWatchdog',
    [Parameter(Mandatory = $true)][string]$LogPath
)

Start-Transcript -Path $LogPath -Force | Out-Null
try {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator)) { throw "not elevated" }
    "elevated: yes"

    $target = Join-Path $InstallRoot 'atfield'
    $bak = Get-ChildItem $InstallRoot -Directory -Filter 'atfield.bak-*' |
           Sort-Object Name -Descending | Select-Object -First 1
    if (-not $bak) { throw "no atfield.bak-* to source native artifacts from" }
    "source: $($bak.Name)"

    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
    Get-Process atfield-service, atfield-sensors -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2

    $copied = 0
    Get-ChildItem $bak.FullName -File | ForEach-Object {
        $dst = Join-Path $target $_.Name
        if (-not (Test-Path $dst)) {
            Copy-Item $_.FullName $dst -Force
            $copied++
            "  + $($_.Name)"
        }
    }
    "copied $copied native artifact(s) the rebuild did not produce"

    Start-Service -Name $ServiceName -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 25; $i++) {
        $s = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
        if ($s -and $s.Status -eq 'Running') { break }
        Start-Sleep -Seconds 1
    }
    $s = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    "service: " + $(if ($s) { $s.Status } else { 'MISSING' })
    "build:   " + (Get-Item (Join-Path $target 'atfield-service.exe')).LastWriteTime
    if ($s -and $s.Status -eq 'Running') { "RESTORE OK" } else { "RESTORE FAILED" }
}
catch { "RESTORE ERROR: $($_.Exception.Message)" }
finally { Stop-Transcript | Out-Null }
