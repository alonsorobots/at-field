"""Every signal a shipped KILL rule depends on, held to the same safety bar.

An audit on 2026-08-30 found 11 gaps across the five signals that kill rules
watch. The shape of the gaps was the alarming part: nearly every sensor was
tested for NOT firing, and several were never tested for actually firing. That
is backwards for a watchdog -- the expensive failure is the guard that stays
quiet, which is exactly what happened when a CPU ran at 95.6 C for an hour with
an armed 90 C rule.

Each signal is held to four properties, one per historical failure:

  fires when dangerous  -- a sustained breach produces a kill action. The gap
                           that let cpu-pkg-hot sit inert.
  silent when safe      -- normal readings never fire. A guard that cries wolf
                           gets disabled, which costs more than it saves.
  garbage is rejected   -- an impossible reading must look like MISSING data,
                           never like "far over threshold". There is a real
                           885510 C incident behind this one; a rule that
                           fires on garbage kills healthy jobs, and one that
                           TRUSTS garbage is worse.
  survives a slow loop  -- the rule still fires when the service ticks slower
                           than configured. The 2026-08-30 regression.

Parametrised over the real shipped rule set so a newly added kill rule inherits
the whole bar automatically, instead of being covered only if someone
remembers.
"""
import pytest

from atfield.config import load_config_from_dict
from atfield.policy import PolicyEngine
from atfield.signals import Sample, is_plausible

_NS = 1_000_000_000

# (signal, unit, threshold, safe value, dangerous value, impossible values)
# Thresholds mirror the shipped config so the scenarios are the real ones.
CRITICAL = [
    ("system.cpu_package_temp_c", "celsius", 90.0, 61.0, 95.6,
     [885510.0, -273.0, float("nan"), float("inf")]),
    ("gpu.0.core_temp_c", "celsius", 88.0, 45.0, 94.0,
     [885510.0, -300.0, float("nan"), float("inf")]),
    ("gpu.0.mem_junction_temp_c", "celsius", 100.0, 60.0, 106.0,
     [885510.0, -300.0, float("nan"), float("inf")]),
    ("system.ram_used_percent", "percent", 92.0, 40.0, 97.3,
     [1e9, -5.0, float("nan"), float("inf")]),
    ("system.commit_percent", "percent", 90.0, 30.0, 99.0,
     [1e9, -5.0, float("nan"), float("inf")]),
]
IDS = [c[0] for c in CRITICAL]


def _engine(signal, unit, threshold, window_s=30, frac=0.67):
    cfg = load_config_from_dict({
        "general": {"tick_hz": 1},
        "rules": [{
            "name": f"guard-{signal}",
            "signal": signal,
            "threshold": threshold,
            "window_s": window_s,
            "min_fraction_over": frac,
            "action": "kill",
        }],
    })
    return PolicyEngine(cfg, available_signals={signal})


def _drive(eng, signal, unit, value, *, seconds=200, interval_s=1.0):
    fired, now = [], 10 * _NS
    for _ in range(max(1, int(seconds / interval_s))):
        s = Sample(value, now, "test", unit)
        fired.extend(eng.tick({signal: s}, now_ns=now))
        now += int(interval_s * _NS)
    return fired


@pytest.mark.parametrize("signal,unit,threshold,safe,danger,garbage", CRITICAL, ids=IDS)
class TestCriticalSensor:
    def test_fires_when_dangerous(self, signal, unit, threshold, safe, danger, garbage):
        eng = _engine(signal, unit, threshold)
        actions = _drive(eng, signal, unit, danger)
        assert actions, (
            f"{signal} sustained at {danger} never tripped its {threshold} "
            f"kill rule -- this guard does not guard"
        )
        assert all(a.kind == "kill" for a in actions)

    def test_silent_when_safe(self, signal, unit, threshold, safe, danger, garbage):
        eng = _engine(signal, unit, threshold)
        assert not _drive(eng, signal, unit, safe)

    def test_garbage_is_rejected_before_it_reaches_a_rule(
        self, signal, unit, threshold, safe, danger, garbage
    ):
        # The service drops implausible samples before the policy engine sees
        # them (service.py, `is_plausible`). Assert the filter actually covers
        # THIS signal's unit -- a unit with no declared range silently accepts
        # anything, which is how 885510 C reached a rule once.
        for bad in garbage:
            assert not is_plausible(bad, unit), (
                f"{bad!r} was accepted as a plausible {unit} reading for "
                f"{signal}; a rule would treat it as a real breach"
            )
        assert is_plausible(safe, unit)
        assert is_plausible(danger, unit)

    def test_garbage_does_not_fire_a_rule_if_it_slips_through(
        self, signal, unit, threshold, safe, danger, garbage
    ):
        # Defence in depth: even if the filter were bypassed, a single
        # impossible spike among normal readings must not reach the sustained
        # fraction and fire.
        eng = _engine(signal, unit, threshold)
        fired, now = [], 10 * _NS
        for i in range(60):
            v = 885510.0 if i == 30 else safe
            fired.extend(eng.tick({signal: Sample(v, now, "test", unit)}, now_ns=now))
            now += _NS
        assert not fired

    @pytest.mark.parametrize("interval_s", [2.0, 4.71, 10.0])
    def test_survives_a_slow_loop(
        self, signal, unit, threshold, safe, danger, garbage, interval_s
    ):
        # 4.71 s is the interval measured on Chronos when every rule went inert.
        eng = _engine(signal, unit, threshold)
        assert _drive(eng, signal, unit, danger, seconds=400, interval_s=interval_s), (
            f"{signal} at {danger} did not fire with the loop ticking every "
            f"{interval_s}s -- the guard is inert at a cadence a real machine hit"
        )
        assert eng.rules_unable_to_fire == ()

    def test_dead_sensor_abstains_rather_than_reading_as_safe(
        self, signal, unit, threshold, safe, danger, garbage
    ):
        # Fail-safe direction: a silent collector must NOT look like "below
        # threshold, all clear". It must abstain, so the starvation path can
        # report a blind rule instead of a calm one.
        eng = _engine(signal, unit, threshold)
        _drive(eng, signal, unit, danger, seconds=60)
        now = 10 * _NS + 300 * _NS
        for _ in range(5):
            assert not eng.tick({}, now_ns=now)      # no sample at all
            now += _NS
