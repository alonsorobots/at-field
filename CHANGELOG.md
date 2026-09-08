# Changelog

All notable changes to AT-Field are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.14] — 2026-09-08 — Say what is eating the tick, and what is heating the chip

### Added

- **A slow tick now names the phase that ate it.** Per-phase wall-clock across
  the loop (pause / collect / credibility / presence / mirror / forensics /
  engine / rss_cap / dispatch / heartbeat), and one WARNING when a tick exceeds
  twice its period, naming the slowest phase with its cost and the full
  breakdown. Pure observation — no threshold that changes behaviour.

  This is the diagnostic that was missing on 2026-09-03, when a per-tick RSS cap
  added as a *safety feature* drove the loop to 0.22 Hz and left every rule
  mathematically unable to fire while  reported . The machine
  ran an hour at Tjmax.  now adapts to the observed cadence and
   reports the symptom — but nothing said what was
  consuming the tick, and Chronos still runs at 0.36 Hz with 18
   events while  accounts for 0.18 ms of a
  2,770 ms tick.
- **** — the variable that actually explains CPU
  temperature. Its absence is why a healthy cooler was once diagnosed as a
  failing pump: the chip pulls 80–126 W at 11–27% usage, so utilization is
  the wrong variable. Diagnostic only; it is not a guard and does not classify
  as thermal.

## [0.4.13] — 2026-09-07 — What the liveness review found

An adversarial review of 0.4.12 ran seven mutations against the suite; five
survived it green. Two were real holes, not just missing tests.

### Fixed

- **`/health.signals_not_live` could not name a signal that never arrived.**
  It iterated the state mirror, and a never-seen signal is not in the mirror —
  so `rules_starved` said 1 while the list that exists so a reader need not
  act on a bare count was empty. A service restarted into a wedged card lands
  in that state and stays there, because a rule with no first sample has
  nothing to go stale.
- **The loop deleted impossible readings instead of marking them.** They were
  popped before the mirror, the forensic stream and the credibility split ever
  saw them, so a stream of them produced no forensic file at all — the 0.4.12
  changelog claim that the stream preserves suspect samples was false of the
  shipped code. The split now happens at the plausibility gate: an impossible
  reading still makes its rule abstain, by being withheld from the engine
  rather than erased.
- **Signal age is measured on the monotonic clock.** A backward wall-clock step
  (NTP, DST, VM resume) made one `/rules` payload report a rule `starved` and
  its signal `live` at once; a forward step flushed a healthy machine to
  `stale` and emptied `/headroom`. The engine already measured starvation
  monotonically, so the two now agree by construction.
- `is_credible`'s docstring inverted its code. Behaviour is unchanged and now
  stated as one principle: **absent** information is trusted (an unknown
  `source_id` defaults to healthy, so a bookkeeping gap cannot mute a rule),
  **present but unrecognised** is not.

## [0.4.12] — 2026-09-07 — One liveness verdict

### Fixed

- **A correct alarm retracted itself.** `PolicyEngine._track_starvation` cleared
  `rule.starved` on ANY arriving sample, with no test of whether the sample
  meant anything. On 2026-09-03 at 12:46:48, 17.5 hours after both GPU
  core-temp rules were correctly starved, the wedged NVML session began
  publishing a constant `0.0 °C` — and the engine logged `state: recovered`,
  "rule is guarding again". `/health` dropped from 2 starved rules to 1 and
  `/headroom` began publishing **1.0**, perfect headroom, for a rule that could
  never fire. That is worse than never alarming, because it manufactures
  confidence.
- **Liveness is now decided once and rendered everywhere.** Four places
  answered "is this signal trustworthy" and gave four answers: the engine
  (`starved`), `/headroom` (dropped starved rules implicitly), and
  `/headroom/detail` and `/signals` (which included frozen values with no
  mark at all). `ServiceState.liveness()` is the single verdict —
  `never` / `suspect` / `stale` / `live` — computed from two facts that are not
  judgement calls: the sample's age, and the health of the collector that
  produced it.
- **The engine now receives only credible samples**
  (`service.split_by_credibility`). Fixing it at that shared entrance means
  "any arriving sample clears starvation" becomes true again, so the pure
  engine and its tests are untouched. Note this matters for exactly one
  collector: nvml is the only one that returns partial samples while DEGRADED
  (system and lhmlib return `{}`, amd goes FAILED) — and those partials were
  the wedge.
- **`/headroom` now accounts for every kill rule**, in exactly one of
  `per_rule` or the new `excluded` map. Previously a starved rule vanished from
  the payload with no record of why — it had no `last_value`, so it hit an
  early `continue` — while a rule reading a fresh garbage zero stayed in the
  fold at 1.0. That is how the two headroom endpoints came to describe
  different machines.

### Added

- `signals.is_credible(sample, collector_health_name)` — the pure predicate.
- `liveness` and `age_s` on every signal in `/signals` and `/headroom/detail`,
  and `liveness` on every rule in `/rules`. Non-live signals are **marked, never
  removed**: a consumer has to be able to see and name a dead signal, and
  dropping it is precisely how a partial failure becomes invisible downstream.
- `/health.signals_not_live` — the count was already there and was not enough;
  a reader cannot act on "1". Each entry names the signal, its verdict, its age
  and the rule it leaves unguarded.
- The forensic stream marks suspect samples rather than dropping them. "What
  was the sensor saying while the guard was off" is the question that file
  exists to answer.

All wire additions are additive; a consumer that does not know the fields is
unaffected, and Kiroshi already honours `liveness` when it appears.

## [0.4.11] — 2026-09-07 — Surviving a driver swap, and one honest headroom

The release that carries the two 2026-09-05 headroom fixes to the fleet. They
had been committed and pushed for two days while every host ran the 09-02
build, which is its own lesson: committed is not deployed.

### Fixed

- **A boot-start service outlived the driver it opened NVML against.** Chronos
  booted, AT-Field started 25 s later against driver 610.88, and Windows PnP
  installed 616.56 ten minutes after that. The session held dead handles for
  4.2 days: GPU 1's calls raised (six signals frozen), GPU 0's returned
  NVML_SUCCESS with `core_temp_c = 0.0` and `power_w = 0.041`. Both GPU
  core-temp kill rules were inert and `/headroom` published 1.0 for one of
  them. The collector now rebuilds its session on sustained DEGRADED (with
  monotonic backoff), watches the installed driver version out-of-band via
  `nvml.dll`'s FileVersion, and — bounded to once per process, above a 120 s
  uptime floor, and only on the driver-swap witness — exits 3 so NSSM can hand
  it a fresh process, which is the documented cure for a library/kernel-module
  mismatch.
- **One failed session rebuild permanently blinded the NVML collector.**
  `_reinit_session()` clears `_handles` on failure and `sample()` returned `{}`
  for an empty handle list, short-circuiting the health accounting and every
  recovery path. A collector publishing nothing looks exactly like a quiet one,
  so nothing noticed.
- **`is_plausible` accepted 0.0 °C.** The celsius floor moves from −50 to 5.
  Powered silicon sits above ambient; a running machine reporting 0 °C is
  reporting nothing. The trade, stated plainly: a machine booted in a
  sub-freezing room has its first samples rejected and its thermal rules starve
  until the silicon passes 5 °C — loud, visible and self-correcting, in place of
  a silent four-day outage.
- **`/headroom` was a one-way ratchet for any thermal rule** (`4a7e799`, from
  2026-09-05). `(threshold − latest)/threshold` treats 0 °C as idle, so a 90 °C
  wall put SAFE at ≤58.5 °C and a loaded 32-core box could only ever shrink.
  Thermal headroom is now `(threshold − mean)/thermal_band_c` with the band
  authored per rule and published as a fact, which also lets the consumer delete
  its own private copy of that limit.
- **The two headroom endpoints described the same machine with different
  arithmetic** (`c79d1c3`, from 2026-09-05) — a 60 s median against a per-rule
  mean, 52% apart on a ramp. One `_detail_center` now backs both.

## [0.4.4] — 2026-06-28 — Reliable in-place upgrades

### Fixed

- **Upgrade-over-running-service race.** Installing a new release on top of an
  existing AT-Field install could leave the service in a half-upgraded state:
  the new `atfield-service.exe` got written, but stale `.pyc` files under
  `_internal/atfield/` did not (the old service was still holding them open
  during NSIS file replacement). The new exe then loaded the old bytecode and
  `/health` would report the *previous* version even though the registry said
  the new one. In some cases the service also failed to start at all after the
  upgrade, requiring a manual `sc start ATFieldWatchdog`. Hardened in three
  layers:
  - `uninstall_service.ps1` now force-kills any lingering `atfield-service`,
    `atfield-sensors`, and `atf` processes after `nssm remove`, waiting up to
    10s with a 250ms poll. This is what the NSIS pre-uninstall hook calls
    during upgrades, so files are truly unlocked before NSIS replaces them.
  - `install_service.ps1` does the same defensive kill at startup, in case it
    is invoked outside the NSIS hook (e.g. `atf install` on a manual rerun).
  - `install_service.ps1` now polls for `Get-Service` to reach `Running` for
    up to 30s after `nssm start`, instead of trusting the first status read
    after a 2s sleep. NSSM returns the moment SCM accepts the start request,
    not when the PyInstaller bundle has finished its (sometimes slow) cold
    bootstrap on a freshly-extracted install.

  Net effect: upgrading from any prior 0.4.x to 0.4.4+ is a single click with
  no manual `sc start` and no stale-bytecode mismatch.

## [0.4.3] — 2026-06-27 — CPU utilization, signal category tabs

### Added

- **CPU utilization signal.** The `system` collector now emits
  `system.cpu_used_percent` (system-wide busy %, via psutil; primed in
  `probe()` so the first sample is a real value, not the 0.0 cold-start
  artifact). Shows up as "CPU used (%)" on the dashboard. No new kill-rule
  ships with it -- high CPU during training is expected, not pathological;
  CPU package temp already covers the actual damage case.
- **Signals screen category tabs.** A new **All / GPU / CPU / Memory** tab
  strip filters the grid down to one bucket at a time, with live counts
  next to each label (e.g. "GPU 6"). The active tab persists per machine
  via the `atfield.signal_category` localStorage key. Composes with the
  existing Manage / hide / drag-reorder system -- hidden stays hidden in
  every tab, and "Other" (voltages and anything uncategorized) only
  surfaces under "All" so per-resource views stay tightly scoped.

  VRAM lives under GPU (not Memory) because it's the *GPU's* memory --
  same grouping every hardware monitor uses. The Memory tab is strictly
  system memory pressure (RAM %, commit %, page file %), answering "is my
  box about to OOM / thrash the page file?".

### Added

- **`pip install atfield` is now real.** The release workflow publishes the
  wheel + sdist to PyPI via Trusted Publishing (GitHub OIDC, no stored tokens).
  pip is now the recommended, SmartScreen-free path to the headless watchdog;
  the one-click installer remains the way to also get the tray dashboard.
- **Branded NSIS installer.** The setup wizard now ships the AT-Field logo as
  its icon plus custom header and sidebar artwork (Tokyo-3 dark + the orange
  hexagon), replacing the generic NSIS chrome. Generated reproducibly by
  `scripts/gen_installer_images.py`.

### Changed

- **Repositioned: "built for AI rigs, useful for any heavy GPU/CPU workload."**
  README, PyPI description, tray store copy, and the About modal now lead with
  the broader hardware-protection story (renders, sims, overclock testing,
  general OOM protection) while keeping AI training as the hero use case.
  README also gains badges and broadened search keywords for discoverability.
- **First-launch tray toast reworded** from "AT-Field is watching" to
  "AT-Field is on guard" with a protection-focused body (less surveillance-y).

The one-step installer shipped in 0.4.0 worked on the dev machine but silently
failed to register the watchdog on a truly clean PC ("service unreachable"
after install). Three independent clean-machine bugs are fixed here.

### Fixed

- **Installer hook pointed at the wrong path.** Tauri v2 stages bundle
  resources directly under the install dir (`$INSTDIR\atfield\…`), but the
  NSIS post-install/pre-uninstall hooks called `$INSTDIR\resources\atfield\…`.
  That path never existed, so the service installer script never ran. The dev
  machine masked it because it already had the service from an earlier setup.
- **`install_service.ps1` aborted on a clean machine.** Its idempotency probe
  ran `nssm status <service>`; for a not-yet-installed service NSSM writes
  "Can't open service!" to stderr, which under `$ErrorActionPreference='Stop'`
  PowerShell promotes to a terminating error — killing the install before it
  could register anything. The existence check now uses
  `Get-Service -ErrorAction SilentlyContinue` (no side effects).
- **NSSM is now bundled** (win64 2.24) instead of downloaded from `nssm.cc` at
  install time. The download step routinely returned HTTP 503 and was the most
  fragile part of setup; `install_service.ps1` now copies the vendored copy
  shipped beside it and only falls back to the network when one isn't present.

### Changed

- Documentation and code comments corrected to reference the real
  `…\AT-Field\atfield\` bundle path (not `…\resources\atfield\`).

## [0.4.0] — 2026-06-24 — One-step install, dashboard polish, hardened sensors

### Added

- **One-step elevated installer.** The Windows installer is now a per-machine
  install whose NSIS post-install hook registers the `ATFieldWatchdog` service
  automatically (and the pre-uninstall hook removes it on uninstall). The
  single UAC consent at launch covers the whole setup — there's no separate
  in-app "Install watchdog" step. The dashboard's Install/Uninstall watchdog
  buttons remain as a repair/fallback path.
- **About modal** with the app version, support links (GitHub star, Buy me a
  coffee), open-source credits, and a nod to its namesake.
- **Signal hide/show** with persisted per-signal visibility, plus
  priority-based ordering of both signals and rules and a "Reset order"
  control.
- **`grant_service_control.ps1`** to grant a non-elevated user start/stop
  control of the watchdog service (no UAC for routine restarts).

### Changed

- **Default theme renamed to "Tokyo-3"** (the calm, no-bloom out-of-box look).
  Saved values from the earlier ids (`nerv`, `civvie`) migrate forward so
  existing users aren't reset to default after upgrade.
- **LHM sensor transport rebuilt: library helper instead of the web
  server.** AT-Field now reads `LibreHardwareMonitorLib.dll` directly via a
  small bundled .NET helper (`atfield-sensors.exe`, from
  `helper/AtfieldSensors.cs`, built with the in-box C# compiler) that streams
  sensors as JSON lines — see `src/atfield/collectors/lhmlib.py`. This
  replaces the fragile LHM GUI/HTTP web-server path (which depended on a
  `http://+:<port>/` URL ACL, a Session-0 WinForms GUI, and silently
  swallowed listener failures). Verified delivering CPU package temp and
  per-GPU memory-junction temp as `LocalSystem`. The legacy LHM GUI is no
  longer auto-started (opt-in via `ATFIELD_RUN_LHM_GUI=1`).
- **Dashboard polish:** a 4-role color model across the EVA themes;
  disabled-rule cards now name the missing collector and link to the fix; the
  Events screen was redesigned for at-a-glance crash triage; sparklines
  rescale with the threshold as a ceiling for consistent readability; and the
  GPU "VRAM junction temp" signal was renamed "VRAM temp".
- **Lower idle cost:** idle CPU usage cut ~7x and LHM startup hardened.
- **Docs:** `install.md` is now a lean user guide; the installer-build steps
  (and the service-must-be-stopped file-lock gotcha) moved to `packaging.md`.

### Removed

- **HWiNFO Shared Memory collector** (added earlier in this unreleased
  cycle, never shipped in a tagged release). The free version's 12-hour
  Shared-Memory cap (auto-deactivates and requires manual re-enabling) and
  the inability to enable it programmatically made it unreliable for an
  always-on watchdog. The bundled LHM library helper already provides the
  same watchdog-relevant signals (CPU package temp, GPU memory-junction
  temp, PSU rail voltages) for free, forever, with zero configuration. May
  return later as an optional third-party plugin.

### Fixed

- **Installer packaging:** bundle the headless sensor helper and all
  LibreHardwareMonitor DLLs, and resolve the install scripts deterministically
  under `_internal/scripts`, so a clean-machine install has working sensors.

## [0.3.0] — 2026-05-15 — Robustness, forensics, and sensor coverage

This release is the response to a hard system reboot the user
experienced during a flux-fill workload on dual RTX 5090s. The
investigation surfaced three product gaps: no per-tick history that
survived the crash, no PSU rail voltage monitoring, and a brittle
LHM-config approach that broke between LHM versions. v0.3 closes
all three plus surfaces the new failure modes in the dashboard.

### Added

- **Default `vram-pressure` rule** (`gpu.*.vram_used_percent` @ 92%,
  30s window, 75% fraction-over). Catches CUDA OOM — the #1 training
  crash cause and previously uncovered by the defaults. Tier metadata
  added so the slider Aggressive/Normal/Relaxed presets work
  immediately. Auto-disables on rigs without a GPU collector.
- **`/health.lhm_supervisor`** field exposes the per-spawn supervisor
  status (running / http_ready / pid / restart_count / last_error /
  next_retry_at) plus a derived single-token `state` (`ready`,
  `process_up_no_http`, `backoff`, `stopping`, `down`) for switch-on-
  string UI rendering. The dashboard's StatusScreen renders a
  `SupervisorPill` on the LHM collector card with state-specific
  detail (PID + restart count when ready, retry countdown when in
  backoff, the supervisor's error string when `process_up_no_http`).
- **First-launch tray toast.** When the autostart writer registers
  the tray for the first time on a user account, fires a system
  notification pointing to the Win11 overflow chevron — addressing
  the "tray didn't show up after reboot" report (which was actually
  Win11 hiding new tray icons by default; autostart was working
  correctly all along).
- **Forensic rolling buffer** (`src/atfield/forensics.py`). Every
  sampled signal is staged in memory and flushed to
  `%ProgramData%\ATField\forensics.jsonl` every 5 seconds. The
  previous run's file is rotated to `forensics-prev.jsonl` (with two
  more numbered archives behind it) on service start, so a hard
  system crash (Kernel-Power 41, BSOD, power loss) doesn't take the
  pre-crash signal history with it. Format is append-only JSONL --
  the only format that's guaranteed partially-readable after a power
  loss. Auto-rotates at 50 MB; ~250 MB on-disk cap.
- **`atf forensics` CLI** for reading the rolling buffer.
  `--since 5m / 1h / 24h / all`, `--signal <substring>`, output as
  `--format table | jsonl | csv`. Includes the previous run's
  archive by default so it works right after a crash without
  manually concatenating files.
- **Rail voltage signals** from LibreHardwareMonitor: when LHM
  enumerates them, AT-Field now exposes `system.psu_12v_volts`,
  `system.psu_5v_volts`, `system.psu_3v3_volts`, and
  `system.cpu_vcore_volts`. Catches PSU sag patterns that correlate
  with NVIDIA TDR / Kernel-Power 41 events on high-transient cards.
  No default rules ship -- thresholds depend on PSU quality and
  board design; users can add a rule via the slider after watching
  their own baseline.
- **`docs/sensors.md`**: full strategy doc covering the layered
  sensor stack (NVML → ROCm-SMI → psutil → bundled LHM →
  auto-detected HWiNFO), license matrix, and roadmap for v0.3
  (HWiNFO Shared Memory collector) and v0.4 (kernel-mode driver).
- **`atf doctor`** now reports the forensic buffer's freshness as
  one of its checks, distinguishing a fresh install (no buffer
  yet) from a stale buffer (service stopped sampling).

### Fixed

- **LHM 0.9.6 compatibility regression.** The v0.2 approach of shipping
  a static pre-baked `LibreHardwareMonitor.config` next to the binary
  broke when LHM 0.9.6 began rewriting the file from its in-memory
  defaults on first boot, silently disabling the HTTP server and
  leaving the dashboard "Degraded". Replaced with `atfield.lhm_config`
  + a supervisor pre-spawn hook: every time the supervisor spawns LHM
  it merges the AT-Field-required keys (`runWebServerMenuItem=True`,
  `webServerPortNumeric.Value=<port>`, `startMinMenuItem=True`,
  `minimizeToTrayMenuItem=True`, `checkUpdatesAtStartMenuItem=False`)
  into whatever's currently on disk, preserving any unrelated keys
  the user set via the LHM UI. Atomic write (temp file + `os.replace`)
  so a power loss mid-write can't leave an unparseable config.
  Version-agnostic: any LHM 0.9.x release that honors the standard
  .NET `appSettings` schema works.
- **LHM HTTP-ready probe.** The supervisor now polls
  `127.0.0.1:<port>` for up to 15 s after spawn and records a clear
  `LhmStatus.last_error` if the server doesn't come up — distinguishing
  "process is alive but server never bound" from "process exited"
  on the dashboard. New `LhmStatus.http_ready` boolean exposes the
  result to the API.

### Changed

- **GPU/CPU device detection in the LHM collector** now matches
  vendor names ("NVIDIA GeForce RTX 5090", "Intel Core i9-13900K",
  "AMD Ryzen 9 7950X3D") rather than requiring the literal word
  "GPU"/"CPU" in the device label, which LHM rarely uses.

- **Per-rule advanced controls** on the Rules tab. Threshold slider was
  the v0.2 primary control; this expands the "Advanced…" toggle on each
  card to let the user edit `window_s` (sustained-for seconds),
  `cooldown_s` (per-rule override of the post-action cooldown), and
  `action` (kill / throttle / log). Each editor commits debounced and
  surfaces server validation errors inline.
- **`PATCH /rules/<base_rule>` accepts a multi-field body**. Beyond the
  v0.2 `{threshold}`-only contract, the API now accepts any subset of
  `{threshold, window_s, cooldown_s, action, min_fraction_over}` in a
  single request. Each field is bounds-checked before the comment-
  preserving on-disk rewrite.
- **`config_writer.update_rule_field()`**: generalized
  comment-preserving, atomic-write rule field mutator. Replaces an
  existing field line in place when present, injects a new one at the
  end of the rule block when not (e.g. `cooldown_s` often omitted from
  defaults). The dashboard whitelists which fields it's allowed to
  mutate via `MUTABLE_RULE_FIELDS`.

### Changed

- **`update_rule_threshold` is now a thin wrapper** over
  `update_rule_field`. Existing callers (CLI, profile presets) keep
  their contract; multi-field PATCH callers use the generalized writer.
- **`/rules` GET surfaces `cooldown_s`** so the editor can show what's
  actually on disk vs. inheriting from
  `kill.post_kill_cooldown_seconds`.

## [0.2.0] — Tauri tray app + dashboard

User-mode tray icon and dashboard alongside the LocalSystem watchdog
service. The watchdog itself is unchanged on the wire (same
`config.toml`, same `events.jsonl`, same kill semantics); the tray adds
a way to *see* what it's doing without grepping logs.

### Added

- **Tauri tray app + dashboard.** Always-on tray icon (Healthy /
  Degraded / Alerting / Down) with a right-click menu (Pause for
  30 m / 1 h / 4 h / Until reboot, Open events.jsonl, Open
  watchdog.log, About, Quit). Left-click toggles the main dashboard
  window. Closing the window hides it -- the tray is the persistent
  surface, the window is a lens.
- **Localhost HTTP API on `127.0.0.1:8765`** (`http.server`-only, no
  FastAPI). Endpoints: `GET /health`, `/signals`, `/signals/history`,
  `/rules`, `/events`; `POST /pause`, `/unpause`, `/reload`,
  `/profile`; `PATCH /rules/<name>`. Loopback bind by default.
- **Dashboard tabs:** Signals (drag-and-drop sortable sparkline grid;
  click a tile to drill into 1 h / 6 h / 24 h history with
  multi-resolution downsampling), Rules (per-rule cards + threshold
  sliders), Events (audit log tail with click-to-expand JSON), Status
  (collector health, version, uptime).
- **Per-rule threshold sliders** with live "Aggressive / Normal /
  Relaxed" tier tooltip while dragging. Debounced PATCH on release;
  service hot-reloads the engine without restart.
- **Profile preset row** (Aggressive / Normal / Relaxed / Custom).
  Custom auto-illuminates when any slider diverges from the canonical
  preset.
- **Kill notifications:** Windows system toast via
  `tauri-plugin-notification` plus an in-app red banner. Headline
  reads "killed train.py" -- the script is extracted from the killed
  process tree's command line and persisted into `events.jsonl` and
  `/health.last_action.script`.
- **HKCU\Run autostart** for the tray app via `winreg`. Idempotent;
  no UAC prompt; user can disable from Task Manager → Startup.
- **Multi-resolution signal history** server-side: 1 Hz for the last
  hour, 10 s averages for 1 – 6 h, 60 s averages for 6 – 24 h. ~115 KB
  per signal.
- **Atomic comment-preserving config rewrites** for slider edits via
  a regex-based mutator (no new TOML round-trip dependency).
- **Brand assets** under `brand/`: hand-painted AT-Field logo set
  with size variants for tray, taskbar, and installer icons.

### Changed

- **Default tray window: 1160 × 720** (was 720 × 720) so the
  two-column Signals grid lands on first launch.
- **Default tab → Signals.** The live data view is what people open
  the dashboard *for*; Status moved to last position.
- **Sparkline color ramp:** brand-coherent (warm slate → brand
  orange at threshold → deep red over) instead of the
  high-saturation plasma colormap. Color is anchored to the value's
  *distance from threshold* rather than the visible Y range, so it
  reads consistently regardless of zoom.
- **Sparkline opacity gradient:** quadratic curve with peaks at
  ~100% opaque and troughs at ~30%. Spikes pop, quiet stretches
  recede.
- **Signal display names** rewritten for glanceability:
  `gpu.0.core_temp_c → "GPU 0 Core Temp (°C)"`. Bytes-suffixed
  signals are hidden from the default Signals grid (their percent
  companion shows the same intensity in a more glanceable unit);
  bytes still on the wire for power-user tooling.
- **System memory signals** renamed to match Windows Task Manager
  terminology: `system.commit_percent → "Committed memory (%)"`,
  `system.swap_used_percent → "Page file used (%)"`.
- **CPU rename:** "CPU package" → "CPU" everywhere user-facing.
- **Rules UI:** humanized titles + descriptions ("GPU running hot")
  instead of raw rule names. Trigger thresholds and lines drawn in
  brand red.
- **Refresh button** now bumps a `refreshGen` counter every screen
  subscribes to, so one click refreshes every poll loop
  simultaneously.

### Fixed

- SVG `<text>` elements inherit `var(--font-sans)` instead of
  falling back to Times New Roman.
- "Service unreachable" copy in the dashboard header now suggests
  concrete next steps (start the service, check the port).

## [0.1.0] — Initial watchdog

First public release. The complete watchdog loop with no UI: a Python
service running as `LocalSystem`, configured by `config.toml`, with an
audit trail in `events.jsonl`.

### Added

- **Conservative-profile defaults** (PLANNING.md §3 / §8): five rules
  for VRAM-junction, GPU-core, system-RAM, pagefile, and CPU-package
  temperature thresholds.
- **Three-tier collector stack:**
  - Tier 1 / NVML (`pynvml`) for per-GPU temps, VRAM usage, power
    draw.
  - Tier 2 / `psutil` for system RAM, swap, CPU.
  - Tier 3 / LibreHardwareMonitor HTTP plugin for VRAM-junction temp
    on consumer GPUs and CPU-package temp.
- **Sliding-window rule evaluation:** N-of-M samples over threshold
  triggers an action. Per-rule cooldowns prevent action storms.
- **Process-tree-aware kill:** walks the tree, finds the launcher
  parent (the dispatcher you actually want to kill), respects
  configurable `killable` / `launcher` / `never_kill` allowlists.
- **Audit trail:** every signal sample, rule verdict, and action is
  appended to `events.jsonl`. Watchdog stdout/stderr to
  `watchdog.log`.
- **CLI:** `atf install`, `atf uninstall`, `atf run`, `atf status`,
  `atf show-config`, `atf events`. Service registration via NSSM.
- **PowerShell installer** (`scripts/install_service.ps1`) that
  downloads NSSM, registers the service as `LocalSystem`, sets it to
  auto-start, and starts it.
- **Multi-OS CI** (Windows + Linux + macOS) running 129 tests with
  ruff lint.

[Unreleased]: https://github.com/alonsorobots/at-field/compare/v0.4.4...HEAD
[0.4.4]: https://github.com/alonsorobots/at-field/compare/v0.4.3...v0.4.4
[0.4.3]: https://github.com/alonsorobots/at-field/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/alonsorobots/at-field/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/alonsorobots/at-field/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/alonsorobots/at-field/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/alonsorobots/at-field/releases/tag/v0.3.0
[0.2.0]: https://github.com/alonsorobots/at-field/releases/tag/v0.2.0
[0.1.0]: https://github.com/alonsorobots/at-field/releases/tag/v0.1.0
