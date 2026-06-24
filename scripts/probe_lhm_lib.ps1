<#
.SYNOPSIS
    Probe hardware sensors directly through the bundled
    LibreHardwareMonitorLib.dll -- NO GUI, NO web server, NO http.sys,
    NO URL ACL. This is the transport AT-Field is moving to.

.DESCRIPTION
    LibreHardwareMonitorLib is a .NET *Framework* 4.7.2 assembly (it calls
    Framework-only APIs such as Mutex(..., MutexSecurity)). It therefore
    must be hosted by Windows PowerShell 5.1 (.NET Framework), NOT
    PowerShell 7 (.NET Core), which is why running it under `pwsh` throws
    "Method not found: Mutex..ctor(... MutexSecurity)". This script
    auto-relaunches itself under powershell.exe if started from Core.

    Driver-backed sensors (CPU package temp via MSR) require elevation;
    GPU memory-junction temp does not. Run elevated to see everything --
    that mirrors how the AT-Field service (LocalSystem) will read them.

.EXAMPLE
    # From any shell, elevated, to confirm CPU package temp populates:
    powershell.exe -ExecutionPolicy Bypass -File scripts\probe_lhm_lib.ps1
#>

# Re-launch under Windows PowerShell 5.1 if we're on PowerShell 7 (Core).
if ($PSVersionTable.PSEdition -eq 'Core') {
    Write-Host "PowerShell Core detected; relaunching under Windows PowerShell 5.1 (.NET Framework)..." -ForegroundColor Yellow
    & "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath @args
    exit $LASTEXITCODE
}

$ErrorActionPreference = 'Stop'

$dllDir = Join-Path (Split-Path $PSScriptRoot -Parent) 'dist\atfield'
$dll = Join-Path $dllDir 'LibreHardwareMonitorLib.dll'
if (-not (Test-Path $dll)) {
    Write-Host "ERROR: LibreHardwareMonitorLib.dll not found at $dll" -ForegroundColor Red
    exit 1
}

# Elevation status -- driver sensors (CPU MSR) need admin/SYSTEM.
$elevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Write-Host ("Runtime: Windows PowerShell {0} (.NET Framework) | Elevated: {1}" -f `
    $PSVersionTable.PSVersion, $elevated) -ForegroundColor Cyan

Set-Location $dllDir
[Reflection.Assembly]::LoadFrom($dll) | Out-Null

$c = New-Object LibreHardwareMonitor.Hardware.Computer
$c.IsCpuEnabled = $true
$c.IsGpuEnabled = $true
$c.IsMotherboardEnabled = $true
$c.IsMemoryEnabled = $true

try {
    $c.Open()
} catch {
    Write-Host ("Computer.Open() FAILED: " + $_.Exception.Message) -ForegroundColor Red
    Write-Host "If this is a driver/Defender block, CPU package temp won't be readable by any method." -ForegroundColor Red
    exit 2
}

$cpuTemp = $null
foreach ($hw in $c.Hardware) {
    $hw.Update()
    foreach ($sh in $hw.SubHardware) { $sh.Update() }
    Write-Host ("HW [{0}] {1}" -f $hw.HardwareType, $hw.Name) -ForegroundColor Green
    $sensors = @($hw.Sensors) + @($hw.SubHardware | ForEach-Object { $_.Sensors })
    foreach ($s in $sensors) {
        $t = "$($s.SensorType)"
        if ($t -eq 'Temperature' -or $t -eq 'Voltage') {
            "   {0,-12} {1,-26} = {2}" -f $t, $s.Name, $s.Value
            if ($t -eq 'Temperature' -and $s.Name -match 'Tctl|Tdie|Package|CPU') {
                if ($null -ne $s.Value -and $s.Value -gt 0) { $cpuTemp = $s.Value }
            }
        }
    }
}
$c.Close()

Write-Host ""
if ($cpuTemp) {
    Write-Host ("RESULT: CPU package temp = {0} C  -- driver works; the web-server layer was the whole problem." -f $cpuTemp) -ForegroundColor Green
} elseif ($elevated) {
    Write-Host "RESULT: CPU package temp still 0/null even elevated -- the kernel driver is blocked (Defender/PawnIO). GPU junction temp still works without it." -ForegroundColor Yellow
} else {
    Write-Host "RESULT: CPU package temp 0/null -- expected when NOT elevated. Re-run elevated to confirm the driver path." -ForegroundColor Yellow
}
