"""AT-Field signal primitives: Sample, SlidingWindow, EMA, N-of-M evaluator.

This module is the substrate that every collector and the policy engine sit
on top of. It is deliberately pure (no I/O, no clocks except injectable
ones, no logging) so the policy layer is fully unit-testable with synthetic
sample streams.

Three core ideas, in priority order:

1. **A signal value is never just a float.** It is a :class:`Sample` carrying
   ``(value, taken_at_ns, source_id, unit)``. This is what makes
   stale-data detection, source attribution, and post-action verification
   possible without bolt-ons later.

2. **Stale samples must look like missing data, not "below threshold".**
   :meth:`SlidingWindow.evict_older_than` drops anything older than its time
   window, and the evaluator below treats a window with too few fresh
   samples as "insufficient data" rather than silently passing. A frozen
   collector therefore makes its rule abstain — never fail open.

3. **Rule evaluation is an N-of-M problem, not an instantaneous compare.**
   :func:`fraction_over_threshold` and :func:`evaluate_window` are pure
   functions of a sample list; the policy engine wires them to live windows
   and cooldowns. A 2-second warmup spike never triggers; a sustained
   problem triggers within ``window_s`` seconds.

All public functions are deterministic and clock-injectable for testing.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Final, Iterable

__all__ = [
    "Sample",
    "SlidingWindow",
    "EMA",
    "Verdict",
    "EvalResult",
    "MIN_SAMPLES_FOR_DECISION",
    "fraction_over_threshold",
    "evaluate_window",
    "monotonic_ns",
]


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------


def monotonic_ns() -> int:
    """Monotonic nanosecond clock; thin wrapper so tests can monkeypatch."""
    return time.monotonic_ns()


_NS_PER_S: Final = 1_000_000_000


# ---------------------------------------------------------------------------
# Sample
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Sample:
    """A single timestamped, source-attributed sensor reading.

    Attributes
    ----------
    value :
        The numeric reading. Units are carried in :attr:`unit` rather than
        encoded in the value (no "85 means 85%, but 85.0 means 85°C" games).
    taken_at_ns :
        Monotonic nanosecond timestamp at the moment the collector read the
        sensor — *not* when the sample was enqueued. Use :func:`monotonic_ns`.
    source_id :
        Stable identifier for the collector that produced this sample
        (e.g. ``"nvml"``, ``"lhm.http"``, ``"psutil"``). Used for audit
        logs, sensor-disagreement detection, and per-source health tracking.
    unit :
        One of ``"celsius"``, ``"percent"``, ``"bytes"``, ``"watts"``, or
        ``"count"``. The policy engine validates that a rule's threshold
        unit matches its signal's unit at config-load time.
    """

    value: float
    taken_at_ns: int
    source_id: str
    unit: str

    def is_stale(self, *, now_ns: int, max_age_ns: int) -> bool:
        return (now_ns - self.taken_at_ns) > max_age_ns


# ---------------------------------------------------------------------------
# Sliding window
# ---------------------------------------------------------------------------


class SlidingWindow:
    """Time-bounded ordered buffer of :class:`Sample` for one signal.

    The window holds samples whose ``taken_at_ns`` is within the last
    ``window_s`` seconds (relative to the most-recent ``add()`` or an
    explicit ``evict_older_than()`` call). It is intentionally not
    thread-safe — the service holds the policy lock around evaluation.

    A small extra slack (``slack_s``, default 0.5 s) is kept past the window
    edge so a sample that arrives a tick late after a brief collector stall
    isn't immediately dropped from a 1-second-resolution window.
    """

    __slots__ = ("_window_ns", "_slack_ns", "_buf", "_last_evict_ns")

    def __init__(self, window_s: float, *, slack_s: float = 0.5) -> None:
        if window_s <= 0:
            raise ValueError(f"window_s must be positive, got {window_s}")
        if slack_s < 0:
            raise ValueError(f"slack_s must be non-negative, got {slack_s}")
        self._window_ns = int(window_s * _NS_PER_S)
        self._slack_ns = int(slack_s * _NS_PER_S)
        self._buf: deque[Sample] = deque()
        self._last_evict_ns = 0

    @property
    def window_ns(self) -> int:
        return self._window_ns

    def __len__(self) -> int:
        return len(self._buf)

    def add(self, sample: Sample) -> None:
        """Append ``sample`` and evict anything older than the window."""
        self._buf.append(sample)
        self._evict(reference_ns=sample.taken_at_ns)

    def evict_older_than(self, *, now_ns: int) -> None:
        """Drop samples older than ``now_ns - window_ns - slack_ns``.

        Useful when the collector goes quiet: calling this on the policy
        tick keeps a stalled signal's window from holding stale "looks
        fine" values forever.
        """
        self._evict(reference_ns=now_ns)

    def _evict(self, *, reference_ns: int) -> None:
        cutoff = reference_ns - self._window_ns - self._slack_ns
        buf = self._buf
        while buf and buf[0].taken_at_ns < cutoff:
            buf.popleft()
        self._last_evict_ns = reference_ns

    def samples(self) -> tuple[Sample, ...]:
        """Snapshot of current contents, oldest-first."""
        return tuple(self._buf)

    def latest(self) -> Sample | None:
        return self._buf[-1] if self._buf else None

    def clear(self) -> None:
        self._buf.clear()


# ---------------------------------------------------------------------------
# Exponential moving average
# ---------------------------------------------------------------------------


class EMA:
    """Stateless-input exponential moving average.

    ``alpha`` is the weight of the new sample; smaller alpha = smoother.
    The first sample bootstraps the average (no startup transient at zero).
    Used by collectors to dampen jittery sensors (LHM in particular) before
    the value reaches the sliding window.
    """

    __slots__ = ("_alpha", "_value")

    def __init__(self, alpha: float) -> None:
        if not 0.0 < alpha <= 1.0:
            raise ValueError(f"alpha must be in (0, 1], got {alpha}")
        self._alpha = alpha
        self._value: float | None = None

    def update(self, x: float) -> float:
        if self._value is None:
            self._value = x
        else:
            self._value = self._alpha * x + (1.0 - self._alpha) * self._value
        return self._value

    @property
    def value(self) -> float | None:
        return self._value

    def reset(self) -> None:
        self._value = None


# ---------------------------------------------------------------------------
# Window evaluation
# ---------------------------------------------------------------------------


class Verdict(Enum):
    """Outcome of evaluating a sliding window against a rule."""

    TRIGGER = "trigger"        #: fraction-over-threshold met or exceeded
    BELOW = "below"            #: enough fresh data, threshold not met
    INSUFFICIENT = "insufficient"  #: not enough fresh samples to decide

    @property
    def fires(self) -> bool:
        return self is Verdict.TRIGGER


@dataclass(frozen=True, slots=True)
class EvalResult:
    verdict: Verdict
    fraction_over: float        # 0.0 if INSUFFICIENT
    samples_considered: int
    samples_required: int
    latest_value: float | None  # None if window empty


# Minimum number of fresh samples required before the evaluator is willing
# to fire. Below this we report INSUFFICIENT. Picked empirically: at the
# default 1 Hz tick this means a rule can't trigger from a single sample,
# which is the whole point of sustained-window logic.
MIN_SAMPLES_FOR_DECISION: Final = 3


def fraction_over_threshold(
    samples: Iterable[Sample],
    threshold: float,
    *,
    comparator: Callable[[float, float], bool] = lambda v, t: v > t,
) -> float:
    """Return the fraction of ``samples`` whose value satisfies the comparator vs threshold.

    Returns 0.0 for an empty input. Default comparator is strict-greater-than
    because every shipped rule (temps, percentages, util) is an upper bound;
    rules wanting lower-bound semantics (e.g. "free VRAM < X") can pass
    ``comparator=lambda v, t: v < t``.
    """
    total = 0
    over = 0
    for s in samples:
        total += 1
        if comparator(s.value, threshold):
            over += 1
    if total == 0:
        return 0.0
    return over / total


def evaluate_window(
    window: SlidingWindow,
    *,
    threshold: float,
    min_fraction_over: float,
    now_ns: int,
    max_sample_age_s: float,
    comparator: Callable[[float, float], bool] = lambda v, t: v > t,
) -> EvalResult:
    """Evaluate a sliding window against a rule's threshold + fraction.

    Parameters
    ----------
    window :
        The :class:`SlidingWindow` for the signal under test. Will be
        compacted to drop samples older than the window plus its slack.
    threshold, min_fraction_over :
        From the matching :class:`atfield.config.RuleConfig`. The rule
        triggers when at least ``min_fraction_over`` of fresh samples
        satisfy ``comparator(value, threshold)``.
    now_ns :
        Current monotonic time. Injected (rather than read here) so policy
        ticks are deterministic and unit-testable.
    max_sample_age_s :
        How old a sample can be and still count as "fresh". Typically
        ``2 / tick_hz`` — a sample older than two ticks means the collector
        is silent, and counting stale data toward "below threshold" would
        let the rule fail open.

    Returns
    -------
    EvalResult
        ``verdict == INSUFFICIENT`` if fewer than :data:`MIN_SAMPLES_FOR_DECISION`
        fresh samples are present. Callers should treat INSUFFICIENT as
        "abstain, do not act" — the collector layer is responsible for
        surfacing why data is missing (via :class:`atfield.collectors.HealthState`).
    """
    if not 0.0 < min_fraction_over <= 1.0:
        raise ValueError(f"min_fraction_over must be in (0, 1], got {min_fraction_over}")
    if max_sample_age_s <= 0:
        raise ValueError(f"max_sample_age_s must be positive, got {max_sample_age_s}")

    window.evict_older_than(now_ns=now_ns)
    max_age_ns = int(max_sample_age_s * _NS_PER_S)
    fresh = tuple(s for s in window.samples() if not s.is_stale(now_ns=now_ns, max_age_ns=max_age_ns))

    latest = fresh[-1].value if fresh else (window.latest().value if window.latest() else None)

    if len(fresh) < MIN_SAMPLES_FOR_DECISION:
        return EvalResult(
            verdict=Verdict.INSUFFICIENT,
            fraction_over=0.0,
            samples_considered=len(fresh),
            samples_required=MIN_SAMPLES_FOR_DECISION,
            latest_value=latest,
        )

    frac = fraction_over_threshold(fresh, threshold, comparator=comparator)
    verdict = Verdict.TRIGGER if frac >= min_fraction_over else Verdict.BELOW
    return EvalResult(
        verdict=verdict,
        fraction_over=frac,
        samples_considered=len(fresh),
        samples_required=MIN_SAMPLES_FOR_DECISION,
        latest_value=latest,
    )
