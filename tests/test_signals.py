"""Tests for :mod:`atfield.signals`.

Every test is clock-injected (no real time.monotonic_ns calls) so behavior is
fully deterministic regardless of CI scheduler latency. The most important
test below is ``test_stale_window_abstains_not_passes`` -- it is the
single property that prevents a frozen collector from causing the watchdog
to silently fail open.
"""

from __future__ import annotations

import pytest

from atfield.signals import (
    EMA,
    MIN_SAMPLES_FOR_DECISION,
    EvalResult,
    Sample,
    SlidingWindow,
    Verdict,
    evaluate_window,
    fraction_over_threshold,
)

NS = 1_000_000_000


# ---------------------------------------------------------------------------
# Sample
# ---------------------------------------------------------------------------


class TestSample:
    def test_is_immutable(self, make_sample):
        s = make_sample(value=1.0)
        with pytest.raises(Exception):  # FrozenInstanceError on dataclass
            s.value = 2.0  # type: ignore[misc]

    def test_is_stale_boundary(self, make_sample):
        s = make_sample(value=1.0, t_s=1.0)
        assert not s.is_stale(now_ns=int(1.5 * NS), max_age_ns=NS)
        assert not s.is_stale(now_ns=2 * NS, max_age_ns=NS)
        assert s.is_stale(now_ns=int(2.001 * NS), max_age_ns=NS)


# ---------------------------------------------------------------------------
# SlidingWindow
# ---------------------------------------------------------------------------


class TestSlidingWindow:
    def test_rejects_bad_inputs(self):
        with pytest.raises(ValueError):
            SlidingWindow(window_s=0)
        with pytest.raises(ValueError):
            SlidingWindow(window_s=-1)
        with pytest.raises(ValueError):
            SlidingWindow(window_s=10, slack_s=-0.1)

    def test_eviction_drops_old_samples(self, make_sample):
        w = SlidingWindow(window_s=10.0, slack_s=0.0)
        for i in range(15):
            w.add(make_sample(value=float(i), t_s=float(i)))
        # window=[t-10, t]: t=14, so we keep t=4..14 inclusive => 11 samples
        assert len(w) == 11
        oldest = w.samples()[0]
        assert oldest.value == 4.0

    def test_slack_keeps_brief_late_arrivals(self, make_sample):
        w = SlidingWindow(window_s=10.0, slack_s=0.5)
        for i in range(11):
            w.add(make_sample(value=float(i), t_s=float(i)))
        # latest=10, window+slack covers [-0.5, 10] -> all 11 retained
        assert len(w) == 11

    def test_evict_older_than_compacts_when_collector_silent(self, make_sample):
        w = SlidingWindow(window_s=5.0, slack_s=0.0)
        for i in range(5):
            w.add(make_sample(value=99.0, t_s=float(i)))
        assert len(w) == 5
        # collector goes silent; service ticks and explicitly compacts
        w.evict_older_than(now_ns=20 * NS)
        assert len(w) == 0

    def test_latest_returns_most_recent(self, make_sample):
        w = SlidingWindow(window_s=10.0)
        assert w.latest() is None
        w.add(make_sample(value=1.0, t_s=0.0))
        w.add(make_sample(value=2.0, t_s=1.0))
        assert w.latest().value == 2.0


# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------


class TestEMA:
    def test_rejects_alpha_out_of_range(self):
        with pytest.raises(ValueError):
            EMA(alpha=0)
        with pytest.raises(ValueError):
            EMA(alpha=1.1)
        with pytest.raises(ValueError):
            EMA(alpha=-0.1)

    def test_first_sample_bootstraps(self):
        e = EMA(alpha=0.1)
        assert e.update(50.0) == 50.0  # no startup transient at 0
        assert e.value == 50.0

    def test_smoothing_progresses_toward_input(self):
        e = EMA(alpha=0.5)
        e.update(0.0)
        e.update(100.0)  # 50
        e.update(100.0)  # 75
        e.update(100.0)  # 87.5
        assert e.value == pytest.approx(87.5)

    def test_reset_clears_state(self):
        e = EMA(alpha=0.5)
        e.update(10.0)
        e.reset()
        assert e.value is None
        assert e.update(20.0) == 20.0


# ---------------------------------------------------------------------------
# fraction_over_threshold
# ---------------------------------------------------------------------------


class TestFractionOverThreshold:
    def test_empty_returns_zero(self):
        assert fraction_over_threshold([], threshold=0) == 0.0

    def test_default_comparator_strict_greater(self, make_sample):
        samples = [make_sample(value=v) for v in (10, 20, 30, 40, 50)]
        # threshold=30 (strict >): only 40 and 50 count -> 2/5
        assert fraction_over_threshold(samples, threshold=30) == 0.4

    def test_lower_bound_comparator(self, make_sample):
        samples = [make_sample(value=v) for v in (10, 20, 30, 40, 50)]
        frac = fraction_over_threshold(samples, threshold=25, comparator=lambda v, t: v < t)
        assert frac == 0.4


# ---------------------------------------------------------------------------
# evaluate_window -- the safety-critical surface
# ---------------------------------------------------------------------------


class TestEvaluateWindow:
    def _w(self, samples: list[tuple[float, float]]) -> SlidingWindow:
        w = SlidingWindow(window_s=60.0)
        for value, t_s in samples:
            w.add(Sample(value=value, taken_at_ns=int(t_s * NS), source_id="t", unit="celsius"))
        return w

    def test_rejects_invalid_args(self):
        w = SlidingWindow(window_s=10.0)
        with pytest.raises(ValueError):
            evaluate_window(w, threshold=1.0, min_fraction_over=0.0,
                            now_ns=0, max_sample_age_s=1.0)
        with pytest.raises(ValueError):
            evaluate_window(w, threshold=1.0, min_fraction_over=1.5,
                            now_ns=0, max_sample_age_s=1.0)
        with pytest.raises(ValueError):
            evaluate_window(w, threshold=1.0, min_fraction_over=0.5,
                            now_ns=0, max_sample_age_s=0.0)

    def test_insufficient_samples_abstains(self):
        w = self._w([(95.0, 0.0), (95.0, 1.0)])  # < MIN_SAMPLES_FOR_DECISION
        r = evaluate_window(w, threshold=90.0, min_fraction_over=0.5,
                            now_ns=2 * NS, max_sample_age_s=10.0)
        assert r.verdict is Verdict.INSUFFICIENT
        assert r.fraction_over == 0.0
        assert r.samples_required == MIN_SAMPLES_FOR_DECISION

    def test_sustained_over_threshold_triggers(self):
        w = self._w([(95.0, float(i)) for i in range(10)])
        r = evaluate_window(w, threshold=90.0, min_fraction_over=0.67,
                            now_ns=10 * NS, max_sample_age_s=20.0)
        assert r.verdict is Verdict.TRIGGER
        assert r.fraction_over == 1.0
        assert r.verdict.fires

    def test_brief_spike_does_not_trigger(self):
        # 2 hot samples at start, 8 cool -> 20% over -> below 67% fraction
        samples = [(95.0, 0.0), (95.0, 1.0)] + [(70.0, float(i)) for i in range(2, 10)]
        w = self._w(samples)
        r = evaluate_window(w, threshold=90.0, min_fraction_over=0.67,
                            now_ns=10 * NS, max_sample_age_s=20.0)
        assert r.verdict is Verdict.BELOW
        assert r.fraction_over == pytest.approx(0.2)
        assert not r.verdict.fires

    def test_stale_window_abstains_not_passes(self):
        """Frozen collector must NOT cause the rule to silently report BELOW.

        This is the cornerstone of the staleness contract. If this property
        ever regresses, every other safety guarantee in the watchdog is
        invalidated.

        Semantics: the *latest* sample's age is what matters. If the latest
        sample is older than max_sample_age_s, the collector has gone quiet
        and we abstain. Old samples within the window otherwise count.
        """
        # 10 hot samples, latest at t=9; we evaluate at t=20 -> latest is 11s old
        w = self._w([(95.0, float(i)) for i in range(10)])
        r = evaluate_window(w, threshold=90.0, min_fraction_over=0.67,
                            now_ns=20 * NS, max_sample_age_s=2.0)
        assert r.verdict is Verdict.INSUFFICIENT, (
            "frozen collector (latest sample too old) must abstain, never fall through to BELOW"
        )

    def test_old_samples_in_window_still_count_when_collector_alive(self):
        """If the latest sample is fresh, older samples in the window count.

        Counterexample to the previous test: a 30s window with samples
        spread across the full 30s should NOT be treated as stale just
        because the oldest samples are 30s old -- the collector is clearly
        still alive (latest sample is recent), so all samples in the
        time-bounded window count toward the fraction.
        """
        # 30 samples spanning 0..29s, evaluating at t=29.5; window=60s so all kept
        samples = [(95.0, float(i)) for i in range(30)]
        w = self._w(samples)
        r = evaluate_window(w, threshold=90.0, min_fraction_over=0.67,
                            now_ns=int(29.5 * NS), max_sample_age_s=2.0)
        assert r.verdict is Verdict.TRIGGER, (
            f"expected TRIGGER with fresh latest + 30 hot samples, got {r}"
        )
        assert r.samples_considered == 30

    def test_fraction_at_min_triggers(self):
        # exactly at min: 6/9 = 0.6667 with min=0.6667 -> trigger (>=)
        # exactly at min: 7/10 = 0.7 with min=0.7 -> trigger (>=)
        samples = [(95.0, float(i)) for i in range(7)] + [(70.0, float(i)) for i in range(7, 10)]
        w = self._w(samples)
        r = evaluate_window(w, threshold=90.0, min_fraction_over=0.7,
                            now_ns=10 * NS, max_sample_age_s=20.0)
        assert r.verdict is Verdict.TRIGGER
        assert r.fraction_over == pytest.approx(0.7)

    def test_fraction_just_below_min_does_not_trigger(self):
        # 4/6 = 0.6666... is strictly less than 0.67 -> BELOW
        samples = [(95.0, float(i)) for i in range(4)] + [(70.0, float(i)) for i in range(4, 6)]
        w = self._w(samples)
        r = evaluate_window(w, threshold=90.0, min_fraction_over=0.67,
                            now_ns=6 * NS, max_sample_age_s=20.0)
        assert r.verdict is Verdict.BELOW
        assert r.fraction_over == pytest.approx(4 / 6)

    def test_returns_evaluation_metadata(self):
        w = self._w([(95.0, float(i)) for i in range(5)])
        r = evaluate_window(w, threshold=90.0, min_fraction_over=0.5,
                            now_ns=5 * NS, max_sample_age_s=20.0)
        assert isinstance(r, EvalResult)
        assert r.samples_considered == 5
        assert r.latest_value == 95.0
