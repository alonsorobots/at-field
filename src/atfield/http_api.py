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

from atfield.policy import Action, EffectiveRule, PolicyEngine

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

# Per-signal ring buffer length. 3600 samples = 1 hour at 1 Hz; with ~20
# signals on a 2-GPU box that's ~4 MB peak. Sparkline-friendly.
_RING_LEN = 3600

# /events default page size. Tray dashboard is interested in "what
# happened recently"; full history is on disk.
_EVENTS_DEFAULT_LIMIT = 200
_EVENTS_MAX_LIMIT = 5000


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
    ) -> None:
        self._lock = threading.RLock()

        self._version = version
        self._observe_only = observe_only
        self._events_path = events_path
        self._watchdog_log_path = watchdog_log_path
        self._state_dir = state_dir

        self._started_at = time.time()
        self._last_tick_at: float = 0.0
        self._tick_count: int = 0
        self._paused_until_unix: float = 0.0  # 0 == not paused
        self._reload_requested = False

        # Latest sample per signal, with wall-clock when the tick loop saw it.
        # value: (unix_ts: float, value: float, source_id: str, unit: str)
        self._latest_signal: dict[str, tuple[float, float, str, str]] = {}
        # Per-signal ring buffer of (unix_ts, value).
        self._history: dict[str, deque[tuple[float, float]]] = {}

        # Engine + actuator references for snapshotting rules.
        self._engine: PolicyEngine | None = None

        # Collector probe results.
        self._collectors: list[_CollectorView] = []

        # Most recent action per rule (for `/health` "last action" + UI badges).
        self._last_action_at: float = 0.0
        self._last_action_kind: str | None = None
        self._last_action_rule: str | None = None

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
                buf = self._history.setdefault(sig, deque(maxlen=_RING_LEN))
                buf.append((now_unix, value))

    def record_action(self, action: Action) -> None:
        with self._lock:
            self._last_action_at = time.time()
            self._last_action_kind = action.kind
            self._last_action_rule = action.rule_name

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
                    }
                    if self._last_action_at
                    else None
                ),
            }

    def snapshot_signals(self, *, since: float | None) -> dict[str, Any]:
        """Return latest values + (filtered) history per signal.

        ``since`` is a unix timestamp; if provided, history is trimmed to
        samples newer than that. Useful for the dashboard to fetch only the
        deltas after its initial load.
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
            for sig, buf in self._history.items():
                if since is None:
                    history[sig] = [[ts, val] for ts, val in buf]
                else:
                    history[sig] = [[ts, val] for ts, val in buf if ts > since]
            return {"latest": latest, "history": history}

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


def _serialize_rule(rule: EffectiveRule, stats: dict[str, Any], now_ns: int) -> dict[str, Any]:
    cooldown_remaining_s = max(0.0, (rule.cooldown_until_ns - now_ns) / 1_000_000_000)
    return {
        "name": rule.name,
        "base_rule": rule.base_rule.name,
        "signal": rule.signal,
        "threshold": rule.base_rule.threshold,
        "window_s": rule.base_rule.window_s,
        "min_fraction_over": rule.base_rule.min_fraction_over,
        "action": rule.base_rule.action,
        "min_samples": rule.min_samples,
        "verdict": stats.get("last_verdict", "INSUFFICIENT"),
        "fraction_over": stats.get("last_fraction", 0.0),
        "latest_value": stats.get("last_value"),
        "triggers": stats.get("triggers", 0),
        "cooldown_remaining_s": cooldown_remaining_s,
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
