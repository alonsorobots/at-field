"""AT-Field policy engine: rule expansion, signal-map negotiation, evaluation.

The :class:`PolicyEngine` is the bridge between collectors (which produce
:class:`atfield.signals.Sample` objects) and the actuator (which consumes
:class:`Action` objects). It owns one :class:`SlidingWindow` per concrete
rule, tracks per-rule cooldowns, and converts the config's globbed rules
into one effective rule per concrete signal.

Lifecycle
---------
1. ``PolicyEngine(cfg, available_signals=...)`` -- expands ``gpu.*.X``
   rules against the working signal map. Rules whose signal is not
   available become :class:`DisabledRule` entries (with a reason); rules
   whose signal *is* available become :class:`EffectiveRule` entries with
   their own sliding window.
2. ``engine.tick(samples, now_ns=...)`` -- each service tick, feed the
   collector output and any rules that triggered come back as a list of
   :class:`Action` objects.
3. ``engine.set_paused(until_ns)`` -- pause-style sentinel; while paused,
   ``tick()`` still updates windows but never emits actions. Backs the
   ``atf pause`` CLI command.

The engine is intentionally pure-Python and clock-injected -- no time.* or
threading inside. The service layer wires it to a real wall clock and
passes the collector snapshots in.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from atfield.config import AtFieldConfig, RuleConfig
from atfield.signals import (
    MIN_SAMPLES_FOR_DECISION,
    EvalResult,
    Sample,
    SlidingWindow,
    evaluate_window,
)

__all__ = [
    "Action",
    "DisabledRule",
    "EffectiveRule",
    "PolicyEngine",
    "SignalHealthChange",
    "expand_rules",
]


_log = logging.getLogger("atfield.policy")

# How long a rule's signal may go missing before we call it starved.
#
# Sized to be un-flappy rather than fast: a single dropped tick, a collector
# hiccup, or an LHM HTTP timeout must not raise an alarm, but a dead sensor
# helper must not stay quiet either. Ten seconds at 1 Hz is ~10 missed samples
# -- far past coincidence, and still a third of the shortest thermal window
# any shipped rule uses.
_DEFAULT_STARVATION_AFTER_S: Final = 10.0

_NS_PER_S: Final = 1_000_000_000

# Smoothing for the OBSERVED tick interval. Low enough that one slow tick does
# not move the requirement much, high enough to follow a real regime change
# (a new per-tick collector, a costly scan) within a handful of ticks.
_CADENCE_EWMA_ALPHA: Final = 0.2

# A gap longer than this is a pause/resume or a clock jump, not a tick rate.
_MAX_PLAUSIBLE_TICK_GAP_NS: Final = 60 * _NS_PER_S


# ---------------------------------------------------------------------------
# Action emitted by the engine
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Action:
    """Decision the policy engine emits when a rule triggers.

    The actuator consumes these. Audit log writes one JSONL entry per
    action regardless of kind.
    """

    kind: str  # "log" | "throttle" | "kill"
    rule_name: str            # effective name, e.g. "gpu-core-hot[gpu.0]"
    base_rule_name: str       # name as written in config.toml
    signal: str
    threshold: float
    fraction_over: float
    samples_considered: int
    latest_value: float
    triggered_at_ns: int
    cooldown_seconds: int     # how long this rule will sleep after this action


# ---------------------------------------------------------------------------
# Effective / disabled rules (post signal-map negotiation)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EffectiveRule:
    """One concrete rule for one concrete signal, with its own window."""

    name: str               # synthesized: "{base}[{signal}]" if expanded, else base
    base_rule: RuleConfig
    signal: str             # concrete (no "*"), guaranteed to be in the working map
    window: SlidingWindow
    min_samples: int        # ceil(window_s * tick_hz * min_fraction_over)
    cooldown_until_ns: int = 0  # zero means "ready to fire"
    # Starvation tracking. ``None`` means "no sample has ever arrived for this
    # signal", which is distinct from "it stopped arriving": the former usually
    # means a collector advertised a signal it cannot actually produce, the
    # latter means a sensor source died mid-run. Deliberately None rather than
    # 0 -- a 0 sentinel is ambiguous with a genuine now_ns of 0.
    last_sample_at_ns: int | None = None
    starved: bool = False


@dataclass(frozen=True, slots=True)
class SignalHealthChange:
    """A rule's input signal started or stopped arriving.

    Why this type exists: a rule whose signal goes missing evaluates to
    ``INSUFFICIENT`` forever, which is indistinguishable -- from the outside --
    from a rule that is simply healthy and below threshold. That is a fail-open
    in a tool whose entire job is to intervene before hardware is damaged, so
    signal loss has to be an *event*, not the absence of one.

    The engine only records these; the service layer logs them, writes them to
    ``events.jsonl`` and mirrors them onto ``/health``. Keeping the engine
    side-effect-free preserves its clock-injected, pure-Python contract.
    """

    rule_name: str
    base_rule_name: str
    signal: str
    action: str             # what the rule would have done: "log"|"throttle"|"kill"
    state: str              # "starved" | "recovered"
    silent_for_s: float     # how long the signal was missing
    ever_seen: bool         # False => the signal never arrived at all this run
    at_ns: int


@dataclass(frozen=True, slots=True)
class DisabledRule:
    """A rule the engine refused to enable, with a human-readable reason.

    Logged at startup via ``PolicyEngine.disabled_rules`` and surfaced to
    the operator through ``atf status``. This is the visible side of
    capability negotiation: the operator should never have to wonder why
    a rule "didn't fire" -- if it was disabled, the engine says so.
    """

    base_rule_name: str
    signal: str
    reason: str


# ---------------------------------------------------------------------------
# Glob expansion
# ---------------------------------------------------------------------------


def expand_rules(
    rules: Iterable[RuleConfig],
    available_signals: set[str],
    *,
    tick_hz: int = 1,
) -> tuple[list[EffectiveRule], list[DisabledRule]]:
    """Expand ``gpu.*.X``-style rules against the working signal map.

    Returns ``(effective, disabled)``. Each globbed rule produces one
    effective rule per matching available signal. A rule whose signal is
    a literal string is enabled iff that string is in the map.

    Glob semantics
    --------------
    Only the second segment may be a ``*`` for now (e.g. ``gpu.*.core_temp_c``,
    ``cpu.*.temp_c``). This matches the patterns the config validator
    accepts in :mod:`atfield.config`.
    """
    effective: list[EffectiveRule] = []
    disabled: list[DisabledRule] = []

    def _min_samples_for(rule: RuleConfig) -> int:
        # "67% of the last 30s at 1Hz" -> need at least 20 over-threshold
        # samples to even be eligible; fewer means the window hasn't been
        # alive long enough to constitute a sustained event.
        return max(1, math.ceil(rule.window_s * tick_hz * rule.min_fraction_over))

    for r in rules:
        if "*" in r.signal:
            # gpu.*.core_temp_c -> ^gpu\.[^.]+\.core_temp_c$
            prefix, _, suffix = r.signal.partition("*")
            matches = sorted(
                s for s in available_signals
                if s.startswith(prefix) and s.endswith(suffix)
                and "." not in s[len(prefix):len(s) - len(suffix)]
            )
            if not matches:
                disabled.append(
                    DisabledRule(
                        base_rule_name=r.name,
                        signal=r.signal,
                        reason=f"no available signals matched glob {r.signal!r}",
                    )
                )
                continue
            for concrete in matches:
                effective.append(
                    EffectiveRule(
                        name=f"{r.name}[{concrete}]",
                        base_rule=r,
                        signal=concrete,
                        window=SlidingWindow(window_s=r.window_s),
                        min_samples=_min_samples_for(r),
                    )
                )
        else:
            if r.signal not in available_signals:
                disabled.append(
                    DisabledRule(
                        base_rule_name=r.name,
                        signal=r.signal,
                        reason=f"signal {r.signal!r} not provided by any probed collector",
                    )
                )
                continue
            effective.append(
                EffectiveRule(
                    name=r.name,
                    base_rule=r,
                    signal=r.signal,
                    window=SlidingWindow(window_s=r.window_s),
                    min_samples=_min_samples_for(r),
                )
            )

    return effective, disabled


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


@dataclass
class _Stats:
    """Internal per-rule counters, for ``atf status`` and audit context."""

    triggers: int = 0
    last_eval_verdict: str = "INSUFFICIENT"
    last_eval_fraction: float = 0.0
    last_eval_value: float | None = None
    last_eval_at_ns: int = 0


class PolicyEngine:
    """Owns rule state, evaluates samples, emits :class:`Action` objects."""

    def __init__(
        self,
        cfg: AtFieldConfig,
        *,
        available_signals: set[str],
        starvation_after_s: float = _DEFAULT_STARVATION_AFTER_S,
    ) -> None:
        self._cfg = cfg
        self._effective, self._disabled = expand_rules(
            cfg.rules, available_signals, tick_hz=cfg.general.tick_hz,
        )
        self._stats: dict[str, _Stats] = {r.name: _Stats() for r in self._effective}
        self._paused_until_ns: int = 0
        self._max_sample_age_s = max(2.0 / max(cfg.general.tick_hz, 1), 2.0)
        self._starvation_after_s = max(
            starvation_after_s, 5.0 / max(cfg.general.tick_hz, 1)
        )
        # Reference point for rules whose signal never arrives at all -- without
        # it, "never seen" would have no clock to be measured against and such a
        # rule would stay silently dead for the life of the process.
        self._first_tick_ns: int | None = None
        self._signal_health_changes: list[SignalHealthChange] = []
        # Observed cadence. min_samples and max_sample_age were both sized from
        # the CONFIGURED tick_hz; when the loop actually runs slower, a window
        # can no longer hold the required count and every rule abstains forever
        # while /health still reports "armed". Measured 2026-08-30: the tick
        # fell to 0.22 Hz, cpu-pkg-hot needed 21 samples in a 30s window that
        # held 7, and a CPU sat >=90 C for an hour with the rule INSUFFICIENT
        # and zero triggers. So both limits are re-derived from the rate the
        # engine is ACTUALLY being ticked at: "67% of the last 30s" keeps its
        # meaning at any cadence, and a slow loop costs RESOLUTION instead of
        # silently disarming the guard.
        self._last_tick_ns: int | None = None
        self._tick_interval_ns: float | None = None
        self._unable_rules: set[str] = set()

        # One-shot startup logging so the operator can see the negotiated state.
        for r in self._effective:
            _log.info(
                "rule enabled: %s -> %s (threshold=%g window=%ds frac>=%g action=%s)",
                r.name, r.signal, r.base_rule.threshold,
                r.base_rule.window_s, r.base_rule.min_fraction_over, r.base_rule.action,
            )
        for d in self._disabled:
            _log.warning("rule DISABLED: %s -- %s", d.base_rule_name, d.reason)

    # -- Read-only views ---------------------------------------------------

    @property
    def effective_rules(self) -> tuple[EffectiveRule, ...]:
        return tuple(self._effective)

    @property
    def disabled_rules(self) -> tuple[DisabledRule, ...]:
        return tuple(self._disabled)

    @property
    def is_paused(self) -> bool:
        return self._paused_until_ns > 0

    def stats_snapshot(self) -> dict[str, dict[str, object]]:
        """Read-only stats for ``atf status`` -- safe to call any time."""
        out: dict[str, dict[str, object]] = {}
        for name, s in self._stats.items():
            out[name] = {
                "triggers": s.triggers,
                "last_verdict": s.last_eval_verdict,
                "last_fraction": s.last_eval_fraction,
                "last_value": s.last_eval_value,
                "last_eval_at_ns": s.last_eval_at_ns,
            }
        return out

    # -- Pause -------------------------------------------------------------

    def set_paused(self, until_ns: int) -> None:
        """Pause action emission until ``until_ns``. ``0`` clears the pause."""
        self._paused_until_ns = max(0, until_ns)

    def is_currently_paused(self, *, now_ns: int) -> bool:
        return now_ns < self._paused_until_ns

    # -- Tick --------------------------------------------------------------

    @property
    def observed_tick_hz(self) -> float | None:
        """Measured tick rate, or None until two ticks have been seen."""
        if not self._tick_interval_ns:
            return None
        return _NS_PER_S / self._tick_interval_ns

    def _note_tick_cadence(self, now_ns: int) -> None:
        last, self._last_tick_ns = self._last_tick_ns, now_ns
        if last is None:
            return
        delta = now_ns - last
        if delta <= 0 or delta > _MAX_PLAUSIBLE_TICK_GAP_NS:
            return          # clock jump, or a pause -- not a cadence reading
        self._tick_interval_ns = (
            float(delta) if self._tick_interval_ns is None
            else (1.0 - _CADENCE_EWMA_ALPHA) * self._tick_interval_ns
            + _CADENCE_EWMA_ALPHA * delta
        )

    def min_samples_for(self, rule: EffectiveRule) -> int:
        """How many samples this rule needs, at the cadence actually observed.

        Falls back to the configured value until a cadence is known, so startup
        behaviour is unchanged. Never drops below the absolute floor: two
        samples can agree by accident, and a guard that fires on one reading is
        a guard that kills jobs on a boost spike.
        """
        hz = self.observed_tick_hz
        if hz is None:
            return rule.min_samples
        need = math.ceil(
            rule.base_rule.window_s * hz * rule.base_rule.min_fraction_over
        )
        return max(MIN_SAMPLES_FOR_DECISION, need)

    def max_sample_age_for_tick(self) -> float:
        """Liveness bound, widened to the observed cadence.

        The same regression bites twice: at 0.21 Hz the newest sample is
        routinely older than the configured 2 s bound, so evaluate_window would
        call a perfectly healthy collector "silent" and abstain even after
        min_samples was fixed.
        """
        hz = self.observed_tick_hz
        if hz is None:
            return self._max_sample_age_s
        return max(2.5 / hz, self._max_sample_age_s)

    def _check_rule_can_ever_fire(self, rule: EffectiveRule, need: int) -> None:
        """Say so, loudly, when a rule is mathematically incapable of firing.

        The failure this exists for is silent by construction: the rule reports
        INSUFFICIENT, which is also what a freshly-started rule reports, and the
        service goes on advertising itself as armed. An operator's only clue was
        a tile that looked calm.
        """
        hz = self.observed_tick_hz
        if hz is None:
            return
        capacity = rule.base_rule.window_s * hz
        unable = capacity < need
        was_unable = rule.name in self._unable_rules
        if unable and not was_unable:
            self._unable_rules.add(rule.name)
            _log.error(
                "RULE CANNOT FIRE: %s needs %d samples but its %ds window holds "
                "only ~%.1f at the observed %.2f Hz tick (configured %d Hz). This "
                "rule is NOT guarding -- action=%s would never run. Find what "
                "slowed the service loop.",
                rule.name, need, rule.base_rule.window_s, capacity, hz,
                self._cfg.general.tick_hz, rule.base_rule.action,
            )
        elif was_unable and not unable:
            self._unable_rules.discard(rule.name)
            _log.warning(
                "rule %s can fire again (%ds window holds ~%.1f samples at "
                "%.2f Hz, needs %d)",
                rule.name, rule.base_rule.window_s, capacity, hz, need,
            )

    @property
    def rules_unable_to_fire(self) -> tuple[str, ...]:
        """Rules that cannot render a verdict at the current cadence."""
        return tuple(sorted(self._unable_rules))

    def tick(
        self,
        samples: dict[str, Sample],
        *,
        now_ns: int,
    ) -> list[Action]:
        """Feed one tick's worth of samples; return triggered actions.

        Updates every rule's window with the matching sample (if present),
        evaluates each rule, applies cooldown gating, and returns whichever
        rules fired. Stale-sample logic is handled inside
        :func:`evaluate_window` -- a rule whose signal stops arriving will
        report ``INSUFFICIENT`` rather than ``BELOW``.

        Signal starvation is also detected here and queued for the service layer
        to drain via :meth:`drain_signal_health_changes`. ``INSUFFICIENT`` alone
        is not enough: it is the same verdict a freshly-started rule reports, so
        it cannot be alarmed on directly without crying wolf every startup.
        """
        actions: list[Action] = []
        paused = self.is_currently_paused(now_ns=now_ns)
        if self._first_tick_ns is None:
            self._first_tick_ns = now_ns
        self._note_tick_cadence(now_ns)
        max_sample_age_s = self.max_sample_age_for_tick()

        for rule in self._effective:
            sample = samples.get(rule.signal)
            if sample is not None:
                rule.window.add(sample)
            # Owns rule.last_sample_at_ns: the recovery event needs the gap
            # measured against the PREVIOUS sample, so the stamp must be
            # advanced after that arithmetic, not before it.
            self._track_starvation(rule, got_sample=sample is not None, now_ns=now_ns)

            need = self.min_samples_for(rule)
            self._check_rule_can_ever_fire(rule, need)
            result: EvalResult = evaluate_window(
                rule.window,
                threshold=rule.base_rule.threshold,
                min_fraction_over=rule.base_rule.min_fraction_over,
                now_ns=now_ns,
                max_sample_age_s=max_sample_age_s,
                min_samples=need,
            )

            stats = self._stats[rule.name]
            stats.last_eval_verdict = result.verdict.name
            stats.last_eval_fraction = result.fraction_over
            stats.last_eval_value = result.latest_value
            stats.last_eval_at_ns = now_ns

            if not result.verdict.fires:
                continue
            if now_ns < rule.cooldown_until_ns:
                continue
            if paused:
                _log.info("rule %s would fire but engine is paused", rule.name)
                continue

            cooldown_s = self._cfg.cooldown_for(rule.base_rule)
            rule.cooldown_until_ns = now_ns + cooldown_s * 1_000_000_000
            stats.triggers += 1

            actions.append(
                Action(
                    kind=rule.base_rule.action,
                    rule_name=rule.name,
                    base_rule_name=rule.base_rule.name,
                    signal=rule.signal,
                    threshold=rule.base_rule.threshold,
                    fraction_over=result.fraction_over,
                    samples_considered=result.samples_considered,
                    latest_value=result.latest_value if result.latest_value is not None else float("nan"),
                    triggered_at_ns=now_ns,
                    cooldown_seconds=cooldown_s,
                )
            )

        return actions

    # -- Signal starvation -------------------------------------------------

    def _track_starvation(
        self, rule: EffectiveRule, *, got_sample: bool, now_ns: int
    ) -> None:
        """Flip ``rule.starved`` on transitions and queue a change record.

        Edge-triggered on purpose: one event when a signal goes dark and one
        when it comes back, never a per-tick stream. A watchdog that spams its
        own audit log at 1 Hz is a watchdog whose audit log stops being read.
        """
        ever_seen = rule.last_sample_at_ns is not None
        # tick() sets _first_tick_ns before calling us, so the fallback is never
        # None here; `or now_ns` keeps that provable without an assert.
        reference_ns = rule.last_sample_at_ns
        if reference_ns is None:
            reference_ns = self._first_tick_ns if self._first_tick_ns is not None else now_ns
        silent_for_s = max(0.0, (now_ns - reference_ns) / 1_000_000_000)

        if got_sample:
            was_starved = rule.starved
            rule.starved = False
            rule.last_sample_at_ns = now_ns
            if was_starved:
                self._signal_health_changes.append(
                    SignalHealthChange(
                        rule_name=rule.name,
                        base_rule_name=rule.base_rule.name,
                        signal=rule.signal,
                        action=rule.base_rule.action,
                        state="recovered",
                        silent_for_s=silent_for_s,
                        ever_seen=True,
                        at_ns=now_ns,
                    )
                )
            return

        if not rule.starved and silent_for_s >= self._starvation_after_s:
            rule.starved = True
            self._signal_health_changes.append(
                SignalHealthChange(
                    rule_name=rule.name,
                    base_rule_name=rule.base_rule.name,
                    signal=rule.signal,
                    action=rule.base_rule.action,
                    state="starved",
                    silent_for_s=silent_for_s,
                    ever_seen=ever_seen,
                    at_ns=now_ns,
                )
            )

    def drain_signal_health_changes(self) -> list[SignalHealthChange]:
        """Return and clear queued starvation/recovery transitions."""
        out = self._signal_health_changes
        self._signal_health_changes = []
        return out

    @property
    def starved_rules(self) -> tuple[EffectiveRule, ...]:
        """Rules currently receiving no samples -- i.e. not actually guarding."""
        return tuple(r for r in self._effective if r.starved)
