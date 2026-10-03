"""NVML "SUCCESS with garbage": withheld at once, a fresh process if it persists.

Regression test for 2026-07-23 22:39:26 (RTX 5090, driver 596.36): after a
wedged NVDEC/CUDA context, NVML returned NVML_SUCCESS while reporting
gpu.0.core_temp_c = 885510 C and util/power = 260640043 -- with memory queries
on the SAME handle still correct. Nothing raised, so the collector never
degraded and never recovered; the poison was sticky to the SESSION and cleared
only on process restart, 8 hours later. Meanwhile /headroom read 0.0 and
Kiroshi's WorkerTuner throttled a 6-worker node to 1.

Until 2026-10-02 the response was an in-process NVML rebuild after three
garbage ticks. That is gone: the evidence for it was a FRESH process ("read
36 C immediately"), and an in-process rebuild during a power transition was
measured to poison the process (tests/test_nvml_rides_out_a_sleep.py). The
garbage is now withheld at once and, if it persists, the collector asks for a
fresh process.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from atfield.collectors import HealthState  # noqa: E402
from atfield.collectors.nvml import NvmlCollector  # noqa: E402
from atfield.signals import is_plausible  # noqa: E402

# The literal values recorded in the forensic stream.
GARBAGE_TEMP = 885510.0
GARBAGE_UTIL = 260640043.0
GOOD_VRAM = 33918509056


class _Garbage:
    """One handle returning the 2026-07-23 readings, all NVML_SUCCESS."""
    NVML_TEMPERATURE_GPU = 0

    def __init__(self):
        self.init_calls = self.shutdown_calls = 0
        self.wedged = True

    def nvmlInit(self):
        self.init_calls += 1

    def nvmlShutdown(self):
        self.shutdown_calls += 1

    def nvmlDeviceGetTemperature(self, h, _w):
        return GARBAGE_TEMP if self.wedged else 41

    def nvmlDeviceGetUtilizationRates(self, h):
        return type("U", (), {"gpu": GARBAGE_UTIL if self.wedged else 3})()

    def nvmlDeviceGetMemoryInfo(self, h):
        return type("M", (), {"used": GOOD_VRAM, "total": 34079899648})()

    def nvmlDeviceGetPowerUsage(self, h):
        return int(GARBAGE_UTIL) if self.wedged else 41000


def _collector(fake, monkeypatch):
    c = NvmlCollector()
    c._health = HealthState.HEALTHY
    c._handles = [object()]
    c._gpu_count = 1
    c._pynvml = fake
    t = {"mono": 0, "wall": 1_790_000_000.0}
    monkeypatch.setattr("atfield.collectors.nvml.monotonic_ns", lambda: t["mono"])
    monkeypatch.setattr("atfield.collectors.nvml._wall_s", lambda: t["wall"])

    def tick():
        out = c.sample()
        t["mono"] += 1_000_000_000
        t["wall"] += 1.0
        return out
    return c, tick


def test_garbage_values_are_recognized_as_implausible():
    assert not is_plausible(GARBAGE_TEMP, "celsius")
    assert not is_plausible(GARBAGE_UTIL, "percent")
    # ...while the memory reading that stayed CORRECT on the same handle passes
    assert is_plausible(33918509056.0, "bytes")
    assert is_plausible(99.2, "percent")


def test_garbage_is_withheld_and_the_correct_reading_survives(monkeypatch):
    fake = _Garbage()
    c, tick = _collector(fake, monkeypatch)
    out = tick()
    assert "gpu.0.core_temp_c" not in out and "gpu.0.util_percent" not in out
    assert out["gpu.0.vram_used_bytes"].value == GOOD_VRAM


def test_sustained_garbage_asks_for_a_fresh_process_and_never_rebuilds(monkeypatch):
    """The 8-hour incident becomes a two-minute one -- and no rebuild runs."""
    fake = _Garbage()
    c, tick = _collector(fake, monkeypatch)
    for _ in range(3):
        tick()
    assert c.health() is HealthState.DEGRADED
    assert c.wants_process_restart is None
    for _ in range(120):
        tick()
    assert c.wants_process_restart and "implausible" in c.wants_process_restart
    assert fake.init_calls == 0 and fake.shutdown_calls == 0


def test_a_short_burst_of_garbage_is_ridden_out(monkeypatch):
    fake = _Garbage()
    c, tick = _collector(fake, monkeypatch)
    for _ in range(5):
        tick()
    fake.wedged = False
    tick()
    assert c.health() is HealthState.HEALTHY
    assert c.wants_process_restart is None
