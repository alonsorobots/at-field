# AT-Field FAQ

Common questions about how AT-Field works, why it killed your job, and
where to look when something seems off.

If your question isn't here, please [open an issue](https://github.com/alonsorobots/at-field/issues)
or peek at `events.jsonl` (canonical record of every signal sample,
rule verdict, and action AT-Field took).

## Can I scrape AT-Field with Prometheus?

Yes. The watchdog service exposes a Prometheus exposition-format
endpoint at `http://127.0.0.1:8765/metrics`. Drop this into your
`prometheus.yml`:

```yaml
scrape_configs:
  - job_name: atfield
    scrape_interval: 5s
    static_configs:
      - targets: ['127.0.0.1:8765']
```

You'll get gauges per signal (`atfield_signal{signal=...,unit=...,source=...}`),
per-rule `fraction_over`/`threshold`, a counter for total TRIGGER
verdicts, plus service-level uptime and pause state. The endpoint is
plain text and binds 127.0.0.1 only -- if you want a remote Grafana to
scrape it, front it with a reverse proxy.

---

## "Why was my training job killed?"

The watchdog killed it because at least one rule's threshold was
exceeded by enough samples within its window. Walk the trail like this:

1. **Open the audit log.**
   - Tray icon → right-click → **Open events.jsonl**, or
   - PowerShell: `notepad $env:PROGRAMDATA\ATField\events.jsonl`
2. **Find the `kill` action near the time of death.** Newest events
   are at the bottom. The relevant lines look like:

   ```jsonc
   {"type": "action", "ts": 1737..., "kind": "kill",
    "rule_name": "vram-junction-hot",
    "signal": "gpu.0.mem_junction_temp_c",
    "trigger_value": 102.4,
    "threshold": 100.0,
    "fraction_over": 0.85,
    "window_s": 20}
   {"type": "kill_report", "ts": 1737...,
    "script": "train.py",
    "kill_root": {"pid": 12345, "name": "python.exe", "script": "train.py"},
    "killed": [{"pid": 12345, "..."}, {"pid": 12346, "..."}]}
   ```

3. **Translate it.** The `rule_name` and `signal` tell you *what*
   tripped (above: VRAM junction temp on GPU 0 hit 102.4 °C, threshold
   was 100 °C, sustained over 85% of a 20-second window). The
   `kill_root.script` tells you *what was killed* (a `train.py`
   process tree).
4. **Decide what to do.** If 100 °C is more aggressive than you want
   to be, drag the **VRAM running hot** slider on the Rules tab to
   the right to relax it. If your card really does spike to 102 °C
   during a normal job, that's a cooling problem — investigate the
   card before you nudge the threshold.

The dashboard also surfaces this in the Events tab with the same
detail — JSONL is just the canonical store.

---

## "Why does the tray icon say *Degraded*?"

Three things flip the icon to yellow:

1. **AT-Field is paused.** You (or someone) hit Pause from the tray
   menu. Right-click → **Unpause** to resume.
2. **A collector that was working is now broken.** This usually means
   GPU temps stopped flowing because LibreHardwareMonitor crashed or
   you yanked an NVML-tracked GPU. The Status tab lists which
   collector is unhappy.
3. **The watchdog hasn't ticked in over 30 seconds.** Either the
   service is wedged, or the box was just suspended.

A collector that was *never* available (e.g. you don't have LHM
installed) is **not** a degradation — it's a default state. AT-Field
just runs the rules it has signals for and lists the ones it had to
disable on the Status tab. Yellow means "something I expected to be
working has broken."

---

## "Why does AT-Field not see CPU temperature on my machine?"

Reading CPU package temperature requires kernel-mode access to MSRs.
AT-Field runs in user mode (just a Python service); it has no business
loading a CPU driver. Tools like HWiNFO and LibreHardwareMonitor (LHM)
get around this with a kernel-mode driver (`WinRing0.sys`) that
exposes the readings over an in-process API.

AT-Field reads CPU temp via LHM's HTTP plugin. If LHM isn't running,
the CPU temp signal is missing — and the `cpu-running-hot` rule is
disabled at startup with a clear reason on the Status tab.

The bundled installer ships LHM out of the box and supervises it as a
child process, so this all happens automatically. For pip-installed or
git-cloned dev installs, run the one-shot fetcher:

```powershell
atf install-lhm
# Drops LHM into ~/.atfield/lhm/ with the web server pre-configured.
# Pass -d <path> to install elsewhere; --force to reinstall.

setx ATFIELD_LHM_EXE "$env:USERPROFILE\.atfield\lhm\LibreHardwareMonitor.exe"
# Then restart the AT-Field service so the supervisor picks it up.
```

If you'd rather install LHM by hand:

1. Download the latest LHM release (`LibreHardwareMonitor-net472.zip`)
   from
   [github.com/LibreHardwareMonitor/LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor).
2. Extract it somewhere persistent (`C:\Tools\LHM\`).
3. Launch `LibreHardwareMonitor.exe` once and from its menu enable
   **Options → Run on Windows Startup** and **Options → Remote Web
   Server → Run**.

AT-Field will pick up its `http://127.0.0.1:8085/data.json` endpoint
on the next service tick (no restart needed).

---

## "Can I run AT-Field on Linux / macOS?"

Not as the killing watchdog, no — the targeting allowlist
(`killable_names`, `launcher_names`, `never_kill_names`) and the
service registration are Windows-shaped. The codebase is
cross-platform clean enough that the test suite runs on Linux + macOS
in CI, but the production target is Windows.

If you want a similar guarantee on Linux: you already have it.
[`earlyoom`](https://github.com/rfjakob/earlyoom) has been the gold
standard for "kill the runaway before the OOM killer panics" for years.
AT-Field is essentially "earlyoom but for VRAM and CPU temp on
Windows." The two play different games.

---

## "I run my training in WSL. Does AT-Field protect me?"

Partially. The Windows host watchdog sees the WSL2 VM as a single
process called `vmmem` — it can't see individual Python or PyTorch
processes inside the VM. So:

- **VRAM-junction kills work**, because the GPU's temperature is a
  hardware property that's the same regardless of which OS owns the
  VRAM. AT-Field will kill `vmmem` if your Linux training job is
  cooking the GPU.
- **System RAM kills are coarse**: AT-Field will see system RAM
  pressure and kill `vmmem`, which means your *entire* WSL session
  goes down — not just the offending process. WSL boots back up the
  next time you launch a terminal.
- **CPU temp kills work** the same way — kill `vmmem` or whatever
  Windows-side dispatcher launched it.

If you want per-process granularity inside WSL, install AT-Field's
Linux-side equivalent (e.g. earlyoom + a custom GPU rule via
`nvidia-smi` polling) within the VM. AT-Field doesn't try to reach
across the VM boundary because (a) we can't, and (b) silently doing
something half-correct here would be worse than admitting the
limitation.

---

## "What's the difference between Aggressive / Normal / Relaxed?"

These are tier *classifications* on the Rules screen sliders, not
fixed values. The same threshold means different things for different
sensors:

- **Aggressive** — fires earlier; protective at the cost of more
  false positives. You'd use this if you've had hardware damage or
  thermal throttling in the past and want a wide safety margin.
- **Normal** — the default conservative profile from PLANNING.md §3.
  Designed to catch *runaway* workloads without interrupting normal
  ones.
- **Relaxed** — extra headroom; only fires on clear hardware
  distress. Use this if AT-Field has been killing legitimate
  long-running jobs and you've already verified your cooling is sound.

Per-rule cutoffs are documented in [`tuning.md`](tuning.md).

---

## "Will AT-Field kill VS Code / Explorer / system processes?"

No. The `targeting.never_kill_names` list (in `config.toml`) protects
critical processes by default: `explorer.exe`, `services.exe`,
`code.exe`, `windbg.exe`, plus AT-Field's own binaries. The actuator
also walks process trees and refuses to kill `services.exe` or
`System Idle Process` regardless of who's parent.

If you're worried about a specific process, add it to
`targeting.never_kill_names` in `config.toml`. The change auto-reloads
on the next service tick.

---

## "Where is the configuration file?"

`%PROGRAMDATA%\ATField\config.toml` is where the bundled installer
drops it. If you're running from source, point `atf` at your own with:

```powershell
atf run --config C:\path\to\config.toml
```

The dashboard sliders write to the same file the service reads. If
the file doesn't exist, the slider materializes a default copy on
first edit so you always have a hand-editable starting point.

---

## "How do I temporarily disable AT-Field for one job?"

Right-click the tray icon → **Pause** → pick a duration. The
watchdog keeps recording samples and rule verdicts but doesn't take
any kill / throttle / log actions while paused. Resumes
automatically at the chosen time, or right-click → **Unpause** to
resume immediately.

For longer-term opt-out (e.g. "I'm running benchmarks all night"),
the cleanest move is to bump every rule's threshold up to its
relaxed value via the **Relaxed** profile preset on the Rules tab.
Reverts cleanly with a click.

---

## "Where do I report a false positive?"

Drop the relevant `events.jsonl` excerpt + the `config.toml` you
were running in a [GitHub issue](https://github.com/alonsorobots/at-field/issues).
Include:

- AT-Field version (tray menu → About, or `atf version`)
- The rule name + signal that triggered (from the `action` event)
- What the workload was actually doing (training? generating? idle?)
- GPU model + driver version (`nvidia-smi` output works)

False positives are how we tighten the conservative profile over
time, so they're genuinely useful to receive.
