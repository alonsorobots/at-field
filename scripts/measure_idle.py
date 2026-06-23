"""Process-level idle-footprint measurement for the AT-Field steady state.

Runs a faithful copy of the service's per-tick loop body (collector
sampling -> policy eval -> in-memory history -> forensic staging) at
tick_hz=1 in THIS process, with no network ports and no LHM spawn, then
reports the process's own CPU% and RSS over the window via psutil.

Usage:  .venv/Scripts/python.exe scripts/measure_idle.py [seconds]

This is a diagnostic/dev tool, not shipped behaviour. It deliberately
excludes the (idle) HTTP server thread and the LHM HTTP poll so the
number reflects the watchdog's own steady-state work; both are called
out separately in docs/footprint.md.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

import psutil


def main() -> None:
    duration_s = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0

    from atfield.collectors.nvml import PER_PROCESS_VRAM_KEY, NvmlCollector
    from atfield.collectors.system import SystemCollector
    from atfield.config import default_config
    from atfield.forensics import ForensicBuffer
    from atfield.http_api import ServiceState
    from atfield.policy import PolicyEngine

    nvml = NvmlCollector()
    nvml.probe()
    sysc = SystemCollector()
    sysc.probe()

    cfg = default_config()
    tmp = Path(tempfile.mkdtemp(prefix="atf_idle_"))
    fbuf = ForensicBuffer(tmp)
    fbuf.start()

    # Bootstrap one sample to learn the signal set, then build the engine.
    boot = {}
    boot.update(nvml.sample())
    boot.update(sysc.sample())
    boot.pop(PER_PROCESS_VRAM_KEY, None)
    engine = PolicyEngine(cfg, available_signals=tuple(boot.keys()))
    state = ServiceState(
        version="measure",
        observe_only=True,
        events_path=tmp / "events.jsonl",
        watchdog_log_path=tmp / "watchdog.log",
        state_dir=tmp,
    )

    proc = psutil.Process(os.getpid())
    proc.cpu_percent(None)  # prime the CPU% counter
    rss_start = proc.memory_info().rss

    t_end = time.monotonic() + duration_s
    ticks = 0
    tick_period = 1.0
    rss_peak = rss_start
    while time.monotonic() < t_end:
        t0 = time.monotonic()
        samples = {}
        samples.update(nvml.sample())
        samples.update(sysc.sample())
        samples.pop(PER_PROCESS_VRAM_KEY, None)
        state.record_tick(now_unix=time.time(), samples=samples)
        fbuf.record(samples, ts=time.time())
        engine.tick(samples, now_ns=time.monotonic_ns())
        ticks += 1
        # Sample RSS occasionally (not every tick) so this probe doesn't
        # inflate the very CPU number we're trying to measure.
        if ticks % 5 == 0:
            rss_peak = max(rss_peak, proc.memory_info().rss)
        sleep_for = max(0.0, tick_period - (time.monotonic() - t0))
        if sleep_for:
            time.sleep(sleep_for)

    # psutil.Process.cpu_percent() is normalized to a SINGLE core: 100.0
    # means one core fully saturated; it can exceed 100 for a multi-core
    # busy process. So this value is already "% of one core".
    cpu_pct_one_core = proc.cpu_percent(None)
    ncpu = psutil.cpu_count(logical=True) or 1
    rss_end = proc.memory_info().rss
    fbuf.stop()
    forensic_bytes = (tmp / "forensics.jsonl").stat().st_size if (tmp / "forensics.jsonl").exists() else 0

    print("\n=== AT-Field idle footprint (modeled steady state, service only) ===")
    print(f"  duration            : {duration_s:.0f} s ({ticks} ticks @ ~1 Hz)")
    print(f"  CPU                 : {cpu_pct_one_core:.3f}% of ONE core")
    print(f"                        = {cpu_pct_one_core / ncpu:.4f}% of this {ncpu}-thread machine's total CPU")
    print(f"  RSS start / peak    : {rss_start / 1e6:.1f} MB / {rss_peak / 1e6:.1f} MB")
    print(f"  RSS end             : {rss_end / 1e6:.1f} MB  (growth: {(rss_end - rss_start) / 1e3:.0f} KB -- bounded by ring buffers)")
    print(f"  forensics on disk   : {forensic_bytes / 1e3:.1f} KB for {ticks} ticks = {forensic_bytes / max(ticks,1):.0f} B/tick")
    print(f"  -> projected disk   : {forensic_bytes / max(ticks,1) * 86400 / 1e6:.1f} MB/day (auto-rotates at 50 MB, ~250 MB cap)\n")

    nvml.shutdown()


if __name__ == "__main__":
    main()
