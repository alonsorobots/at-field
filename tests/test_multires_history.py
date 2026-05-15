"""Tests for the multi-resolution per-signal history rings.

These cover the math of tier-downsampling (means, not last-value), the
splice logic when assembling a 1h/6h/24h window from multiple tiers,
and the ServiceState wrapper's `snapshot_signal_history` endpoint.
"""

from __future__ import annotations

import time

import pytest

from atfield.http_api import ServiceState, _MultiResHistory, _Tier

# ---------------------------------------------------------------------------
# _Tier
# ---------------------------------------------------------------------------


class TestTier:
    def test_aggregate_one_emits_immediately(self):
        t = _Tier(capacity=10, aggregate=1)
        emitted = t.push(100.0, 5.0)
        assert emitted == (100.0, 5.0)
        assert list(t.samples) == [(100.0, 5.0)]

    def test_aggregate_n_emits_only_on_n_pushes(self):
        t = _Tier(capacity=10, aggregate=3)
        assert t.push(1.0, 10.0) is None
        assert t.push(2.0, 20.0) is None
        emitted = t.push(3.0, 30.0)
        # Mean of [10, 20, 30] = 20; ts is the LATEST upstream ts.
        assert emitted == (3.0, 20.0)
        assert list(t.samples) == [(3.0, 20.0)]

    def test_emit_uses_mean_not_last(self):
        """Documented contract: averaged tier samples are MEANS, not the
        last value in the window. This matters because the user wants
        a 30s spike from 4 hours ago to be visible in the longer window
        (averaged into a 10s sample), not invisible because the next
        tick happened to be at baseline.
        """
        t = _Tier(capacity=10, aggregate=10)
        # 9 baseline samples, then 1 spike.
        for i in range(9):
            t.push(float(i), 0.0)
        emitted = t.push(9.0, 100.0)
        assert emitted is not None
        _ts, mean = emitted
        # mean of nine 0s and one 100 = 10
        assert mean == pytest.approx(10.0)

        # Sustained spike across half the window: [0]*5 + [50]*5 → mean 25.
        t = _Tier(capacity=10, aggregate=10)
        for _ in range(5):
            t.push(0.0, 0.0)
        for _ in range(4):
            t.push(0.0, 50.0)
        last = t.push(0.0, 50.0)
        assert last is not None
        assert last[1] == pytest.approx(25.0)

    def test_capacity_enforced(self):
        t = _Tier(capacity=3, aggregate=1)
        for i in range(5):
            t.push(float(i), float(i))
        # Only the last 3 survive.
        assert list(t.samples) == [(2.0, 2.0), (3.0, 3.0), (4.0, 4.0)]


# ---------------------------------------------------------------------------
# _MultiResHistory
# ---------------------------------------------------------------------------


class TestMultiResHistory:
    def test_tier0_gets_every_push(self):
        h = _MultiResHistory()
        for i in range(20):
            h.push(float(i), float(i))
        assert len(h.tier0.samples) == 20

    def test_tier1_aggregates_10_pushes_into_1(self):
        h = _MultiResHistory()
        for i in range(30):
            h.push(float(i), float(i))
        # 30 pushes → 3 tier 1 samples (means of 0-9, 10-19, 20-29).
        assert len(h.tier1.samples) == 3
        assert h.tier1.samples[0][1] == pytest.approx(4.5)   # mean(0..9)
        assert h.tier1.samples[1][1] == pytest.approx(14.5)  # mean(10..19)
        assert h.tier1.samples[2][1] == pytest.approx(24.5)  # mean(20..29)

    def test_tier2_aggregates_60_pushes_into_1(self):
        h = _MultiResHistory()
        for i in range(120):
            h.push(float(i), float(i))
        # 120 pushes / 60 per tier-2 sample = 2 tier 2 samples.
        assert len(h.tier2.samples) == 2
        # mean of 0..59 = 29.5; mean of 60..119 = 89.5
        assert h.tier2.samples[0][1] == pytest.approx(29.5)
        assert h.tier2.samples[1][1] == pytest.approx(89.5)

    def test_slice_one_hour_uses_tier0_only(self):
        h = _MultiResHistory()
        now = 10000.0
        for i in range(100):
            h.push(now - (100 - i), float(i))  # 100 samples spanning 100s
        result = h.slice(hours=1.0, now_unix=now)
        assert result == [(now - (100 - i), float(i)) for i in range(100)]

    def test_slice_one_hour_drops_older_tier0_samples(self):
        h = _MultiResHistory()
        now = 10000.0
        # First sample is 2 hours ago (way outside 1h window).
        h.push(now - 7200, 999.0)
        for i in range(10):
            h.push(now - (10 - i), float(i))
        result = h.slice(hours=1.0, now_unix=now)
        # The 2-hour-old sample should NOT appear.
        assert all(ts >= now - 3600 for ts, _ in result)
        assert (now - 7200, 999.0) not in result

    def test_slice_six_hours_splices_tier1_and_tier0(self):
        h = _MultiResHistory()
        now = 100000.0
        # Synthesize: push 10800 samples spanning 6 hours (1 sample/s = 21600 too many).
        # Actually let's push 7200 samples covering exactly 2 hours so we
        # have data in tier 0 (last hour) AND tier 1 (1-2 h ago).
        for i in range(7200):
            ts = now - (7200 - i)  # oldest 7200s ago, newest 1s ago
            h.push(ts, float(i))
        result = h.slice(hours=6.0, now_unix=now)
        # tier 0 holds the most-recent 3600 seconds (1 h). tier 1 holds
        # 720 averaged samples covering 7200 seconds. After splice for a
        # 6h window we expect: tier-0 last hour (3600 samples) + tier-1
        # samples covering 1h-2h ago.
        assert len(result) > 3600
        assert all(ts >= now - 6 * 3600 for ts, _ in result)
        # No duplicates across the splice boundary.
        # For each pair of adjacent samples, the older should be from
        # tier 1 (10 s spacing) and the newer from tier 0 (1 s spacing).
        # We don't verify spacing here, just that no two samples have
        # the exact same ts.
        seen = set()
        for ts, _ in result:
            assert ts not in seen, f"duplicate ts {ts} in spliced result"
            seen.add(ts)

    def test_slice_with_no_data(self):
        h = _MultiResHistory()
        result = h.slice(hours=24.0, now_unix=time.time())
        assert result == []


# ---------------------------------------------------------------------------
# ServiceState integration
# ---------------------------------------------------------------------------


def _make_state(tmp_path):
    return ServiceState(
        version="0.0.0-test",
        observe_only=True,
        events_path=tmp_path / "events.jsonl",
        watchdog_log_path=tmp_path / "watchdog.log",
        state_dir=tmp_path,
    )


class _FakeSample:
    def __init__(self, value: float, source: str = "fake", unit: str = "celsius"):
        self.value = value
        self.source_id = source
        self.unit = unit


class TestServiceStateMultiRes:
    def test_record_tick_populates_history(self, tmp_path):
        state = _make_state(tmp_path)
        # Anchor to "now" so the snapshot's wall-clock cutoff doesn't
        # filter our samples out as ancient history.
        base = time.time() - 30
        for i in range(15):
            state.record_tick(
                now_unix=base + i,
                samples={"gpu.0.core_temp_c": _FakeSample(60.0 + i)},
            )
        snap = state.snapshot_signal_history("gpu.0.core_temp_c", hours=1.0)
        assert snap["count"] == 15
        assert snap["unit"] == "celsius"
        assert snap["source"] == "fake"
        assert all(s[1] >= 60.0 for s in snap["samples"])

    def test_unknown_signal_returns_empty(self, tmp_path):
        state = _make_state(tmp_path)
        snap = state.snapshot_signal_history("nonexistent.signal", hours=1.0)
        assert snap["count"] == 0
        assert snap["samples"] == []
        assert snap["unit"] == ""

    def test_hours_clamped_to_24(self, tmp_path):
        state = _make_state(tmp_path)
        state.record_tick(
            now_unix=time.time(),
            samples={"x": _FakeSample(1.0)},
        )
        snap = state.snapshot_signal_history("x", hours=999.0)
        assert snap["hours"] == 24.0

    def test_hours_floor_prevents_zero(self, tmp_path):
        state = _make_state(tmp_path)
        snap = state.snapshot_signal_history("x", hours=0.0)
        assert snap["hours"] > 0.0

    def test_snapshot_signals_still_works(self, tmp_path):
        """Backwards-compat: the existing /signals endpoint reads from
        tier 0 of the multi-res history. Make sure it still emits the
        same shape it always did."""
        state = _make_state(tmp_path)
        base = time.time() - 5
        for i in range(5):
            state.record_tick(
                now_unix=base + i,
                samples={"sig": _FakeSample(float(i))},
            )
        snap = state.snapshot_signals(since=None)
        assert "latest" in snap
        assert "history" in snap
        assert "sig" in snap["latest"]
        assert len(snap["history"]["sig"]) == 5
