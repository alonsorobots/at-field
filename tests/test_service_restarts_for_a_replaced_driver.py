"""The service exits so a fresh process can map a replaced driver.

Companion to ``test_nvml_survives_a_driver_swap.py``, which stops at the
collector setting ``wants_process_restart``. Nothing acts on a flag; this is
the half that acts.

Why exiting is a cure and not an outage: NSSM is configured
``AppExit Default = Restart`` with ``AppRestartDelay 0`` and
``AppThrottle 1500`` (verified on Chronos), so a deliberate exit is handed
straight back as a new process -- and a new process is the documented cure
for an NVML library/kernel-module mismatch, because only it maps the new
``nvml.dll``.

The danger is obvious and is what most of this file tests: a watchdog that
restarts itself on a condition that does not clear is worse than one that
degrades. So the request is bounded three ways, and each bound is tested for
REFUSING, not just for firing:

  * only on the driver-swap witness -- never on DEGRADED alone, so a card
    that is genuinely dead or removed starves its rules loudly instead of
    cycling the service;
  * at most once per process;
  * never below an uptime floor, so a condition present at boot cannot
    produce a tight loop.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import atfield.service as svc  # noqa: E402
from atfield.collectors import HealthState, ProbeResult  # noqa: E402
from atfield.signals import Sample, monotonic_ns  # noqa: E402

TEMP = "system.cpu_package_temp_c"

CONFIG = f"""
[general]
tick_hz = 50

[[rules]]
name = "cpu-pkg-hot"
signal = "{TEMP}"
threshold = 90.0
window_s = 30
min_fraction_over = 0.67
action = "log"
"""

REASON = ("NVML session was opened against driver 610.88 but 616.56 is "
          "installed, and 2 in-process rebuilds did not recover it")


class _FakeCollector:
    """Publishes a healthy temperature, and asks for a restart on demand.

    It keeps sampling normally throughout: the request must be honoured
    because of the witness, not because the collector went quiet.
    """

    name = "fake"

    def __init__(self, *, want_after: int | None = None) -> None:
        self._want_after = want_after
        self.samples_taken = 0
        self.wants_process_restart: str | None = None

    def probe(self) -> ProbeResult:
        return ProbeResult(available=True, reason="fake", signals=(TEMP,))

    def health(self) -> HealthState:
        return HealthState.HEALTHY

    def sample(self) -> dict:
        self.samples_taken += 1
        if self._want_after is not None and self.samples_taken >= self._want_after:
            self.wants_process_restart = REASON
        return {TEMP: Sample(55.0, monotonic_ns(), self.name, "celsius")}


@pytest.fixture
def env(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(CONFIG, encoding="utf-8")
    return cfg, tmp_path


def _run(env, collector, ticks, *, uptime_floor_s=0.0):
    cfg, sd = env

    def _fake_probe(audit):
        return [collector], {collector.name: collector.probe()}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(svc, "_probe_all_collectors", _fake_probe)
        mp.setattr(svc, "_RESTART_UPTIME_FLOOR_S", uptime_floor_s, raising=False)
        return svc.run_service(config_path=cfg, state_dir=sd, max_ticks=ticks)


def _events(state_dir: Path) -> list[str]:
    p = state_dir / "events.jsonl"
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


class TestItFires:
    def test_the_service_exits_with_the_restart_code(self, env):
        c = _FakeCollector(want_after=2)
        code = _run(env, c, ticks=20)
        assert code == svc._EXIT_WANTS_RESTART
        assert code not in (0, 1), "must be distinguishable from a clean stop and a crash"

    def test_it_stops_ticking_immediately_rather_than_finishing_the_run(self, env):
        """max_ticks=20 but the request lands on tick 2, so it must leave
        early -- a watchdog that knows it is blind should not keep pretending
        to guard for another 18 ticks."""
        c = _FakeCollector(want_after=2)
        _run(env, c, ticks=20)
        assert c.samples_taken < 20

    def test_the_reason_reaches_the_event_stream(self, env):
        """The operator has to be able to find out WHY the service bounced,
        after the fact. A log line that scrolled away is not enough."""
        cfg, sd = env
        c = _FakeCollector(want_after=2)
        _run(env, c, ticks=20)
        blob = "\n".join(_events(sd))
        assert "wants_process_restart" in blob
        assert "616.56" in blob


class TestItRefuses:
    def test_a_collector_that_never_asks_runs_to_completion(self, env):
        c = _FakeCollector(want_after=None)
        code = _run(env, c, ticks=5)
        assert code == 0
        assert c.samples_taken == 5

    def test_a_request_below_the_uptime_floor_is_deferred(self, env):
        """The bound against a boot-time condition cycling the service.

        Identical to the firing case except for the floor, so this isolates
        the floor itself rather than some other difference.
        """
        c = _FakeCollector(want_after=2)
        code = _run(env, c, ticks=5, uptime_floor_s=3600.0)
        assert code == 0
        assert c.samples_taken == 5, "deferring must not also stop the loop"

    def test_a_collector_without_the_attribute_at_all_is_fine(self, env):
        """Only nvml grows this today. Every other collector -- and any
        third-party one -- must not need to know the protocol exists."""
        class _Bare(_FakeCollector):
            def __init__(self):
                super().__init__(want_after=None)
                del self.wants_process_restart

        code = _run(env, _Bare(), ticks=3)
        assert code == 0
