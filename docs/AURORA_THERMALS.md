# Aurora thermals — read this before touching any GPU temperature rule

> ## STOP
>
> If you are looking at an Aurora GPU temperature and thinking *"that's too
> hot, the sensor must be broken / the card is cooking / I should raise the
> threshold / I should disable the rule"* — **you are about to repeat a
> documented incident.** It has happened at least three times, once badly
> enough to run a healthy machine into 63 spurious hard-kills and once badly
> enough that a real hardware protection was switched off on a guess.
>
> **Do not change a thermal threshold or disable a thermal rule on Aurora
> without doing the four checks in [§5](#5-before-you-touch-anything).**

Written 2026-09-08 from the incident history and verified against the live
host. Aurora is the machine where GPU thermal reasoning has gone wrong the most
often, because its cards have a *different sensor set* from every other host in
the fleet and the difference is invisible unless you look for it.

---

## 1. The hardware, and why it is the odd one out

| | Aurora | Chronos / DEMETER |
|---|---|---|
| GPU | **2 × RTX 2070 SUPER** (Turing, GDDR6) | 2 × RTX 5090 (Blackwell, GDDR7) |
| CPU | i7-9700K | 9950X3D |
| Chassis | Alienware (PSU headroom **never measured** — see §6) | tower |

**The sensor sets are exactly complementary.** This one table explains every
Aurora thermal incident:

| sensor | RTX 2070 SUPER (Turing) | RTX 5090 (Blackwell) |
|---|---|---|
| GPU Core | ✅ real | ✅ real |
| GPU Hot Spot | ✅ **real** | ❌ NVIDIA removed the API |
| Memory Junction | ❌ **does not exist in hardware** | ✅ real |

Memory-junction telemetry begins with GDDR6X on the RTX 30-series, so Turing
has none at all. On Blackwell, NVIDIA deleted the hot-spot sensor API — GPU-Z
and HWiNFO report nothing for it on a 5090 — while GDDR7 per-module temps were
community-unlocked, and those are what LibreHardwareMonitor reads.

So: **a rule that is correct on Chronos can be meaningless on Aurora, and vice
versa.** Anything that treats the fleet as homogeneous is wrong.

## 2. What the numbers should look like

Measured on the live host, 2026-09-08, idle:

```
nvidia-smi : GPU0 36 °C  37.49 W      GPU1 36 °C  34.46 W
AT-Field   : gpu.0.core_temp_c    40.00   gpu.0.hotspot_temp_c  52.00
             gpu.1.core_temp_c    40.00   gpu.1.hotspot_temp_c  54.12
             system.cpu_package_temp_c 66.00
```

**A hot spot 12–14 °C above core at idle is NORMAL and correct.** Under
sustained load the gap widens to 10–25 °C by construction. Hot-spot readings of
**85–95 °C under load are ordinary**; the driver does not throttle until ~110 °C.

Reference points for this card:
- rated core max: **89 °C**
- hot spot under a full 215 W synthetic load: **~102 °C — still in spec**
- fans at idle: **3416 RPM (58%) and 2310 RPM (42%)** per HWiNFO

## 3. The three false theories, and what killed each

These are the actual wrong conclusions reached about this machine. Each looked
compelling. Each was wrong.

### ❌ "The memory junction is at 96 °C on an idle card — that sensor is garbage"

**What was believed:** `gpu.0.mem_junction_temp_c` read 88–101 °C on a card
sitting at 42 °C core drawing 10 W, while the card genuinely working at 214 W
reported a sane 54 °C. Obviously a broken sensor. The guard was disabled to
stop the kills.

**Why it was wrong — two separate errors stacked:**
1. `gpu.0.power_w` comes from **NVML** and `gpu.0.mem_junction_temp_c` comes
   from **LHM**, and *nothing establishes that those two collectors enumerate
   GPUs in the same order.* They were transposed. The "idle card reporting 96 °C"
   was the **loaded** card's sensor.
2. The sensor was real in the sense that it was reporting a real physical
   quantity — the **die hot spot** — just under the wrong name (§4).

**What killed the theory:** a controlled experiment the user demanded. Loading
physical GPU 0 drove `gpu.1.mem_junction_temp_c` from 78.6 → 102.8 °C while
`gpu.0`'s sat flat at 77.5; loading physical GPU 1 did the exact reverse.

**The cost:** a real hardware protection was off until that experiment ran.

### ❌ "~102 °C means the VRAM is cooking"

**What was believed:** a 102 °C reading proved the memory was overheating and
the card needed repadding.

**Why it was wrong:** the figure came from a **synthetic matmul hammer**, not
the real workload, and it was the **die hot spot** at full 215 W — warm, and
inside spec. Repadding was seriously considered for a non-problem.

### ❌ "Aurora's fans never ramp — replace Alienware Fusion with FanControl"

**What was believed:** `nvidia-smi` showed fans at 20%, so they were not
responding.

**Why it was wrong:** `nvidia-smi`'s fan field is a **target**, not the actual
state. HWiNFO showed the fans at **3416 RPM (58%) and 2310 RPM (42%) at idle** —
the user's existing Zo_Hi profile and +35% offset were working correctly.

> ⚠️ This trap is still live. `nvidia-smi --query-gpu=fan.speed` reports **18%**
> on Aurora right now. **Do not read that as the fan state.**

## 4. The incident this all caused

LibreHardwareMonitor **invents** a "GPU Memory Junction" entry on Turing by
copying the hot spot. AT-Field's pattern list accepted it:

```python
re.compile(r"gpu hot ?spot", re.IGNORECASE),  # last resort: hot-spot is close
```

Three independent confirmations that this was one sensor reported twice:
1. LHM on Aurora: Hot Spot `77.188` and Memory Junction `77.188` — **identical
   to three decimal places**, at idle, 44 W.
2. The user's **HWiNFO export**: a `GPU Hot Spot Temperature` column and **no
   memory-junction column at all**.
3. Chronos's 5090s: junction present, hot spot absent, and the core→junction
   delta *moves independently* (+12.8 on one card, +1.3 on the other) — which
   is what a genuinely separate sensor looks like.

The rule was set at **90 °C — inside a hot spot's normal working range.**
Aurora's kill log:

```
56 × vram-junction-hot[gpu.0]     <- phantom sensor
 7 × vram-junction-hot[gpu.1]     <- phantom sensor
 1 × gpu-core-hot[gpu.0]
 1 × gpu-core-hot[gpu.1]          <- the REAL temperature guard
```

**63 kills from a sensor that does not exist; 2 from the real one.** The cores
were fine throughout. A healthy machine was parked as "thermally incapable" on
a misread instrument, in a kill→restart→kill loop that produced almost no work
and discarded an in-flight shard every time.

### The fix, shipped in `04fda0d`

- Hot spot gets its **own** signal, `gpu.N.hotspot_temp_c`, and its own rule
  `gpu-hotspot-hot` at **100 °C**.
- `synthetic_junction()` drops a "memory junction" that duplicates the hot spot
  exactly.
- Real junctions on the 5090s are untouched.
- Deliberately **not** discriminated using NVML: `temperature.memory` reads N/A
  on the 5090 too, so that test would have thrown away the one guard that works.

**Current correct state on Aurora** (verify with `/rules`):
```
gpu-core-hot[gpu.0/1.core_temp_c]        83 °C   ACTIVE
gpu-hotspot-hot[gpu.0/1.hotspot_temp_c]  100 °C  ACTIVE
vram-junction-hot                        DISABLED — "no available signals
                                         matched glob 'gpu.*.mem_junction_temp_c'"
```
**That DISABLED line is the correct outcome on Turing, not a fault.** Do not
"fix" it.

## 5. Before you touch anything

A thermal threshold or rule on Aurora may only be changed after all four:

1. **Name the sensor.** Which physical quantity is it — core, hot spot, or
   memory junction? Check it exists on *this* hardware (§1). A Turing card has
   no memory junction; a Blackwell card has no hot spot.
2. **Check for duplicates.** If two signals are identical to several decimal
   places, they are one sensor. Compare the LHM dump directly.
3. **Never cross collectors to make an argument.** NVML and LHM index GPUs
   differently — NVML by PCI order, LHM by sensor-enumeration order. On this
   fleet they have been observed **transposed**. If your reasoning pairs an
   NVML number with an LHM number for "the same card", it is unsound.
4. **Load one physical card and watch which signal moves.** This is the only
   test that has ever settled an Aurora sensor question. It takes minutes and
   it has overturned every confident wrong answer so far.

**And the rule that outranks all four:** *disabling a guard is an action with a
hardware cost if you are wrong.* Spurious kills are recoverable — AT-Field
requeues without burning retries, and the work comes back. A cooked card does
not. **When uncertain, leave the guard on and raise the question.**

## 6. Still-open Aurora exposures

- **PSU headroom has never been measured.** Two 2070 SUPERs at ~200 W each plus
  a 9700K, in an Alienware chassis, at a draw the host never saw until a
  scheduling fix started loading *both* cards. Aurora once vanished instantly
  with no thermal warning; a PSU protection trip was hypothesised and later
  **retracted** (the user had switched it off) — but nothing has ever tested the
  actual headroom, and both cards still load.
- **AT-Field cannot see fans or the pump on any host.** Zero fan/RPM signals;
  the sensor helper needs the driver. A genuine cooling failure is invisible
  until the temperature rises, at which point the response is a kill, not
  prevention. The one time this data mattered, it came from the user manually
  exporting HWiNFO.
- **`nvidia-smi` fan % is a target, not a measurement** (§3).

## 7. If you remember nothing else

1. Aurora's cards have a **hot spot and no memory junction**. The 5090s are the
   reverse.
2. A hot spot 10–25 °C above core is **normal**. 85–95 °C under load is
   **normal**. Throttling begins ~110 °C.
3. `vram-junction-hot` **DISABLED** on Aurora is **correct**.
4. NVML and LHM GPU indices **have been transposed on this fleet**. Never pair
   them.
5. `nvidia-smi` fan % is a **target**, not the fan.
6. Every confident wrong answer here was overturned by **one experiment** —
   load a card, see which number moves. Do that instead of inferring.

## See also

- `docs/../tests/test_gpu_temp_sensor_identity.py` — pinned to both machines' real sensor dumps
- `tests/test_gpu_index_is_per_collector.py` — the transposed-index guard
- `04fda0d` "stop publishing a GPU hot spot as a memory-junction temperature"
- `7dc97df` "a gpu index means nothing outside the collector that assigned it"
- `~/.claude/plans/thermal-safety-survey.md` — the full fleet-wide incident survey
