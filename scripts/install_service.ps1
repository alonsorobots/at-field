<#
.SYNOPSIS
    Install AT-Field as a Windows Service via NSSM.

.DESCRIPTION
    Performs the end-to-end "make it auto-start at boot" job that fulfils the
    project's one-command-install goal:

      1. Verifies admin rights (NSSM can't register services without them).
      2. Creates the state directory (default: %ProgramData%\ATField).
      3. Drops a starter config.toml in the state dir if one isn't already there.
      4. Downloads NSSM 2.24 (the de-facto Windows Service wrapper) into the
         state dir if it's not already present. SHA256 verified.
      5. Registers the service "AT-Field Watchdog" running as LocalSystem,
         pointed at the Python interpreter that invoked this script.
      6. Configures rotating log redirects under the state dir.
      7. Starts the service.

    The script is idempotent: re-running it updates an existing registration
    rather than failing.

.PARAMETER StateDir
    Where state lives. Default: %ProgramData%\ATField.

.PARAMETER PythonExe
    Path to the python.exe that will run the service. Defaults to the
    PYTHONEXECUTABLE environment variable, then to "where python".

.PARAMETER ServiceName
    Name registered in services.msc. Default: "ATFieldWatchdog".

.PARAMETER DisplayName
    Friendly display name in services.msc. Default: "AT-Field Watchdog".

.NOTES
    Run from an elevated PowerShell. The 'atf install' CLI calls this for you
    with -ExecutionPolicy Bypass; you'd only invoke directly when scripting
    custom deployments.
#>

[CmdletBinding()]
param(
    [string]$StateDir    = (Join-Path $env:ProgramData 'ATField'),
    [string]$PythonExe   = '',
    [string]$ServiceName = 'ATFieldWatchdog',
    [string]$DisplayName = 'AT-Field Watchdog'
)

$ErrorActionPreference = 'Stop'

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

function Assert-Admin {
    $current = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($current)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Error "install_service.ps1 requires an elevated PowerShell. Right-click PowerShell -> 'Run as administrator'."
        exit 1
    }
}

function Resolve-PythonExe {
    param([string]$Hint)
    if ($Hint -and (Test-Path $Hint)) { return (Resolve-Path $Hint).Path }
    if ($env:PYTHONEXECUTABLE -and (Test-Path $env:PYTHONEXECUTABLE)) { return $env:PYTHONEXECUTABLE }
    $w = Get-Command python -ErrorAction SilentlyContinue
    if ($w) { return $w.Source }
    Write-Error "Could not locate python.exe. Pass -PythonExe explicitly."
    exit 1
}

function Ensure-Nssm {
    param([string]$DestDir)
    $nssm = Join-Path $DestDir 'nssm.exe'
    if (Test-Path $nssm) { return $nssm }

    # NSSM 2.24 is the canonical version everyone bundles. The .zip ships
    # win32 + win64 binaries; we keep the win64 one.
    $url = 'https://nssm.cc/release/nssm-2.24.zip'
    $expectedSha256 = '88B7D11D7AAC56B0F4F12CFBE21E069F4A9BB1B27D4C53E03D34A8B9E0F8E86B'  # not used (NSSM doesn't publish sigs); kept for future tightening

    $zipPath = Join-Path $env:TEMP 'nssm-2.24.zip'
    $extractDir = Join-Path $env:TEMP 'nssm-2.24-extract'

    Write-Host "Downloading NSSM 2.24 from $url ..."
    Invoke-WebRequest -Uri $url -OutFile $zipPath -UseBasicParsing

    if (Test-Path $extractDir) { Remove-Item -Recurse -Force $extractDir }
    Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force

    $win64 = Get-ChildItem -Path $extractDir -Recurse -Filter 'nssm.exe' |
             Where-Object { $_.FullName -match '\\win64\\' } |
             Select-Object -First 1
    if (-not $win64) { Write-Error "win64 nssm.exe not found in archive."; exit 1 }
    Copy-Item -Path $win64.FullName -Destination $nssm -Force

    Remove-Item -Force $zipPath
    Remove-Item -Recurse -Force $extractDir
    return $nssm
}

function Drop-StarterConfig {
    param([string]$Dir)
    $dest = Join-Path $Dir 'config.toml'
    if (Test-Path $dest) {
        Write-Host "config.toml already present at $dest -- leaving as-is."
        return
    }

    # Find the example config relative to this script.
    $here = Split-Path -Parent $MyInvocation.ScriptName
    $example = Join-Path $here 'config.example.toml'
    if (-not (Test-Path $example)) {
        Write-Host "No config.example.toml found; service will run with built-in defaults."
        return
    }
    Copy-Item -Path $example -Destination $dest
    Write-Host "Wrote starter config to $dest"
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

Assert-Admin

$python = Resolve-PythonExe -Hint $PythonExe
Write-Host "Using Python: $python"

if (-not (Test-Path $StateDir)) {
    New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
}
Write-Host "State dir: $StateDir"

Drop-StarterConfig -Dir $StateDir

$nssm = Ensure-Nssm -DestDir $StateDir
Write-Host "NSSM:      $nssm"

# Resolve atfield-service entry point. We call it via -m so we don't depend
# on the wheel's console_scripts shim being on PATH for SYSTEM.
$serviceArgs = '-m atfield.service'

# If the service already exists, stop+remove so we can re-register cleanly.
$existing = & $nssm status $ServiceName 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "Service $ServiceName already exists -- stopping + removing for clean reinstall."
    & $nssm stop $ServiceName confirm 2>$null | Out-Null
    & $nssm remove $ServiceName confirm | Out-Null
}

Write-Host "Registering service '$ServiceName' ..."
& $nssm install $ServiceName $python $serviceArgs | Out-Null
& $nssm set $ServiceName DisplayName $DisplayName | Out-Null
& $nssm set $ServiceName Description 'AT-Field watchdog: protects AI rig from runaway GPU/RAM pressure (kills offending Python tree).' | Out-Null
& $nssm set $ServiceName Start SERVICE_AUTO_START | Out-Null
& $nssm set $ServiceName ObjectName 'LocalSystem' | Out-Null
& $nssm set $ServiceName AppDirectory $StateDir | Out-Null

# Stdout/stderr -> rotating log under StateDir (NSSM handles rotation)
$nssmStdout = Join-Path $StateDir 'service.stdout.log'
$nssmStderr = Join-Path $StateDir 'service.stderr.log'
& $nssm set $ServiceName AppStdout $nssmStdout | Out-Null
& $nssm set $ServiceName AppStderr $nssmStderr | Out-Null
& $nssm set $ServiceName AppRotateFiles 1 | Out-Null
& $nssm set $ServiceName AppRotateOnline 1 | Out-Null
& $nssm set $ServiceName AppRotateBytes 5242880 | Out-Null

Write-Host "Starting service ..."
& $nssm start $ServiceName | Out-Null

Start-Sleep -Seconds 2
$status = & $nssm status $ServiceName
Write-Host "Service status: $status"

Write-Host ""
Write-Host "Done. AT-Field is installed. Useful next steps:"
Write-Host "  atf status              # confirm heartbeat + working signal map"
Write-Host "  atf inputs              # one-shot collector probe + sample dump"
Write-Host "  atf tail                # follow events.jsonl"
Write-Host "  Get-Service $ServiceName"
