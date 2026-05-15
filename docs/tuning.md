# Tuning AT-Field for your hardware

The defaults in [PLANNING.md §3](../PLANNING.md) target a
conservative, mainstream-NVIDIA, mainstream-Windows-AI box: kill
nothing for the first hour, kill the right thing thereafter. This
document is your reference for moving the dials when "mainstream"
doesn't fit your rig.

## How to read this doc

For each rule, three columns:

- **Aggressive** — the lowest threshold that's still a real signal of
  hardware distress. Use this if you want a wide safety margin.
- **Normal** — the conservative default that ships in `config.toml`.
- **Relaxed** — the highest threshold that won't run your card past
  the manufacturer's stated tolerance. Use this if AT-Field's
  defaults are killing legitimate jobs.

You can change the values in the dashboard by dragging the slider on
the Rules tab; the tooltip tells you which tier you're in. Or hit one
of the **Aggressive / Normal / Relaxed** preset buttons at the top of
that tab to apply a profile to every rule at once.

If you'd rather edit `config.toml` directly:

```powershell
notepad $env:PROGRAMDATA\ATField\config.toml
# the service auto-reloads within ~1 second of the file changing
```

---

## VRAM junction temperature (`vram-junction-hot`)

The single most important rule, and the one most operators get wrong.
The "junction" is the hottest spot on the memory die — it's typically
~20 °C hotter than what NVIDIA reports as "memory temperature" via
NVML, and the failure mode at high junction temps is *quiet*: GDDR6X
throttles silently, your throughput drops, and over enough hours you
shorten the chip's lifespan.

| Tier       | Threshold | Notes |
|------------|-----------|-------|
| Aggressive | 88 °C     | Conservative for RTX 30/40 series; kills before throttle. |
| Normal     | 92 °C     | Conservative default. NVIDIA's spec is 105 °C max but throttling kicks in much earlier. |
| Relaxed    | 100 °C    | Top end of "still safe long-term" per most teardown reviews. |

**Per-card guidance:**

| GPU                          | Aggressive | Normal | Relaxed |
|------------------------------|-----------:|-------:|--------:|
| RTX 3080 / 3080 Ti / 3090    | 88         | 92     | 96      |
| RTX 3090 Ti                  | 90         | 95     | 100     |
| RTX 4080 / 4090              | 85         | 90     | 95      |
| RTX 4080 SUPER / 4090 SUPER  | 85         | 90     | 95      |
| RTX 5080 / 5090 (early data) | 88         | 92     | 96      |
| A100 (datacenter)            | 80         | 85     | 90      |
| H100 (datacenter)            | 80         | 85     | 90      |

You need LibreHardwareMonitor running for this signal to exist
(NVML doesn't expose junction on consumer GPUs). See
[faq.md](faq.md#why-does-at-field-not-see-cpu-temperature-on-my-machine).

## GPU core temperature (`gpu-core-hot`)

NVML reports this directly so the rule is always available. Less
sensitive than VRAM junction in practice — modern coolers hit thermal
throttle on core well before the chip is in distress.

| Tier       | Threshold | Notes |
|------------|-----------|-------|
| Aggressive | 78 °C     | Below the throttle setpoint on most cards. |
| Normal     | 83 °C     | Conservative default. NVIDIA throttles at 84-87 °C depending on model. |
| Relaxed    | 88 °C     | At-throttle; kill if sustained at this point. |

Datacenter cards (A100, H100) run cooler under sustained load — drop
each tier by ~5 °C if you're protecting one of those.

## System RAM pressure (`ram-pressure`)

Fires when sustained system RAM utilization stays high enough that
either (a) the OS OOM killer is about to make the choice for you, or
(b) paging is going to murder your training loop's throughput.

| Tier       | Threshold | Notes |
|------------|-----------|-------|
| Aggressive | 78 %      | Leaves headroom for sudden allocations (e.g. dataloader spikes). |
| Normal     | 85 %      | Conservative default. |
| Relaxed    | 93 %      | Right before the OS goes to swap aggressively. |

If you're running on a 128 GB+ workstation, lean Aggressive — the
absolute headroom (10s of GB) at 80 % is plenty for spikes. On a 16 GB
laptop, Relaxed is the only option that won't false-positive on
normal browser usage.

## Pagefile / committed memory (`pagefile-pressure`)

Windows commit charge = RAM + pagefile usage. This rule catches the
"my training process VirtualAlloc'd 200 GB and now Windows is paging
to a 7200 RPM HDD" case before you sit there for an hour wondering
why iteration N is taking 100x longer than iteration N-1.

| Tier       | Threshold | Notes |
|------------|-----------|-------|
| Aggressive | 83 %      | Aggressive but realistic for 16-32 GB RAM rigs with growing workloads. |
| Normal     | 90 %      | Conservative default. |
| Relaxed    | 96 %      | At-saturation; only fires when pagefile is already on the wall. |

Don't drop this one to Aggressive on a low-RAM laptop unless you're
willing to lose long-running browser tabs.

## CPU package temperature (`cpu-pkg-hot`)

Catches runaway CPU work — a stuck dataloader thread, a CPU-only
inference job pinned to all cores, or a thermal-paste-failing AIO.

| Tier       | Threshold | Notes |
|------------|-----------|-------|
| Aggressive | 82 °C     | Most consumer CPUs throttle around 95 °C; 82 is "warm but happy". |
| Normal     | 90 °C     | Conservative default. |
| Relaxed    | 96 °C     | At-throttle for most consumer SKUs (Tjmax). |

Datacenter Xeons / EPYCs run hotter spec'd (Tjmax is often 105 °C+);
bump every tier up by ~10 °C on those. AMD Threadripper sits between
the two — the sensor reading reported as "Tdie" is usable, but
Threadripper's TCTL has a built-in offset on some chips that AT-Field
doesn't compensate for. Cross-check with HWiNFO before tuning.

This rule needs LibreHardwareMonitor (CPU temp isn't in NVML, and
psutil doesn't expose it on Windows).

---

## Window length and `min_fraction_over`

Each rule has two parameters besides the threshold:

- **`window_s`** — how many recent seconds of samples we look at.
- **`min_fraction_over`** — what fraction of those samples have to
  exceed the threshold before we trip.

The default values (e.g. 20 s window, 0.67 fraction) mean "if you're
over the line for ~13 of the last 20 seconds, you're over." The
intent is to filter out brief spikes from sustained problems —
training kernels often hit a peak temp during a backward pass that
lasts a single second, and AT-Field shouldn't kill the job over that.

If you're getting false positives on legitimate temperature
oscillations (e.g. a tightly controlled fan curve causing a true
85 ↔ 90 °C cycle every 5 s), bump `window_s` up to 60-90 s and lower
`min_fraction_over` to 0.5. That's a "long sustained problem" filter,
not a "brief spike" one.

If you're getting false negatives (workload is clearly damaging the
hardware but the rule doesn't fire), drop `window_s` to 10 s and bump
`min_fraction_over` to 0.8. That's a "fast hard fault" filter.

## Cooldown

After a rule fires, it goes into cooldown for `post_kill_cooldown_seconds`
(default 60). During cooldown, the rule still records signal samples
and verdicts but won't take another action — you don't want a tight
loop where the watchdog kills the same job five times in a minute as
its restarts come up and immediately re-trip the rule.

If you have a system that legitimately respawns workloads on kill
(e.g. Ray with auto-restart), bump cooldown up to 300 s or more so
the respawn cycle has time to either succeed or fail cleanly.
