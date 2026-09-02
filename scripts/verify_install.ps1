<#
.SYNOPSIS
    Prove a deployed AT-Field can actually SEE this machine's temperatures.

.DESCRIPTION
    A version string proves nothing. On Chronos 2026-09-01 a freshly installed
    build was Running, answered /healthz, and reported the new version -- while
    publishing 2 thermal signals instead of 5. No CPU package temperature, so
    `cpu-pkg-hot` could never fire. Every check that looked at the service
    rather than at the SIGNALS said it was fine.

    So this checks the observable:

      1. the service is Running
      2. it answers /signals
      3. it publishes at least -MinThermal thermal signals
      4. it publishes a CPU package temperature (the rule that has actually
         killed work on this fleet fires on that one)
      5. no gpu.N.mem_junction_temp_c on a card that has no such sensor --
         the phantom-sensor bug, which fabricated a junction reading from a hot
         spot and cost aurora 63 spurious kills
      6. EVERY live thermal signal has a rule watching it

    CHECK 6 IS THE ONE THIS SCRIPT WAS MISSING, and its absence bit immediately.
    Deploying the phantom-sensor fix RENAMED aurora's signal from
    gpu.N.mem_junction_temp_c to gpu.N.hotspot_temp_c. Aurora's config.toml in
    ProgramData predates the gpu-hotspot-hot rule and overrides the code
    defaults, so after a deploy that passed every other check the machine had:
    the phantom rule correctly disabled, and NOTHING watching the real hot spot.
    Idle, that is invisible; under load the hot spot runs 85-95 C unguarded.
    Signals existing is not the same as signals being watched.

    Exit code is non-zero when any of those fail, so a deploy script can gate on
    it instead of hoping.

.PARAMETER ExpectNoJunctionGpus
    Indices of GPUs KNOWN to lack a memory-junction sensor (2070 SUPER: 0,1 on
    aurora). If a junction reading appears for one of these, the phantom-sensor
    bug is present and the fix did not land.
#>
param(
    [string]$Url         = 'http://127.0.0.1:8765/signals',
    [string]$RulesUrl    = 'http://127.0.0.1:8765/rules',
    [string]$ServiceName = 'ATFieldWatchdog',
    [int]$MinThermal     = 3,
    [int[]]$ExpectNoJunctionGpus = @(),
    [int]$TimeoutSec     = 40
)

$fail = @()

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-not $svc)                    { $fail += "service $ServiceName not registered" }
elseif ($svc.Status -ne 'Running'){ $fail += "service status is $($svc.Status), not Running" }
else                              { "service            : Running" }

$body = $null
$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
    try { $body = (Invoke-WebRequest -Uri $Url -TimeoutSec 8 -UseBasicParsing).Content; break }
    catch { Start-Sleep -Seconds 2 }
}
if (-not $body) {
    $fail += "no response from $Url within ${TimeoutSec}s"
} else {
    $latest = ($body | ConvertFrom-Json).latest
    $names  = @($latest.PSObject.Properties.Name)
    $therm  = @($names | Where-Object { $_ -like '*temp*' })
    "signals total      : $($names.Count)"
    "thermal signals    : $($therm.Count)  (minimum $MinThermal)"
    foreach ($t in ($therm | Sort-Object)) {
        "   {0,-34} {1}" -f $t, $latest.$t.value
    }
    if ($therm.Count -lt $MinThermal) {
        $fail += "only $($therm.Count) thermal signals (expected >= $MinThermal) -- the watchdog is partly blind"
    }
    if (-not ($therm | Where-Object { $_ -like '*cpu_package*' })) {
        $fail += "NO CPU package temperature -- cpu-pkg-hot can never fire"
    }
    foreach ($g in $ExpectNoJunctionGpus) {
        $bad = $therm | Where-Object { $_ -eq "gpu.$g.mem_junction_temp_c" }
        if ($bad) {
            $fail += ("gpu.$g reports a memory junction it does not have -- " +
                      "the phantom-sensor bug is PRESENT (fix 04fda0d missing)")
        } else {
            "gpu.$g junction    : correctly absent"
        }
    }
}

# --- 6. every live thermal signal must be WATCHED -------------------------
if ($body) {
    try {
        $rules = (Invoke-WebRequest -Uri $RulesUrl -TimeoutSec 10 -UseBasicParsing).Content | ConvertFrom-Json
    } catch { $rules = $null }
    if (-not $rules) {
        $fail += "could not read $RulesUrl -- cannot prove the signals are watched"
    } else {
        $watched = @($rules.effective | ForEach-Object { $_.signal })
        $latest2 = ($body | ConvertFrom-Json).latest
        $thermal = @($latest2.PSObject.Properties.Name | Where-Object { $_ -like '*temp*' })
        $unwatched = @($thermal | Where-Object { $watched -notcontains $_ })
        ""
        "rules watching     : $($watched.Count)"
        if ($unwatched.Count) {
            foreach ($u in $unwatched) {
                $fail += "thermal signal '$u' has NO rule watching it -- it is published and unguarded"
            }
        } else {
            "thermal coverage   : every thermal signal has a rule"
        }
        foreach ($d in @($rules.disabled)) {
            "disabled rule      : $($d.rule) -- $($d.reason)"
        }
    }
}

""
if ($fail.Count) {
    "VERIFY FAILED:"
    $fail | ForEach-Object { "  - $_" }
    exit 1
}
"VERIFY OK"
exit 0
