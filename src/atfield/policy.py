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

from atfield.config import AtFieldConfig, RuleConfig
from atfield.signals import (
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
    "expand_rules",
]


_log = logging.getLogger("atfield.policy")


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
    ) -> None:
        self._cfg = cfg
        self._effective, self._disabled = expand_rules(
            cfg.rules, available_signals, tick_hz=cfg.general.tick_hz,
        )
        self._stats: dict[str, _Stats] = {r.name: _Stats() for r in self._effective}
        self._paused_until_ns: int = 0
        self._max_sample_age_s = max(2.0 / max(cfg.general.tick_hz, 1), 2.0)

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
        """
        actions: list[Action] = []
        paused = self.is_currently_paused(now_ns=now_ns)

        for rule in self._effective:
            sample = samples.get(rule.signal)
            if sample is not None:
                rule.window.add(sample)

            result: EvalResult = evaluate_window(
                rule.window,
                threshold=rule.base_rule.threshold,
                min_fraction_over=rule.base_rule.min_fraction_over,
                now_ns=now_ns,
                max_sample_age_s=self._max_sample_age_s,
                min_samples=rule.min_samples,
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
