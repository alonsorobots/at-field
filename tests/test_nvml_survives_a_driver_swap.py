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
#394 and the NVIDIA developer forums both describe the same mismatch). What
this file pins is that the collector notices and tries to cure itself, by
three independent routes:

  C. sustained DEGRADED -> rebuild the session, with backoff so a genuinely
     dead GPU cannot spin. A state fact; no judgement about any value.
  B. the installed driver no longer matches the one this session opened
     against -- read out-of-band from nvml.dll's own file version, because a
     wedged session cannot be trusted to report its own staleness. This is the
     only route that sees the SILENT shape.
  5 C floor. Silicon in a running machine does not read below ambient. The one
     value claim made anywhere in this change, and it is physical, not
     statistical -- deliberately not "constant for N samples", which is how a
     REAL sensor got called garbage and disabled on Aurora (04fda0d).

And, because prior art says a process that still has the OLD nvml.dll mapped
may not be curable in-process at all: when B has fired and two rebuilds have
not restored health, the collector asks for a process restart. NSSM is
configured AppExit=Restart, so exiting IS a cure. Bounded to once per process,
with an uptime floor, and only on B's exact witness -- never on DEGRADED
alone, so a genuinely broken NVML degrades loudly instead of crash-looping the
watchdog.
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
# C: sustained DEGRADED cures itself
# ---------------------------------------------------------------------------


def test_a_raising_handle_eventually_triggers_a_rebuild(monkeypatch):
    """The GPU 1 shape. RED before this change: `_reinit_session` had exactly
    one caller and it was the implausibility branch, so a session that failed
    by raising degraded and then sat there for 4.2 days."""
    fake = FakePynvml([FakeHandle(REAL_TEMP_C, 4, 4627042304, REAL_POWER_W),
                       FakeHandle(0, 0, 0, 0, raises=True)])
    c = _collector(fake)
    calls = {"n": 0}
    monkeypatch.setattr(NvmlCollector, "_reinit_session",
                        lambda self: calls.__setitem__("n", calls["n"] + 1) or True)

    for _ in range(c._max_consecutive):
        c.sample()
    assert c.health() is HealthState.DEGRADED
    assert calls["n"] == 1, "a DEGRADED session must attempt to cure itself"


def test_the_rebuild_backs_off_and_does_not_spin(monkeypatch):
    """A permanently dead GPU must not rebuild the session every tick.

    Driven by monotonic time, not tick counts -- the service loop is not 1 Hz
    (measured 2.7-6.7 s per tick on Chronos).
    """
    fake = FakePynvml([FakeHandle(0, 0, 0, 0, raises=True)])
    c = _collector(fake)
    calls = {"n": 0}
    monkeypatch.setattr(NvmlCollector, "_reinit_session",
                        lambda self: calls.__setitem__("n", calls["n"] + 1) or False)

    now = {"t": 0}
    monkeypatch.setattr("atfield.collectors.nvml.monotonic_ns", lambda: now["t"])

    for _ in range(c._max_consecutive):
        c.sample()
    assert calls["n"] == 1

    now["t"] += 5_000_000_000          # 5 s later -- inside the first backoff
    c.sample()
    assert calls["n"] == 1, "rebuilt again while still backing off"

    now["t"] += 60_000_000_000         # well past it
    c.sample()
    assert calls["n"] == 2


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


def test_a_replaced_driver_forces_a_rebuild_even_when_nothing_raises(monkeypatch):
    """The SILENT shape, and the reason B exists.

    Every call returns NVML_SUCCESS with a plausible number, so C never fires
    -- health is HEALTHY. Only the out-of-band read knows the session is
    talking to a driver that is gone.
    """
    fake = FakePynvml([FakeHandle(REAL_TEMP_C, 4, 4627042304, REAL_POWER_W)])
    c = _collector(fake, driver=SESSION_DRIVER)
    monkeypatch.setattr(NvmlCollector, "_installed_driver_version",
                        lambda self: INSTALLED_DRIVER)
    calls = {"n": 0}
    monkeypatch.setattr(NvmlCollector, "_reinit_session",
                        lambda self: calls.__setitem__("n", calls["n"] + 1) or True)

    c.sample()
    assert calls["n"] == 1
    # ...and once the rebuild succeeds, /health must stop advertising the
    # driver this session no longer talks to. That string is what a human
    # compares against nvidia-smi at deploy time.
    assert c._driver_version == INSTALLED_DRIVER
    assert c.driver_replaced is False


def test_a_matching_driver_does_nothing(monkeypatch):
    """The refusing direction."""
    fake = FakePynvml([FakeHandle(REAL_TEMP_C, 4, 4627042304, REAL_POWER_W)])
    c = _collector(fake, driver=INSTALLED_DRIVER)
    monkeypatch.setattr(NvmlCollector, "_installed_driver_version",
                        lambda self: INSTALLED_DRIVER)
    calls = {"n": 0}
    monkeypatch.setattr(NvmlCollector, "_reinit_session",
                        lambda self: calls.__setitem__("n", calls["n"] + 1) or True)
    c.sample()
    assert calls["n"] == 0
    assert c.driver_replaced is False


def test_an_unreadable_file_version_never_fires(monkeypatch):
    """Fail-safe. A wrong or missing read must cost detection, not cause a
    restart loop -- so None means 'I cannot tell', never 'it changed'."""
    fake = FakePynvml([FakeHandle(REAL_TEMP_C, 4, 4627042304, REAL_POWER_W)])
    c = _collector(fake, driver=SESSION_DRIVER)
    monkeypatch.setattr(NvmlCollector, "_installed_driver_version", lambda self: None)
    calls = {"n": 0}
    monkeypatch.setattr(NvmlCollector, "_reinit_session",
                        lambda self: calls.__setitem__("n", calls["n"] + 1) or True)
    c.sample()
    assert calls["n"] == 0
    assert c.driver_replaced is False


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
# Escalation: when in-process rebuilding is not the cure
# ---------------------------------------------------------------------------


def test_escalates_only_after_rebuilds_have_failed(monkeypatch):
    """Prior art says a process holding the OLD nvml.dll may not be curable in
    process at all -- `nvmlInit` itself can refuse with a version mismatch.
    Then the cure is a fresh process, which NSSM already provides."""
    fake = FakePynvml([FakeHandle(WEDGED_TEMP_C, 0, 852713472, WEDGED_POWER_W)],
                      init_ok=False)
    c = _collector(fake, driver=SESSION_DRIVER)
    monkeypatch.setattr(NvmlCollector, "_installed_driver_version",
                        lambda self: INSTALLED_DRIVER)
    now = {"t": 0}
    monkeypatch.setattr("atfield.collectors.nvml.monotonic_ns", lambda: now["t"])

    c.sample()
    assert c.wants_process_restart is None, "one failed rebuild is not enough"

    now["t"] += 90_000_000_000        # past the backoff, > 60 s spanned
    c.sample()
    assert c.wants_process_restart, "two failed rebuilds after a driver swap must escalate"
    assert INSTALLED_DRIVER in c.wants_process_restart


def test_never_escalates_on_DEGRADED_alone(monkeypatch):
    """The bound that stops a broken NVML from crash-looping the watchdog.

    Without B's exact witness, a dead GPU degrades loudly and its rules starve
    -- it does not take the whole service down with it, over and over.
    """
    fake = FakePynvml([FakeHandle(0, 0, 0, 0, raises=True)], init_ok=False)
    c = _collector(fake, driver=SESSION_DRIVER)
    monkeypatch.setattr(NvmlCollector, "_installed_driver_version",
                        lambda self: SESSION_DRIVER)      # driver did NOT change
    now = {"t": 0}
    monkeypatch.setattr("atfield.collectors.nvml.monotonic_ns", lambda: now["t"])
    for _ in range(10):
        now["t"] += 120_000_000_000
        c.sample()
    assert c.health() is HealthState.DEGRADED
    assert c.wants_process_restart is None
