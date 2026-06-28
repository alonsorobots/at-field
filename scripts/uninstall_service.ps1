<#
.SYNOPSIS
    Uninstall the AT-Field Windows Service.

.DESCRIPTION
    Stops and removes the service via NSSM. Leaves the state directory
    (logs, events.jsonl, config.toml) in place so the operator can inspect
    history after uninstalling. Pass -PurgeStateDir to remove it.
#>

[CmdletBinding()]
param(
    [string]$ServiceName = 'ATFieldWatchdog',
    [string]$StateDir    = (Join-Path $env:ProgramData 'ATField'),
    [switch]$PurgeStateDir
)

$ErrorActionPreference = 'Stop'

function Assert-Admin {
    $current = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($current)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Error "uninstall_service.ps1 requires an elevated PowerShell."
        exit 1
    }
}

Assert-Admin

# -----------------------------------------------------------------------------
# Process cleanup helper
# -----------------------------------------------------------------------------
#
# Even after `nssm stop` returns, the spawned atfield-service.exe (and the
# atfield-sensors.exe helper it launches) can take a beat to actually exit,
# AND under some shutdown paths they linger entirely. When this script is
# called from the NSIS pre-uninstall hook of an upgrade, NSIS will start
# overwriting files the moment we return -- if anything is still holding
# atfield/_internal/atfield/__init__.pyc (or any other bundled file), that
# file silently DOES NOT get replaced. The new service.exe then loads stale
# bytecode at next start and the user sees an "upgraded to 0.4.X but
# /health reports the OLD version" mismatch.
#
# Defense in depth: after nssm tells us the service is gone, poll for any
# residual atfield processes and force-kill them with a hard timeout so the
# installer never races against held file handles. This is idempotent and a
# no-op on a clean uninstall.
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

    # Final report: anything still alive is going to lock files for NSIS.
    $stillAlive = Get-Process -Name $targets -ErrorAction SilentlyContinue
    if ($stillAlive) {
        $names = ($stillAlive | ForEach-Object { "$($_.ProcessName)(pid=$($_.Id))" }) -join ', '
        Write-Host "WARNING: processes still running after force-kill timeout: $names"
    }
}

if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) {
    Write-Host "Service $ServiceName is not installed; nothing to remove."
    # Even with no service, kill any orphan processes (e.g. atf.exe run
    # foreground for dev) so a parent installer can replace files safely.
    Stop-AtfieldProcesses
} else {
    $nssm = Join-Path $StateDir 'nssm.exe'
    if (-not (Test-Path $nssm)) {
        Write-Host "NSSM not found at $nssm; falling back to sc.exe."
        & sc.exe stop $ServiceName 2>$null | Out-Null
        & sc.exe delete $ServiceName | Out-Null
    } else {
        Write-Host "Stopping service $ServiceName ..."
        & $nssm stop $ServiceName confirm 2>$null | Out-Null
        Write-Host "Removing service $ServiceName ..."
        & $nssm remove $ServiceName confirm | Out-Null
    }
    Write-Host "Cleaning up any residual AT-Field processes ..."
    Stop-AtfieldProcesses
}

if ($PurgeStateDir.IsPresent) {
    if (Test-Path $StateDir) {
        Write-Host "Purging state directory $StateDir ..."
        Remove-Item -Recurse -Force $StateDir
    }
} else {
    Write-Host "State directory left at $StateDir (pass -PurgeStateDir to remove)."
}

Write-Host "Done."
