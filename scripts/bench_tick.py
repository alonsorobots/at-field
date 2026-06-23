"""Microbenchmark: per-tick CPU cost of the AT-Field collectors.

Run with the venv python:  .venv/Scripts/python.exe scripts/bench_tick.py

Measures the wall-time of one tick's work (NVML sample, system sample,
forensics staging) so we can reason about the always-on CPU footprint:
at tick_hz=1, a tick that takes T ms uses ~T/10 percent of ONE core.

This is a diagnostic/dev tool, not shipped behaviour.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable


def _bench(label: str, fn: Callable[[], object], n: int, warmup: int = 5) -> float:
    for _ in range(warmup):
        fn()
    samples: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    mean = statistics.mean(samples)
    p95 = sorted(samples)[int(0.95 * (len(samples) - 1))]
    print(f"  {label:42s} mean={mean:7.3f} ms   p95={p95:7.3f} ms   (n={n})")
    return mean


def main() -> None:
    from atfield.collectors.nvml import NvmlCollector
    from atfield.collectors.system import SystemCollector

    print("=== AT-Field per-tick microbenchmark ===\n")

    nvml = NvmlCollector()
    r = nvml.probe()
    print(f"nvml: available={r.available} ({r.reason[:60]})")
    sysc = SystemCollector()
    rs = sysc.probe()
    print(f"system: available={rs.available} ({rs.reason[:60]})\n")

    total = 0.0
    if r.available:
        total += _bench("nvml.sample() FULL (incl. proc enum)", nvml.sample, 200)

        # Isolate the per-process enumeration cost: time just the metric
        # reads vs just the compute-process enumeration.
        pynvml = nvml._pynvml
        handles = nvml._handles

        def metrics_only() -> None:
            for h in handles:
                pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
                pynvml.nvmlDeviceGetUtilizationRates(h)
                pynvml.nvmlDeviceGetMemoryInfo(h)
                try:
                    pynvml.nvmlDeviceGetPowerUsage(h)
                except Exception:
                    pass

        def procs_only() -> None:
            for h in handles:
                try:
                    pynvml.nvmlDeviceGetComputeRunningProcesses_v3(h)
                except Exception:
                    pass

        _bench("  nvml metric reads only (no proc enum)", metrics_only, 200)
        _bench("  nvml compute-proc enumeration only", procs_only, 200)

    if rs.available:
        total += _bench("system.sample()", sysc.sample, 200)

    # --- Full downstream tick: policy eval + history + forensics staging ---
    import tempfile
    from pathlib import Path

    from atfield.config import default_config
    from atfield.forensics import ForensicBuffer
    from atfield.http_api import ServiceState
    from atfield.policy import PolicyEngine

    cfg = default_config()
    samples = {}
    if r.available:
        samples.update(nvml.sample())
    if rs.available:
        samples.update(sysc.sample())
    samples.pop("gpu.processes", None)

    engine = PolicyEngine(cfg, available_signals=tuple(samples.keys()))
    tmp = Path(tempfile.mkdtemp(prefix="atf_bench_"))
    state = ServiceState(
        version="bench",
        observe_only=True,
        events_path=tmp / "events.jsonl",
        watchdog_log_path=tmp / "watchdog.log",
        state_dir=tmp,
    )
    fbuf = ForensicBuffer(tmp)  # not started; just timing record() staging

    import time as _t

    def engine_tick() -> None:
        engine.tick(samples, now_ns=_t.monotonic_ns())

    def record_tick() -> None:
        state.record_tick(now_unix=_t.time(), samples=samples)

    def forensics_record() -> None:
        fbuf.record(samples, ts=_t.time())

    total += _bench("engine.tick() (policy eval)", engine_tick, 500)
    total += _bench("ServiceState.record_tick() (history)", record_tick, 500)
    total += _bench("ForensicBuffer.record() (staging)", forensics_record, 500)

    print(f"\n  >>> approx total per-tick work (steady state): {total:.3f} ms")
    print(f"  >>> at tick_hz=1 that's ~{total / 10.0:.3f}% of ONE core\n")

    nvml.shutdown()


if __name__ == "__main__":
    main()
