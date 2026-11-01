<#
.SYNOPSIS
    Keep the AT-Field tray running: every 15 minutes (and shortly after logon),
    start it if it is not running. Run elevated; safe to re-run.

.DESCRIPTION
    THE BUG THIS REPLACES (2026-10-31, Chronos + DEMETER). The tray had been
    started by a scheduled task whose action WAS the tray. Task Scheduler's
    default ExecutionTimeLimit is 72 hours, so exactly 72 h later (10-28 11:41
    -> 10-31 11:41) it terminated the task -- and the tray with it (result
    0x41306, "terminated"). Nothing watches the tray, so the icon and every
    heat/kill pop-up silently vanished; the service stayed armed throughout.

    Here the task runs a hidden checker (wscript //B, no window) that exits
    at once: the tray is never the task's own process, so no time limit or
    task deletion can reach it. The checker looks for a running tray before
    launching one, so it coexists with the installer's HKCU Run entry (the
    tray is not single-instance; a blind launch would add a second icon).
    A tray the user Quits comes back within 15 minutes.
#>
param(
    [string]$InstallRoot = 'C:\Program Files\AT-Field',
    [string]$User,                                  # default: the logged-on user
    [string]$TaskName = 'ATFieldTrayRelaunch'
)
$ErrorActionPreference = 'Stop'

$tray = Join-Path $InstallRoot 'at-field-tray.exe'
if (-not (Test-Path $tray)) { throw "no tray at $tray" }
if (-not $User) { $User = (Get-CimInstance Win32_ComputerSystem).UserName }
if (-not $User) { throw "nobody is logged on; pass -User DOMAIN\name" }

# Lives beside the tray in Program Files: admin-write-only, like what it launches.
$vbs = Join-Path $InstallRoot 'tray_keepalive.vbs'
@"
' AT-Field tray keepalive (scripts\register_tray_keepalive.ps1). Start the tray if none is running.
Set procs = GetObject("winmgmts:\\.\root\cimv2").ExecQuery("SELECT ProcessId FROM Win32_Process WHERE Name='at-field-tray.exe'")
If procs.Count = 0 Then CreateObject("WScript.Shell").Run """$tray""", 1, False
"@ | Set-Content -Path $vbs -Encoding ASCII

$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "//B //Nologo `"$vbs`""
$logon = New-ScheduledTaskTrigger -AtLogOn -User $User
$logon.Delay = 'PT2M'            # let the HKCU Run entry win the race at logon
$every = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
            -RepetitionInterval (New-TimeSpan -Minutes 15)
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
            -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $logon, $every `
    -Settings $settings -Principal $principal -Force | Out-Null

$t = Get-ScheduledTask -TaskName $TaskName
"registered $TaskName for ${User}: every 15 min + 2 min after logon; limit $($t.Settings.ExecutionTimeLimit)"
