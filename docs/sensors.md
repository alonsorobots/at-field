# Sensor coverage strategy

> **TL;DR.** AT-Field reads the most reliable sensor source available on each system, in this order: NVML (NVIDIA), ROCm-SMI (AMD), psutil (always), LibreHardwareMonitor (bundled, MPL-2.0). LHM is consumed **headlessly via its library** (a tiny bundled `atfield-sensors.exe` that loads `LibreHardwareMonitorLib.dll`), not via its GUI web server. We always ship LHM.

This document explains why, what each tool gives us, and what's still on the
roadmap.

---

## The core problem

Windows does not have a stable, vendor-neutral sensor API. The closest thing
is `MSAcpi_ThermalZoneTemperature` (WMI), which on consumer motherboards is
famously useless -- it returns `27.85 °C` constant on most Z690 / X670 boards
and is ignored by every monitoring tool that takes itself seriously. The
"good" sensor data lives behind:

- Vendor-specific kernel drivers (NVIDIA `nvml.dll`, AMD `rocm-smi`, Intel
  Power Gadget, ASUS `AsIO3.sys`, ...).
- A signed kernel-mode driver that pokes raw `MSR`s and Super I/O ports
  (LibreHardwareMonitor and HWiNFO both ship one; AT-Field cannot ship one
  without a $300/yr EV cert and a separate driver-signing pipeline).
- Vendor-specific embedded controllers reachable only via undocumented SMBus
  / EC commands (ASUS Aura, MSI Mystic Light, etc -- not relevant for
  watchdog purposes).

There is no single Python package that reads "every voltage and temperature
on every Windows machine". The robust strategy is *layered fallback* with
honest reporting of what's available on this specific host.

---

## What each layer gives us

| Layer | License | Bundled? | Sensors |
|---|---|---|---|
| **psutil** (Tier 1) | BSD-3 | YES (pip dep) | RAM %, swap %, per-process CPU/mem, disk I/O, NIC I/O. **No temperatures, no voltages.** |
| **NVML** via `pynvml` | NVIDIA EULA (free for redist) | YES (pip dep) | NVIDIA GPU: core temp, util %, VRAM used, power draw, PCIe link state, per-process VRAM. **Not VRAM junction temp on consumer cards.** |
| **ROCm-SMI** | MIT | NO (CLI shipped with AMD driver) | AMD GPU: core temp, util %, VRAM used, power draw, junction temp on cards that report it. |
| **LibreHardwareMonitor** (via library helper) | MPL 2.0 | YES (vendored at install time) | CPU package temp, GPU memory junction temp, PSU rail voltages (+12V / +5V / +3.3V), VCore, fan RPM, motherboard temps, hard drive temps. |
| **WMI** `MSAcpi_ThermalZoneTemperature` | Built into Windows | n/a | Last-resort thermal zone temp. Often broken. |

### What "bundled" actually means

- **psutil** and **pynvml** are normal pip dependencies installed as part of
  the Python service.
- **LHM v0.9.6** (the .NET Framework 4.7.2 build) is downloaded at
  *installer build time* by [`scripts/fetch_lhm.ps1`](../scripts/fetch_lhm.ps1)
  and dropped into `dist/atfield/`. The Tauri NSIS installer bundles the
  whole folder. This is MPL-2.0-compliant: we ship LHM unmodified, alongside
  a clearly-labeled `LICENSE-third-party.md` documenting the license. v0.9.6
  specifically matters because it added the NvAPI workaround for the RTX
  50-series memory-junction sensor that NVIDIA removed from the public NVML
  surface (see footnote 3 in the matrix above).
### How LHM is read: the library helper, not the web server

AT-Field does **not** drive LHM's GUI nor its optional HTTP "web server".
That path was unreliable as a background service: it binds the wildcard
prefix `http://+:<port>/` (which `http.sys` refuses without a URL ACL), runs
a WinForms GUI under a Session-0 `LocalSystem` service, and silently swallows
listener failures -- so the LHM-derived signals would go dark with no error.

Instead we read `LibreHardwareMonitorLib.dll` directly -- its documented
headless use case -- through a tiny bundled .NET helper,
[`helper/AtfieldSensors.cs`](../helper/AtfieldSensors.cs), compiled to
`atfield-sensors.exe` with the in-box C# compiler (no .NET SDK required) and
streaming readings to the service as JSON lines. No web server, no port, no
URL ACL, no GUI, no Session-0 dependency. Driver-backed sensors (CPU package
temp via MSR) need elevation; the watchdog runs as `LocalSystem`, so it gets
them. See [`src/atfield/collectors/lhmlib.py`](../src/atfield/collectors/lhmlib.py).

### Why we don't support HWiNFO

HWiNFO64 was evaluated (and a Shared Memory collector briefly shipped) but
**removed**, for two decisive reasons:

- **The free version caps Shared Memory at 12 hours.** After 12 h of runtime
  it auto-deactivates and must be manually re-enabled -- a non-starter for an
  always-on watchdog. Removing the cap requires the paid Pro license.
- **It can't be set up programmatically.** Shared Memory is off by default
  (since v7.00), there's no command-line switch to enable it (registry only,
  and HWiNFO must be restarted), and its license forbids us bundling it.

Since the bundled LHM library helper already provides the same watchdog-
relevant signals (CPU package temp, GPU memory-junction temp, PSU rail
voltages) for free, forever, with zero configuration, HWiNFO added fragility
and a "half-lit, looks-broken" UI state for no net gain. If broader vendor
coverage is ever needed, it can return as an optional third-party *plugin*
(see the collector entry-point mechanism in
[`src/atfield/collectors/__init__.py`](../src/atfield/collectors/__init__.py)).

---

## Per-signal coverage matrix (the honest version)

These confidence numbers are estimated from a survey of GitHub issues,
NVML release notes, LibreHardwareMonitor PRs, the Steam hardware survey,
and the actual `nvml.dll` / `nvidia-smi` behaviour on the dev rig. They
are *not* aspirational. Where we know a signal is broken on some
hardware, it's flagged with a footnote.

Run `atf doctor` to see the live picture on your machine -- this matrix
tells you what to *expect* before you install.

| Signal | Source | Custom enthusiast desktop (DIY ASUS/MSI/Gigabyte) | Prebuilt gaming PC (NZXT/iBUYPOWER/CyberPowerPC) | Prebuilt corporate (Dell OptiPlex / HP EliteDesk) | NVIDIA datacenter (A100/H100) | AMD GPU desktop |
|---|---|---|---|---|---|---|
| `system.ram_used_percent` | psutil | **100%** | **100%** | **100%** | **100%** | **100%** |
| `system.swap_used_percent` | psutil | **100%** | **100%** | **100%** | **100%** | **100%** |
| `system.commit_percent` | psutil | **100%** | **100%** | **100%** | **100%** | **100%** |
| `gpu.N.core_temp_c` | NVML | **99%** | **99%** | **99%**¹ | **100%** | n/a (NVML) |
| `gpu.N.util_percent` | NVML | **99%** | **99%** | **99%**¹ | **100%** | n/a |
| `gpu.N.vram_used_bytes` | NVML | **99%** | **99%** | **99%**¹ | **100%** | n/a |
| `gpu.N.power_w` | NVML | **95%** | **95%** | **70%**² | **100%** | n/a |
| `gpu.processes` (per-PID VRAM map) | NVML | **99%** | **99%** | **99%** | **100%** | n/a |
| `gpu.N.mem_junction_temp_c` | LHM | **80%**³ | **75%** | **20%**⁴ | **0%**⁵ | **40%**⁶ |
| `system.cpu_package_temp_c` | LHM | **95%** | **90%** | **40%**⁷ | n/a | **95%** |
| `system.cpu_vcore_volts` | LHM | **80%** | **65%** | **15%**⁷ | n/a | **80%** |
| `system.psu_12v_volts` | LHM | **70%**⁸ | **50%** | **5%**⁹ | n/a | **70%** |
| `system.psu_5v_volts` | LHM | **70%**⁸ | **50%** | **5%**⁹ | n/a | **70%** |
| `system.psu_3v3_volts` | LHM | **70%**⁸ | **50%** | **5%**⁹ | n/a | **70%** |

Footnotes:

1. **Prebuilt corporate machines** sometimes ship without a discrete GPU
   (Intel/AMD integrated only). When they do have one, NVML works
   normally; the "1%" miss is integrated-only systems where AT-Field
   simply reports the NVML collector as unavailable -- no GPU rules
   apply, the rest of the watchdog continues.
2. **Power draw** is reported by NVML on most NVIDIA cards except some
   laptop GPUs and older Quadro variants where the driver disables it
   for OEM reasons. NVML returns "not supported" cleanly and AT-Field
   marks the signal unavailable.
3. **GPU memory junction temp on RTX 30/40 series**: works reliably via
   LHM's NvAPI shim. **RTX 5090 / 5080**: NVIDIA *deliberately removed*
   the hot-spot sensor from the public NVML / nvidia-smi surface in the
   50-series driver (confirmed on the dev rig:
   `nvidia-smi --query-gpu=temperature.memory` returns `N/A`). LHM
   v0.9.6 ships an NvAPI workaround that reads the memory temperature
   via a separate path; that's why we bundle 0.9.6 specifically. If
   even 0.9.6's workaround fails on a future driver, this signal goes
   to "unavailable" rather than silently mis-reporting -- the v0.9.4
   bug returned `255°C` constantly and was the entire reason we now
   pin 0.9.6.
4. **Mem junction temp on prebuilt corporate**: typically not exposed
   because corporate desktops rarely ship discrete GPUs, and when they
   do (workstation SKUs) the OEM driver often locks down NvAPI calls.
5. **NVIDIA datacenter cards** (A100 / H100 / B200): expose memory
   temp via NVML's *memory error* metric, not via a sensor reading. We
   don't currently surface that. They're also outside our target
   audience (designed for server farms with their own monitoring).
6. **AMD GPU mem junction**: AMD's API surface is `rocm-smi` on Linux
   and a sparse subset on Windows. LHM reads the junction temp on
   RDNA2/RDNA3 cards when the AMD driver cooperates -- coverage is
   spotty and varies by driver version.
7. **CPU sensors on prebuilt corporate desktops**: OEMs often use
   custom-locked Super I/O firmware that doesn't expose the standard
   register layouts LHM expects. The LHM kernel driver can sometimes
   not even *load* on locked-down Dell / HP firmwares (Secure Boot
   policy, custom UEFI). When LHM can't read the SuperIO, all the
   board-side signals (CPU package, voltages) go to unavailable
   together.
8. **PSU rail voltages on DIY enthusiast boards**: ASUS, MSI, Gigabyte,
   and ASRock boards from ~2018+ usually expose +12V / +5V / +3.3V
   through their Super I/O (ITE IT87xx or Nuvoton NCTxxxx). LHM has
   broad coverage of those chips, but *every new board generation* has
   a handful of register-map surprises that don't get fixed until a
   user files an issue and the maintainers add it. Brand-new boards
   (released in the last 6 months) miss more often than 2-year-old
   boards.
9. **Rail voltages on prebuilt corporate**: virtually never available
   for the same Super I/O lockdown reason as note 7. Don't write rules
   that depend on them.

### What this means for default rules

The watchdog ships rules only for signals that hit ≥95% across the four
mainstream segments (custom enthusiast, prebuilt gaming, prebuilt
corporate with dGPU, datacenter):

| Default rule | Signal | Confidence |
|---|---|---|
| `vram-bytes-high` | `gpu.N.vram_used_percent` | 99% |
| `gpu-core-hot` | `gpu.N.core_temp_c` | 99% |
| `ram-high` | `system.ram_used_percent` | 100% |
| `pagefile-high` | `system.swap_used_percent` | 100% |
| `vram-junction-hot` | `gpu.N.mem_junction_temp_c` | 80% / disables cleanly when missing |
| `cpu-pkg-hot` | `system.cpu_package_temp_c` | 95% / disables cleanly when missing |

Rail voltage rules are *deliberately* not shipped by default because
the 5–70% coverage means a default rule would either no-op for most
users or fire wrongly on the rest. They're available as signals; users
can add rules via the UI slider on rigs where the values look stable.

---

## Why rail voltages matter (added in v0.2)

After investigating a Kernel-Power 41 hard restart in the dev environment
(see `docs/postmortems/2026-05-15-kernel-power-41.md` for the gory detail),
we added +12V / +5V / +3.3V / VCore signals because:

- NVIDIA RTX 4090 and 5090 cards exhibit *transient power spikes* well over
  their TDP -- 600 W cards drawing 800 W for sub-millisecond windows. PSUs
  rated for the steady-state load can fall behind on the transient.
- A 1 Hz rail voltage sample won't catch the sub-ms sag itself, but it does
  catch the *baseline*: a +12V that drifts from 12.10 V (idle) to 11.65 V
  (full load) means the rail is loaded near the edge of its regulation
  envelope. ATX spec is +/- 5% (11.4 -- 12.6 V); anything below 11.7 V
  under load is a flag worth raising in a rule.
- Samples flow into the [forensic rolling buffer](../src/atfield/forensics.py)
  on every tick, so post-crash diagnostics can correlate "rail dropped at
  04:07:48, GPU TDR at 04:07:51, hard reset at 04:07:51".

There are no default rules on rail voltages yet -- thresholds depend on PSU
quality and board design, and we'd rather ship zero rules than wrong rules.
The signals are wired up; users can add a rule via the UI's slider once
they've watched their own baseline for a few hours.

---

## The forensic rolling buffer

`src/atfield/forensics.py`. Every sampled signal lands in
`%ProgramData%\ATField\forensics.jsonl` on a 5 second cadence. On service
start the previous run's file is rotated to `forensics-prev.jsonl`, with
two more numbered archives behind it. File format is one JSON object per
line:

```json
{"ts": 1778894831.245, "samples": {"gpu.0.core_temp_c": 67.0, ...}}
```

This survives hard crashes by design: append-only JSONL is the only format
that's guaranteed partially-readable after a power loss, because the worst
case is a torn last line that grep skips. SQLite WAL files and Parquet
column buffers can leave the file in a corrupt state if the kernel didn't
get a chance to fsync.

Footprint: ~4 MB / hour at 1 Hz with 14 signals. Auto-rotates at 50 MB.

---

## Roadmap: v0.3 and v0.4

### v0.3 (next minor)

- **LHM library helper transport** — *landed in v0.3.x*. Replaced the
  fragile LHM GUI/web-server path with a headless reader of
  `LibreHardwareMonitorLib.dll` (`atfield-sensors.exe`, built from
  `helper/AtfieldSensors.cs`). Surfaces GPU memory-junction temp, CPU
  package temp, and PSU rail voltages with no web server, port, URL ACL, or
  Session-0 dependency. See `src/atfield/collectors/lhmlib.py`.
- **HWiNFO Shared Memory collector** — *briefly shipped in v0.3.1, then
  removed.* The free version's 12-hour Shared-Memory cap and the inability
  to enable it programmatically made it unreliable for an always-on
  watchdog; the bundled LHM helper covers the same signals. May return as an
  optional third-party plugin.
- **Forensic CLI**: `atf forensics --since 1h` reads
  `forensics.jsonl[.prev]` and prints a CSV / pandas-friendly time-series
  for post-crash analysis.

### v0.4 (later)

- **Vendor-specific deep collectors**: opt-in modules for NVIDIA `nvidia-smi
  dmon` (high-rate GPU power telemetry, 100 Hz instead of 1 Hz), Intel Power
  Gadget (per-core P-state), AMD `rocm-smi --showtopo` (NUMA pinning).
  These trade simplicity for fidelity and won't be enabled by default.
- **Driver-mode kernel collector** (long shot): a signed driver of our own
  that reads MSRs and Super I/O ports without the LHM round-trip. Removes
  LHM from the dependency chain. Requires EV cert + Microsoft attestation
  -- realistic only if AT-Field gets enough adoption to justify the
  process.
- **WMI fallback**: a last-resort `MSAcpi_ThermalZoneTemperature` reader
  for systems where LHM finds no CPU sensor. Marked unreliable in the UI.

---

## Sources for the confidence numbers

The numbers above are estimated, not measured -- a true measurement
would require shipping AT-Field to thousands of users and instrumenting
the probe results. They are grounded in:

- **NVML reliability (95–100%)**: the NVML API has been stable since
  CUDA 7.0 (2015) and is shipped *with the NVIDIA driver itself*, so
  it can't go missing on a system that has working NVIDIA hardware.
  The "1% miss" allowance is for the rare driver upgrade that lands a
  buggy NVML build (we've seen it happen ~once per year historically,
  always patched within a week).
- **psutil (100%)**: it reads documented Win32 perf counters that
  predate Windows 7. No realistic failure mode short of a corrupted
  Windows install.
- **LHM GPU mem junction (80%)**: based on the LHM issue tracker --
  RTX 30-series and 40-series have ~5 open issues collectively
  related to junction temp, all hardware-specific (e.g. "RTX 4060 Ti
  16 GB doesn't expose junction sensor" -- a known firmware quirk
  affecting maybe 2% of that SKU). RTX 50-series specifically dropped
  to ~50% in v0.9.4 because of NVIDIA's hot-spot removal, hence the
  v0.9.6 bump.
- **LHM CPU package temp on DIY (95%)**: derived from the AMD17Cpu.cs
  and IntelCpu.cs coverage in the LHM repo -- they enumerate every
  CPU family back to Sandy Bridge / Bulldozer with explicit MSR
  layouts. Modern (2020+) CPUs without coverage are rare and usually
  get patched in within a release or two.
- **LHM CPU package on Dell / HP (40%)**: based on the GitHub issue
  pattern "LHM kernel driver fails to load on Dell OptiPlex 7090" /
  "HP ProDesk 600 G6: no CPU temp". These appear quarterly on the
  issue tracker and are typically *not* fixable from LHM's side --
  the OEM firmware blocks low-level access on purpose.
- **PSU rail voltages on DIY (70%)**: a survey of the
  `SuperIOHardware.cs` file in LHM (~6,000 lines of per-board
  register maps) shows broad coverage of ASUS / MSI / Gigabyte /
  ASRock boards, with new boards needing manual entries. Coverage
  drops for boards released in the last 6 months.
- **PSU rail voltages on prebuilt corporate (5%)**: same Super I/O
  lockdown reason as the CPU temp note. Dell / HP / Lenovo
  systematically disable the registers via UEFI policy.
- **The "prebuilt gaming" middle column**: NZXT, iBUYPOWER,
  CyberPowerPC, etc. use standard retail motherboards, so coverage
  tracks "DIY enthusiast" minus a haircut for the few that pick
  oddball boards LHM doesn't know yet.

### Live verification: `atf doctor`

The whole point of `atf doctor` is to convert this matrix from a
table of probabilities into ground truth on your machine. Run it
after install:

```
> atf doctor

AT-Field doctor

8 check(s) passed:
  + state dir present: C:\ProgramData\ATField
  + heartbeat fresh (1.3s old)
  + all rules active per last startup
  + collector system: OK (psutil works)
  + collector nvml: OK (2 GPUs)
  + collector lhm: OK (4 sensor(s) mapped)
  + forensic buffer fresh (45.2 KB, last write 2.1s ago)
  + config valid: C:\ProgramData\ATField\config.toml

All clear.
```

If a collector is unavailable, doctor reports it with a concrete fix.
The signal coverage table above is the prior; doctor gives you the
posterior.

## License compliance summary

| Component | License | How we comply |
|---|---|---|
| LibreHardwareMonitor | MPL 2.0 | Ship unmodified; reproduce license in `LICENSE-third-party.md`; link to the upstream repo from the FAQ. |
| psutil | BSD-3 | Standard pip dep; license bundled in the Python wheel. |
| pynvml | BSD-3 | Standard pip dep. |
| nvidia-ml.dll | NVIDIA EULA | Distributed with the NVIDIA driver, not by us. |
| ROCm-SMI | MIT | Distributed with the AMD driver; we shell out to it. |

If you spot a compliance issue, please open an issue on the repo -- the
project goal is to be a model OSS citizen, not a license risk.
