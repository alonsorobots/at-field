<#
.SYNOPSIS
    Compile the AT-Field headless sensor helper (helper\AtfieldSensors.cs)
    into dist\atfield\atfield-sensors.exe, next to the bundled
    LibreHardwareMonitorLib.dll so its references resolve at runtime.

.DESCRIPTION
    Uses the in-box .NET Framework C# compiler (csc.exe) -- no .NET SDK
    required, present on every Windows 10/11. The helper targets .NET
    Framework 4.7.2 and is written to C# 5 so this compiler can build it.

.PARAMETER OutDir
    Directory containing LibreHardwareMonitorLib.dll / Newtonsoft.Json.dll
    and where the exe is written. Defaults to <repo>\dist\atfield.
#>
param(
    [string]$OutDir
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
if (-not $OutDir) { $OutDir = Join-Path $repo 'dist\atfield' }

$src = Join-Path $repo 'helper\AtfieldSensors.cs'
$exe = Join-Path $OutDir 'atfield-sensors.exe'
$lhm = Join-Path $OutDir 'LibreHardwareMonitorLib.dll'
$json = Join-Path $OutDir 'Newtonsoft.Json.dll'

foreach ($p in @($src, $lhm, $json)) {
    if (-not (Test-Path $p)) { throw "required input not found: $p" }
}

$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path $csc)) {
    throw "in-box C# compiler not found at $csc (is .NET Framework 4.x installed?)"
}

Write-Host "Compiling $src -> $exe" -ForegroundColor Cyan
& $csc /nologo /target:exe /platform:x64 "/out:$exe" "/reference:$lhm" "/reference:$json" $src
if ($LASTEXITCODE -ne 0) { throw "csc.exe failed with exit code $LASTEXITCODE" }

Write-Host ("Built {0} ({1:N0} bytes)" -f $exe, (Get-Item $exe).Length) -ForegroundColor Green
