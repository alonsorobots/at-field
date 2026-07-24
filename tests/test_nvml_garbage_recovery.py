"""NVML "SUCCESS with garbage" detection + session rebuild.

Regression test for 2026-07-23 22:39:26 (RTX 5090, driver 596.36): after a
wedged NVDEC/CUDA context, NVML returned NVML_SUCCESS while reporting
gpu.0.core_temp_c = 885510 C and util/power = 260640043 -- with memory queries
on the SAME handle still correct. Nothing raised, so the collector never
degraded and never recovered; the poison was sticky to the SESSION and cleared
only on process restart, 8 hours later. Meanwhile /headroom read 0.0 and
Kiroshi's WorkerTuner throttled a 6-worker node to 1.
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
from atfield.signals import Sample, monotonic_ns  # noqa: E402

# The literal values recorded in the forensic stream.
GARBAGE_TEMP = 885510.0
GARBAGE_UTIL = 260640043.0


def _collector():
    c = NvmlCollector.__new__(NvmlCollector)
    c._health = HealthState.HEALTHY
    c._consecutive_failures = 0
    c._max_consecutive = 3
    c._implausible_streak = 0
    c._handles = [object()]
    c._gpu_count = 1
    c._pynvml = None
    return c


def _s(v, unit):
    return Sample(value=v, taken_at_ns=monotonic_ns(), source_id="nvml", unit=unit)


def test_garbage_values_are_recognized_as_implausible():
    from atfield.signals import is_plausible
    assert not is_plausible(GARBAGE_TEMP, "celsius")
    assert not is_plausible(GARBAGE_UTIL, "percent")
    # ...while the memory reading that stayed CORRECT on the same handle passes
    assert is_plausible(33918509056.0, "bytes")
    assert is_plausible(99.2, "percent")


def test_reinit_is_attempted_after_sustained_garbage(monkeypatch):
    """Three consecutive impossible ticks must trigger a session rebuild --
    not a FAILED health flag, which would permanently blind the GPU."""
    c = _collector()
    calls = {"n": 0}
    monkeypatch.setattr(NvmlCollector, "_reinit_session",
                        lambda self: (calls.__setitem__("n", calls["n"] + 1), True)[1])

    # Simulate the collector's post-sample logic on garbage ticks.
    for tick in range(3):
        out = {"gpu.0.core_temp_c": _s(GARBAGE_TEMP, "celsius")}
        from atfield.signals import is_plausible
        implausible = [k for k, s in out.items() if not is_plausible(s.value, s.unit)]
        for k in implausible:
            out.pop(k, None)
        if implausible:
            c._implausible_streak += 1
            if c._implausible_streak >= 3:
                if c._reinit_session():
                    c._implausible_streak = 0
        # the garbage must never survive into the emitted samples
        assert "gpu.0.core_temp_c" not in out

    assert calls["n"] == 1, "expected exactly one rebuild at the 3rd bad tick"


def test_reinit_session_rebuilds_handles():
    """A successful rebuild re-acquires handles and reports True."""
    c = _collector()

    class FakeNvml:
        def __init__(self):
            self.shutdown = 0
            self.init = 0
        def nvmlShutdown(self):
            self.shutdown += 1
        def nvmlInit(self):
            self.init += 1
        def nvmlDeviceGetCount(self):
            return 2
        def nvmlDeviceGetHandleByIndex(self, i):
            return f"handle{i}"

    fake = FakeNvml()
    c._pynvml = fake
    c._handles = []
    assert c._reinit_session() is True
    assert fake.shutdown == 1 and fake.init == 1
    assert c._handles == ["handle0", "handle1"]
    assert c._gpu_count == 2


def test_reinit_failure_is_survivable():
    """If the rebuild fails we must not raise -- just report False and retry."""
    c = _collector()

    class BrokenNvml:
        def nvmlShutdown(self):
            raise RuntimeError("already down")
        def nvmlInit(self):
            raise RuntimeError("driver gone")

    c._pynvml = BrokenNvml()
    assert c._reinit_session() is False


def test_no_reinit_while_readings_are_sane():
    c = _collector()
    from atfield.signals import is_plausible
    out = {"gpu.0.core_temp_c": _s(36.0, "celsius"),
           "gpu.0.util_percent": _s(100.0, "percent")}
    implausible = [k for k, s in out.items() if not is_plausible(s.value, s.unit)]
    assert implausible == []
    assert c._implausible_streak == 0
