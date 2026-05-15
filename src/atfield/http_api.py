"""Localhost HTTP API the tray/UI reads.

Design summary
--------------
The watchdog service is a long-lived sync loop. The tray app is a separate
user-mode process (Tauri, eventually) that needs to poll for status, signal
history, rule verdicts, and recent events, and to push pause/unpause/reload
commands. This module is the bridge.

Choices
-------
* **Stdlib only.** ``http.server.ThreadingHTTPServer`` + ``json``. No
  FastAPI/uvicorn. The 7-endpoint surface is tiny and the service main loop
  is sync; an async stack would only add complexity and install size.
* **127.0.0.1 by default.** No auth, but we bind loopback only. Operators
  who want remote access have to opt in via ``[api] bind`` and accept
  responsibility for fronting it with a reverse proxy.
* **A single :class:`ServiceState` object is the contract.** The tick loop
  in :mod:`atfield.service` calls ``state.update_*`` methods after each
  tick; the HTTP handlers read snapshots under a lock. The state object
  has zero behavior beyond holding data.
* **Events are tailed from disk.** ``/events`` reads the last N lines of
  ``events.jsonl`` rather than maintaining a parallel in-memory queue, so
  the source of truth stays the file the operator can grep.

The wire format is JSON for everything. Timestamps are unix seconds (float)
to be cross-process safe; the policy engine uses ``monotonic_ns`` internally
but the tick loop converts to wall clock when pushing samples into state.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from atfield.config_writer import (
    MUTABLE_RULE_FIELDS,
    ConfigWriteError,
    update_rule_field,
    update_rule_threshold,
)
from atfield.policy import Action, EffectiveRule, PolicyEngine
from atfield.prometheus_exporter import (
    PROMETHEUS_CONTENT_TYPE,
    render_metrics,
)
from atfield.rule_profiles import (
    PROFILE_PRESETS,
    RULE_PROFILES,
    classify,
)

__all__ = [
    "DEFAULT_API_HOST",
    "DEFAULT_API_PORT",
    "ApiServer",
    "ServiceState",
    "make_handler",
]


_log = logging.getLogger("atfield.http_api")


DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8765

# /events default page size. Tray dashboard is interested in "what
# happened recently"; full history is on disk.
_EVENTS_DEFAULT_LIMIT = 200
_EVENTS_MAX_LIMIT = 5000

# Per-signal multi-resolution history. The watchdog ticks at 1 Hz, so we
# tier-downsample to keep both the recent ("what just happened?") and the
# historical ("did this happen overnight?") views cheap to compute and
# cheap to serialize.
#
#   tier 0: 1 sample/s, last  60 min  →  3600 raw samples
#   tier 1: 1 sample / 10 s, last  6 h →   2160 averaged samples
#   tier 2: 1 sample / 60 s, last 24 h →   1440 averaged samples
#
# Each downsampled tier stores the MEAN of its window (not last-value), so
# 24-hour-back history isn't subject to whichever value happened to be
# sampled at the minute boundary. Total per signal: ~7200 (ts, value)
# tuples ≈ 115 KB; with ~20 signals on a 2-GPU box that's ~2.3 MB.
_TIER0_INTERVAL_S = 1
_TIER0_CAPACITY = 3600   # 60 min
_TIER1_AGGREGATE = 10    # combine 10 tier-0 samples → one tier-1
_TIER1_CAPACITY = 2160   # 6 hours of 10-second averages
_TIER2_AGGREGATE = 6     # combine 6 tier-1 samples → one tier-2
_TIER2_CAPACITY = 1440   # 24 hours of 1-minute averages


# ---------------------------------------------------------------------------
# State container the service writes and the API reads
# ---------------------------------------------------------------------------


@dataclass
class _CollectorView:
    name: str
    available: bool
    reason: str
    signals: list[str]
    health: str  # HealthState.name


class _Tier:
    """One ring of (ts, value) plus a pending-aggregation accumulator.

    On each upstream push we increment a sum + count; once we've got
    ``aggregate`` upstream samples we emit (mean, latest_ts) into our
    own ring AND return that tuple so the caller can cascade to the
    next tier. Constant-time append; ``capacity`` bounded growth.
    """

    __slots__ = ("_aggregate", "_capacity", "_pending_count", "_pending_sum", "samples")

    def __init__(self, *, capacity: int, aggregate: int) -> None:
        self.samples: deque[tuple[float, float]] = deque(maxlen=capacity)
        self._pending_sum = 0.0
        self._pending_count = 0
        self._aggregate = aggregate
        self._capacity = capacity

    def push(self, ts: float, value: float) -> tuple[float, float] | None:
        self._pending_sum += value
        self._pending_count += 1
        if self._pending_count >= self._aggregate:
            mean = self._pending_sum / self._pending_count
            self.samples.append((ts, mean))
            self._pending_sum = 0.0
            self._pending_count = 0
            return (ts, mean)
        return None


class _MultiResHistory:
    """Three-tier downsampled history for a single signal.

    Memory: capacity bounded; ~7200 (ts, value) tuples = ~115 KB worst
    case. Append cost: O(1) per tier, ≤ 3 tier appends per upstream push.
    Read cost: O(N) to slice the window we care about.
    """

    __slots__ = ("tier0", "tier1", "tier2")

    def __init__(self) -> None:
        # Tier 0 is the raw per-tick stream; aggregate=1 means every push
        # immediately emits to its ring AND cascades to tier 1.
        self.tier0 = _Tier(capacity=_TIER0_CAPACITY, aggregate=1)
        self.tier1 = _Tier(capacity=_TIER1_CAPACITY, aggregate=_TIER1_AGGREGATE)
        self.tier2 = _Tier(capacity=_TIER2_CAPACITY, aggregate=_TIER2_AGGREGATE)

    def push(self, ts: float, value: float) -> None:
        emitted0 = self.tier0.push(ts, value)
        if emitted0 is None:
            return
        # tier 0 emits every push (aggregate=1). Cascade to tier 1, which
        # emits once per 10 pushes; cascade to tier 2, which emits once
        # per 6 tier-1 emissions (i.e. once per 60 tier-0 pushes = 1 min).
        emitted1 = self.tier1.push(*emitted0)
        if emitted1 is None:
            return
        self.tier2.push(*emitted1)

    def slice(self, *, hours: float, now_unix: float) -> list[tuple[float, float]]:
        """Return a single time-ordered list of samples covering ``hours`` back.

        Density tiers are spliced: most recent 1 hour from tier 0,
        1-6 hours from tier 1, 6-24 hours from tier 2. The boundaries
        are inclusive at the older end and exclusive at the newer end
        so we don't double-count overlapping samples.
        """
        cutoff = now_unix - hours * 3600
        if hours <= 1.0:
            return [s for s in self.tier0.samples if s[0] >= cutoff]
        # Newer-than-1h from tier 0; older from tier 1 or 2.
        t0_cutoff = now_unix - 3600
        t0 = [s for s in self.tier0.samples if s[0] >= t0_cutoff]
        if hours <= 6.0:
            t1 = [s for s in self.tier1.samples if cutoff <= s[0] < t0_cutoff]
            return t1 + t0
        t1_cutoff = now_unix - 6 * 3600
        t1 = [s for s in self.tier1.samples if t1_cutoff <= s[0] < t0_cutoff]
        t2 = [s for s in self.tier2.samples if cutoff <= s[0] < t1_cutoff]
        return t2 + t1 + t0


class ServiceState:
    """Thread-safe state mirror for the API layer.

    The service main loop is the sole writer. Every method that mutates
    holds ``self._lock``; every reader takes a snapshot under the lock and
    works with the snapshot outside it. Snapshots are plain dicts/lists so
    the JSON encoder does not need to walk shared mutable state.
    """

    def __init__(
        self,
        *,
        version: str,
        observe_only: bool,
        events_path: Path,
        watchdog_log_path: Path,
        state_dir: Path,
        config_path: Path | None = None,
    ) -> None:
        self._lock = threading.RLock()

        self._version = version
        self._observe_only = observe_only
        self._events_path = events_path
        self._watchdog_log_path = watchdog_log_path
        self._state_dir = state_dir
        # Path to the on-disk config.toml. None means "no path was passed
        # at startup" -- in that case PATCH /rules will materialize a
        # default config under state_dir/config.toml on first write so the
        # slider has somewhere to persist.
        self._config_path = config_path

        self._started_at = time.time()
        self._last_tick_at: float = 0.0
        self._tick_count: int = 0
        self._paused_until_unix: float = 0.0  # 0 == not paused
        self._reload_requested = False

        # Latest sample per signal, with wall-clock when the tick loop saw it.
        # value: (unix_ts: float, value: float, source_id: str, unit: str)
        self._latest_signal: dict[str, tuple[float, float, str, str]] = {}
        # Per-signal multi-resolution history. Tier 0 backs /signals (last
        # hour of raw samples); slice() backs /signals/history (up to 24h
        # of mixed-resolution samples).
        self._history: dict[str, _MultiResHistory] = {}

        # Engine + actuator references for snapshotting rules.
        self._engine: PolicyEngine | None = None

        # Collector probe results.
        self._collectors: list[_CollectorView] = []

        # Most recent action per rule (for `/health` "last action" + UI
        # badges + tray notification trigger). We extend beyond the action
        # itself to surface the killed script when the report rolls in --
        # the tray reads `last_action.script` to title the system toast.
        self._last_action_at: float = 0.0
        self._last_action_kind: str | None = None
        self._last_action_rule: str | None = None
        self._last_action_signal: str | None = None
        self._last_action_script: str | None = None

    # ------------------------------------------------------------------
    # Writers (called by the service tick loop)
    # ------------------------------------------------------------------

    def attach_engine(self, engine: PolicyEngine) -> None:
        with self._lock:
            self._engine = engine

    def set_collectors(self, views: Iterable[_CollectorView]) -> None:
        with self._lock:
            self._collectors = list(views)

    def update_collector_health(self, name: str, health_name: str) -> None:
        with self._lock:
            for cv in self._collectors:
                if cv.name == name:
                    cv.health = health_name
                    return

    def record_tick(self, *, now_unix: float, samples: dict[str, Any]) -> None:
        """Called after each policy tick. ``samples`` is ``{signal: Sample}``."""
        with self._lock:
            self._last_tick_at = now_unix
            self._tick_count += 1
            for sig, sample in samples.items():
                value = float(sample.value)
                self._latest_signal[sig] = (now_unix, value, sample.source_id, sample.unit)
                hist = self._history.get(sig)
                if hist is None:
                    hist = _MultiResHistory()
                    self._history[sig] = hist
                hist.push(now_unix, value)

    def record_action(self, action: Action) -> None:
        with self._lock:
            self._last_action_at = time.time()
            self._last_action_kind = action.kind
            self._last_action_rule = action.rule_name
            self._last_action_signal = action.signal
            # Cleared here; populated by record_kill_report once we know
            # WHAT was actually killed.
            self._last_action_script = None

    def record_kill_report(self, script: str | None) -> None:
        """Called immediately after `record_action(kill)` once the kill
        has executed and the script behind the kill root is known. Lets
        the tray notification say "killed train.py" instead of just
        "killed something". """
        with self._lock:
            self._last_action_script = script

    def set_paused_until(self, until_unix: float) -> None:
        with self._lock:
            self._paused_until_unix = max(0.0, until_unix)

    def consume_reload_request(self) -> bool:
        """Returns True if a reload was requested since the last call."""
        with self._lock:
            if self._reload_requested:
                self._reload_requested = False
                return True
            return False

    # Internal: HTTP handler calls this when /reload comes in.
    def _request_reload(self) -> None:
        with self._lock:
            self._reload_requested = True

    # ------------------------------------------------------------------
    # Read snapshots (called by the HTTP handler)
    # ------------------------------------------------------------------

    def snapshot_health(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            heartbeat_age = now - self._last_tick_at if self._last_tick_at else None
            paused = self._paused_until_unix > now
            collector_views = [
                {
                    "name": cv.name,
                    "available": cv.available,
                    "reason": cv.reason,
                    "health": cv.health,
                    "signals": list(cv.signals),
                }
                for cv in self._collectors
            ]
            engine = self._engine
            rules_active = len(engine.effective_rules) if engine else 0
            rules_disabled = len(engine.disabled_rules) if engine else 0
            return {
                "version": self._version,
                "mode": "observe-only" if self._observe_only else "armed",
                "paused": paused,
                "paused_until": self._paused_until_unix if paused else None,
                "started_at": self._started_at,
                "uptime_s": now - self._started_at,
                "tick_count": self._tick_count,
                "last_tick_at": self._last_tick_at or None,
                "heartbeat_age_s": heartbeat_age,
                "collectors": collector_views,
                "rules_active": rules_active,
                "rules_disabled": rules_disabled,
                "last_action": (
                    {
                        "at": self._last_action_at,
                        "kind": self._last_action_kind,
                        "rule": self._last_action_rule,
                        "signal": self._last_action_signal,
                        # Populated after the kill report rolls in. None for
                        # log/throttle actions or kills where no script could
                        # be extracted from the launcher cmdline.
                        "script": self._last_action_script,
                    }
                    if self._last_action_at
                    else None
                ),
            }

    def snapshot_signals(self, *, since: float | None) -> dict[str, Any]:
        """Return latest values + (filtered) tier-0 history per signal.

        ``since`` is a unix timestamp; if provided, history is trimmed to
        samples newer than that. Useful for the dashboard to fetch only the
        deltas after its initial load. For longer windows / drill-down
        views, see :meth:`snapshot_signal_history`.
        """
        with self._lock:
            latest = {
                sig: {
                    "value": value,
                    "ts": ts,
                    "source": source,
                    "unit": unit,
                }
                for sig, (ts, value, source, unit) in self._latest_signal.items()
            }
            history: dict[str, list[list[float]]] = {}
            for sig, hist in self._history.items():
                buf = hist.tier0.samples
                if since is None:
                    history[sig] = [[ts, val] for ts, val in buf]
                else:
                    history[sig] = [[ts, val] for ts, val in buf if ts > since]
            return {"latest": latest, "history": history}

    def snapshot_signal_history(self, signal: str, *, hours: float) -> dict[str, Any]:
        """Multi-resolution history for ONE signal, for the drill-down view.

        Returns a single time-ordered samples list spliced from the three
        density tiers: most recent 1 h at 1 Hz, 1-6 h ago at 10 s, 6-24 h
        ago at 60 s. Each sample inside an averaged tier is the MEAN of
        the underlying raw samples in its window, not the most recent
        value -- so spikes outside sample boundaries don't get hidden.

        ``hours`` is clamped to the configured retention (currently 24 h).
        Unknown signal → empty samples list (not an error).
        """
        # Clamp first so callers can pass anything and we just return what
        # we have. 24 h is the max our 60-second tier can cover.
        hours = max(0.001, min(24.0, float(hours)))
        with self._lock:
            hist = self._history.get(signal)
            now = time.time()
            samples: list[tuple[float, float]] = (
                hist.slice(hours=hours, now_unix=now) if hist is not None else []
            )
            latest = self._latest_signal.get(signal)
            unit = latest[3] if latest is not None else ""
            source = latest[2] if latest is not None else ""
        return {
            "signal": signal,
            "hours": hours,
            "now": now,
            "unit": unit,
            "source": source,
            "samples": [[ts, val] for ts, val in samples],
            "count": len(samples),
        }

    def snapshot_rules(self) -> dict[str, Any]:
        with self._lock:
            engine = self._engine
            if engine is None:
                return {"effective": [], "disabled": []}
            stats = engine.stats_snapshot()
            now_ns = time.monotonic_ns()
            effective = [_serialize_rule(r, stats.get(r.name, {}), now_ns) for r in engine.effective_rules]
            disabled = [
                {
                    "rule": d.base_rule_name,
                    "signal": d.signal,
                    "reason": d.reason,
                }
                for d in engine.disabled_rules
            ]
            return {"effective": effective, "disabled": disabled}

    def events_path(self) -> Path:
        return self._events_path

    def watchdog_log_path(self) -> Path:
        return self._watchdog_log_path

    def state_dir(self) -> Path:
        return self._state_dir

    def config_path(self) -> Path:
        """Return the on-disk config path. Falls back to
        ``state_dir/config.toml`` when no explicit path was registered at
        startup -- the slider needs SOMEWHERE to write."""
        with self._lock:
            return self._config_path or (self._state_dir / "config.toml")

    def set_config_path(self, path: Path | None) -> None:
        with self._lock:
            self._config_path = path

    # ------------------------------------------------------------------
    # Mutating writers used by PATCH /rules and POST /profile
    # ------------------------------------------------------------------

    def patch_rule_threshold(self, base_rule_name: str, new_threshold: float) -> None:
        """Persist a slider's new threshold to disk and queue a reload.

        Validation lives here (not in the handler) so the CLI can use
        the same path. Raises ``ValueError`` for unknown / out-of-range
        rules and ``ConfigWriteError`` for I/O failures -- the HTTP layer
        maps each to the appropriate status code.
        """
        profile = RULE_PROFILES.get(base_rule_name)
        if profile is None:
            raise ValueError(f"unknown rule {base_rule_name!r}")
        if not (profile.min <= new_threshold <= profile.max):
            raise ValueError(
                f"threshold {new_threshold} out of range for {base_rule_name} "
                f"(allowed: {profile.min}..{profile.max})"
            )
        update_rule_threshold(self.config_path(), base_rule_name, new_threshold)
        self._request_reload()

    def patch_rule_fields(
        self,
        base_rule_name: str,
        updates: dict[str, object],
    ) -> dict[str, object]:
        """Apply a multi-field PATCH to a rule. Each field in ``updates`` is
        validated against :data:`MUTABLE_RULE_FIELDS` and bounds-checked
        where applicable. Threshold goes through the existing tier-aware
        ``patch_rule_threshold`` so its slider validation stays consistent.
        Returns the dict of accepted updates so the API can echo them.
        """
        if not isinstance(updates, dict) or not updates:
            raise ValueError("PATCH body must be a non-empty JSON object")
        unknown = set(updates) - set(MUTABLE_RULE_FIELDS)
        if unknown:
            raise ValueError(
                f"unknown field(s) {sorted(unknown)}; "
                f"allowed: {sorted(MUTABLE_RULE_FIELDS)}"
            )

        accepted: dict[str, object] = {}
        for field, value in updates.items():
            if field == "threshold":
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise ValueError("'threshold' must be a number")
                self.patch_rule_threshold(base_rule_name, float(value))
                accepted[field] = float(value)
                continue
            if field == "window_s":
                if not isinstance(value, int) or isinstance(value, bool):
                    raise ValueError("'window_s' must be an integer (seconds)")
                if not (1 <= value <= 600):
                    raise ValueError(f"window_s must be 1..600, got {value}")
                update_rule_field(self.config_path(), base_rule_name, field, value)
                accepted[field] = value
            elif field == "cooldown_s":
                if not isinstance(value, int) or isinstance(value, bool):
                    raise ValueError("'cooldown_s' must be an integer (seconds)")
                if not (0 <= value <= 24 * 3600):
                    raise ValueError(f"cooldown_s must be 0..86400, got {value}")
                update_rule_field(self.config_path(), base_rule_name, field, value)
                accepted[field] = value
            elif field == "min_fraction_over":
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise ValueError("'min_fraction_over' must be a number")
                f = float(value)
                if not (0.0 <= f <= 1.0):
                    raise ValueError(f"min_fraction_over must be 0..1, got {f}")
                update_rule_field(self.config_path(), base_rule_name, field, f)
                accepted[field] = f
            elif field == "action":
                if value not in {"kill", "throttle", "log"}:
                    raise ValueError(f"action must be kill / throttle / log, got {value!r}")
                update_rule_field(self.config_path(), base_rule_name, field, value)
                accepted[field] = value
        # Threshold-only path already requested a reload; otherwise we
        # need one here. Simpler to just request unconditionally -- the
        # service collapses repeated requests at the next tick.
        self._request_reload()
        return accepted

    def apply_profile_preset(self, profile_name: str) -> dict[str, float]:
        """Apply an Aggressive/Normal/Relaxed preset to every known rule.

        Returns the mapping of rule name -> new threshold so the caller
        can echo it back. Raises ``ValueError`` for unknown profile names.
        Single reload is queued at the end (not one per rule) so the
        engine sees a consistent set of changes.
        """
        preset = PROFILE_PRESETS.get(profile_name)
        if preset is None:
            raise ValueError(
                f"unknown profile {profile_name!r}; "
                f"valid: {sorted(PROFILE_PRESETS.keys())}"
            )
        applied: dict[str, float] = {}
        for rule_name, threshold in preset.items():
            update_rule_threshold(self.config_path(), rule_name, threshold)
            applied[rule_name] = threshold
        self._request_reload()
        return applied


def _serialize_rule(rule: EffectiveRule, stats: dict[str, Any], now_ns: int) -> dict[str, Any]:
    cooldown_remaining_s = max(0.0, (rule.cooldown_until_ns - now_ns) / 1_000_000_000)
    base_name = rule.base_rule.name
    threshold = rule.base_rule.threshold
    profile = RULE_PROFILES.get(base_name)
    # Slider metadata is OPTIONAL -- only known rules get a `tuning` block;
    # custom user-defined rules without tier definitions still surface
    # cleanly (the UI just doesn't show a slider for them).
    tuning: dict[str, Any] | None = None
    if profile is not None:
        tuning = {
            "min": profile.min,
            "max": profile.max,
            "aggressive_max": profile.aggressive_max,
            "relaxed_min": profile.relaxed_min,
            "step": profile.step,
            "unit": profile.unit,
            "current_tier": classify(base_name, threshold),
            "presets": {
                "aggressive": profile.aggressive_value,
                "normal": profile.normal_value,
                "relaxed": profile.relaxed_value,
            },
        }
    return {
        "name": rule.name,
        "base_rule": base_name,
        "signal": rule.signal,
        "threshold": threshold,
        "window_s": rule.base_rule.window_s,
        "min_fraction_over": rule.base_rule.min_fraction_over,
        "action": rule.base_rule.action,
        # Per-rule override (None means inherit from kill.post_kill_cooldown_seconds).
        # Surfaced so the Settings sliders can show what the user actually wrote.
        "cooldown_s": rule.base_rule.cooldown_s,
        "min_samples": rule.min_samples,
        "verdict": stats.get("last_verdict", "INSUFFICIENT"),
        "fraction_over": stats.get("last_fraction", 0.0),
        "latest_value": stats.get("last_value"),
        "triggers": stats.get("triggers", 0),
        "cooldown_remaining_s": cooldown_remaining_s,
        "tuning": tuning,
    }


# ---------------------------------------------------------------------------
# Pause sentinel I/O (mirrors atfield.service for consistency)
# ---------------------------------------------------------------------------


_PAUSE_SENTINEL_FILENAME = "pause.sentinel"


def _write_pause_sentinel(state_dir: Path, *, duration_s: float | None) -> str:
    """Write the pause sentinel file. Returns the ISO timestamp written.

    ``duration_s=None`` means "pause indefinitely" — we encode that as a
    1-year-from-now expiry which the operator/tray can clear with /unpause.
    """
    seconds = 365 * 24 * 3600 if duration_s is None else max(1.0, float(duration_s))
    until = datetime.now(timezone.utc).replace(microsecond=0) + _td(seconds=seconds)
    iso = until.isoformat()
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / _PAUSE_SENTINEL_FILENAME).write_text(iso + "\n", encoding="utf-8")
    return iso


def _clear_pause_sentinel(state_dir: Path) -> bool:
    p = state_dir / _PAUSE_SENTINEL_FILENAME
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False


def _td(*, seconds: float):
    # Tiny helper so we don't need to import datetime.timedelta at module top.
    from datetime import timedelta
    return timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# Events tailer
# ---------------------------------------------------------------------------


def _tail_events(path: Path, *, since: float | None, limit: int) -> list[dict[str, Any]]:
    """Read the last ``limit`` JSONL events, filtered by ``ts >= since``.

    Reads the whole file once then slices in memory. The file is rotated at
    the operator's discretion and isn't expected to grow unbounded; if it
    becomes a hot spot we can add reverse-tail later.
    """
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if since is not None:
                    ts = event.get("ts")
                    if not isinstance(ts, (int, float)) or ts < since:
                        continue
                out.append(event)
    except OSError:
        return []
    return out[-limit:]


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------


def make_handler(state: ServiceState) -> type[BaseHTTPRequestHandler]:
    """Build a per-server handler class bound to one :class:`ServiceState`.

    Returning a closure-built class is the standard stdlib pattern for
    injecting state into ``BaseHTTPRequestHandler`` (which is instantiated
    fresh per request by the server).
    """

    class _Handler(BaseHTTPRequestHandler):
        # Quiet the default per-request stderr spam. We log explicitly when
        # something interesting happens.
        def log_message(self, format: str, *args: Any) -> None:
            _log.debug("api: " + format, *args)

        # ------------------------------------------------------------------
        # CORS
        # ------------------------------------------------------------------
        #
        # The Tauri tray app serves its frontend from `tauri://localhost`
        # (production) or `http://localhost:5174` (vite dev). Both are
        # cross-origin relative to `http://127.0.0.1:8765`, so the WebView
        # blocks our fetch() unless the API explicitly allows it. Since
        # the API only binds 127.0.0.1 (i.e. it's reachable from the same
        # machine only), `Access-Control-Allow-Origin: *` is safe -- a
        # remote attacker can't reach this socket regardless of CORS.
        #
        # We answer OPTIONS preflights for all methods so POSTs with a
        # Content-Type: application/json body work too.

        def _send_cors_headers(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "86400")

        def do_OPTIONS(self) -> None:
            self.send_response(HTTPStatus.NO_CONTENT)
            self._send_cors_headers()
            self.end_headers()

        # ------------------------------------------------------------------
        # PATCH dispatch -- slider-driven rule mutations
        # ------------------------------------------------------------------

        def do_PATCH(self) -> None:
            try:
                parsed = urlsplit(self.path)
                path = parsed.path.rstrip("/") or "/"
                body = self._read_json_body()
                # PATCH /rules/<base_rule_name>
                # Body may include any of {threshold, window_s, cooldown_s,
                # action, min_fraction_over}. Each field is independently
                # validated and persisted; partial-success isn't supported
                # (any failure aborts before later fields are touched).
                if path.startswith("/rules/"):
                    rule_name = path[len("/rules/"):]
                    if not rule_name or "/" in rule_name:
                        self._send_error(HTTPStatus.BAD_REQUEST, "missing rule name")
                        return
                    if not isinstance(body, dict) or not body:
                        self._send_error(
                            HTTPStatus.BAD_REQUEST,
                            "body must be a non-empty JSON object",
                        )
                        return
                    try:
                        accepted = state.patch_rule_fields(rule_name, body)
                    except ValueError as exc:
                        self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
                        return
                    except ConfigWriteError as exc:
                        _log.error("config write failed: %s", exc)
                        self._send_error(
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                            f"config write failed: {exc}",
                        )
                        return
                    payload: dict[str, Any] = {
                        "rule": rule_name,
                        "accepted": accepted,
                        "reload_queued": True,
                    }
                    # Echo the slider's tier classification when threshold
                    # was part of the patch -- preserves the v0.1 contract
                    # the dashboard depends on for optimistic UI updates.
                    if "threshold" in accepted and isinstance(accepted["threshold"], (int, float)):
                        payload["threshold"] = float(accepted["threshold"])
                        payload["tier"] = classify(rule_name, float(accepted["threshold"]))
                    self._send_json(payload)
                else:
                    self._send_error(HTTPStatus.NOT_FOUND, f"unknown endpoint {path}")
            except Exception:
                _log.exception("api: PATCH %s crashed", self.path)
                self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error")

        # ------------------------------------------------------------------
        # GET dispatch
        # ------------------------------------------------------------------

        def do_GET(self) -> None:
            try:
                parsed = urlsplit(self.path)
                path = parsed.path.rstrip("/") or "/"
                qs = parse_qs(parsed.query)
                if path == "/health":
                    self._send_json(state.snapshot_health())
                elif path == "/signals":
                    since = _maybe_float(qs.get("since"))
                    self._send_json(state.snapshot_signals(since=since))
                elif path == "/signals/history":
                    signal = qs.get("signal", [None])[0]
                    if not signal:
                        self._send_error(HTTPStatus.BAD_REQUEST, "missing 'signal' parameter")
                        return
                    hours = _maybe_float(qs.get("hours")) or 1.0
                    self._send_json(state.snapshot_signal_history(signal, hours=hours))
                elif path == "/rules":
                    self._send_json(state.snapshot_rules())
                elif path == "/events":
                    since = _maybe_float(qs.get("since"))
                    limit = _maybe_int(qs.get("limit"))
                    if limit is None:
                        limit = _EVENTS_DEFAULT_LIMIT
                    limit = max(1, min(_EVENTS_MAX_LIMIT, limit))
                    events = _tail_events(state.events_path(), since=since, limit=limit)
                    self._send_json({"events": events, "count": len(events)})
                elif path == "/metrics":
                    # Prometheus text-exposition. Build all three snapshots
                    # under the state lock and hand them to the renderer.
                    body = render_metrics(
                        health=state.snapshot_health(),
                        signals=state.snapshot_signals(since=None),
                        rules=state.snapshot_rules(),
                    )
                    self._send_text(body, content_type=PROMETHEUS_CONTENT_TYPE)
                else:
                    self._send_error(HTTPStatus.NOT_FOUND, f"unknown endpoint {path}")
            except Exception:
                _log.exception("api: GET %s crashed", self.path)
                self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error")

        # ------------------------------------------------------------------
        # POST dispatch
        # ------------------------------------------------------------------

        def do_POST(self) -> None:
            try:
                parsed = urlsplit(self.path)
                path = parsed.path.rstrip("/") or "/"
                body = self._read_json_body()
                if path == "/pause":
                    duration = body.get("duration_s") if isinstance(body, dict) else None
                    if duration is not None and not isinstance(duration, (int, float)):
                        self._send_error(HTTPStatus.BAD_REQUEST, "duration_s must be a number")
                        return
                    iso = _write_pause_sentinel(state.state_dir(), duration_s=duration)
                    state.set_paused_until(time.time() + (duration if duration else 365 * 24 * 3600))
                    self._send_json({"paused": True, "until": iso})
                elif path == "/unpause":
                    cleared = _clear_pause_sentinel(state.state_dir())
                    state.set_paused_until(0.0)
                    self._send_json({"paused": False, "cleared": cleared})
                elif path == "/reload":
                    state._request_reload()
                    self._send_json({"reload_queued": True})
                elif path == "/profile":
                    # POST /profile body: {"profile": "aggressive"|"normal"|"relaxed"}
                    profile_name = body.get("profile") if isinstance(body, dict) else None
                    if not isinstance(profile_name, str):
                        self._send_error(HTTPStatus.BAD_REQUEST,
                                         "'profile' must be a string")
                        return
                    try:
                        applied = state.apply_profile_preset(profile_name)
                    except ValueError as exc:
                        self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
                        return
                    except ConfigWriteError as exc:
                        _log.error("profile apply failed: %s", exc)
                        self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR,
                                         f"config write failed: {exc}")
                        return
                    self._send_json({
                        "profile": profile_name,
                        "applied": applied,
                        "reload_queued": True,
                    })
                else:
                    self._send_error(HTTPStatus.NOT_FOUND, f"unknown endpoint {path}")
            except Exception:
                _log.exception("api: POST %s crashed", self.path)
                self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error")

        # ------------------------------------------------------------------
        # Helpers
        # ------------------------------------------------------------------

        def _read_json_body(self) -> Any:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return {}

        def _send_json(self, payload: Any, *, status: int = 200) -> None:
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(data)

        def _send_text(
            self,
            data: bytes,
            *,
            content_type: str,
            status: int = 200,
        ) -> None:
            """Send a pre-encoded body with a caller-chosen Content-Type.

            Used by /metrics (Prometheus text exposition) where we need
            text/plain instead of application/json.
            """
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(data)

        def _send_error(self, status: HTTPStatus, message: str) -> None:
            self._send_json({"error": message, "status": int(status)}, status=int(status))

    return _Handler


def _maybe_float(values: list[str] | None) -> float | None:
    if not values:
        return None
    try:
        return float(values[0])
    except ValueError:
        return None


def _maybe_int(values: list[str] | None) -> int | None:
    if not values:
        return None
    try:
        return int(values[0])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


class ApiServer:
    """Wrap :class:`ThreadingHTTPServer` with start/stop semantics.

    Designed to live on a daemon thread spawned by :func:`atfield.service.run_service`.
    Stopping is idempotent and safe to call from a signal handler.
    """

    def __init__(
        self,
        state: ServiceState,
        *,
        host: str = DEFAULT_API_HOST,
        port: int = DEFAULT_API_PORT,
    ) -> None:
        self._state = state
        self._host = host
        self._port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        if self._server is None:
            return (self._host, self._port)
        return self._server.server_address[:2]  # type: ignore[return-value]

    def start(self) -> None:
        if self._server is not None:
            return
        handler_cls = make_handler(self._state)
        try:
            server = ThreadingHTTPServer((self._host, self._port), handler_cls)
        except OSError as exc:
            _log.warning("api: cannot bind %s:%d (%s); HTTP API disabled this run", self._host, self._port, exc)
            return
        server.daemon_threads = True
        thread = threading.Thread(
            target=server.serve_forever,
            name="atfield-http-api",
            daemon=True,
        )
        thread.start()
        self._server = server
        self._thread = thread
        _log.info("api: listening on http://%s:%d/", self._host, self._port)

    def stop(self, *, timeout: float = 2.0) -> None:
        srv, thr = self._server, self._thread
        self._server = None
        self._thread = None
        if srv is None:
            return
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            _log.debug("api: server.shutdown() raised", exc_info=True)
        if thr is not None:
            thr.join(timeout=timeout)


# ---------------------------------------------------------------------------
# Helper for the service module to build a CollectorView from a probe result.
# Kept here (rather than in service.py) so the View shape stays co-located
# with ServiceState.
# ---------------------------------------------------------------------------


def collector_view_from_probe(name: str, probe_result: Any, health_name: str) -> _CollectorView:
    return _CollectorView(
        name=name,
        available=bool(probe_result.available),
        reason=str(probe_result.reason),
        signals=list(probe_result.signals),
        health=health_name,
    )
