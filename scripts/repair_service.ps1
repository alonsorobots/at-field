<#
.SYNOPSIS
    Re-register and start ATFieldWatchdog; roll back if the new build won't run.

.DESCRIPTION
    Written after a deploy failed HALF-WAY and left the machine with no running
    watchdog. Two lessons are baked in:

    1. **$ErrorActionPreference = 'Stop' plus a native exe is a trap.** nssm
       writes "The service has not been started" to stderr when asked to stop an
       already-stopped service -- a NO-OP -- and PowerShell turns any native
       stderr into a terminating error under 'Stop'. The deploy died on a
       success case, after it had already removed the service. So the installer
       is invoked with 'Continue' and judged by the SERVICE STATE afterwards,
       not by whether a child process wrote to stderr.

    2. **A deploy that can't start must undo itself.** AT-Field is what stops
       these machines cooking; leaving it down because a new binary is bad is a
       worse outcome than running the old one. If the service will not reach
       Running on the new build, this restores the most recent atfield.bak-*
       and re-registers that instead.
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
            [Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "not elevated"
    }
    "elevated: yes"

    function Register-And-Start([string]$dir) {
        $exe = Join-Path $dir 'atfield-service.exe'
        if (-not (Test-Path $exe)) { return "no exe at $exe" }
        $inst = Join-Path $dir 'scripts\install_service.ps1'
        if (-not (Test-Path $inst)) { $inst = Join-Path $dir '_internal\scripts\install_service.ps1' }
        if (-not (Test-Path $inst)) { return "no installer under $dir" }
        # 'Continue', deliberately -- see the note above.
        $ErrorActionPreference = 'Continue'
        & $inst -BundledExe $exe -ServiceName $ServiceName 2>&1 |
            ForEach-Object { "    installer: $_" }
        Start-Sleep -Seconds 3
        $s = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
        if ($s -and $s.Status -ne 'Running') {
            Start-Service -Name $ServiceName -ErrorAction SilentlyContinue
        }
        for ($i = 0; $i -lt 20; $i++) {
            $s = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
            if ($s -and $s.Status -eq 'Running') { return 'Running' }
            Start-Sleep -Seconds 1
        }
        $s = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
        return $(if ($s) { "$($s.Status)" } else { 'MISSING' })
    }

    # Refresh the installer INSIDE the deployed bundle before using it. The
    # bundle ships its own copy of install_service.ps1, so a fix made in the
    # repo does not reach the machine until the bundle is rebuilt -- and the
    # bug being fixed here is IN that installer. Without this the repair keeps
    # re-running the broken copy it just tried to replace.
    $repoInstaller = Join-Path $PSScriptRoot 'install_service.ps1'
    $target = Join-Path $InstallRoot 'atfield'
    foreach ($rel in @('scripts\install_service.ps1', '_internal\scripts\install_service.ps1')) {
        $dst = Join-Path $target $rel
        if (Test-Path (Split-Path $dst -Parent)) {
            Copy-Item $repoInstaller $dst -Force -ErrorAction SilentlyContinue
            "refreshed bundled installer: $rel"
        }
    }
    "attempting NEW build at $target"
    $state = Register-And-Start $target
    "  -> service state: $state"

    if ($state -ne 'Running') {
        "NEW BUILD DID NOT START -- rolling back"
        $bak = Get-ChildItem $InstallRoot -Directory -Filter 'atfield.bak-*' |
               Sort-Object Name -Descending | Select-Object -First 1
        if (-not $bak) { throw "no atfield.bak-* to roll back to" }
        $failed = "$target.failed-" + (Get-Date -Format 'yyyyMMdd-HHmmss')
        Rename-Item -Path $target -NewName (Split-Path $failed -Leaf)
        Rename-Item -Path $bak.FullName -NewName 'atfield'
        "restored $($bak.Name); the failed build is at $failed"
        $state = Register-And-Start $target
        "  -> service state after rollback: $state"
    }

    $s = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    "FINAL service status : " + $(if ($s) { $s.Status } else { 'MISSING' })
    "FINAL installed build: " +
        (Get-Item (Join-Path $target 'atfield-service.exe')).LastWriteTime
    if ($s -and $s.Status -eq 'Running') { "REPAIR OK" } else { "REPAIR FAILED" }
}
catch {
    "REPAIR ERROR: $($_.Exception.Message)"
}
finally {
    Stop-Transcript | Out-Null
}
