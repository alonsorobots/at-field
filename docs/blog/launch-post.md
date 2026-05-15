# Why your Windows AI rig needs an earlyoom equivalent (and how I built one)

*A 2-binary, 25 MB, one-installer story about VRAM, dead processes, and
why I wrote a watchdog instead of buying another GPU.*

---

## The 3 AM problem

If you've ever left a model training overnight on a Windows box, you
already know how this story ends. You wake up to one of:

1. A blue screen, because the GPU's VRAM-controller temperature ran
   away while you were sleeping and the driver finally tipped over.
2. A 20-minute Windows boot loop, because a Python process pinned 100%
   of physical RAM, the swap file ate every block of free disk, and
   `services.msc` tried to start while the disk was still thrashing.
3. The training script "succeeded" but produced garbage outputs,
   because OOM-related allocator failures got swallowed silently.

On Linux the playbook is well known: install
[`earlyoom`](https://github.com/rfjakob/earlyoom),
[`oomd`](https://github.com/facebookincubator/oomd), or even just lean
on the kernel's OOM killer with sane `oom_score_adj` overrides. They
watch the box's vital signs and pull the plug on the loudest offender
*before* the whole system gets so wedged that you have to walk over and
hit the power button.

On Windows the playbook is...  Task Manager, Reboot, hope. The kernel
has its own OOM-handling but it lives in the inner loop of the Memory
Manager, optimizes for "let the GUI shell respond" rather than "save
the user's work", and notably does not understand "this VRAM-junction
sensor is spiking faster than it can dissipate".

So I wrote one.

## What "good" looks like

Three goals, in priority order:

1. **Save the rig.** When a training job is about to take down the
   box, kill *that* process and only that process, fast enough that
   the Windows compositor never stutters.
2. **Work on most setups.** No "edit this config first", no "ah you'll
   need this driver pack". Single double-click installer, run as admin
   once, watchdog is up.
3. **Stay out of the way.** Sit in the system tray, draw <1% CPU,
   never log a wall of text the user has to wade through. Surface
   "what just happened" only when something interesting actually
   happens.

The result is **AT-Field**. Two binaries:

| Binary                   | Runs as       | Job                                                   |
| ------------------------ | ------------- | ----------------------------------------------------- |
| `atfield-service.exe`    | LocalSystem   | Reads sensors, evaluates rules, kills offending procs |
| `at-field-tray.exe`      | User mode     | Tray icon + dashboard, autostarts at login            |

…wrapped in a single ~25 MB NSIS installer.

## How it decides who to kill

The kernel OOM is reactive: by the time it fires, the system is
*already* over the cliff. AT-Field is predictive in a tiny way: each
"rule" is an N-of-M sliding-window evaluator over a single signal.

Concretely, the default `gpu-vram-junction-hot` rule says:

> If, over any 30-second window, at least 67% of samples have
> `gpu.0.mem_junction_temp_c > 105°C`, kill the process tree that owns
> the most VRAM on GPU 0.

Why N-of-M instead of an instantaneous threshold? Because consumer
sensors *spike*. A single 105°C reading on a Founders 4090 means
nothing; 20 of them in 30 seconds means something is wrong. The math
also makes the rule self-debounce: as soon as the workload backs off
the fraction-over drops below the trigger and the timer resets.

The signals that drive these rules come from three sources, layered:

1. **NVIDIA NVML** (in-process) for GPU temperature, utilization, VRAM
   usage, power. Fast, reliable, and ships in every recent driver.
2. **psutil + Win32 GlobalMemoryStatusEx** for system RAM, page-file
   pressure, per-process RAM. Stdlib.
3. **LibreHardwareMonitor** (HTTP at `127.0.0.1:8085`) for the things
   NVML and the Windows API can't see: VRAM *junction* temperature on
   consumer cards, CPU package temperature, motherboard sensors. We
   bundle LHM (MPL 2.0, vendor unmodified) so the user doesn't have
   to install it separately.

A "kill" isn't `taskkill /pid X /f`. It's a process-tree walk that
finds the *root dispatcher* (your `train.py`, not the `python.exe`
that wraps it), reports what got killed and why, and writes a
structured event to `events.jsonl` so you can ask "why did my training
die?" the next morning and get a real answer.

## The shape of the system

Five Python modules do the watchdog work:

```
collectors/      Sensor adapters: NVML, psutil, LHM, (skeleton) AMD
signals.py       Sliding-window data structures
policy.py        Rule definitions + N-of-M evaluator
actuator.py      Process-tree-aware killer + throttle (suspend/resume)
service.py       The 1 Hz tick loop tying it all together
```

…plus an HTTP API on `127.0.0.1:8765` (`http.server`, no FastAPI -- 7
endpoints don't justify an async stack), and a Tauri tray app that
talks to it.

The HTTP layer is where the "stay out of the way" goal pays off: every
piece of state the dashboard cares about is a snapshot endpoint, every
mutating action is a single POST/PATCH. The dashboard polls at 1 Hz
(user-tunable in Settings) and renders sparklines that look like
they're live because the watchdog *is* live.

## The Tauri tray app

Why Tauri instead of bundling a Python GUI? Three reasons:

1. **Idle cost.** A Tauri tray uses ~30 MB of RAM at rest. A bundled
   Python UI runs ~80 MB and you're paying for a full interpreter you
   don't strictly need on the user-mode side.
2. **First-class system integration.** Native tray icons, native
   notifications, HKCU\Run autostart -- all things Tauri already does
   well on Windows.
3. **Aesthetics.** Tailwind 4 + Framer Motion ships visual polish you
   couldn't pay a designer enough to add to Tkinter.

The dashboard has four tabs: **Signals** (live sparklines + click-to-
drill-into-history per signal, with 1-second / 10-second / 60-second
multi-resolution storage covering 24 hours), **Rules** (per-rule
slider + Aggressive/Normal/Relaxed presets), **Events** (tail of the
audit log with click-to-expand JSON detail), and **Status** (collector
health, preferences). Every interaction goes through the same
localhost HTTP API, which means the same operations are scriptable
from PowerShell or curl if you want to.

The single most important UX moment is the kill report. When AT-Field
takes a process down, the user sees:

* A native Windows toast titled "AT-Field killed train.py" with the
  rule and signal that fired.
* An in-app red banner the next time they look at the dashboard.
* A new entry in `events.jsonl` with the full process-tree snapshot.

…and then the rule's cooldown begins, so the same trigger can't
re-fire for 5 minutes. We are aggressive about killing the right
process; we are conservative about killing the same thing twice.

## What I cut

A few things I deliberately did *not* build for v1.0, in case you were
about to suggest them:

* **Async everything.** The hot loop is sync because it runs once per
  second and nothing it does blocks for more than a few milliseconds.
  An async stack here would only add complexity, restart edges, and
  install size.
* **Custom kernel-mode sensor reading.** LHM is MPL-2.0 and does it
  better than I could write in three weekends. We supervise it as a
  child process and walk away.
* **A web dashboard on a separate port.** The Tauri tray *is* the
  dashboard. One process, one set of state, one thing to keep alive.
* **Accounts / cloud / telemetry.** It runs on your box, talks to your
  loopback, and writes its own log files. Period.

## Where this goes next

Things on the roadmap once v0.2 is in users' hands:

* AMD ROCm-SMI collector (the probe path is already in place; live
  sampling is gated on having an AMD rig in CI).
* Per-rule window/cooldown sliders, full GET/PATCH /config so the
  dashboard's "Settings" tab is feature-complete.
* Prometheus exporter (already shipping at `/metrics`); a curated
  Grafana dashboard JSON to drop into your existing stack.
* Plugin entry points for third-party collectors (HWiNFO,
  motherboard-specific shared-memory adapters, etc.).

But honestly, the most important thing is the boring one: getting it
in front of enough people that we learn what it gets wrong. If you've
been bitten by a Windows AI rig at 3 AM, please try the installer at
[github.com/alonsorobots/at-field](https://github.com/alonsorobots/at-field)
and tell me when it kills something it shouldn't have. That's what the
issue tracker is for.

---

*AT-Field is MIT-licensed. The bundled LibreHardwareMonitor is MPL
2.0; full third-party licensing is in
[LICENSE-third-party.md](https://github.com/alonsorobots/at-field/blob/main/LICENSE-third-party.md).*

*Built with Python 3.12, Rust 1.77, Tauri 2, React 19, and roughly
fourteen liters of cold brew.*
