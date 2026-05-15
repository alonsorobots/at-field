# AT-Field — Planning & Handoff Document

> **Purpose of this doc:** capture every design decision made in the bootstrap conversation so a fresh Cursor agent (or human) can pick up implementation without losing context. The full chat transcript that produced these decisions lives at `docs/chat-history.md`.

## 1. Elevator pitch

**AT-Field** is an always-on Windows hardware watchdog for AI workloads. It runs as a Windows Service, watches GPU/VRAM temperatures, system RAM, pagefile, and CPU temperature, and intercepts runaway Python/PyTorch processes **before** they damage hardware — with sustained-window logic that tolerates short load-time spikes.

The name is an Evangelion reference (the Absolute Terror Field that protects an Eva from harm), retconned as a backronym: **A**bsolute **T**hermal-and-memory **Field**. Eva fans get the wink; ML engineers reading the README get a self-descriptive acronym.

Discoverability is handled separately from the brand via topics, description SEO, README keywords, awesome-list submissions, and a launch blog post (see §10).

## 2. Why this exists

Researched alternatives (see `docs/chat-history.md` §Search-results for the full sweep):

| Repo | What it does | Why it doesn't fit |
|---|---|---|
| [Yp-pro/VRAM-Guard](https://github.com/Yp-pro/VRAM-Guard) | VRAM-temp throttle, tray app | No RAM/pagefile, no service, no Python-tree targeting |
| [Thymester/System-Resource-Monitor](https://github.com/Thymester/System-Resource-Monitor) | Threshold alerts | Alerts only — never kills |
| [Youda008/HwMonitorService](https://github.com/Youda008/HwMonitorService) | LHM-as-service over TCP | Data plane only, no policy/action |
| [darkbrain-fc/HardwareSupervisor](https://github.com/darkbrain-fc/HardwareSupervisor) | C# fan-curve service | Wrong domain |
| [NoCoderRandom/killall](https://github.com/NoCoderRandom/killall) | CLI `killall gpu --threshold N` | Manual primitive, no sustained-window logic |

**The gap:** there is no "earlyoom for Windows + GPU" that combines (a) sustained multi-signal triggers, (b) GPU temp + VRAM + RAM + pagefile + CPU temp, (c) process-tree-aware Python-launcher targeting, (d) true Windows Service deployment.

## 3. Locked-in decisions

| Decision | Value | Rationale |
|---|---|---|
| **Name** | AT-Field | Eva-reference brand, recontextualized as backronym |
| **Repo** | `alonsorobots/at-field` on GitHub | already created |
| **Package** | `atfield` (PyPI) | hyphen-stripped for pip compatibility |
| **CLI** | `atf` (primary), `atfield` (alias) | short, memorable |
| **Service display name** | `AT-Field Watchdog` | what shows in `services.msc` |
| **License** | MIT | max adoption |
| **Language** | Python ≥ 3.10 | low barrier for ML-crowd contributions |
| **Service deployment** | NSSM, runs as `LocalSystem` at boot | works without user login |
| **Notification mode (initial)** | Log-only (`events.jsonl` + rotating log) | no toast/Discord clutter; can add later |
| **Threshold profile (initial)** | Conservative | VRAM-junction >90°C/20s, GPU core >83°C/30s, RAM >85%/60s, CPU >90°C/30s |
| **Kill mode** | Graceful SIGTERM → 5s grace → SIGKILL, **process-tree aware** | see §5.3 |
| **Kill scope (initial)** | Python-only (python.exe / pythonw.exe / python3.exe), config.toml extensible | conservative default, easy to widen |
| **Working dir** | `C:\Users\admin\Desktop\RESEARCH\at-field` | local repo |
| **State dir (runtime)** | `%ProgramData%\ATField\` | machine-wide, service-readable |
| **Cursor workspace** | `C:\Users\admin\Desktop\RESEARCH\workspaces\at-field.code-workspace` | points at the repo via `../at-field` |

## 4. Target hardware (this user's rig, for tuning defaults)

- **CPU:** AMD Ryzen 9 9950X3D (360mm AIO)
- **GPU:** 2× NVIDIA RTX 5090, 32 GB GDDR7 each
- **RAM:** 128 GB DDR5-5600
- **OS:** Windows 11 Home 64-bit

Multi-GPU enumeration is therefore a first-class requirement. Per-GPU rules and per-GPU process maps are mandatory.

## 5. Architecture

```
┌──────────────────── Windows Service (NSSM-wrapped) ────────────────────┐
│                                                                          │
│   ┌─────────────┐    ┌────────────────────────────┐    ┌──────────────┐│
│   │ Collectors  │ ─► │ SignalStore (sliding win)  │ ─► │ PolicyEngine ││
│   │             │    │ - per-signal EMA           │    │ - rules eval ││
│   │ • NVML      │    │ - N-of-M over threshold    │    │ - cooldowns  ││
│   │ • nvidia-smi│    │ - hysteresis               │    │ - escalation ││
│   │ • psutil    │    └────────────────────────────┘    └──────┬───────┘│
│   │ • LHM HTTP  │                                              │        │
│   └─────────────┘                                              ▼        │
│                                                  ┌────────────────────┐ │
│                                                  │ Actuator            │ │
│                                                  │ - tree-aware kill   │ │
│                                                  │ - launcher heuristic│ │
│                                                  │ - audit log         │ │
│                                                  └────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
                  %ProgramData%\ATField\
                  ├── config.toml      (user-editable rules)
                  ├── watchdog.log     (rotating, INFO+ events)
                  └── events.jsonl     (one line per action, audit trail)
```

### 5.1 Signal set (1 Hz tick)

**Per-GPU (NVML):**
- `gpu[i].core_temp_c`
- `gpu[i].mem_junction_temp_c` *(via LHM — NVML doesn't expose VRAM temp on consumer cards)*
- `gpu[i].vram_used_bytes` / `vram_total_bytes`
- `gpu[i].util_percent`
- `gpu[i].power_w` / `power_limit_w`

**Per-GPU process map (`nvidia-smi --query-compute-apps`):**
- `{gpu_idx → [(pid, used_vram_bytes), …]}`

**System (psutil):**
- `system.ram_used_percent`
- `system.swap_used_percent` (pagefile)
- `system.commit_percent` (Win32 `GlobalMemoryStatusEx` via ctypes for true commit charge)
- `system.cpu_package_temp_c` (via LHM)

### 5.2 Sustained-window rule model

Every rule is `{ signal, threshold, window_s, min_fraction_over, cooldown_s, action }`.

> Example: `{signal="gpu0.mem_junction_temp_c", threshold=95, window_s=20, min_fraction_over=0.67, cooldown_s=120, action="kill"}` means: *"if VRAM junction temp is over 95 °C for at least 2/3 of the last 20 seconds, take action `kill` and don't re-trigger for 120 s."*

A 2-second spike during model warmup never triggers. An actual sustained problem triggers within ~15 s.

### 5.3 Kill targeting (the important part)

User-raised concern (verbatim from chat): *"many jobs have coordinators with self-healing workers so you need to know how to actually kill dispatcher. Not sure how you can best generalize this to all code and setups."*

**Generalized strategy:**

```python
def find_kill_root(offender_pid: int, launcher_names: set[str]) -> psutil.Process:
    """Walk up the parent chain. Stop at the highest python.exe (or known launcher)
    whose parent is NOT itself python or a known launcher. Return that PID as
    the root to kill — its entire descendant tree comes with it."""
    proc = psutil.Process(offender_pid)
    while True:
        parent = proc.parent()
        if parent is None: break
        if parent.name().lower() in PYTHON_NAMES | launcher_names:
            proc = parent
            continue
        break
    return proc
```

`launcher_names` is configurable and ships with: `{"torchrun", "accelerate", "deepspeed", "mpiexec", "ray", "ray-worker", "jupyter", "ipykernel_launcher"}`.

Then enumerate descendants (`proc.children(recursive=True)`), SIGTERM all → wait 5 s → SIGKILL survivors.

The whole tree gets logged to `events.jsonl` as a single audit record with parent and children PIDs/names/cmdlines.

### 5.4 Cross-cutting concerns

- **Admin/SYSTEM:** service runs as `LocalSystem` so it can SIGKILL processes in any user session.
- **Self-protection:** never kill self (PID of the service process is filtered out).
- **Safe-mode:** if config.toml is malformed → run in observe-only mode, log everything, kill nothing.
- **Pause:** `atf pause 30m` writes a sentinel file with an expiry timestamp; service skips actions while sentinel valid.
- **Health beacon:** the service writes a heartbeat file every 10 s; CLI `atf status` reads it.

## 6. Repo layout (target)

```
at-field/
├── README.md                       ← keyword-rich for SEO (see §10)
├── PLANNING.md                     ← this doc
├── LICENSE                         ← MIT, in place
├── pyproject.toml                  ← in place
├── .gitignore                      ← in place
├── src/atfield/
│   ├── __init__.py
│   ├── __main__.py                 ← `python -m atfield`
│   ├── cli.py                      ← Typer app, commands: status / pause / unpause / tail / test-kill / version / install / uninstall
│   ├── service.py                  ← entrypoint for NSSM, signal-handled main loop
│   ├── collectors/
│   │   ├── __init__.py
│   │   ├── nvml.py                 ← per-GPU temp/util/VRAM via NVML
│   │   ├── nvidia_smi.py           ← per-process VRAM via subprocess (NVML can't always do this)
│   │   ├── system.py               ← psutil RAM/swap + Win32 commit-charge
│   │   └── lhm.py                  ← LibreHardwareMonitor HTTP client (CPU pkg temp, GPU mem junction temp)
│   ├── signals.py                  ← SlidingWindow, EMA, N-of-M rule evaluator
│   ├── policy.py                   ← load config → evaluate signals → emit Actions
│   ├── actuator.py                 ← process-tree-aware killer (see §5.3)
│   ├── config.py                   ← TOML schema, defaults (conservative profile), validation
│   ├── audit.py                    ← events.jsonl writer + rotating log setup
│   └── lhm_bootstrap.py            ← download/start LibreHardwareMonitor.exe sidecar
├── tests/
│   ├── conftest.py
│   ├── test_signals.py             ← deterministic sliding-window tests
│   ├── test_policy.py
│   ├── test_actuator.py            ← fake process trees via FakeProc
│   └── test_config.py
├── scripts/
│   ├── install_service.ps1         ← downloads NSSM, registers service as LocalSystem
│   ├── uninstall_service.ps1
│   └── config.example.toml         ← annotated conservative-profile default
├── docs/
│   ├── architecture.md             ← deeper version of §5
│   ├── tuning.md                   ← how to pick thresholds per GPU model
│   ├── faq.md                      ← "why did it kill my job?"
│   └── chat-history.md             ← bootstrap conversation transcript
└── .github/workflows/
    ├── ci.yml                      ← ruff + pytest on push (Windows runner)
    └── release.yml                 ← build wheel + PyInstaller exe on tag
```

## 7. Implementation order (todo list at handoff)

| # | Status | Task |
|---|---|---|
| 1 | ✅ done | Scaffold (LICENSE, .gitignore, pyproject.toml) |
| 2 | ⏳ next | `src/atfield/config.py` — TOML schema, defaults, validation |
| 3 | ⏳ next | `src/atfield/signals.py` — SlidingWindow, EMA, N-of-M evaluator + unit tests |
| 4 | ⏳ next | `src/atfield/collectors/{nvml,nvidia_smi,system,lhm}.py` — sensor adapters |
| 5 | ⏳ next | `src/atfield/policy.py` — rule loader + evaluation loop |
| 6 | ⏳ next | `src/atfield/actuator.py` — process-tree-aware kill with launcher heuristic |
| 7 | ⏳ next | `src/atfield/audit.py` — events.jsonl + rotating log |
| 8 | ⏳ next | `src/atfield/service.py` — main loop with graceful shutdown for NSSM |
| 9 | ⏳ next | `src/atfield/cli.py` — Typer commands (status/pause/tail/test-kill/install/uninstall/version) |
| 10 | ⏳ next | `scripts/install_service.ps1` + `uninstall_service.ps1` — NSSM-based |
| 11 | ⏳ next | `scripts/config.example.toml` — annotated conservative defaults |
| 12 | ⏳ next | Tests: `test_signals.py`, `test_policy.py`, `test_actuator.py`, `test_config.py` |
| 13 | ⏳ next | `.github/workflows/ci.yml` — Windows runner, ruff + pytest |
| 14 | ⏳ next | Write proper `README.md` (replaces 2-line stub) — keyword-rich, screenshots placeholder |
| 15 | ⏳ next | `docs/architecture.md`, `docs/tuning.md`, `docs/faq.md` |
| 16 | ⏳ later | `.github/workflows/release.yml` — PyInstaller exe on tag |
| 17 | ⏳ later | Submit to `awesome-windows`, `awesome-python`, `awesome-machine-learning` |
| 18 | ⏳ later | Launch blog post: *"Why your Windows AI rig needs an earlyoom equivalent (and how I built one)"* |

## 8. Configuration shape (initial spec)

`config.example.toml` will look approximately like this:

```toml
# AT-Field configuration. Edit and reload with `atf reload`.
# See https://github.com/alonsorobots/at-field/blob/main/docs/tuning.md

[general]
tick_hz = 1                  # collector poll rate
log_level = "INFO"
state_dir = "C:\\ProgramData\\ATField"

[targeting]
# Process names the actuator is allowed to kill. Walk up the tree past these.
killable_names    = ["python.exe", "pythonw.exe", "python3.exe"]
launcher_names    = ["torchrun", "accelerate", "deepspeed", "mpiexec", "ray", "jupyter", "ipykernel_launcher"]
never_kill_names  = ["explorer.exe", "services.exe", "code.exe", "windbg.exe"]

[kill]
mode             = "graceful"   # graceful | aggressive
grace_seconds    = 5
post_kill_cooldown_seconds = 60

# === Rules ===
# Each rule: signal, threshold, window_s, min_fraction_over (0-1), action.
# action ∈ "log" | "throttle" | "kill"

[[rules]]
name              = "vram-junction-hot"
signal            = "gpu.*.mem_junction_temp_c"
threshold         = 90
window_s          = 20
min_fraction_over = 0.67
action            = "kill"

[[rules]]
name              = "gpu-core-hot"
signal            = "gpu.*.core_temp_c"
threshold         = 83
window_s          = 30
min_fraction_over = 0.67
action            = "kill"

[[rules]]
name              = "ram-pressure"
signal            = "system.ram_used_percent"
threshold         = 85
window_s          = 60
min_fraction_over = 0.75
action            = "kill"

[[rules]]
name              = "pagefile-pressure"
signal            = "system.commit_percent"
threshold         = 90
window_s          = 60
min_fraction_over = 0.75
action            = "kill"

[[rules]]
name              = "cpu-pkg-hot"
signal            = "system.cpu_package_temp_c"
threshold         = 90
window_s          = 30
min_fraction_over = 0.67
action            = "kill"
```

The `gpu.*.X` glob expands per-detected-GPU at startup.

## 9. Key external APIs we depend on

- **`nvidia-ml-py`** (NVML bindings): GPU enumeration, core temp, util, VRAM bytes, power.
- **`nvidia-smi --query-compute-apps=pid,gpu_uuid,used_memory --format=csv,noheader`**: per-process VRAM. NVML can do this too via `nvmlDeviceGetComputeRunningProcesses` but the subprocess fallback is sometimes more reliable on consumer drivers.
- **`psutil`**: process tree, suspend/resume/terminate/kill, virtual_memory(), swap_memory().
- **Win32 `GlobalMemoryStatusEx`** (via ctypes): true commit charge percent (more accurate than `psutil.swap_memory()` on Windows).
- **LibreHardwareMonitor** v0.9.5+ HTTP API (`/data.json` on `127.0.0.1:8085`): CPU package temp, GPU memory junction temp (the VRAM temp that NVML doesn't expose on consumer cards).
- **NSSM** v2.24+: Windows Service wrapper. Downloaded by `install_service.ps1`.

VRAM Guard's `lhm_client.py` (MIT-compatible behavior — we're not copying code but learning from approach) demonstrates the LHM-as-subprocess pattern; see `docs/chat-history.md` for the relevant excerpts.

## 10. README SEO strategy (since brand-name isn't searchable)

Per the chat decision (§"naming-strategy"), discoverability is split from branding. The README must:

1. **H1:** `# AT-Field — Windows GPU/VRAM/RAM watchdog for AI workloads`
2. **GitHub repo description (set via UI):** *"Always-on Windows hardware watchdog for AI workloads. Monitors NVIDIA GPU/VRAM temps, system RAM, pagefile and CPU temperature; kills runaway Python/PyTorch processes before they damage your hardware. Runs as a Windows Service."*
3. **GitHub topics (up to 20):** `gpu-monitor, vram, nvidia, windows-service, thermal-protection, python, pytorch, machine-learning, oom-killer, hardware-monitor, ai-tools, process-killer, gpu-watchdog, nvml, ryzen, rtx, deep-learning, gpu-temperature, ai-safety, vram-guard`
4. **First paragraph keyword-stuffed** with natural phrasing of every search query above.
5. **`## Why?` section** literally listing the search queries that should land here ("Looking for earlyoom on Windows? GPU VRAM temperature too high? Process kept crashing your machine during training?").
6. **PyPI keywords** mirror GitHub topics (already in `pyproject.toml`).
7. **Awesome-list submissions** are issue/PR work, scheduled post-v0.1 (todo §18).

## 11. Quick orientation for the next agent

When you (next agent) start in `C:\Users\admin\Desktop\RESEARCH\at-field`:

1. Read this `PLANNING.md` (you're here).
2. Skim `docs/chat-history.md` if you want the original reasoning behind any decision.
3. Pick up at todo §2 (`src/atfield/config.py`) and march through §3 → §17.
4. Commit per-module with conventional commits (`feat: signals - implement sliding window`, etc).
5. When all of §2–§13 are green and tests pass on Windows, tag `v0.1.0` and the user can decide on a release blog post.

If anything in §3 (locked-in decisions) feels wrong as you implement, **stop and ask before changing** — those were the result of an interactive Q&A. Don't silently revise them.
