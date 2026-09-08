"""A tick that overruns its period must say WHICH PHASE ate the time.

2026-09-03: a per-tick RSS cap I had added as a *safety feature* walked 402
processes including cmdline -- median 0.70 s, worst 4.32 s, against a 1.0 s
budget. The service tick degraded to 0.22 Hz. Every rule computes
`min_samples` from the CONFIGURED rate, so a 30 s window that should have held
21 samples held about 7, and all seven rules returned `INSUFFICIENT` forever --
which is also what a freshly-started rule returns. `/health` went on reporting
`mode: armed, rules_active: 7, rules_starved: 0`.

Chronos then ran **719 of 766 samples in one hour at or above 90 C, peaking at
95.62 C**. A safety feature disarmed every other safety feature, and nothing
said so.

Two things were added afterwards: `min_samples` now adapts to the observed
cadence, and `rules_unable_to_fire` reports a rule that is mathematically
incapable of firing. Both describe the SYMPTOM. Neither says what is consuming
the tick, and as of 2026-09-08 Chronos still runs at **0.36 Hz against a
configured 1 Hz** with 18 `RULE CANNOT FIRE` events logged -- while
`bench_tick.py` accounts for **0.18 ms** of a 2,770 ms tick. Everything anyone
has profiled is 0.006% of the time. Nobody knows where the rest goes.

This file pins the diagnostic that answers that: when a tick overruns, the log
names the slowest phase and its cost. Pure observation -- no behaviour change,
no new decision, nothing that can itself starve the loop.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import atfield.service as svc  # noqa: E402
from atfield.collectors import HealthState, ProbeResult  # noqa: E402
from atfield.signals import Sample, monotonic_ns  # noqa: E402

SIGNAL = "system.cpu_package_temp_c"

CONFIG = """
[general]
tick_hz = 20

[[rules]]
name = "cpu-pkg-hot"
signal = "system.cpu_package_temp_c"
threshold = 90.0
window_s = 30
min_fraction_over = 0.67
action = "log"
"""


class _SlowCollector:
    """Healthy, correct, and deliberately expensive -- the 2026-09-03 shape.

    Nothing about this collector is broken. It returns good samples every
    tick. It simply takes longer than the tick budget, which is exactly why
    the original failure was invisible: every downstream check saw healthy
    data.
    """

    name = "system"

    def __init__(self, cost_s: float) -> None:
        self._cost_s = cost_s
        self.samples_taken = 0

    def probe(self):
        return ProbeResult(available=True, reason="fake", signals=(SIGNAL,))

    def health(self):
        return HealthState.HEALTHY

    def sample(self):
        time.sleep(self._cost_s)
        self.samples_taken += 1
        return {SIGNAL: Sample(55.0, monotonic_ns(), "system", "celsius")}


def _run(tmp_path, collector, ticks):
    cfg = tmp_path / "config.toml"
    cfg.write_text(CONFIG, encoding="utf-8")

    def _fake_probe(audit):
        return [collector], {collector.name: collector.probe()}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(svc, "_probe_all_collectors", _fake_probe)
        svc.run_service(config_path=cfg, state_dir=tmp_path, max_ticks=ticks)


def test_an_overrunning_tick_names_the_phase_that_ate_it(tmp_path, caplog):
    """RED before this change: the loop overran silently.

    tick_hz=20 gives a 50 ms budget; the collector costs 300 ms. The warning
    must name `collect` -- the phase actually responsible -- not merely report
    that the tick was slow.
    """
    caplog.set_level("WARNING", logger="atfield.service")
    _run(tmp_path, _SlowCollector(0.30), ticks=4)

    slow = [r.getMessage() for r in caplog.records if "SLOW TICK" in r.getMessage()]
    assert slow, "a 300 ms tick against a 50 ms budget produced no warning"
    assert "collect" in slow[0], f"the slowest phase was not named: {slow[0]!r}"


def test_the_warning_carries_numbers_not_just_a_label(tmp_path, caplog):
    """A name without a cost cannot be triaged: the operator needs to know
    whether one phase dominates or the time is spread."""
    caplog.set_level("WARNING", logger="atfield.service")
    _run(tmp_path, _SlowCollector(0.30), ticks=4)

    msg = next(r.getMessage() for r in caplog.records if "SLOW TICK" in r.getMessage())
    assert "ms" in msg
    # The measured cost of the guilty phase must appear, to within slop.
    import re
    nums = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*ms", msg)]
    assert any(250 <= n <= 600 for n in nums), f"no plausible cost in {msg!r}"


def test_a_healthy_tick_is_SILENT(tmp_path, caplog):
    """The refusing direction, and the one that keeps this usable.

    A diagnostic that fires every tick is noise, and noise is why the last
    signal went unread for four days. tick_hz=20, collector costs ~0 --
    nothing may be logged.
    """
    caplog.set_level("WARNING", logger="atfield.service")
    _run(tmp_path, _SlowCollector(0.0), ticks=6)
    assert not [r for r in caplog.records if "SLOW TICK" in r.getMessage()]


def test_the_instrumentation_itself_is_not_a_cost(tmp_path):
    """Measuring the loop must not slow the loop -- the whole incident was a
    well-meant addition to this function.

    Compares 12 ticks of a zero-cost collector against the wall time that many
    ticks should take, allowing generous slop for CI. If timing ever grows
    something expensive (formatting a message every tick, say), this catches
    the class if not every instance.
    """
    c = _SlowCollector(0.0)
    started = time.monotonic()
    _run(tmp_path, c, ticks=12)
    elapsed = time.monotonic() - started
    assert c.samples_taken == 12
    # 12 ticks at 20 Hz = 0.6 s of intended sleeping. Anything near a second
    # of overhead on top means the timing is not free.
    assert elapsed < 3.0, f"12 near-empty ticks took {elapsed:.2f}s"
