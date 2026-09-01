<#
.SYNOPSIS
    Replace this machine's installed AT-Field with a freshly built bundle.

.DESCRIPTION
    Stops the service, swaps C:\Program Files\AT-Field\atfield\ for the given
    dist folder, and re-registers via the repo's own install_service.ps1.

    WHY A SCRIPT RATHER THAN A FEW ELEVATED COMMANDS. Elevation is a separate
    process with its own console that disappears when it exits, so an ad-hoc
    `Start-Process -Verb RunAs` leaves nobody able to read what happened. This
    writes a transcript to -LogPath so the unelevated caller can read the
    result afterwards, which is the difference between "it probably worked"
    and knowing.

    KEEPS A ROLLBACK. The previous install is renamed aside rather than
    deleted, so a bad build is one rename away from being undone. AT-Field is
    the thing that protects these machines from cooking themselves; deploying
    it without a way back would be the wrong trade.

.PARAMETER DistDir
    The freshly built dist\atfield folder to install.

.PARAMETER LogPath
    Transcript destination, readable by the unelevated caller.
#>
param(
    [Parameter(Mandatory = $true)][string]$DistDir,
    [Parameter(Mandatory = $true)][string]$LogPath,
    [string]$InstallRoot  = 'C:\Program Files\AT-Field',
    [string]$ServiceName  = 'ATFieldWatchdog'
)

Start-Transcript -Path $LogPath -Force | Out-Null
$ErrorActionPreference = 'Stop'
try {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $isAdmin = (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) { throw "not elevated -- rerun via Start-Process -Verb RunAs" }
    "elevated: yes"

    if (-not (Test-Path (Join-Path $DistDir 'atfield-service.exe'))) {
        throw "no atfield-service.exe under $DistDir"
    }
    "source build: " + (Get-Item (Join-Path $DistDir 'atfield-service.exe')).LastWriteTime

    $target = Join-Path $InstallRoot 'atfield'
    if (Test-Path $target) {
        "installed build (being replaced): " +
            (Get-Item (Join-Path $target 'atfield-service.exe')).LastWriteTime
    }

    "stopping $ServiceName ..."
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    # NSSM can take a moment to let go of the exe; a copy over a held file
    # fails with a permission error that looks like an ACL problem and is not.
    for ($i = 0; $i -lt 15; $i++) {
        $s = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
        if (-not $s -or $s.Status -eq 'Stopped') { break }
        Start-Sleep -Seconds 1
    }
    Get-Process atfield-service, atfield-sensors -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    "service stopped"

    if (Test-Path $target) {
        $backup = "$target.bak-" + (Get-Date -Format 'yyyyMMdd-HHmmss')
        Rename-Item -Path $target -NewName (Split-Path $backup -Leaf)
        "previous install kept at: $backup"
    }
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    Copy-Item -Path (Join-Path $DistDir '*') -Destination $target -Recurse -Force
    "copied build -> $target"

    $installer = Join-Path $target 'scripts\install_service.ps1'
    if (-not (Test-Path $installer)) {
        $installer = Join-Path $target '_internal\scripts\install_service.ps1'
    }
    if (-not (Test-Path $installer)) { throw "install_service.ps1 not found under $target" }
    "re-registering via $installer"
    & $installer -BundledExe (Join-Path $target 'atfield-service.exe') `
                 -ServiceName $ServiceName

    Start-Sleep -Seconds 5
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    "service status: " + $(if ($svc) { $svc.Status } else { 'MISSING' })
    "installed build now: " +
        (Get-Item (Join-Path $target 'atfield-service.exe')).LastWriteTime
    "DEPLOY OK"
}
catch {
    "DEPLOY FAILED: $($_.Exception.Message)"
    "$($_.ScriptStackTrace)"
}
finally {
    Stop-Transcript | Out-Null
}
