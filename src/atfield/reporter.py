"""Push kill/pressure events out to whichever external services want them.

AT-Field stays generic and job-agnostic: it has no concept of what a "gig",
"job", or "coordinator" is, and this module doesn't change that. All it does
is watch its own client-manifest directory
(``%ProgramData%\\ATField\\clients\\<name>\\*.json``) -- already a generic,
open convention any service can use to register itself with AT-Field -- for
manifests carrying an optional ``atfield_event_webhook`` field. Any service
that wants push notifications for kill/pressure events simply writes that
field into its own manifest; AT-Field neither knows nor cares which service
that is or what it does with the events.

Best-effort only: a slow or unreachable subscriber must never block or crash
the watchdog's kill dispatch path. Every failure mode here is swallowed and
logged, never raised.

Kill and guard-health reports that fail to deliver are spooled on disk and
retried (0.4.19): until then a kill that happened while the subscriber was
restarting was simply lost. Telemetry is never spooled.
"""

from __future__ import annotations

import json
import logging
import math
import os
import queue
import socket
import threading
import time
from pathlib import Path
from typing import Any

import requests

_log = logging.getLogger("atfield.reporter")

_HOSTNAME = socket.gethostname()

_MANIFEST_GLOB = "clients/*/*.json"
_WEBHOOK_FIELD = "atfield_event_webhook"
_WEBHOOK_CACHE_TTL_S = 30.0
_POST_TIMEOUT_S = 3.0

_cached_webhooks: list[str] = []
_cached_at: float = 0.0

# Delivery runs on a background daemon thread so a slow/unreachable subscriber
# never stalls the watchdog's tick loop -- which is exactly the loop that must
# stay responsive during a thermal/OOM storm, i.e. precisely when a subscriber
# (e.g. a coordinator that's also under load) is most likely to be slow. The
# queue is bounded and drops on overflow: this is best-effort observability,
# not a delivery guarantee, and a persistently-down subscriber must not grow
# memory without limit.
_QUEUE_MAX = 256
_send_queue: "queue.Queue[tuple[str, dict[str, Any], Path | None]]" = queue.Queue(maxsize=_QUEUE_MAX)
_worker_started = False
_worker_lock = threading.Lock()

# RETRY SPOOL (0.4.19). An event worth retrying -- a kill, a near-limit warning, a
# guard going inert -- that the subscriber did not accept is appended to
# <state_dir>/reporter_spool.jsonl and resent by the delivery thread with backoff
# until a 2xx. Bounded: past _SPOOL_MAX the oldest are dropped with a warning.
# Only the delivery thread touches the file, so the tick loop never waits on it.
_SPOOLED_TYPES = frozenset({"kill_report", "guard_health"})
_SPOOL_NAME = "reporter_spool.jsonl"
_SPOOL_MAX = 500
_RETRY_MIN_S = 15.0
_RETRY_MAX_S = 300.0
_spool_dirs: set[Path] = set()


def _post(url: str, payload: dict[str, Any]) -> tuple[bool, bool]:
    """(delivered, worth_retrying). A refusal retrying cannot fix (401, 404, 422)
    is not retried; a network failure, a timeout or a server error is."""
    try:
        r = requests.post(url, json=payload, timeout=_POST_TIMEOUT_S)
    except Exception:
        _log.debug("failed to report event to subscriber %s", url, exc_info=True)
        return False, True
    if 200 <= r.status_code < 300:
        return True, False
    retry = r.status_code >= 500 or r.status_code in (408, 429)
    if not retry:
        _log.warning("subscriber %s refused a %s event with HTTP %d; not retrying",
                     url, payload.get("type"), r.status_code)
    return False, retry


def _read_spool(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _write_spool(path: Path, lines: list[str]) -> None:
    if not lines:
        path.unlink(missing_ok=True)
        return
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _spool_append(spool_dir: Path, url: str, payload: dict[str, Any]) -> None:
    try:
        path = spool_dir / _SPOOL_NAME
        lines = _read_spool(path) + [json.dumps({"url": url, "payload": payload})]
        if len(lines) > _SPOOL_MAX:
            _log.warning("reporter spool over %d events; dropping the %d oldest",
                         _SPOOL_MAX, len(lines) - _SPOOL_MAX)
            lines = lines[-_SPOOL_MAX:]
        _write_spool(path, lines)
        _spool_dirs.add(spool_dir)
    except Exception:  # noqa: BLE001
        _log.exception("could not spool an undelivered %s event", payload.get("type"))


def spool_size(spool_dir: Path) -> int:
    try:
        return len(_read_spool(spool_dir / _SPOOL_NAME))
    except Exception:  # noqa: BLE001
        return 0


def _deliver(url: str, payload: dict[str, Any], spool_dir: Path | None) -> bool:
    ok, retry = _post(url, payload)
    if not ok and retry and spool_dir is not None and payload.get("type") in _SPOOLED_TYPES:
        _spool_append(spool_dir, url, payload)
    return ok


def _drain_spool(spool_dir: Path) -> int:
    """Resend every spooled event once; keep what still fails. Returns how many were
    delivered. After one retryable failure the rest for that URL wait for the next
    pass, so a black-holed subscriber costs one timeout per pass, not one per event."""
    try:
        path = spool_dir / _SPOOL_NAME
        lines = _read_spool(path)
        if not lines:
            return 0
        keep: list[str] = []
        unreachable: set[str] = set()
        delivered = 0
        for ln in lines:
            try:
                item = json.loads(ln)
                url, payload = item["url"], item["payload"]
            except (ValueError, KeyError, TypeError):
                _log.warning("dropping a malformed reporter spool line")
                continue
            if url in unreachable:
                keep.append(ln)
                continue
            ok, retry = _post(url, payload)
            if ok:
                delivered += 1
            elif retry:
                unreachable.add(url)
                keep.append(ln)
        _write_spool(path, keep)
        if delivered:
            _log.info("delivered %d spooled event(s); %d still waiting", delivered, len(keep))
        return delivered
    except Exception:  # noqa: BLE001
        _log.exception("reporter spool drain failed")
        return 0


def _delivery_worker() -> None:
    backoff = _RETRY_MIN_S
    next_retry = time.monotonic() + backoff
    while True:
        timeout = max(0.01, next_retry - time.monotonic()) if _spool_dirs else None
        try:
            url, payload, spool_dir = _send_queue.get(timeout=timeout)
        except queue.Empty:
            pass
        else:
            try:
                if spool_dir is not None and spool_dir not in _spool_dirs and spool_size(spool_dir):
                    _spool_dirs.add(spool_dir)          # left over from a previous run
                    next_retry = time.monotonic()
                _deliver(url, payload, spool_dir)
            except Exception:  # noqa: BLE001
                _log.exception("event delivery failed")
            finally:
                _send_queue.task_done()
        if _spool_dirs and time.monotonic() >= next_retry:
            progressed = sum(_drain_spool(d) for d in list(_spool_dirs))
            waiting = [d for d in list(_spool_dirs) if spool_size(d)]
            _spool_dirs.intersection_update(waiting)
            backoff = _RETRY_MIN_S if (progressed or not waiting) else min(backoff * 2, _RETRY_MAX_S)
            next_retry = time.monotonic() + backoff


def _ensure_worker() -> None:
    global _worker_started
    if _worker_started:
        return
    with _worker_lock:
        if _worker_started:
            return
        threading.Thread(target=_delivery_worker, name="atfield-reporter", daemon=True).start()
        _worker_started = True


def _discover_webhooks(state_dir: Path) -> list[str]:
    """Find every distinct webhook URL currently registered by any client.

    A client subscribes just by including ``atfield_event_webhook`` in the
    manifest it already writes for itself under ``clients/<name>/``. Multiple
    manifests (including stale ones from long-dead processes) can point at
    the same URL; dedupe by URL so a subscriber isn't POSTed to twice.
    """
    urls: set[str] = set()
    try:
        for p in state_dir.glob(_MANIFEST_GLOB):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            url = data.get(_WEBHOOK_FIELD)
            if url:
                urls.add(url)
    except OSError:
        _log.exception("failed to glob %s for event-webhook subscribers", state_dir / _MANIFEST_GLOB)
    return sorted(urls)


def _webhooks(state_dir: Path) -> list[str]:
    global _cached_webhooks, _cached_at
    now = time.monotonic()
    if (now - _cached_at) < _WEBHOOK_CACHE_TTL_S:
        return _cached_webhooks
    _cached_webhooks = _discover_webhooks(state_dir)
    _cached_at = now
    return _cached_webhooks


def _finite_or_none(x: Any) -> float | None:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def report_kill(state_dir: Path, *, action: Any, report: Any) -> None:
    """POST a kill/pressure event to every currently-registered subscriber.

    ``action`` is an :class:`atfield.policy.Action`; ``report`` is an
    :class:`atfield.actuator.KillReport`. Mirrors the shape already written
    to ``events.jsonl`` by :meth:`atfield.audit.AuditWriter.write_kill_report`
    (same field names) so a subscriber doesn't need two mental models. The
    webhook URL is POSTed to exactly as registered -- no path is appended --
    so each subscriber owns its own endpoint shape.
    """
    webhooks = _webhooks(state_dir)
    if not webhooks:
        return  # nobody currently subscribed on this machine

    payload = {
        "type": "kill_report",
        # Which machine this event happened on. A subscriber receiving the
        # POST cannot otherwise know -- the request could come from any host
        # in a fleet -- so this is required, not decorative.
        "host": _HOSTNAME,
        "rule": action.rule_name,
        "signal": action.signal,
        "threshold": action.threshold,
        "action": action.kind,
        # A near-limit warning (a log/throttle rule with notify = true), not a kill.
        "notify": bool(getattr(action, "notify", False)),
        # The reading that fired the rule; None when not finite -- a NaN would make
        # the body invalid JSON and the subscriber would refuse the whole event.
        "value": _finite_or_none(getattr(action, "latest_value", None)),
        "kill_root": (
            {"pid": report.kill_root.pid, "name": report.kill_root.name}
            if report.kill_root
            else None
        ),
        "succeeded": report.succeeded,
        "skipped_reason": report.skipped_reason,
        "ts": time.time(),
    }
    _ensure_worker()
    for url in webhooks:
        try:
            _send_queue.put_nowait((url, payload, state_dir))
        except queue.Full:
            # Subscriber persistently unreachable and events piling up: drop
            # the newest rather than block the tick loop or grow unbounded.
            _log.debug("reporter queue full; dropping event for %s", url)


def report_guard_health(
    state_dir: Path, *, rule: str, signal: str, action: str, detail: str
) -> None:
    """POST a "this rule has gone INERT" event to every registered subscriber.

    The counterpart to :func:`report_kill`, and the more important one. A kill
    is loud by construction -- work dies and somebody notices. A guard that
    stops guarding is silent: the rule reports ``INSUFFICIENT``, which is also
    what a freshly-started rule reports, and ``/health`` goes on saying
    ``armed``.

    On 2026-09-02 a wedged NVML session left both GPU core-temp rules inert for
    **4.2 days**. Detection was never the problem -- ``SIGNAL LOST`` was logged
    at ERROR within 10.6 s, written to ``events.jsonl``, and counted in
    ``/health`` -- but every one of those channels is local to the machine, and
    the loudest thing a human could actually see was a tray tooltip. On
    2026-09-03 a slow tick loop did the same to all seven rules at once and the
    machine ran an hour at Tjmax.

    So this leaves the host. The subscriber (Kiroshi's coordinator) records it
    in ``atfield_events``, which its ``status`` surfaces **whether or not any
    worker is running on this host** -- the previous route out went through a
    worker's tuner snapshot, so an idle machine with a dead guard told nobody.

    ``type``/``kind`` is ``guard_health``, and that discriminator is
    load-bearing: the same stream feeds a thermal-hold policy that bans hosts,
    and this event names the same ``rule`` as a kill while meaning the
    opposite. See kiroshi ``tests/test_guard_health_events_never_ban_a_host.py``.

    Best-effort and non-blocking, like every other path in this module: a
    subscriber that is down must never slow the tick loop that also handles
    thermal emergencies.
    """
    webhooks = _webhooks(state_dir)
    if not webhooks:
        return

    payload = {
        "type": "guard_health",
        "kind": "guard_health",
        "host": _HOSTNAME,
        "rule": rule,
        "signal": signal,
        # The action that WOULD have fired and now cannot -- the reason this
        # matters. A "log" rule going inert is a nuisance; a "kill" rule going
        # inert is an unguarded machine.
        "action": action,
        "detail": detail,
        "ts": time.time(),
    }
    _ensure_worker()
    for url in webhooks:
        try:
            _send_queue.put_nowait((url, payload, state_dir))
        except queue.Full:
            _log.debug("reporter queue full; dropping guard_health for %s", url)


# ---------------------------------------------------------------------------
# Periodic host telemetry (0.4.17)
# ---------------------------------------------------------------------------
#
# Everything above leaves the machine only when something HAPPENS. A hub that
# wants to show an IDLE host's temperatures had nothing to show: they reached
# it only inside a worker's heartbeat, and an idle host has no worker. So once
# every ``general.telemetry_interval_s`` this posts ONE compact row to the same
# kind of webhook, typed ``telemetry`` so no consumer reads it as a kill.
#
# ``rule`` is ``atfield.telemetry`` and deliberately NOT ``fleet.*``: Kiroshi's
# hub reads ``fleet.*`` rows as posted ABOUT a host by its liveness probe; this
# row is the host itself speaking, so it is a sign of life from that host.

TELEMETRY_KIND = "telemetry"
TELEMETRY_RULE = "atfield.telemetry"
TELEMETRY_SIGNAL = "host"


def telemetry_signals(samples: dict[str, Any], *, credible: set[str],
                      now_unix: float) -> dict[str, list[float]]:
    """``{signal: [value, unix_ts]}`` for every BELIEVABLE thermal signal and
    every percent-unit reservoir (RAM, commit, swap, VRAM %).

    A reading the tick loop withheld from the rules as unbelievable (a wedged
    NVML session's constant 0.0 C) is withheld here too: the hub would show it
    as the host's temperature. Each value carries the unix time it was READ
    (from its monotonic ``taken_at_ns``), not when this row was built.
    """
    from atfield.signal_class import classify_signal
    from atfield.signals import monotonic_ns

    now_ns = monotonic_ns()
    out: dict[str, list[float]] = {}
    for name, s in samples.items():
        if name not in credible:
            continue
        cls = classify_signal(name)
        if not (cls == "thermal" or (cls == "reservoir" and s.unit == "percent")):
            continue
        read_at = now_unix - max(0, now_ns - int(s.taken_at_ns)) / 1e9
        out[name] = [round(float(s.value), 1), round(read_at, 1)]
    return out


def _newest_webhook(state_dir: Path) -> str | None:
    """The webhook of the most recently written manifest that carries one.

    Kill events go to every registered URL; a once-a-minute ping must not.
    Manifests of long-dead processes stay on disk (DEMETER held 116, naming two
    hubs), and the newest is the hub this host most recently worked for.
    """
    best: tuple[float, str] | None = None
    try:
        for p in state_dir.glob(_MANIFEST_GLOB):
            try:
                url = json.loads(p.read_text(encoding="utf-8")).get(_WEBHOOK_FIELD)
                mtime = p.stat().st_mtime
            except (OSError, ValueError, AttributeError):
                continue
            if url and (best is None or mtime > best[0]):
                best = (mtime, url)
    except OSError:
        _log.debug("failed to glob %s for the telemetry webhook", state_dir, exc_info=True)
    return best[1] if best else None


def report_telemetry(state_dir: Path, samples: dict[str, Any], *, credible: set[str],
                     now_unix: float) -> bool:
    """Queue ONE telemetry row for the newest subscriber. True if queued.

    Non-blocking like every path here: discovery is a glob of a small
    directory, delivery is the background thread.
    """
    url = _newest_webhook(state_dir)
    if not url:
        return False
    payload = {
        "type": TELEMETRY_KIND,
        "kind": TELEMETRY_KIND,
        "host": _HOSTNAME,
        "rule": TELEMETRY_RULE,
        "signal": TELEMETRY_SIGNAL,
        "detail": json.dumps({
            "v": 1, "ts": round(now_unix, 1),
            "signals": telemetry_signals(samples, credible=credible, now_unix=now_unix),
        }, separators=(",", ":"), sort_keys=True),
        "ts": now_unix,
    }
    _ensure_worker()
    try:
        _send_queue.put_nowait((url, payload, state_dir))
    except queue.Full:
        _log.debug("reporter queue full; dropping telemetry for %s", url)
        return False
    return True


class TelemetryPinger:
    """Rate-limits :func:`report_telemetry` to one row per ``interval_s``.

    The FIRST call sends, so a restarted service is visible at once rather than
    a minute later. ``interval_s <= 0`` is off.
    """

    def __init__(self, interval_s: float) -> None:
        self.interval_s = float(interval_s or 0)
        self._last: float | None = None

    def maybe_report(self, state_dir: Path, samples: dict[str, Any], *,
                     credible: set[str], now_unix: float) -> bool:
        if self.interval_s <= 0:
            return False
        if self._last is not None and now_unix - self._last < self.interval_s:
            return False
        self._last = now_unix
        try:
            return report_telemetry(state_dir, samples, credible=credible, now_unix=now_unix)
        except Exception:                                   # noqa: BLE001
            _log.debug("telemetry report failed", exc_info=True)
            return False
