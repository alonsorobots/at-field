# Resource footprint — what does it cost to run AT-Field always-on?

> Short answer: **far less than the hardware it protects.** On a measured
> RTX 5090 ×2 workstation the watchdog service uses **~0.1–0.3% of a single
> CPU core** (≈0.01% of the machine's total CPU), **~55–85 MB of RAM** (bounded,
> not growing), and appends **~40–90 MB/day** of forensic history that
> auto-rotates and is hard-capped at ~250 MB on disk. It reads GPU telemetry
> the exact same way `nvidia-smi` does — it does **not** launch GPU kernels,
> allocate VRAM, or slow your training. Electricity attributable to it is on
> the order of **$1–4 per year**.

This page exists for one reason: if someone says *"it's useful, but I don't
want to pay for the cost of having that thing running always,"* the numbers
below are the answer — and they're reproducible on your own box with the two
scripts in `scripts/`.

---

## TL;DR table

| Resource | AT-Field service (always-on) | Notes |
|---|---|---|
| **CPU** | ~0.1–0.3% of **one** core | ≈0.01% of a 32-thread workstation. Ticks once per second. |
| **RAM** | ~55–85 MB resident, **bounded** | History is fixed-size ring buffers (~1.6 MB); the rest is the Python runtime + NVML + the HTTP server. Does not grow over time. |
| **Disk write** | ~40–90 MB/day, **capped ~250 MB** | Append-only forensic JSONL, batched every 5 s, auto-rotated at 50 MB. ~0.003%/yr of a typical NVMe's write endurance. |
| **GPU** | read-only telemetry only | Same NVML counters `nvidia-smi` reads. No kernels, no VRAM, no measurable training impact. |
| **Network** | loopback only, idle | `127.0.0.1:8765` HTTP server does nothing until the dashboard or Prometheus polls it. |
| **Power** | well under 1 W (service alone) | See the electricity math below. |
| **LibreHardwareMonitor** (optional) | ~150–330 MB RAM + light CPU | Only needed for CPU-package and VRAM-junction temps. Drop it and the watchdog still runs on NVML + system signals. |

---

## How the work is spent per tick

The service loop runs at `tick_hz = 1` (once per second). Everything it does
in a tick, measured with `scripts/bench_tick.py` on the reference box
(2× RTX 5090, driver 596.36):

| Tick stage | Cost (mean) |
|---|---|
| `nvml.sample()` — temp/util/VRAM/power for both GPUs | **0.014 ms** |
| `system.sample()` — RAM, pagefile, CPU via psutil | 0.055 ms |
| `engine.tick()` — evaluate every rule window | 0.056 ms |
| `record_tick()` — push into in-memory history | 0.005 ms |
| `forensics.record()` — stage one line for the flusher | 0.006 ms |
| **Total per tick** | **≈0.135 ms** |

0.135 ms of work once per second is **0.0135% of one core** of pure compute.
The end-to-end process measurement (`scripts/measure_idle.py`, which also pays
for Python's scheduler, GC, and the 1 Hz wakeup) lands at **0.1–0.3% of one
core** — still negligible, and dominated by interpreter overhead rather than
AT-Field's own work.

### The one optimization that mattered

NVML's per-process *compute-process enumeration*
(`nvmlDeviceGetComputeRunningProcesses`) costs **~0.75 ms** — about **99%** of
the entire collector cost — while every metric read combined (temperature,
utilization, VRAM, power, both GPUs) is **0.006 ms**.

That enumeration is only actually *needed* when a kill fires (to map a GPU
rule to the offending PIDs). So the collector no longer runs it every tick.
It now:

- refreshes the process map on a **5 s cadence** (enough to keep the
  dashboard's GPU-process *count* live), and
- **force-refreshes at kill time**, so targeting uses up-to-the-moment PIDs —
  strictly *fresher* than the old per-tick map.

Net effect on the steady-state tick: **~0.92 ms → ~0.14 ms (≈6.8× cheaper)**,
with no loss of kill accuracy.

---

## Why memory stays flat

Per-signal history is stored in three `collections.deque(maxlen=…)` rings
(raw 1 Hz for the last hour, 10 s means for 1–6 h, 1-min means for 6–24 h).
Worst case is ~7,200 `(timestamp, value)` tuples per signal ≈ **115 KB**;
across ~14 signals that's **~1.6 MB** of history that can **never** grow past
its cap. Everything else in the ~55–85 MB resident set is the Python
interpreter, `pynvml`/`psutil`, and the stdlib HTTP server — all fixed at
import time. The idle measurement shows ~150 KB of growth over the first
minute (the rings filling) and then a plateau.

## Why disk wear is a non-issue

The forensic stream exists so that a *hard* crash (BSOD, Kernel-Power 41, PSU
sag) still leaves the seconds of GPU/temp/power history that bracketed the
event. It's append-only JSONL, **batched and flushed once every 5 seconds** by
a background thread (the main loop never blocks on disk), and **auto-rotated**
at 50 MB with at most a few archives kept — a hard ceiling around **250 MB**
on disk regardless of uptime.

Write *volume* is ~40–90 MB/day depending on how many signals are live
(~450 bytes/tick here). That's roughly **15–35 GB/year**. A typical 2 TB NVMe
is rated for ~1,200 TBW, so AT-Field's forensic writes consume on the order of
**0.003% of the drive's endurance per year**. It is not a meaningful source of
wear.

## Why it doesn't slow your training

GPU signals come from **NVML**, the same in-process NVIDIA library `nvidia-smi`
uses. Reading a temperature or VRAM counter is a lightweight query against the
driver's telemetry — it does **not** submit CUDA work, allocate VRAM, or
contend for SM time. The measured cost of reading *all* metrics for two 5090s
is 6 microseconds. Your training job will not notice it.

The watchdog also never busy-waits: between ticks it does a blocking
`Event.wait(…)`, so the thread is genuinely asleep (no spin), which is what
keeps both CPU and power near zero.

---

## Electricity: the actual dollars

The service's own CPU draw (~0.2% of one core) is on the order of tens of
milliwatts. Even attributing a deliberately generous **1.5 W average** to the
*whole* stack — including LibreHardwareMonitor continuously polling
motherboard/GPU sensors — the math is:

```
1.5 W × 24 h × 365 d = 13.1 kWh / year
  @ $0.17/kWh (2025 US avg)  ≈ $2.23 / year
  @ $0.30/kWh (high CA/EU)   ≈ $3.94 / year
```

Run **without** LHM (NVML + system signals only) and the service alone is
comfortably under 0.5 W → **under $1/year**.

For scale: a single unattended thermal-runaway or VRAM-OOM event that corrupts
a multi-day training run, or a memory-junction temperature left to sit at
110 °C overnight, costs anywhere from *a wasted week of compute* to *a dead
$2,000+ GPU*. AT-Field's entire annual running cost is less than a cup of
coffee.

---

## Don't want even that? Tune it down.

The footprint is already negligible, but every knob is yours:

- **Halve the CPU again:** set `tick_hz = 1` is the default; the loop is linear
  in tick rate, so the cost scales directly with it. (Going below 1 Hz trades
  reaction latency for CPU and is rarely worth it — 1 Hz is already <0.3% of a
  core.)
- **Drop LHM entirely:** if you don't need CPU-package or VRAM-junction temps,
  don't install it. The watchdog auto-disables the rules that need those
  signals (with a clear reason on the Status tab) and reclaims ~150–330 MB of
  RAM. NVML thermal/VRAM/power protection is unaffected.
- **Shrink the forensic stream:** lower the rotation cap if 250 MB is too much
  for a small boot drive (see `forensics.py`).
- **Pause it:** right-click the tray → Pause for a window when you explicitly
  want zero intervention.

---

## Reproduce these numbers yourself

```powershell
# Per-tick microbenchmark (where the CPU goes, by stage):
.venv\Scripts\python.exe scripts\bench_tick.py

# Process-level idle footprint over 60 s (CPU%, RSS, disk/day):
.venv\Scripts\python.exe scripts\measure_idle.py 60
```

Both are read-only and safe to run alongside a live service — they don't touch
`%ProgramData%\ATField` or spawn LibreHardwareMonitor. Numbers will vary with
GPU count and driver, but the order of magnitude won't.
