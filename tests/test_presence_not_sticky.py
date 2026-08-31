"""The presence throttle must be withdrawn when its signal disappears.

THE INCIDENT (2026-08-30). ``presence.sentinel`` is written when a human is at
the machine, and any scheduler may poll it -- kiroshi's worker tuner does, and
caps a runner's workers while it exists. The service loop only ever updated
that file *while a ``system.input_idle_s`` sample was arriving*, so once the
signal stopped being reported the last assertion could never be withdrawn.

Found on DEMETER: a runner pinned to 1 of 3 workers by a sentinel written SIX
HOURS earlier, by a previous service process, on a node whose collectors were
no longer reporting ``input_idle_s`` at all. Nothing was broken, nothing
logged, and the machine simply ran at a third of its capacity indefinitely.

The asymmetry is what sets the direction of the fix. A stale "present"
throttles a host forever and looks like nothing at all; a stale "absent" costs
an operator some noise while they are actually at the keyboard, and corrects
itself the instant the signal returns. So unknown must mean NOT present.
"""
import pathlib

import pytest

from atfield import service as svc
from atfield.collectors import HealthState, ProbeResult
from atfield.signals import Sample, monotonic_ns

IDLE = "system.input_idle_s"

CONFIG = """
[general]
tick_hz = 1

[presence]
enabled = true
idle_threshold_s = 60

[[rules]]
name = "ram-pressure"
signal = "system.ram_used_percent"
threshold = 92.0
window_s = 30
min_fraction_over = 0.67
action = "log"
"""


class _FakeIdleCollector:
    """Reports input_idle_s for the first ``n_ticks`` samples, then nothing.

    Modelling the DISAPPEARANCE is the whole point: the bug is invisible while
    the signal keeps arriving.
    """

    name = "fake"

    def __init__(self, idle_value: float, n_ticks: int) -> None:
        self._idle = idle_value
        self._left = n_ticks
        self.samples_taken = 0

    def probe(self) -> ProbeResult:
        return ProbeResult(available=True, reason="fake", signals=(IDLE,))

    def health(self) -> HealthState:
        return HealthState.HEALTHY

    def sample(self) -> dict:
        self.samples_taken += 1
        if self._left <= 0:
            return {}
        self._left -= 1
        return {IDLE: Sample(self._idle, monotonic_ns(), self.name, "count")}


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text(CONFIG, encoding="utf-8")
    # Withdraw as soon as the signal is missing, so the test does not sleep out
    # the real two-minute grace window.
    monkeypatch.setattr(svc, "_PRESENCE_STALE_AFTER_S", 0.0, raising=False)
    return cfg, tmp_path


def _run(env, collector, ticks):
    cfg, sd = env
    import atfield.service as s

    def _fake_probe(audit):
        return [collector], {collector.name: collector.probe()}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(s, "_probe_all_collectors", _fake_probe)
        s.run_service(config_path=cfg, state_dir=sd, max_ticks=ticks)
    return sd / svc.PRESENCE_SENTINEL_FILENAME


class TestPresenceIsWithdrawnWhenTheSignalStops:
    def test_present_then_signal_disappears_clears_the_sentinel(self, env):
        # 3 ticks with a human at the keyboard, then the signal goes away.
        sentinel = _run(env, _FakeIdleCollector(idle_value=1.0, n_ticks=3), ticks=8)
        assert not sentinel.exists(), (
            "the presence throttle survived its own signal disappearing -- a "
            "runner would stay capped forever, which is the DEMETER incident"
        )

    def test_a_sentinel_left_by_a_previous_process_is_withdrawn(self, env):
        # The real case: the file predates this process entirely and no
        # input_idle_s ever arrives, so nothing in the loop would touch it.
        cfg, sd = env
        stale = sd / svc.PRESENCE_SENTINEL_FILENAME
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("stale", encoding="utf-8")
        sentinel = _run(env, _FakeIdleCollector(idle_value=1.0, n_ticks=0), ticks=5)
        assert not sentinel.exists(), (
            "a sentinel inherited from a previous service process was never "
            "withdrawn -- exactly how DEMETER stayed at 1 of 3 workers"
        )

    def test_presence_still_asserted_while_the_signal_says_so(self, env):
        # The fix must not simply disable presence: a human at the keyboard
        # must still throttle the machine.
        sentinel = _run(env, _FakeIdleCollector(idle_value=1.0, n_ticks=99), ticks=4)
        assert sentinel.exists(), "presence detection stopped working entirely"

    def test_idle_machine_never_asserts_presence(self, env):
        sentinel = _run(env, _FakeIdleCollector(idle_value=9999.0, n_ticks=99), ticks=4)
        assert not sentinel.exists()
