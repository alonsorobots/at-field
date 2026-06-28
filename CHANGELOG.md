# Changelog

All notable changes to AT-Field are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
