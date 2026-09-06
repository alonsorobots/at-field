"""GATE C (at-field half): /headroom must be dimensionally meaningful per CLASS.

Run: pytest tests/test_headroom_is_class_aware.py -q

THE DEFECT
==========
`snapshot_headroom` collapses every kill rule with ONE formula:

    headroom = (threshold - latest) / threshold

For a RESERVOIR (percent-of-capacity) that is fine: 0 is genuinely "empty", so
the ratio means something. For a THERMAL signal it is meaningless -- it treats
0 degrees C as "idle", which is not a physical zero for anything. Two concrete
consequences, both measured against the shipped `cpu-pkg-hot` rule
(threshold 90 C, window 30 s, min_fraction_over 0.67):

1. THE RATCHET. Kiroshi's AIMD grows above headroom 0.35 and cuts below 0.15.
   Under `(90 - T)/90` that means SAFE requires T <= 58.5 C and DANGER starts at
   T >= 76.5 C. A loaded 32-core box sits at 75-85 C. So on this path the
   controller can only ever HOLD or SHRINK -- it can never grow back, whatever
   the machine does. That is a one-way ratchet with no recovery, and it is
   invisible because every individual decision looks locally reasonable.

2. IT DISAGREES WITH THE CONSUMER. Kiroshi's `/headroom/detail` path computes
   thermal headroom as degrees-remaining over a BAND (25 C). At 84 C the scalar
   says 0.067 (DANGER, cut 25%) and the detail path says 0.24 (hold). Same
   machine, same instant, opposite actions -- and which one is in force depends
   only on whether `/headroom/detail` happened to answer. A silently bimodal
   control law.

THE FIX, AND THE DESIGN LINE IT MUST NOT CROSS
==============================================
`snapshot_headroom_detail`'s own docstring states the split: "AT-Field owns the
DESCRIPTION, Kiroshi owns POLICY -- no reservation coefficient, no AIMD, no
policy." So the fix moves exactly one thing into AT-Field: the THERMAL BAND, an
authored property of the wall ("back off within N degrees of this threshold"),
alongside the threshold the operator already authors. It does NOT move Kiroshi's
reservation coefficient K, which is policy and stays in Kiroshi.

Consequence, stated so it is not a surprise: for THERMAL signals the two paths
now agree exactly -- which is the ratchet case and the one that matters. For
RESERVOIR signals Kiroshi's detail path additionally reserves `K * spike`, so it
remains slightly MORE cautious than the scalar. The fallback is therefore never
more aggressive than the primary, which is the property worth having.
"""
from __future__ import annotations

import time

import pytest

from atfield.signals import Sample

from atfield.config import (ApiConfig, AtFieldConfig, GeneralConfig, KillConfig,
                            PresenceConfig, RuleConfig, TargetingConfig)
from atfield.http_api import ServiceState
from atfield.policy import PolicyEngine

CPU = "system.cpu_package_temp_c"
RAM = "system.ram_used_percent"

#: The shipped values, so this test is about the real rule and not a toy.
CPU_THRESHOLD = 90.0
CPU_WINDOW_S = 30
#: Kiroshi's AIMD bands (slot_tuner.SAFE / DANGER).
SAFE, DANGER = 0.35, 0.15


def _engine(thermal_band_c: float | None = None) -> PolicyEngine:
    kw = {} if thermal_band_c is None else {"thermal_band_c": thermal_band_c}
    cfg = AtFieldConfig(
        general=GeneralConfig(), targeting=TargetingConfig(), kill=KillConfig(),
        api=ApiConfig(), presence=PresenceConfig(),
        rules=(
            RuleConfig(name="cpu-pkg-hot", signal=CPU, threshold=CPU_THRESHOLD,
                       window_s=CPU_WINDOW_S, min_fraction_over=0.67,
                       action="kill", **kw),
            RuleConfig(name="ram-pressure", signal=RAM, threshold=92.0,
                       window_s=60, min_fraction_over=0.75, action="kill"),
        ),
    )
    return PolicyEngine(cfg, available_signals={CPU, RAM})


def _state(tmp_path, thermal_band_c: float | None = None) -> ServiceState:
    st = ServiceState(version="0.0.0-test", observe_only=False,
                      events_path=tmp_path / "events.jsonl",
                      watchdog_log_path=tmp_path / "watchdog.log",
                      state_dir=tmp_path)
    st.attach_engine(_engine(thermal_band_c))
    return st


def _feed(state, signal, values, unit="celsius", dt=1.0):
    """Push a series of samples so mean/window are real, not one reading.

    Drives the engine exactly as the service does -- `engine.tick` populates the
    rule stats the scalar reads, `record_tick` populates the history both
    endpoints share. Feeding only one of the two would let the endpoints
    describe different sample sets, which is the very thing under test.
    """
    eng = state._engine
    now = time.time()
    for i, v in enumerate(values):
        smp = Sample(value=float(v), taken_at_ns=time.monotonic_ns(),
                     source_id="test", unit=unit)
        eng.tick({signal: smp}, now_ns=time.monotonic_ns())
        state.record_tick(now_unix=now - (len(values) - i) * dt,
                          samples={signal: smp})


# ------------------------------------------------------------- THE RATCHET
@pytest.mark.parametrize("temp_c", [70.0, 74.0, 78.0])
def test_a_warm_but_safe_cpu_can_still_reach_SAFE(tmp_path, temp_c):
    """THE LOAD-BEARING RED.

    A CPU-bound host under real load sits at 70-80 C against a 90 C wall -- 10
    to 20 degrees of genuine margin. The controller MUST be able to read that as
    safe, or it can never grow back after any cut, forever.

    Under `(threshold - latest)/threshold` it cannot: 70 C reads 0.222, below
    SAFE=0.35, so growth is impossible at any temperature a busy box actually
    reaches.
    """
    st = _state(tmp_path, thermal_band_c=25.0)
    _feed(st, CPU, [temp_c] * 30)
    hr = st.snapshot_headroom()["min_headroom"]
    assert hr >= SAFE, (
        f"at {temp_c:.0f} C against a {CPU_THRESHOLD:.0f} C wall the scalar "
        f"reports headroom {hr:.3f} < SAFE={SAFE}. The controller can never "
        f"grow at any temperature a loaded host reaches: a one-way ratchet.")


def test_the_danger_line_still_lands_where_the_operator_authored_it(tmp_path):
    """The control on the fix: fixing the ratchet must not disarm the wall.

    With a 25 C band, DANGER=0.15 corresponds to 90 - 0.15*25 = 86.25 C -- close
    to the wall, which is the point. A fix that simply reports "plenty of room"
    everywhere would pass the ratchet test and be far worse than the bug.
    """
    st = _state(tmp_path, thermal_band_c=25.0)
    _feed(st, CPU, [87.0] * 30)
    hr = st.snapshot_headroom()["min_headroom"]
    assert hr < DANGER, (
        f"at 87 C -- three degrees under a 90 C kill wall -- headroom is "
        f"{hr:.3f}, which is not DANGER. The fix disarmed the wall.")


def test_scalar_and_detail_agree_for_a_thermal_signal(tmp_path):
    """The bimodality: same machine, same instant, two endpoints, one answer.

    Before: 84 C read 0.067 on the scalar (cut 25%) and 0.24 on detail (hold),
    and which one applied depended on whether /headroom/detail answered.
    """
    st = _state(tmp_path, thermal_band_c=25.0)
    _feed(st, CPU, [84.0] * 30)
    scalar = st.snapshot_headroom()["min_headroom"]
    detail = st.snapshot_headroom_detail()["per_signal"][CPU]
    expected = (CPU_THRESHOLD - detail["mean"]) / 25.0
    assert scalar == pytest.approx(expected, abs=1e-9), (
        f"scalar {scalar:.3f} vs detail-derived {expected:.3f} -- the control "
        f"law is bimodal on endpoint availability")


def test_a_single_hot_sample_does_not_trip_a_sustained_rule(tmp_path):
    """The proxy must have the same time constant as the kill it predicts.

    `cpu-pkg-hot` needs 67% of a 30 s window over 90 C before anything dies, but
    the scalar used `last_value` -- ONE instantaneous sample. So a lone 95 C tick
    inside an 80 C window read as headroom 0.0 and cut the pool 25%, predicting a
    kill that the rule itself would never have fired.
    """
    st = _state(tmp_path, thermal_band_c=25.0)
    _feed(st, CPU, [80.0] * 29 + [95.0])
    hr = st.snapshot_headroom()["min_headroom"]
    assert hr > DANGER, (
        f"one 95 C sample in an otherwise 80 C window drove headroom to "
        f"{hr:.3f} (DANGER). The proxy is twitchier than the sustained rule it "
        f"is supposed to predict.")


# ------------------------------------------------------- RESERVOIR unchanged
def test_a_reservoir_signal_keeps_its_fraction_of_capacity_meaning(tmp_path):
    """Percent-of-capacity has a real zero, so the ratio was always correct
    there. The class-aware fix must not disturb it."""
    st = _state(tmp_path, thermal_band_c=25.0)
    _feed(st, CPU, [40.0] * 30)              # thermally idle; not the binder
    _feed(st, RAM, [46.0] * 30, unit="percent")
    per = st.snapshot_headroom()["per_rule"]
    assert per["ram-pressure"] == pytest.approx((92.0 - 46.0) / 92.0, abs=1e-9)


def test_the_band_is_authored_not_invented(tmp_path):
    """The band must come from the RULE. PHASE3.5 10a: the walls are authored
    once, in AT-Field, and there is deliberately no second set of limits inside
    Kiroshi -- which is exactly what THERMAL_HEADROOM_BAND_C=25.0 was."""
    tight = _state(tmp_path / "a", thermal_band_c=10.0)
    wide = _state(tmp_path / "b", thermal_band_c=40.0)
    for st in (tight, wide):
        _feed(st, CPU, [82.0] * 30)
    hr_tight = tight.snapshot_headroom()["min_headroom"]
    hr_wide = wide.snapshot_headroom()["min_headroom"]

    # DIRECTION MATTERS and is easy to get backwards -- I did, first time.
    # The band is "how many degrees out from the wall to start backing off".
    # A WIDE band is the CONSERVATIVE setting: 8 degrees of margin is most
    # of a 10 C band (0.8, relaxed) but only a fifth of a 40 C band (0.2,
    # alarmed). So wider band => lower headroom => the consumer holds sooner.
    assert hr_wide < hr_tight, (
        f"band 40 gave headroom {hr_wide:.3f} and band 10 gave "
        f"{hr_tight:.3f}; a WIDER band must be MORE cautious, not less")
    assert hr_tight == pytest.approx((90.0 - 82.0) / 10.0, abs=1e-9)
    assert hr_wide == pytest.approx((90.0 - 82.0) / 40.0, abs=1e-9)
