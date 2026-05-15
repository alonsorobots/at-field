# Changelog

All notable changes to AT-Field are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Notes for the next release tag (v0.2.0 candidate)

The dashboard polish + rule sliders + tray autostart work currently sits
on `main`. Once the bundled NSIS installer (which wraps the Python
service binaries together with the tray app) lands, this section gets
promoted to a proper `## [0.2.0]` heading.

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

[Unreleased]: https://github.com/alonsorobots/at-field/compare/v0.1.0...HEAD
[0.2.0]: https://github.com/alonsorobots/at-field/releases/tag/v0.2.0
[0.1.0]: https://github.com/alonsorobots/at-field/releases/tag/v0.1.0
