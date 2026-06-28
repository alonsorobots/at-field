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
    Ignored when -BundledExe is supplied.

.PARAMETER BundledExe
    Path to a PyInstaller-built atfield-service.exe (a fully self-contained
    binary; see packaging/pyinstaller/atfield.spec). When supplied, NSSM
    runs this directly instead of "python -m atfield.service" -- no Python
    install required on the target machine. The rest of the dist/atfield/
    folder (containing _internal/ and atf.exe) is expected to live next to
    the bundled exe.

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
    [string]$BundledExe  = '',
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

# Defense-in-depth: kill any lingering atfield-* processes before we touch the
# service. Mirrors Stop-AtfieldProcesses in uninstall_service.ps1. The NSIS
# pre-uninstall hook already calls uninstall_service.ps1 during upgrades, so
# this should normally be a no-op -- but if the uninstall path was skipped
# (manual rerun, atf install from CLI, NSIS racing the service shutdown), the
# old service.exe can still be holding _internal/atfield/*.pyc open. Replacing
# those files happens BEFORE this script runs (NSIS sequence: files ->
# postinstall hook); but if old processes are alive at THAT moment, we get a
# stale-bytecode mismatch (exe at v0.4.4, bytecode at v0.4.3, /health reports
# the old version). Killing them here is too late to fix the *bytecode* race,
# but is essential to keep nssm install/start from racing against a still-
# running ghost service.
function Stop-AtfieldProcesses {
    param([int]$TimeoutMs = 10000)
    $targets = @('atfield-service', 'atfield-sensors', 'atf')
    $deadline = (Get-Date).AddMilliseconds($TimeoutMs)
    while ((Get-Date) -lt $deadline) {
        $alive = Get-Process -Name $targets -ErrorAction SilentlyContinue
        if (-not $alive) { return }
        foreach ($p in $alive) {
            try { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } catch {}
        }
        Start-Sleep -Milliseconds 250
    }
}

# Poll for the service to actually reach Running after `nssm start`. NSSM's
# start command returns 0 the moment SCM accepts the start request, NOT when
# the service has finished its bootstrap -- and on a freshly-extracted
# PyInstaller bundle (cold disk, Defender scanning .pyd files, etc.) that
# bootstrap can take a few seconds. The old code did `Start-Sleep 2; nssm
# status` and accepted whatever it got, which on slow first-launch produced
# Stopped/Automatic and a service that never came up without manual nudging.
function Wait-ServiceRunning {
    param(
        [Parameter(Mandatory)][string]$Name,
        [int]$TimeoutSec = 30
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
        if ($svc -and $svc.Status -eq 'Running') { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
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

    # Prefer a copy vendored next to this script. The Tauri/PyInstaller bundle
    # ships nssm.exe alongside install_service.ps1 (and the dev checkout keeps
    # it in scripts\vendor\), so a normal install needs ZERO network access --
    # nssm.cc is frequently flaky (HTTP 503) and download-at-install is the
    # single most fragile step of the whole setup. We just copy it into the
    # state dir and move on.
    $bundledCandidates = @(
        (Join-Path $PSScriptRoot 'nssm.exe'),
        (Join-Path $PSScriptRoot 'vendor\nssm.exe')
    )
    foreach ($cand in $bundledCandidates) {
        if (Test-Path $cand) {
            Write-Host "Using bundled NSSM: $cand"
            Copy-Item -Path $cand -Destination $nssm -Force
            return $nssm
        }
    }

    # Fallback only when no bundled copy is present (e.g. a bare `atf install`
    # from a pip checkout without the vendored binary).
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
    $here = $PSScriptRoot
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

# Defensive cleanup: kill any stale atfield-service / atfield-sensors / atf
# processes left behind by a previous run (failed install, upgrade where the
# uninstall hook didn't fully wait, dev `atf run` foreground). Safe no-op on
# a clean install -- nothing to kill.
Stop-AtfieldProcesses

# Decide which mode we're in: bundled (PyInstaller exe shipped with the
# Tauri installer) or pip-installed (developer / `pip install atfield` path).
# Bundled wins when supplied; pip path is the fallback so existing users'
# `atf install` keeps working unchanged.
$bundledMode = $false
$bundledResolved = ''
if ($BundledExe) {
    if (-not (Test-Path $BundledExe)) {
        Write-Error "BundledExe '$BundledExe' does not exist."
        exit 1
    }
    $bundledResolved = (Resolve-Path $BundledExe).Path
    $bundledMode = $true
    Write-Host "Bundled binary: $bundledResolved"
} else {
    $python = Resolve-PythonExe -Hint $PythonExe
    Write-Host "Using Python: $python"
}

if (-not (Test-Path $StateDir)) {
    New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
}
Write-Host "State dir: $StateDir"

Drop-StarterConfig -Dir $StateDir

$nssm = Ensure-Nssm -DestDir $StateDir
Write-Host "NSSM:      $nssm"

# If the service already exists, stop+remove so we can re-register cleanly.
# Use Get-Service (not `nssm status`) for the existence probe: on a clean
# machine `nssm status <missing>` writes "Can't open service!" to stderr,
# which -- under $ErrorActionPreference='Stop' -- PowerShell promotes to a
# terminating NativeCommandError and aborts the whole install. Get-Service
# with -ErrorAction SilentlyContinue is the side-effect-free way to ask.
if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    Write-Host "Service $ServiceName already exists -- stopping + removing for clean reinstall."
    & $nssm stop $ServiceName confirm 2>$null | Out-Null
    & $nssm remove $ServiceName confirm | Out-Null
}

Write-Host "Registering service '$ServiceName' ..."
if ($bundledMode) {
    # The bundled exe knows how to find its own state dir (default
    # %ProgramData%\ATField); no extra args. AppDirectory is set to the
    # bundle dir so the _internal/ runtime resolves correctly.
    $bundleDir = Split-Path -Parent $bundledResolved
    & $nssm install $ServiceName $bundledResolved | Out-Null
    & $nssm set $ServiceName AppDirectory $bundleDir | Out-Null
} else {
    # Run via -m so we don't depend on the wheel's console_scripts shim
    # being on PATH for the SYSTEM account.
    & $nssm install $ServiceName $python '-m atfield.service' | Out-Null
    & $nssm set $ServiceName AppDirectory $StateDir | Out-Null
}
& $nssm set $ServiceName DisplayName $DisplayName | Out-Null
& $nssm set $ServiceName Description 'AT-Field watchdog: protects AI rig from runaway GPU/RAM pressure (kills offending Python tree).' | Out-Null
& $nssm set $ServiceName Start SERVICE_AUTO_START | Out-Null
& $nssm set $ServiceName ObjectName 'LocalSystem' | Out-Null

# Auto-detect a nearby LibreHardwareMonitor.exe and bake its path into
# the service's environment so the supervisor can find it without the
# user setting ATFIELD_LHM_EXE manually after install. Without this the
# service runs but the LHM supervisor never starts and the LHM-derived
# signals (mem_junction_temp_c, cpu_package_temp_c, +12V rail) silently
# stay disabled. We probe several layouts so the SAME script works for
# both the dev checkout and the shipped NSIS bundle:
#   1. Existing ATFIELD_LHM_EXE in this elevated session (preserved).
#   2. <repoRoot>\dist\atfield\LibreHardwareMonitor.exe
#      (dev workflow: PyInstaller-built bundle in a source tree).
#   3. <repoRoot>\LibreHardwareMonitor.exe  (installed bundle, flat
#      scripts\ layout: repoRoot is <InstallDir>\atfield\ where the vendored
#      DLLs sit flat next to it).
#   4. <repoRoot>\..\LibreHardwareMonitor.exe  (installed bundle, scripts
#      staged under _internal\scripts\: repoRoot is <InstallDir>\atfield\
#      _internal, so the DLLs are one level up in <InstallDir>\atfield\).
#   5. %ProgramFiles%\LibreHardwareMonitor\LibreHardwareMonitor.exe
#      (upstream installer path).
# $PSScriptRoot (not $MyInvocation.ScriptName) is the script's own directory:
# when launched via `powershell -File install_service.ps1` there is no calling
# script, so $MyInvocation.ScriptName is empty and `Split-Path -Parent ''`
# throws a terminating ParameterArgumentValidationError mid-install.
$scriptDir = $PSScriptRoot
$repoRoot = Split-Path -Parent $scriptDir
$repoRootParent = if ($repoRoot) { Split-Path -Parent $repoRoot } else { $null }
$lhmExe = $env:ATFIELD_LHM_EXE
if (-not $lhmExe) {
    $lhmCandidates = @(
        (Join-Path $repoRoot 'dist\atfield\LibreHardwareMonitor.exe'),
        (Join-Path $repoRoot 'LibreHardwareMonitor.exe'),
        $(if ($repoRootParent) { Join-Path $repoRootParent 'LibreHardwareMonitor.exe' }),
        (Join-Path $env:ProgramFiles 'LibreHardwareMonitor\LibreHardwareMonitor.exe')
    )
    foreach ($cand in $lhmCandidates) {
        if ($cand -and (Test-Path $cand)) { $lhmExe = (Resolve-Path $cand).Path; break }
    }
}

# Locate -- and if necessary build -- the headless sensor helper. AT-Field
# reads CPU package / GPU memory-junction / PSU voltage sensors through this
# (atfield-sensors.exe -> LibreHardwareMonitorLib), NOT LHM's fragile GUI
# web server. The exe lives next to the bundled LibreHardwareMonitorLib.dll.
$sensorExe = $env:ATFIELD_SENSOR_EXE
if (-not $sensorExe) {
    $helperDir = if ($lhmExe) { Split-Path -Parent $lhmExe } else { Join-Path $repoRoot 'dist\atfield' }
    $candidate = Join-Path $helperDir 'atfield-sensors.exe'
    if (-not (Test-Path $candidate)) {
        $buildScript = Join-Path $scriptDir 'build_helper.ps1'
        $libDll = Join-Path $helperDir 'LibreHardwareMonitorLib.dll'
        if ((Test-Path $buildScript) -and (Test-Path $libDll)) {
            try {
                Write-Host "Building sensor helper into $helperDir ..."
                & powershell -NoProfile -ExecutionPolicy Bypass -File $buildScript -OutDir $helperDir | Out-Null
            } catch {
                Write-Host "sensor helper build failed (continuing): $_"
            }
        }
    }
    if (Test-Path $candidate) { $sensorExe = (Resolve-Path $candidate).Path }
}

# Bake discovered paths into the service environment so the SYSTEM account
# finds them without the user exporting anything. ATFIELD_LHM_EXE still
# helps the helper locate its sibling DLLs (and powers the optional LHM GUI
# under ATFIELD_RUN_LHM_GUI=1).
$envExtra = @()
if ($lhmExe) {
    Write-Host "LHM DLLs:  $lhmExe"
    $envExtra += "ATFIELD_LHM_EXE=$lhmExe"
} else {
    Write-Host "LHM DLLs:  not found nearby; run 'atf install-lhm' or set ATFIELD_LHM_EXE."
}
if ($sensorExe) {
    Write-Host "Sensors:   $sensorExe"
    $envExtra += "ATFIELD_SENSOR_EXE=$sensorExe"
} else {
    Write-Host "Sensors:   helper not found; CPU/GPU-junction/PSU signals stay disabled until you build it (scripts\build_helper.ps1) or set ATFIELD_SENSOR_EXE."
}
if ($envExtra.Count -gt 0) {
    & $nssm set $ServiceName AppEnvironmentExtra @envExtra | Out-Null
}

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

# NSSM `start` returns the moment SCM accepts the start request, not when
# the service has actually bootstrapped. On a freshly-extracted PyInstaller
# bundle the first launch can take several seconds (cold disk, Defender
# scanning .pyd files). Poll until we see Running, with a 30s ceiling that
# is generous for any realistic cold start while still failing loudly if
# the service can't come up at all.
if (Wait-ServiceRunning -Name $ServiceName -TimeoutSec 30) {
    Write-Host "Service status: Running"
} else {
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    $status = if ($svc) { $svc.Status } else { 'Unknown' }
    Write-Host "WARNING: service did not reach Running within 30s (current: $status). Check %ProgramData%\ATField\service.stderr.log."
}

Write-Host ""
Write-Host "Done. AT-Field is installed. Useful next steps:"
Write-Host "  atf status              # confirm heartbeat + working signal map"
Write-Host "  atf inputs              # one-shot collector probe + sample dump"
Write-Host "  atf tail                # follow events.jsonl"
Write-Host "  Get-Service $ServiceName"
