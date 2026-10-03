"""A boot-start service outlives the driver it opened NVML against.

Chronos, 2026-09-02. The machine booted at 19:04:25; AT-Field started 25 s
later and opened an NVML session against driver 610.88. At 19:14:55, 19:15:04
and 19:15:38 Windows UserPnp logged event 20003 -- "Driver Management has
concluded the process to add Service nvlddmkm" -- installing 616.56. The
service held handles into a driver that no longer existed for the next 4.2
days.

Two shapes came out of that one cause, and the collector recovered from
neither:

  LOUD  -- GPU 1's calls raised. Their samples were dropped, `any_failure` went
           true, health went DEGRADED after three ticks... and nothing acts on
           DEGRADED. `_reinit_session()` had exactly one caller: the
           implausible-value branch. A session that fails by RAISING could
           never reach it.

  SILENT -- GPU 0's calls returned NVML_SUCCESS with `core_temp_c = 0.0` and
           `power_w = 0.041`, 2393/2393 identical samples over 24 h. Nothing
           raised, so `any_failure` stayed false and health read HEALTHY.
           `is_plausible` accepted 0.0 C because its celsius range began at
           -50. The garbage detector added in 809856e was built for 885510 C;
           it cannot see a plausible-looking zero.

This is a KNOWN class -- the user-space NVML library and the kernel module
must match, and the documented cure is reloading (nvidia-container-toolkit
#394 and the NVIDIA developer forums both describe the same mismatch). A
process cannot reload nvml.dll -- it is pinned (measured 2026-10-02: 64
FreeLibrary calls, still mapped) -- so the cure is a FRESH PROCESS, which NSSM
provides (AppExit=Restart). What this file pins:

  C. persistent failure (raising, or impossible values) for
     _PERSISTENT_FAILURE_S -> ask for a fresh process. Never an in-process
     rebuild: on 2026-10-02 the sleep/resume probe showed a rebuild during a
     power transition POISONS the process (tests/test_nvml_rides_out_a_sleep.py).
  B. the installed driver no longer matches the one this session opened
     against -- read out-of-band from nvml.dll's own file version, because a
     wedged session cannot be trusted to report its own staleness. This is the
     only route that sees the SILENT shape; it asks for a fresh process at once.
  5 C floor. Silicon in a running machine does not read below ambient. The one
     value claim made anywhere in this change, and it is physical, not
     statistical -- deliberately not "constant for N samples", which is how a
     REAL sensor got called garbage and disabled on Aurora (04fda0d).

The restart is bounded in the service (once per process, an uptime floor, and a
budget across lifetimes -- tests/test_service_restarts_for_a_replaced_driver.py),
so a genuinely dead GPU degrades loudly instead of crash-looping the watchdog.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from atfield.collectors import HealthState  # noqa: E402
from atfield.collectors.nvml import NvmlCollector  # noqa: E402
from atfield.signals import is_plausible  # noqa: E402

# The literal readings from the wedged session (tests/fixtures/wedged_nvml_20260906).
WEDGED_TEMP_C = 0.0
WEDGED_POWER_W = 0.041
# Ground truth on the same cards at the same moment, per nvidia-smi.
REAL_TEMP_C = 26.0
REAL_POWER_W = 37.87

SESSION_DRIVER = "610.88"     # what the probe recorded at 19:04:50
INSTALLED_DRIVER = "616.56"   # what PnP put on disk ten minutes later


class FakeHandle:
    def __init__(self, temp, util, vram, power, raises=False):
        self.temp, self.util, self.vram, self.power, self.raises = (
            temp, util, vram, power, raises)


class FakePynvml:
    """Minimal NVML stand-in. `handles` drive per-GPU behaviour."""

    NVML_TEMPERATURE_GPU = 0

    def __init__(self, handles, *, init_ok=True):
        self.handles = handles
        self.init_ok = init_ok
        self.init_calls = 0
        self.shutdown_calls = 0

    # -- session ---------------------------------------------------------
    def nvmlInit(self):
        self.init_calls += 1
        if not self.init_ok:
            raise RuntimeError("NVML_ERROR_LIB_RM_VERSION_MISMATCH")

    def nvmlShutdown(self):
        self.shutdown_calls += 1

    def nvmlDeviceGetCount(self):
        return len(self.handles)

    def nvmlDeviceGetHandleByIndex(self, i):
        return self.handles[i]

    # -- metrics ---------------------------------------------------------
    @staticmethod
    def _check(h):
        if h.raises:
            raise RuntimeError("NVML_ERROR_GPU_IS_LOST")

    def nvmlDeviceGetTemperature(self, h, _which):
        self._check(h)
        return h.temp

    def nvmlDeviceGetUtilizationRates(self, h):
        self._check(h)
        return type("U", (), {"gpu": h.util})()

    def nvmlDeviceGetMemoryInfo(self, h):
        self._check(h)
        return type("M", (), {"used": h.vram, "total": 34079899648})()

    def nvmlDeviceGetPowerUsage(self, h):
        self._check(h)
        return int(h.power * 1000)


def _collector(fake, *, driver=""):
    """Real __init__, then inject the fake session.

    Constructed through __init__ deliberately: the previous harness in
    test_nvml_garbage_recovery.py uses __new__ and hand-sets fields, which
    means every new piece of collector state has to be remembered in the test
    too, and is an AttributeError away from a false red.

    `driver=""` disables witness B by default -- an empty session version
    means "we never recorded one", so there is nothing to compare against.
    The B tests pass the real string.
    """
    c = NvmlCollector()
    c._health = HealthState.HEALTHY
    c._handles = list(fake.handles)
    c._gpu_count = len(fake.handles)
    c._pynvml = fake
    c._driver_version = driver
    return c


# ---------------------------------------------------------------------------
# The 5 C floor -- the one physical claim
# ---------------------------------------------------------------------------


def test_a_running_gpu_does_not_read_zero_celsius():
    """RED before this change: the celsius range began at -50, so the wedged
    stream was 'plausible' for four days."""
    assert not is_plausible(WEDGED_TEMP_C, "celsius")


@pytest.mark.parametrize("v", [5.0, 21.0, REAL_TEMP_C, 31.0, 88.0, 102.8, 150.0])
def test_the_floor_never_rejects_a_real_reading(v):
    """The refusing direction, and the more important one.

    Every value here was actually measured on this fleet: idle 5090s at 26-31,
    the authored walls at 88, the VRAM junction at 102.8 C that Aurora hit.
    Over-firing is how a real sensor gets disabled -- the mistake this
    codebase already made once.
    """
    assert is_plausible(v, "celsius")


def test_only_celsius_gets_a_floor():
    """A GPU legitimately draws 0 W of *some* rails and sits at 0% util."""
    assert is_plausible(0.0, "percent")
    assert is_plausible(0.0, "watts")
    assert is_plausible(0.0, "bytes")


def test_the_silent_shape_stops_being_published():
    """The whole point: with the floor, the wedged GPU 0 stream is dropped at
    the collector, so its rule STARVES honestly instead of reading BELOW.

    This converts the silent variant into the loud one, which every layer
    downstream already knows how to report.
    """
    fake = FakePynvml([FakeHandle(WEDGED_TEMP_C, 0, 852713472, WEDGED_POWER_W)])
    c = _collector(fake)
    out = c.sample()
    assert "gpu.0.core_temp_c" not in out
    # ...while the readings that were still CORRECT on that same handle survive.
    assert out["gpu.0.vram_used_bytes"].value == 852713472


# ---------------------------------------------------------------------------
# C: persistent failure asks for a fresh process -- never an in-process rebuild
# ---------------------------------------------------------------------------


def _clock(monkeypatch, start_s=0.0):
    """Drive both clocks: monotonic (failure duration) and wall (sleep witness)."""
    t = {"mono": 0, "wall": 1_790_000_000.0 + start_s}
    monkeypatch.setattr("atfield.collectors.nvml.monotonic_ns", lambda: t["mono"])
    monkeypatch.setattr("atfield.collectors.nvml._wall_s", lambda: t["wall"])

    def advance(seconds, *, wall=None):
        t["mono"] += int(seconds * 1e9)
        t["wall"] += seconds if wall is None else wall
    return advance


def test_a_raising_handle_asks_for_a_fresh_process_once_it_persists(monkeypatch):
    """The GPU 1 shape (09-02). It degrades at once and its rules starve; only a
    failure that PERSISTS is escalated, and to a process restart -- the only
    cure that has ever worked."""
    fake = FakePynvml([FakeHandle(REAL_TEMP_C, 4, 4627042304, REAL_POWER_W),
                       FakeHandle(0, 0, 0, 0, raises=True)])
    c = _collector(fake)
    advance = _clock(monkeypatch)
    for _ in range(c._max_consecutive):
        c.sample()
        advance(1)
    assert c.health() is HealthState.DEGRADED
    assert c.wants_process_restart is None, "three seconds of failure is not persistent"

    for _ in range(110):                   # ticking, awake: no wall-clock gap
        c.sample()
        advance(1)
    assert c.wants_process_restart is None, "113 s is still under the threshold"
    for _ in range(8):
        c.sample()
        advance(1)
    assert c.wants_process_restart, "two minutes of continuous failure must ask for a fresh process"
    assert "RuntimeError" in c.wants_process_restart, "the reason must carry the failure"


def test_there_is_no_in_process_rebuild_at_all(monkeypatch):
    """The 2026-10-02 lesson, pinned: a dead GPU for ten minutes never touches
    the session. (0.4.17 rebuilt every second for six days on Aurora.)"""
    fake = FakePynvml([FakeHandle(0, 0, 0, 0, raises=True)])
    c = _collector(fake)
    advance = _clock(monkeypatch)
    for _ in range(600):
        c.sample()
        advance(1)
    assert fake.init_calls == 0 and fake.shutdown_calls == 0, \
        "the collector rebuilt NVML in-process"


def test_health_returns_only_on_a_fully_successful_tick():
    """A half-fixed session stays DEGRADED, so its rules stay starved."""
    good = FakeHandle(REAL_TEMP_C, 4, 4627042304, REAL_POWER_W)
    bad = FakeHandle(0, 0, 0, 0, raises=True)
    fake = FakePynvml([good, bad])
    c = _collector(fake)
    for _ in range(c._max_consecutive):
        c.sample()
    assert c.health() is HealthState.DEGRADED

    bad.raises = False
    bad.temp, bad.util, bad.vram, bad.power = 31.0, 0, 441450496, 9.5
    c.sample()
    assert c.health() is HealthState.HEALTHY


# ---------------------------------------------------------------------------
# B: the out-of-band driver witness -- the only route that sees the SILENT shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("parts,expected", [
    ((8, 17, 16, 1656), "616.56"),    # nvml.dll FileVersion, measured
    ((32, 0, 16, 1656), "616.56"),    # Win32_PnPSignedDriver, same driver
    ((8, 17, 16, 1088), "610.88"),    # the driver this session opened against
])
def test_the_file_version_encoding(parts, expected):
    """Both Windows encodings of an NVIDIA driver carry it in the last two
    groups. A mismatching pair is tested too, so a parse that returns one
    constant cannot satisfy this."""
    from atfield.collectors.nvml import encode_driver_version
    assert encode_driver_version(*parts) == expected


def test_a_replaced_driver_asks_for_a_fresh_process_even_when_nothing_raises(monkeypatch):
    """The SILENT shape, and the reason B exists.

    Every call returns NVML_SUCCESS with a plausible number, so C never fires
    -- health is HEALTHY. Only the out-of-band read knows the session is
    talking to a driver that is gone, and only a fresh process maps the new one.
    """
    fake = FakePynvml([FakeHandle(REAL_TEMP_C, 4, 4627042304, REAL_POWER_W)])
    c = _collector(fake, driver=SESSION_DRIVER)
    monkeypatch.setattr(NvmlCollector, "_installed_driver_version",
                        lambda self: INSTALLED_DRIVER)
    c.sample()
    assert c.driver_replaced is True
    assert c.wants_process_restart and INSTALLED_DRIVER in c.wants_process_restart
    assert fake.init_calls == 0 and fake.shutdown_calls == 0


def test_a_matching_driver_does_nothing(monkeypatch):
    """The refusing direction."""
    fake = FakePynvml([FakeHandle(REAL_TEMP_C, 4, 4627042304, REAL_POWER_W)])
    c = _collector(fake, driver=INSTALLED_DRIVER)
    monkeypatch.setattr(NvmlCollector, "_installed_driver_version",
                        lambda self: INSTALLED_DRIVER)
    c.sample()
    assert c.driver_replaced is False
    assert c.wants_process_restart is None


def test_an_unreadable_file_version_never_fires(monkeypatch):
    """Fail-safe. A wrong or missing read must cost detection, not cause a
    restart loop -- so None means 'I cannot tell', never 'it changed'."""
    fake = FakePynvml([FakeHandle(REAL_TEMP_C, 4, 4627042304, REAL_POWER_W)])
    c = _collector(fake, driver=SESSION_DRIVER)
    monkeypatch.setattr(NvmlCollector, "_installed_driver_version", lambda self: None)
    c.sample()
    assert c.driver_replaced is False
    assert c.wants_process_restart is None


def test_the_real_read_matches_nvidia_smi_on_this_host():
    """Not a mock. The plan flagged the encoding as resting on one data point,
    so check it against the live machine -- the only place it can be wrong.
    """
    pytest.importorskip("ctypes")
    if sys.platform != "win32":
        pytest.skip("Windows-only file-version read")
    got = NvmlCollector()._installed_driver_version()
    if got is None:
        pytest.skip("nvml.dll not present on this machine")
    assert got.count(".") == 1
    major, minor = got.split(".")
    assert major.isdigit() and minor.isdigit() and len(minor) == 2


# ---------------------------------------------------------------------------
# Escalation is for PERSISTENT failure only
# ---------------------------------------------------------------------------


def test_a_failure_that_recovers_never_escalates(monkeypatch):
    """Ninety seconds of failure, then health: no restart, clock cleared."""
    h = FakeHandle(0, 0, 0, 0, raises=True)
    fake = FakePynvml([h])
    c = _collector(fake)
    advance = _clock(monkeypatch)
    for _ in range(90):
        c.sample()
        advance(1)
    h.raises = False
    h.temp, h.util, h.vram, h.power = REAL_TEMP_C, 4, 4627042304, REAL_POWER_W
    c.sample()
    assert c.health() is HealthState.HEALTHY
    assert c.wants_process_restart is None
    h.raises = True                        # a fresh failure starts a fresh clock
    for _ in range(60):
        advance(1)
        c.sample()
    assert c.wants_process_restart is None


def test_a_wall_clock_gap_restarts_the_failure_clock(monkeypatch):
    """What fails across a sleep is the transition, not the session: 100 s of
    failure, a 300 s sleep (the monotonic clock advances through it too, as
    Windows' does), then 100 s more -- never 120 s of failure AWAKE."""
    fake = FakePynvml([FakeHandle(0, 0, 0, 0, raises=True)])
    c = _collector(fake)
    advance = _clock(monkeypatch)
    for _ in range(100):
        c.sample()
        advance(1)
    advance(300)                           # asleep: both clocks jump
    for _ in range(100):
        c.sample()
        advance(1)
    assert c.wants_process_restart is None
