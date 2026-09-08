"""A guard that stops guarding must tell someone OFF this machine.

On 2026-09-02 both GPU core-temp rules went inert and stayed inert for 4.2
days. Detection was never the problem. `SIGNAL LOST` was logged at ERROR
within 10.6 seconds, written to events.jsonl, and counted in /health.

Every one of those channels is local to the host. The loudest thing a human
could actually see was a tray tooltip they had to hover. The only route out
went through a Kiroshi worker's tuner snapshot -- so an idle machine with a
dead guard told nobody at all, which is precisely the state a host sits in
between jobs.

This pins that the notice is pushed to the registered subscriber, whose
`status` shows it with no worker running.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from atfield import reporter  # noqa: E402


def _subscribe(tmp_path: Path, url: str = "http://coordinator/atfield/event") -> Path:
    """Register a subscriber the way Kiroshi's processreg.py does: a manifest
    under clients/<name>/ carrying the webhook field."""
    d = tmp_path / "clients" / "kiroshi"
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(
        json.dumps({"name": "kiroshi", reporter._WEBHOOK_FIELD: url}), encoding="utf-8")
    return tmp_path


def _sent(monkeypatch, calls=None) -> list:
    out = []
    # The URL list is cached for _WEBHOOK_CACHE_TTL_S; clear it so each test
    # discovers its own tmp_path rather than a neighbour's.
    monkeypatch.setattr(reporter, "_cached_at", 0.0, raising=False)
    monkeypatch.setattr(reporter, "_cached_webhooks", [], raising=False)
    monkeypatch.setattr(reporter, "_ensure_worker",
                        lambda: (calls.append(1) if calls is not None else None))
    monkeypatch.setattr(reporter._send_queue, "put_nowait", lambda item: out.append(item))
    return out


def test_an_inert_guard_is_pushed_to_the_subscriber(tmp_path, monkeypatch):
    sent = _sent(monkeypatch)
    reporter.report_guard_health(
        _subscribe(tmp_path), rule="gpu-core-hot[gpu.0.core_temp_c]",
        signal="gpu.0.core_temp_c", action="kill",
        detail="signal silent for 63106s")
    assert sent, "an inert kill rule notified nobody outside this machine"
    _url, payload = sent[0]
    assert payload["signal"] == "gpu.0.core_temp_c"
    assert payload["action"] == "kill"
    assert "63106" in payload["detail"]
    assert payload["host"]


def test_the_event_is_TYPED_so_it_cannot_be_read_as_a_kill(tmp_path, monkeypatch):
    """Load-bearing. The subscriber feeds this stream to a thermal-hold policy
    that BANS HOSTS, and this payload names the same `rule` as a kill while
    meaning the opposite -- the guard is not acting and the host may be cold.
    Without the discriminator, notices that a machine is unprotected would
    park it."""
    sent = _sent(monkeypatch)
    reporter.report_guard_health(
        _subscribe(tmp_path), rule="gpu-core-hot[gpu.0.core_temp_c]",
        signal="gpu.0.core_temp_c", action="kill", detail="x")
    payload = sent[0][1]
    assert payload["kind"] == "guard_health"
    assert payload["type"] == "guard_health"


def test_no_subscriber_means_no_work_and_no_error(tmp_path, monkeypatch):
    """The refusing direction. Most machines have no subscriber registered,
    and this runs inside the tick loop that also handles thermal emergencies.

    Asserts the delivery THREAD is never started, not merely that nothing was
    queued -- an empty send loop already yields an empty queue, so checking
    only that could not tell the early return from its absence. Caught by
    mutation.
    """
    started: list = []
    sent = _sent(monkeypatch, calls=started)
    reporter.report_guard_health(
        tmp_path, rule="r", signal="s", action="kill", detail="d")
    assert sent == []
    assert started == [], "spun up a delivery thread with nobody to deliver to"
