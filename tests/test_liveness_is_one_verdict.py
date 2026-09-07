"""Liveness is per-signal, means arriving AND credible, and is decided once.

On 2026-09-03 at 12:46:48 this service logged, for a kill rule guarding a GPU
that was reading 0.0 C from a wedged NVML session:

    {"type":"signal_health","state":"recovered",
     "rule":"gpu-core-hot[gpu.0.core_temp_c]","silent_for_s":63106}

"rule is guarding again." It was not. `_track_starvation` clears
``rule.starved`` on ANY arriving sample, with no test of whether the sample
means anything, so a constant garbage zero retracted a correct alarm and
turned it into an explicit all-clear. `/health` went from 2 starved rules to 1
and `/headroom` began publishing 1.0 -- perfect headroom -- for a rule that
could never fire.

That is worse than never alarming, because it manufactures confidence.

Four places answered "is this signal trustworthy" and gave four answers: the
engine (``starved``), ``/headroom`` (drops starved rules, implicitly),
``/headroom/detail`` (includes everything, frozen values and all), and
``/signals`` (the same). This pins ONE verdict, computed from two facts that
are not judgement calls -- the sample's age, and the health of the collector
that produced it -- with every endpoint rendering it rather than re-deciding.

The discriminating test in here is
``test_a_PLAUSIBLE_value_from_a_degraded_collector_is_also_refused``. The 5 C
floor shipped in the previous phase already stops a literal 0.0, so a test
using only the fixture's zero would pass for the wrong reason -- it would be
testing the floor, not the credibility gate. A plausible 45.0 C arriving from
a DEGRADED collector separates them.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import atfield.service as svc  # noqa: E402
from atfield.collectors import HealthState, ProbeResult  # noqa: E402
from atfield.config import default_config  # noqa: E402
from atfield.http_api import ServiceState  # noqa: E402
from atfield.policy import PolicyEngine  # noqa: E402
from atfield.signals import Sample, is_credible, monotonic_ns  # noqa: E402

WEDGED = Path(__file__).parent / "fixtures" / "wedged_nvml_20260906"

GPU0 = "gpu.0.core_temp_c"
RULE0 = f"gpu-core-hot[{GPU0}]"

# Straight out of the capture.
WEDGED_ZERO = 0.0
REAL_TEMP = 26.0
# Plausible, and still not believable, because of where it came from.
PLAUSIBLE_FROM_A_BROKEN_COLLECTOR = 45.0

CONFIG = f"""
[general]
tick_hz = 1

[[rules]]
name = "gpu-core-hot"
signal = "gpu.*.core_temp_c"
threshold = 88.0
window_s = 30
min_fraction_over = 0.67
action = "kill"
"""


def _s(value: float, source: str = "nvml", unit: str = "celsius",
       taken_at_ns: int | None = None) -> Sample:
    """A sample. `taken_at_ns` MUST come from the same clock the engine runs
    on -- a real monotonic stamp against a synthetic engine clock makes every
    sample look ancient, and the window silently evicts all of them."""
    return Sample(value=value,
                  taken_at_ns=monotonic_ns() if taken_at_ns is None else taken_at_ns,
                  source_id=source, unit=unit)


# ---------------------------------------------------------------------------
# is_credible -- the pure predicate
# ---------------------------------------------------------------------------


def test_credibility_needs_BOTH_a_healthy_collector_and_a_possible_value():
    assert is_credible(_s(REAL_TEMP), "HEALTHY")
    # A possible value from a collector that has told us it is broken.
    assert not is_credible(_s(PLAUSIBLE_FROM_A_BROKEN_COLLECTOR), "DEGRADED")
    # An impossible value from a collector that thinks it is fine -- the
    # silent shape, where nothing raised so health never moved.
    assert not is_credible(_s(WEDGED_ZERO), "HEALTHY")
    assert not is_credible(_s(WEDGED_ZERO), "DEGRADED")


# ---------------------------------------------------------------------------
# The gate at the engine's entrance
# ---------------------------------------------------------------------------


def test_the_loop_splits_samples_before_the_engine_sees_them():
    good = _s(REAL_TEMP, source="nvml")
    bad = _s(PLAUSIBLE_FROM_A_BROKEN_COLLECTOR, source="lhm")
    credible, suspect = svc.split_by_credibility(
        {"a": good, "b": bad}, {"nvml": "HEALTHY", "lhm": "DEGRADED"})
    assert set(credible) == {"a"}
    assert set(suspect) == {"b"}


def test_a_sample_from_an_unknown_source_is_trusted():
    """Fail-safe. A collector we have no health for must not be silently
    muted -- that would blind a rule for a bookkeeping gap."""
    credible, suspect = svc.split_by_credibility({"a": _s(REAL_TEMP, "mystery")}, {})
    assert set(credible) == {"a"} and not suspect


# ---------------------------------------------------------------------------
# The service-level replay: dark, then something arrives
# ---------------------------------------------------------------------------


class _Replay:
    """ServiceState + PolicyEngine driven tick by tick through the REAL gate.

    Deliberately calls ``svc.split_by_credibility`` rather than handing
    samples straight to ``engine.tick``: a harness that bypasses the filter
    would pass before the change and prove nothing.
    """

    class _Clock:
        """Wall clock for the API layer, driven by the replay's own tick.

        `liveness()` is computed at READ time from `time.time()`, so a replay
        on a synthetic clock has to hand the API layer the same clock or every
        signal reads as decades old. monotonic_ns is passed through untouched
        -- the engine owns that one.
        """
        def __init__(self, owner): self._owner = owner
        def time(self): return self._owner.now
        def monotonic_ns(self): return int(self._owner.now * 1e9)

    def __init__(self, tmp_path: Path):
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text(CONFIG, encoding="utf-8")
        from atfield.config import load_config
        cfg = load_config(cfg_path)
        self.engine = PolicyEngine(cfg, available_signals=(GPU0,))
        self.state = ServiceState(
            version="test", observe_only=True,
            events_path=tmp_path / "events.jsonl",
            watchdog_log_path=tmp_path / "w.log",
            state_dir=tmp_path,
        )
        self.state.attach_engine(self.engine)
        self.now = 1_000_000.0
        import atfield.http_api as _api
        self._mp = pytest.MonkeyPatch()
        self._mp.setattr(_api, "time", _Replay._Clock(self))

    def tick(self, value=None, *, health="HEALTHY", dt=1.0):
        self.now += dt
        now_ns = int(self.now * 1e9)
        samples = {} if value is None else {GPU0: _s(value, taken_at_ns=now_ns)}
        credible, _suspect = svc.split_by_credibility(samples, {"nvml": health})
        self.engine.tick(credible, now_ns=now_ns)
        self.state.record_tick(now_unix=self.now, samples=samples,
                               credible=set(credible))

    def dark(self, seconds: float):
        # One long silent gap, the way 17.5 hours of nothing actually looked.
        self.tick(None, dt=seconds)

    def rule(self):
        return next(r for r in self.state.snapshot_rules()["effective"]
                    if r["name"] == RULE0)


@pytest.fixture
def replay(tmp_path):
    r = _Replay(tmp_path)
    yield r
    r._mp.undo()


def test_dark_then_a_garbage_zero_STAYS_starved(replay):
    """The 2026-09-03 12:46:48 event, and it must not happen again."""
    for _ in range(5):
        replay.tick(32.0)
    assert replay.rule()["starved"] is False

    replay.dark(63106.0)          # 17.5 h, the real gap
    assert replay.rule()["starved"] is True

    for _ in range(5):
        replay.tick(WEDGED_ZERO, health="DEGRADED")
    r = replay.rule()
    assert r["starved"] is True, "a garbage zero retracted the alarm again"
    assert r["liveness"] == "suspect"


def test_a_PLAUSIBLE_value_from_a_degraded_collector_is_also_refused(replay):
    """THE DISCRIMINATING CASE.

    45 C is a perfectly possible GPU temperature, so the 5 C floor cannot
    reject it and the previous phase's fix does not apply. Only the
    collector's own admission that it is broken does. Without this test the
    one above would pass for the wrong reason.
    """
    for _ in range(5):
        replay.tick(32.0)
    replay.dark(63106.0)
    for _ in range(5):
        replay.tick(PLAUSIBLE_FROM_A_BROKEN_COLLECTOR, health="DEGRADED")
    r = replay.rule()
    assert r["starved"] is True
    assert r["liveness"] == "suspect"


def test_dark_then_a_real_value_from_a_healthy_collector_RECOVERS(replay):
    """The Aurora direction (04fda0d): no heuristic may block a real sensor.

    Green before this change too, and it must stay green -- that is the
    point. A gate that only ever refuses is as useless as one that only ever
    accepts.
    """
    for _ in range(5):
        replay.tick(32.0)
    replay.dark(63106.0)
    assert replay.rule()["starved"] is True

    for _ in range(5):
        replay.tick(REAL_TEMP, health="HEALTHY")
    r = replay.rule()
    assert r["starved"] is False, "a real sensor was refused -- the Aurora mistake"
    assert r["liveness"] == "live"


def test_a_FRESH_timestamp_is_not_liveness(replay):
    """The input an age-only check is blind to.

    This test MUST use a current timestamp or it proves nothing -- the
    analogue of the drift guard in c79d1c3 that was written against constant
    data, the one input where its bug was invisible.
    """
    for _ in range(3):
        replay.tick(PLAUSIBLE_FROM_A_BROKEN_COLLECTOR, health="DEGRADED")
    sig = replay.state.snapshot_signals(since=None)["latest"][GPU0]
    assert sig["age_s"] < 5, "the sample IS fresh; that is the whole point"
    assert sig["liveness"] == "suspect"


# ---------------------------------------------------------------------------
# One verdict: the endpoints cannot disagree
# ---------------------------------------------------------------------------


def test_the_endpoints_cannot_disagree(replay):
    for _ in range(5):
        replay.tick(32.0)
    replay.dark(63106.0)
    for _ in range(5):
        replay.tick(WEDGED_ZERO, health="DEGRADED")

    headroom = replay.state.snapshot_headroom()
    detail = replay.state.snapshot_headroom_detail()
    health = replay.state.snapshot_health()
    signals = replay.state.snapshot_signals(since=None)["latest"]

    # Out of the fold, and SAID so rather than silently absent.
    assert RULE0 not in headroom["per_rule"]
    assert RULE0 in headroom["excluded"]
    # Stronger: EVERY kill rule is accounted for in exactly one of the two.
    # Nothing may simply disappear -- that is what the two endpoints were
    # disagreeing through.
    kill_rules = {r["name"] for r in replay.state.snapshot_rules()["effective"]}
    assert kill_rules == set(headroom["per_rule"]) | set(headroom["excluded"])
    assert not (set(headroom["per_rule"]) & set(headroom["excluded"]))
    # Still listed everywhere, marked -- removing it is how a partial failure
    # becomes invisible to a consumer.
    assert detail["per_signal"][GPU0]["liveness"] == "suspect"
    assert signals[GPU0]["liveness"] == "suspect"
    # /health names it, so a reader does not have to diff two endpoints.
    assert GPU0 in [e["signal"] for e in health["signals_not_live"]]
    assert health["rules_starved"] == len(
        [r for r in replay.state.snapshot_rules()["effective"] if r["starved"]])


def test_a_healthy_machine_marks_nothing(replay):
    """The refusing direction for the whole verdict.

    Ticks past min_samples so the rule has a real reading -- a rule still
    filling its first window is legitimately "no_reading", which is a
    different state from anything liveness reports.
    """
    for _ in range(25):
        replay.tick(REAL_TEMP)
    health = replay.state.snapshot_health()
    assert health["signals_not_live"] == []
    assert health["rules_starved"] == 0
    assert replay.state.snapshot_headroom()["excluded"] == {}
    assert replay.state.snapshot_signals(since=None)["latest"][GPU0]["liveness"] == "live"


def test_stale_and_starved_are_ONE_number(replay):
    """`grace_s` is the engine's own starvation threshold, not a second
    constant that can drift from it."""
    assert replay.state.grace_s() == replay.engine.starvation_after_s

    for _ in range(3):
        replay.tick(32.0)
    replay.dark(replay.engine.starvation_after_s + 1.0)
    assert replay.state.liveness(GPU0, now=replay.now) == "stale"
    assert replay.rule()["starved"] is True


def test_a_signal_never_seen_is_never_not_stale(replay):
    assert replay.state.liveness("gpu.9.core_temp_c", now=replay.now) == "never"


# ---------------------------------------------------------------------------
# Against the real captured payload
# ---------------------------------------------------------------------------


def test_the_wedged_capture_would_now_be_marked():
    """Replay the real thing: both shapes, from the file, in one payload."""
    detail = json.loads((WEDGED / "headroom_detail.json").read_text(encoding="utf-8"))
    ps = detail["per_signal"]
    # gpu.1 stopped arriving 4.2 days ago -> stale by age alone.
    assert detail["ts"] - ps["gpu.1.core_temp_c"]["latest_ts"] > 3 * 86400
    # gpu.0 kept arriving, at a value that is now impossible -> suspect, and
    # NOT reachable by any age check.
    assert detail["ts"] - ps[GPU0]["latest_ts"] < 60
    assert not is_credible(_s(ps[GPU0]["latest"]), "DEGRADED")


# ---------------------------------------------------------------------------
# End to end: the LOOP must actually wire the filter to the engine
# ---------------------------------------------------------------------------
#
# The replay above calls split_by_credibility itself, which makes it a good
# test of the gate and NO test of whether run_service uses it. Mutating
# `engine.tick(credible_samples)` back to `engine.tick(samples)` left every
# test above green -- a hole exactly the size of the original bug. This closes
# it by driving the real loop.

LOOP_SIGNAL = "gpu.0.core_temp_c"

LOOP_CONFIG = """
[general]
tick_hz = 50

[[rules]]
name = "gpu-core-hot"
signal = "gpu.*.core_temp_c"
threshold = 88.0
window_s = 30
min_fraction_over = 0.67
action = "log"
"""


class _DegradedButTalkative:
    """Reports DEGRADED while emitting a perfectly ordinary temperature.

    This is the nvml wedge in miniature, and the only collector shape that
    produces it: nvml is the sole collector here that returns partial samples
    while DEGRADED (system and lhmlib both return {}; amd goes FAILED).
    """

    name = "nvml"

    def __init__(self, health: HealthState) -> None:
        self._health = health
        self.samples_taken = 0

    def probe(self):
        return ProbeResult(available=True, reason="fake", signals=(LOOP_SIGNAL,))

    def health(self):
        return self._health

    def sample(self):
        self.samples_taken += 1
        return {LOOP_SIGNAL: _s(PLAUSIBLE_FROM_A_BROKEN_COLLECTOR, source="nvml")}


def _run_loop(tmp_path, collector, ticks):
    cfg = tmp_path / "config.toml"
    cfg.write_text(LOOP_CONFIG, encoding="utf-8")
    captured = {}

    real_state_cls = svc.ServiceState

    class _Capturing(real_state_cls):          # noqa: D401 - test shim
        def attach_engine(self, engine):
            captured["engine"] = engine
            captured["state"] = self
            return super().attach_engine(engine)

    def _fake_probe(audit):
        return [collector], {collector.name: collector.probe()}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(svc, "_probe_all_collectors", _fake_probe)
        mp.setattr(svc, "ServiceState", _Capturing)
        svc.run_service(config_path=cfg, state_dir=tmp_path, max_ticks=ticks)
    return captured


def test_the_LOOP_withholds_suspect_samples_from_the_engine(tmp_path):
    """RED if run_service passes `samples` instead of `credible_samples`."""
    c = _DegradedButTalkative(HealthState.DEGRADED)
    cap = _run_loop(tmp_path, c, ticks=8)

    assert c.samples_taken == 8, "the collector kept publishing throughout"

    # The DIRECT observable: the engine's own window for that rule. Asserting
    # `starved` here would be wrong -- starvation needs 10 s of silence and
    # this loop runs for 0.16 s, so it reads False either way and the test
    # would pass whether or not the filter is wired.
    er = next(r for r in cap["engine"].effective_rules if r.signal == LOOP_SIGNAL)
    assert len(er.window) == 0, (
        "the engine consumed samples from a DEGRADED collector -- the loop is "
        "not using split_by_credibility")
    rule = next(r for r in cap["state"].snapshot_rules()["effective"]
                if r["signal"] == LOOP_SIGNAL)
    assert rule["liveness"] == "suspect"
    # ...and the reading is still visible, marked, not deleted.
    latest = cap["state"].snapshot_signals(since=None)["latest"][LOOP_SIGNAL]
    assert latest["value"] == PLAUSIBLE_FROM_A_BROKEN_COLLECTOR
    assert latest["liveness"] == "suspect"


def test_the_LOOP_passes_healthy_samples_through(tmp_path):
    """The refusing direction: a healthy collector must not be muted."""
    c = _DegradedButTalkative(HealthState.HEALTHY)
    cap = _run_loop(tmp_path, c, ticks=8)
    er = next(r for r in cap["engine"].effective_rules if r.signal == LOOP_SIGNAL)
    assert len(er.window) == 8, "a healthy collector was muted"
    rule = next(r for r in cap["state"].snapshot_rules()["effective"]
                if r["signal"] == LOOP_SIGNAL)
    assert rule["starved"] is False
    assert rule["liveness"] == "live"


def test_the_forensic_stream_keeps_the_suspect_reading(tmp_path):
    """A suspect sample is EVIDENCE. The stream must not quietly drop it.

    "What was the sensor saying while the guard was off" is the question the
    forensic file exists to answer, and on 2026-09-02 the answer -- a constant
    0.0 across 2393 samples -- is the only reason the failure could be
    characterised at all.
    """
    from atfield.forensics import FORENSICS_FILENAME, ForensicBuffer

    buf = ForensicBuffer(tmp_path, flush_interval_s=60.0)
    buf.start()
    buf.record({"a": _s(REAL_TEMP), "b": _s(PLAUSIBLE_FROM_A_BROKEN_COLLECTOR)},
               ts=1.0, suspect={"b"})
    buf.record({"a": _s(REAL_TEMP)}, ts=2.0, suspect=set())
    buf.stop()          # drains pending

    lines = [json.loads(l) for l in
             (tmp_path / FORENSICS_FILENAME).read_text(encoding="utf-8").splitlines() if l]
    assert lines[0]["samples"]["b"] == PLAUSIBLE_FROM_A_BROKEN_COLLECTOR
    assert lines[0]["suspect"] == ["b"]
    # An ordinary tick's line is unchanged -- no key, no churn in the stream.
    assert "suspect" not in lines[1]
