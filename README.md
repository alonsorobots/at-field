# AT-Field — Windows GPU/VRAM/RAM watchdog for AI workloads

> Always-on Windows hardware watchdog for AI workloads. Monitors NVIDIA GPU and VRAM temperatures, GPU memory usage, system RAM, pagefile, and CPU package temperature — and kills runaway Python / PyTorch processes **before** they damage your hardware. Runs as a Windows Service. Tolerates load-time spikes via sustained-window thresholds.

> **Status:** v0.1 (CLI + service) is implementation-complete on `main`; v0.2 adds a Tauri tray app + dashboard ([at-field-tray/](at-field-tray/)) and a single-installer `.exe`.

In *Neon Genesis Evangelion*, an **AT Field** is an absolute defensive barrier that prevents catastrophic damage to a high-power system. Here it's recontextualized as a backronym — **A**bsolute **T**hermal-and-memory **Field** — a Python-aware Windows service that intercepts the AI jobs trying to melt your rig.

## Two pieces, one tool

```
┌────────────────────────────────┐          ┌─────────────────────────────────┐
│  AT-Field Watchdog Service     │          │  AT-Field Tray + Dashboard      │
│  Python, NSSM, LocalSystem     │ ◄──HTTP──│  Tauri (Rust + React),          │
│  Always-on, no UI              │  loopback │  user-mode, optional            │
│  src/atfield/                  │   :8765   │  at-field-tray/                 │
└────────────────────────────────┘          └─────────────────────────────────┘
```

The **service** (this repo, `src/atfield/`) is the engine — it does all the
sensing, deciding, and killing. It runs as a Windows Service so it stays alive
without a logged-in user.

The **tray app** ([at-field-tray/](at-field-tray/)) is a thin lens over the
service. It puts a status dot in your system tray (green / yellow / red /
gray), pops open a dashboard with live signal sparklines and recent events,
and gives you a one-click pause toggle. It's optional — the service works
fine headless.

## Project goals

These are the north stars every design decision is measured against:

1. **One-command install.** `pip install atfield && atf install` should be the entire setup. No manual NSSM download, no manual sensor-daemon install, no editing service definitions in `services.msc`.
2. **Works on most setups out of the box.** Any Windows 10/11 box with stock Python 3.10+ and an NVIDIA GPU should get useful protection immediately. Single-GPU, multi-GPU, AMD-CPU, Intel-CPU — capability is detected at startup; rules whose sensors aren't available are auto-disabled with a clear log message rather than failing the service.
3. **Zero config for the common case.** The shipped defaults protect a typical AI rig without the user opening `config.toml`. Configuration exists for power users, not as a prerequisite. Adding new hardware support must not require existing users to migrate their config.

## Why?

If any of these are your search query, you're in the right place:

- *"earlyoom for Windows"*
- *"automatic process killer when GPU VRAM temperature too high"*
- *"NVIDIA RTX VRAM thermal protection sustained threshold"*
- *"kill python process when RAM exceeds threshold Windows service"*
- *"PyTorch / accelerate / torchrun killed my machine again"*
- *"how do I protect dual-GPU AI rig from runaway training jobs"*

Existing tools either monitor without acting ([System-Resource-Monitor](https://github.com/Thymester/System-Resource-Monitor)), throttle a single VRAM signal ([VRAM-Guard](https://github.com/Yp-pro/VRAM-Guard), [VRAM Shield](https://vramshield.com/)), or are manual primitives ([killall](https://github.com/NoCoderRandom/killall)). AT-Field combines **multi-signal sustained triggers**, **process-tree-aware Python targeting**, and **true Windows Service** deployment.

## What it does

- **Watches every signal that matters:** per-GPU core temp, GPU memory junction temp (the GDDR temp NVML doesn't expose on consumer cards — read via LibreHardwareMonitor), VRAM used %, system RAM %, pagefile / commit charge, CPU package temp.
- **Sustained-window logic, not instantaneous:** a 2-second spike during model warmup never triggers. An actual sustained problem triggers within ~15 seconds.
- **Process-tree-aware kill:** when a runaway job is detected, walks the parent chain past known AI launchers (`torchrun`, `accelerate`, `deepspeed`, `mpiexec`, `ray`, `jupyter`) to find the *dispatcher*, then terminates the whole tree. Self-healing workers can't respawn.
- **Runs as a Windows Service** (via NSSM) under `LocalSystem` — works without an interactive login, survives reboots, no tray icon required.
- **Capability-negotiated:** at startup, every collector probes its source. Anything missing (no LHM running? AMD GPU? CPU sensor doesn't expose package temp?) downgrades to "rule disabled, with a clear log line" — never to "watchdog crashed" or "rule silently never fires".
- **Audit trail:** every action lands in `%ProgramData%\ATField\events.jsonl` with full process tree, signal values that triggered the rule, and rule name.
- **Safe by default:** malformed config → observe-only mode (kills demoted to log entries), never kills the service's own PID, `never_kill_names` filter for `explorer.exe`, `services.exe`, etc.

## Install

```powershell
# 1. Install the package
pip install atfield

# 2. Register the Windows Service (run from elevated PowerShell)
atf install
```

That's it. The installer downloads NSSM into `%ProgramData%\ATField\`, registers `AT-Field Watchdog` as a `LocalSystem` auto-start service, drops a starter `config.toml`, and starts the service. Existing config is preserved on reinstall.

For VRAM-junction-temp and CPU-package-temp protection (the two most thermally-relevant signals on consumer NVIDIA cards), additionally install [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) v0.9.5+ and enable its built-in HTTP server (Options → Run web server, port 8085). The watchdog auto-detects it next time the service ticks; no config changes needed.

## Usage

```bash
atf status          # service health, working signal map, disabled rules
atf inputs          # one-shot probe + sample dump (use this to verify setup)
atf tail            # follow events.jsonl
atf pause 30m       # suspend kill actions for 30 minutes
atf unpause
atf test-kill <PID> # dry-run the kill walk-up against a real PID
atf run             # foreground run (for debugging without NSSM)
atf uninstall
```

`atf inputs` is the verification tool — run it after installing to confirm which sensors are visible and which rules will be active:

```text
$ atf inputs
system: OK
  reason: psutil + Win32 GlobalMemoryStatusEx OK
    system.commit_percent = 11.681 percent
    system.ram_used_percent = 18.600 percent

nvml: OK
  reason: NVML driver 596.36, 2 GPU(s): NVIDIA GeForce RTX 5090, NVIDIA GeForce RTX 5090
    gpu.0.core_temp_c = 38.000 celsius
    gpu.0.vram_used_percent = 8.891 percent
    gpu.1.core_temp_c = 35.000 celsius
    ...

lhm: unavailable
  reason: LibreHardwareMonitor HTTP not reachable at http://127.0.0.1:8085/data.json;
          ensure LHM is installed, running, and 'Run web server' is enabled in Options
```

## Configuration

Defaults are conservative and protect a typical AI rig with no edits. To customize, see [`scripts/config.example.toml`](scripts/config.example.toml). The installer drops this in `%ProgramData%\ATField\config.toml` if no config exists.

A rule has the form:

```toml
[[rules]]
name              = "gpu-core-hot"
signal            = "gpu.*.core_temp_c"   # glob expands per detected GPU
threshold         = 83
window_s          = 30
min_fraction_over = 0.67                  # 67% of last 30s over threshold
action            = "kill"                # log | throttle | kill
```

Rules referencing signals no probed collector provides are auto-disabled with a startup log line — adding new hardware support never breaks existing configs.

## Status

Pre-release v0.1.0. End-to-end verified on the development rig (2× RTX 5090). CI runs the test suite on Windows + Linux × Python 3.10/3.11/3.12 plus a wheel install + CLI smoke test.

Primary development rig:

- **CPU:** AMD Ryzen 9 9950X3D
- **GPU:** 2× NVIDIA RTX 5090 (32 GB GDDR7)
- **RAM:** 128 GB DDR5-5600
- **OS:** Windows 11

Should work on any Windows 10/11 box with NVIDIA driver ≥ 535. AMD-GPU support is best-effort via LibreHardwareMonitor; a dedicated AMD/ROCm collector is a v0.2 candidate (PRs welcome — the `Collector` protocol in [`src/atfield/collectors/__init__.py`](src/atfield/collectors/__init__.py) is the public extension point).

## Architecture

```
┌──────────────────── Windows Service (NSSM-wrapped) ──────────────────┐
│                                                                       │
│   ┌─────────────┐   ┌────────────────────────┐   ┌─────────────────┐ │
│   │ Collectors  │──►│ SignalStore + windows  │──►│ PolicyEngine    │ │
│   │ • system    │   │ - per-rule sliding     │   │ - glob expand   │ │
│   │ • nvml      │   │ - latest-sample        │   │ - rule eval     │ │
│   │ • lhm       │   │   liveness check       │   │ - cooldowns     │ │
│   │ • plugins   │   └────────────────────────┘   └────────┬────────┘ │
│   └─────────────┘                                          │          │
│                                                            ▼          │
│                                              ┌───────────────────┐    │
│                                              │ Actuator          │    │
│                                              │ - launcher walk-up│    │
│                                              │ - tree kill       │    │
│                                              │ - self-protection │    │
│                                              └─────────┬─────────┘    │
│                                                        ▼              │
│                                              %ProgramData%\ATField\   │
│                                              ├── config.toml          │
│                                              ├── watchdog.log         │
│                                              ├── events.jsonl         │
│                                              └── heartbeat.txt        │
└───────────────────────────────────────────────────────────────────────┘
```

## License

[MIT](LICENSE)
