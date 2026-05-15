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

if ($PurgeStateDir.IsPresent) {
    if (Test-Path $StateDir) {
        Write-Host "Purging state directory $StateDir ..."
        Remove-Item -Recurse -Force $StateDir
    }
} else {
    Write-Host "State directory left at $StateDir (pass -PurgeStateDir to remove)."
}

Write-Host "Done."
