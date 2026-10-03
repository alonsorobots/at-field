"""A sleep / Fast-Startup shutdown is ridden out: NVML is left alone and heals by itself.

The recorded timeline, Aurora 2026-10-02 (2x RTX 2070 SUPER, driver 610.60),
from tools/nvml_resume_probe.py -- four long-lived processes, SYSTEM and user,
identical in all four:

  16:45:25  entering sleep. temp 0 C (NVML_SUCCESS), VRAM used
            18446744073673375744 on an 8 GB card (NVML_SUCCESS), util and
            power NVML_ERROR_UNKNOWN (999).
  ...       298 s asleep (the wall clock -- and Windows' monotonic clock --
            jump across it).
  16:50:26  awake: GPU 0 still garbage, GPU 1 fine.
  16:50:27  temps/util/power fine, GPU 0 VRAM still the wrapped value.
  16:50:28  everything fine -- in the processes that had NOT touched NVML.
  The processes that ran nvmlShutdown+nvmlInit during the garbage could never
  read VRAM again (nvml.dll is pinned; a fresh process read it fine).

AT-Field 0.4.17 rebuilt during exactly this and stayed broken for six days.
This replays the timeline and pins: no rebuild, no restart request, healthy
the moment the readings are, and the failures are LOGGED with their codes.
A Fast Startup "shut down" hibernates session 0 the same way (Aurora's
LastBootUpTime stayed 2026-09-26 through two power-offs).
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from atfield.collectors import HealthState  # noqa: E402
from atfield.collectors.nvml import NvmlCollector  # noqa: E402

TOTAL = 8589934592
WRAPPED = 18446744073673375744


class NVMLError(Exception):
    def __init__(self, value):
        super().__init__(f"NVML error {value}")
        self.value = value


class Replay:
    """Per-GPU state: 'ok', 'garbage' (sleep entry) or 'vram_wrapped'."""
    NVML_TEMPERATURE_GPU = 0

    def __init__(self):
        self.state = ["ok", "ok"]
        self.init_calls = self.shutdown_calls = 0

    def nvmlInit(self):
        self.init_calls += 1

    def nvmlShutdown(self):
        self.shutdown_calls += 1

    def nvmlDeviceGetTemperature(self, h, _w):
        return 0 if self.state[h] == "garbage" else 46

    def nvmlDeviceGetUtilizationRates(self, h):
        if self.state[h] == "garbage":
            raise NVMLError(999)
        return type("U", (), {"gpu": 2})()

    def nvmlDeviceGetMemoryInfo(self, h):
        used = WRAPPED if self.state[h] in ("garbage", "vram_wrapped") else 1020309504
        return type("M", (), {"used": used, "total": TOTAL})()

    def nvmlDeviceGetPowerUsage(self, h):
        if self.state[h] == "garbage":
            raise NVMLError(999)
        return 41790


def test_the_recorded_sleep_is_ridden_out(monkeypatch, caplog):
    fake = Replay()
    c = NvmlCollector()
    c._health = HealthState.HEALTHY
    c._handles = [0, 1]
    c._gpu_count = 2
    c._pynvml = fake
    t = {"mono": 0, "wall": 1_790_984_700.0}
    monkeypatch.setattr("atfield.collectors.nvml.monotonic_ns", lambda: t["mono"])
    monkeypatch.setattr("atfield.collectors.nvml._wall_s", lambda: t["wall"])

    def tick(dt=1.0):
        out = c.sample()
        t["mono"] += int(dt * 1e9)
        t["wall"] += dt
        return out

    caplog.set_level(logging.INFO, logger="atfield.collectors.nvml")
    for _ in range(5):
        tick()                                             # healthy, idle
    fake.state = ["garbage", "garbage"]                    # 16:45:25 entering sleep
    out = tick()
    assert "gpu.0.core_temp_c" not in out, "0 C must be withheld"
    assert "gpu.0.vram_used_bytes" not in out, "a wrapped uint64 must be withheld"
    tick()
    tick(298.0)                                            # asleep: both clocks jump
    fake.state = ["garbage", "ok"]                         # 16:50:26
    tick()
    fake.state = ["vram_wrapped", "ok"]                    # 16:50:27
    tick()
    fake.state = ["ok", "ok"]                              # 16:50:28
    out = tick()

    assert c.health() is HealthState.HEALTHY
    assert out["gpu.0.vram_used_bytes"].value == 1020309504
    assert c.wants_process_restart is None, "a sleep must not restart the watchdog"
    assert fake.init_calls == 0 and fake.shutdown_calls == 0, \
        "the collector touched NVML during the transition -- the 0.4.17 failure"
    logged = caplog.text
    assert "nvml:999" in logged, "the failure codes must be logged"
    assert "wall clock jumped" in logged
    assert "recovered" in logged
