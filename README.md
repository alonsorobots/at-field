# AT-Field — Windows GPU/VRAM/RAM watchdog for AI workloads

> Always-on Windows hardware watchdog for AI workloads. Monitors NVIDIA GPU and VRAM temperatures, GPU memory usage, system RAM, pagefile, and CPU package temperature — and kills runaway Python / PyTorch processes **before** they damage your hardware. Runs as a Windows Service. Tolerates load-time spikes via sustained-window thresholds.

> **Status:** 🚧 design locked, implementation in progress. See [`PLANNING.md`](PLANNING.md) for the roadmap and [`docs/chat-history.md`](docs/chat-history.md) for the design rationale.

In *Neon Genesis Evangelion*, an **AT Field** is an absolute defensive barrier that prevents catastrophic damage to a high-power system. Here it's recontextualized as a backronym — **A**bsolute **T**hermal-and-memory **Field** — a Python-aware Windows service that intercepts the AI jobs trying to melt your rig.

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
- **Sustained-window logic, not instantaneous:** a 2-second spike during model warmup never triggers. An actual sustained problem triggers in seconds.
- **Process-tree-aware kill:** when a runaway job is detected, walks the parent chain past known AI launchers (`torchrun`, `accelerate`, `deepspeed`, `mpiexec`, `ray`, `jupyter`) to find the *dispatcher*, then terminates the whole tree. Self-healing workers can't respawn.
- **Runs as a Windows Service** (via NSSM) under `LocalSystem` — works without an interactive login, survives reboots, no tray icon required.
- **Audit trail:** every action lands in `%ProgramData%\ATField\events.jsonl` with full process tree, signal values that triggered the rule, and rule name.
- **Safe by default:** malformed config → observe-only mode, never kills.

## Status

Pre-release. Design is locked (see [`PLANNING.md`](PLANNING.md)). Implementation is in progress on `main`. v0.1.0 target is a working service with conservative defaults for NVIDIA + AMD-CPU Windows 11 rigs.

## Hardware targets

Primary development rig:

- **CPU:** AMD Ryzen 9 9950X3D
- **GPU:** 2× NVIDIA RTX 5090 (32 GB GDDR7)
- **RAM:** 128 GB DDR5-5600
- **OS:** Windows 11

Should work on any Windows 10/11 box with NVIDIA driver ≥ 535. AMD GPU support is not in scope for v0.1 (PRs welcome).

## License

[MIT](LICENSE)
