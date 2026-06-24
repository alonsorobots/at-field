<#
.SYNOPSIS
    Grant a user start/stop/restart rights on the AT-Field service so it can
    be controlled WITHOUT elevation (no UAC prompt per restart).

.DESCRIPTION
    Windows services are controllable only by Administrators by default. On a
    UAC system an admin's *non-elevated* token has the Administrators group
    filtered out, which is why `Restart-Service ATFieldWatchdog` fails without
    "Run as administrator". This script adds a single scoped allow-ACE for the
    user's own SID (which is present in BOTH the elevated and non-elevated
    tokens) to the service's security descriptor.

    The granted rights are deliberately limited to operational control:
      CC  SERVICE_QUERY_CONFIG       LC  SERVICE_QUERY_STATUS
      SW  SERVICE_ENUMERATE_DEPENDENTS   RP  SERVICE_START
      WP  SERVICE_STOP               LO  SERVICE_INTERROGATE
      CR  SERVICE_USER_DEFINED_CONTROL   RC  READ_CONTROL
    It does NOT grant DC (change config), SD (delete), WD (write-DAC) or WO
    (write-owner), so the user cannot reconfigure or remove the service.

    Run ONCE, elevated. Idempotent (re-running is a no-op if already granted).

.PARAMETER ServiceName
    Service to grant control over. Default: ATFieldWatchdog.

.PARAMETER UserSid
    SID to grant. Defaults to the SID of the user running this script.
#>
[CmdletBinding()]
param(
    [string]$ServiceName = 'ATFieldWatchdog',
    [string]$UserSid = ''
)

$ErrorActionPreference = 'Stop'

if (-not $UserSid) {
    $UserSid = ([Security.Principal.WindowsIdentity]::GetCurrent()).User.Value
}
Write-Host "Granting operational control of '$ServiceName' to SID $UserSid"

# Operational-only ACE (see .DESCRIPTION for the rights breakdown).
$ace = "(A;;CCLCSWRPWPLOCRRC;;;$UserSid)"

# Read the current security descriptor (SDDL). sc.exe prints it on the line
# that contains an ACE, e.g. "D:(A;;...)(A;;...)S:(AU;...)".
$sddl = (& sc.exe sdshow $ServiceName |
         ForEach-Object { $_.Trim() } |
         Where-Object { $_ -match ':\(' } |
         Select-Object -First 1)
if (-not $sddl) {
    throw "Could not read security descriptor for '$ServiceName'."
}

if ($sddl -like "*$ace*") {
    Write-Host "Grant already present -- nothing to do."
    Write-Host $sddl
    exit 0
}

# Insert our DACL ACE before the SACL section (S:) if present, else append.
if ($sddl -match 'S:') {
    $newSddl = $sddl -replace '(S:)', ($ace + '$1')
} else {
    $newSddl = $sddl + $ace
}

& sc.exe sdset $ServiceName $newSddl
if ($LASTEXITCODE -ne 0) {
    throw "sc.exe sdset failed with exit code $LASTEXITCODE"
}

Write-Host "Success. New security descriptor:"
Write-Host $newSddl
Write-Host ""
Write-Host "You can now run 'Restart-Service $ServiceName' from a normal (non-elevated) shell."
