"""A rule must still fire when the service loop runs slower than configured.

THE INCIDENT (2026-08-30). ``min_samples`` and ``max_sample_age_s`` were both
sized from the CONFIGURED ``general.tick_hz`` (1). The real tick had degraded
to ~0.22 Hz, so ``cpu-pkg-hot`` required 21 samples from a 30 s window that
could only hold ~7. Every rule on every node returned ``INSUFFICIENT`` forever
and fired nothing, while ``/health`` kept reporting ``mode: armed,
rules_active: 7, rules_starved: 0``. A CPU ran at >=90 C for 719 of 766 samples
in one hour, peaking 95.62 C, and the 90 C kill rule never ran.

Nothing in the suite caught it because every ``tick_hz`` test asserts that the
config VALUE parses -- ``tick_hz = 2`` loads, ``-1`` is rejected -- and none
asks whether a rule can actually fire at the rate the service achieves. These
tests drive the engine at a deliberately slow cadence and assert on outcomes.
"""
import math

import pytest

from atfield.config import load_config_from_dict
from atfield.policy import PolicyEngine
from atfield.signals import MIN_SAMPLES_FOR_DECISION, Sample

_NS = 1_000_000_000
SIGNAL = "system.cpu_package_temp_c"


def _cfg(window_s=30, frac=0.67, threshold=90.0, tick_hz=1):
    return load_config_from_dict({
        "general": {"tick_hz": tick_hz},
        "rules": [{
            "name": "cpu-pkg-hot",
            "signal": SIGNAL,
            "threshold": threshold,
            "window_s": window_s,
            "min_fraction_over": frac,
            "action": "kill",
        }],
    })


def _engine(**kw):
    return PolicyEngine(_cfg(**kw), available_signals={SIGNAL})


def _run(engine, *, value, seconds, interval_s, start_ns=10 * _NS):
    """Tick the engine at `interval_s` for `seconds`; return actions fired."""
    fired = []
    now = start_ns
    for _ in range(max(1, int(seconds / interval_s))):
        s = Sample(value, now, "test", "celsius")
        fired.extend(engine.tick({SIGNAL: s}, now_ns=now))
        now += int(interval_s * _NS)
    return fired


class TestFiresAtSlowCadence:
    def test_sustained_overheat_fires_at_the_configured_rate(self):
        # Control: at 1 Hz this always worked, and must keep working.
        eng = _engine()
        assert _run(eng, value=95.6, seconds=90, interval_s=1.0)

    @pytest.mark.parametrize("interval_s", [2.0, 4.71, 10.0])
    def test_sustained_overheat_still_fires_when_the_loop_is_slow(self, interval_s):
        # THE REGRESSION. 4.71 s is the interval measured on Chronos.
        eng = _engine()
        assert _run(eng, value=95.6, seconds=300, interval_s=interval_s), (
            f"a 95.6 C CPU did not trigger a 90 C kill rule at "
            f"{1/interval_s:.2f} Hz -- the guard is inert, which is exactly the "
            f"2026-08-30 failure"
        )

    def test_a_cool_cpu_still_does_not_fire_at_a_slow_cadence(self):
        # The fix must not buy sensitivity by making the rule trigger-happy.
        eng = _engine()
        assert not _run(eng, value=61.0, seconds=300, interval_s=4.71)

    def test_brief_spike_does_not_fire(self):
        # 67% of the window must still mean 67% of the window.
        eng = _engine()
        now = 10 * _NS
        for i in range(40):
            v = 95.6 if i % 5 == 0 else 70.0     # 20% over threshold
            s = Sample(v, now, "test", "celsius")
            assert not eng.tick({SIGNAL: s}, now_ns=now)
            now += int(4.71 * _NS)


class TestCadenceDerivedLimits:
    def test_observed_hz_tracks_the_real_interval(self):
        eng = _engine()
        _run(eng, value=70.0, seconds=200, interval_s=4.0)
        assert eng.observed_tick_hz == pytest.approx(0.25, rel=0.15)

    def test_requirement_scales_with_observed_cadence(self):
        eng = _engine()
        rule = eng.effective_rules[0]
        assert rule.min_samples == math.ceil(30 * 1 * 0.67)   # configured: 21
        _run(eng, value=70.0, seconds=200, interval_s=4.71)
        need = eng.min_samples_for(rule)
        capacity = 30 * eng.observed_tick_hz
        assert need <= capacity, (
            f"needs {need} samples from a window holding {capacity:.1f}"
        )
        assert need >= MIN_SAMPLES_FOR_DECISION

    def test_never_below_the_absolute_floor(self):
        # A very slow loop must not reduce the rule to a single reading.
        eng = _engine()
        _run(eng, value=70.0, seconds=2000, interval_s=60.0)
        assert eng.min_samples_for(eng.effective_rules[0]) >= MIN_SAMPLES_FOR_DECISION

    def test_configured_value_used_before_any_cadence_is_known(self):
        eng = _engine()
        assert eng.observed_tick_hz is None
        rule = eng.effective_rules[0]
        assert eng.min_samples_for(rule) == rule.min_samples

    def test_liveness_bound_widens_with_the_cadence(self):
        # Otherwise a healthy collector looks "silent" and the rule abstains
        # even after min_samples is fixed -- the same bug, second symptom.
        eng = _engine()
        _run(eng, value=70.0, seconds=200, interval_s=4.71)
        assert eng.max_sample_age_for_tick() > 4.71


class TestUnableToFireIsLoud:
    def test_a_rule_that_cannot_fire_is_reported(self, caplog):
        # 3 s window at ~0.1 Hz cannot hold even the floor of 3 samples.
        eng = PolicyEngine(_cfg(window_s=3), available_signals={SIGNAL})
        with caplog.at_level("ERROR"):
            _run(eng, value=70.0, seconds=400, interval_s=10.0)
        assert "cpu-pkg-hot" in eng.rules_unable_to_fire
        assert any("CANNOT FIRE" in r.message for r in caplog.records)

    def test_a_healthy_rule_is_not_reported(self):
        eng = _engine()
        _run(eng, value=70.0, seconds=200, interval_s=1.0)
        assert eng.rules_unable_to_fire == ()
