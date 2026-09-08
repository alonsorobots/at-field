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
"""

from __future__ import annotations

import json
import logging
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
_send_queue: "queue.Queue[tuple[str, dict[str, Any]]]" = queue.Queue(maxsize=_QUEUE_MAX)
_worker_started = False
_worker_lock = threading.Lock()


def _delivery_worker() -> None:
    while True:
        url, payload = _send_queue.get()
        try:
            requests.post(url, json=payload, timeout=_POST_TIMEOUT_S)
        except Exception:
            _log.debug("failed to report event to subscriber %s", url, exc_info=True)
        finally:
            _send_queue.task_done()


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
            _send_queue.put_nowait((url, payload))
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
            _send_queue.put_nowait((url, payload))
        except queue.Full:
            _log.debug("reporter queue full; dropping guard_health for %s", url)
