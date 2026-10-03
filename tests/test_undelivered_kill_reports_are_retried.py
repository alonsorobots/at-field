"""A kill / warning report the subscriber missed is retried until delivered (0.4.19).

Until 0.4.19 each event was POSTed once with a 3 s timeout and dropped on failure: a
kill that happened while Kiroshi's coordinator was restarting (or the host was
offline) never reached the steward. Pinned here against a REAL local HTTP server:
  * subscriber down -> spooled on disk -> subscriber up -> delivered exactly once,
    with the original timestamp;
  * the spool is bounded (oldest dropped);
  * telemetry is never spooled (a minute-old temperature is noise, not news);
  * a refusal that retrying cannot fix (4xx) is not retried;
  * a broken spool never raises into the caller;
  * end to end through the background thread: the event arrives after the server comes up.
"""
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from atfield import reporter  # noqa: E402


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Sub:
    """A subscriber that can be started late on a fixed port."""

    def __init__(self, port, status=200):
        self.port, self.status, self.got, self.srv = port, status, [], None

    def up(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.got.append(json.loads(body))
                self.send_response(outer.status)
                self.end_headers()

            def log_message(self, *a):
                pass
        self.srv = HTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def down(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/atfield/event"


@pytest.fixture(autouse=True)
def _fresh_spool_registry(monkeypatch):
    monkeypatch.setattr(reporter, "_spool_dirs", set())


def _kill(ts=1000.0, pid=1):
    return {"type": "kill_report", "rule": "cpu-pkg-hot", "action": "kill", "ts": ts,
            "kill_root": {"pid": pid, "name": "python.exe"}}


def test_down_then_up_delivers_exactly_once_with_the_original_ts(tmp_path):
    sub = Sub(_free_port())
    reporter._deliver(sub.url, _kill(ts=1234.5), tmp_path)          # nobody listening
    assert reporter.spool_size(tmp_path) == 1
    sub.up()
    try:
        assert reporter._drain_spool(tmp_path) == 1
        assert reporter._drain_spool(tmp_path) == 0                  # nothing left to resend
    finally:
        sub.down()
    assert [p["ts"] for p in sub.got] == [1234.5]
    assert reporter.spool_size(tmp_path) == 0


def test_a_server_error_is_retried_but_a_refusal_is_not(tmp_path):
    sub = Sub(_free_port(), status=503).up()
    try:
        reporter._deliver(sub.url, _kill(), tmp_path)
        assert reporter.spool_size(tmp_path) == 1                    # 503: try again later
        sub.status = 401
        reporter._drain_spool(tmp_path)
        assert reporter.spool_size(tmp_path) == 0                    # 401: retrying cannot fix it
    finally:
        sub.down()


def test_the_spool_is_bounded_and_drops_the_oldest(tmp_path, monkeypatch):
    monkeypatch.setattr(reporter, "_SPOOL_MAX", 3)
    sub = Sub(_free_port(), status=503).up()        # fast to fail (a refused port takes 2 s on Windows)
    try:
        for i in range(5):
            reporter._deliver(sub.url, _kill(ts=float(i), pid=i), tmp_path)
    finally:
        sub.down()
    lines = (tmp_path / reporter._SPOOL_NAME).read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["payload"]["ts"] for x in lines] == [2.0, 3.0, 4.0]


def test_telemetry_is_never_spooled(tmp_path):
    url = Sub(_free_port()).url
    reporter._deliver(url, {"type": "telemetry", "kind": "telemetry", "ts": 1.0}, tmp_path)
    assert reporter.spool_size(tmp_path) == 0


def test_a_broken_spool_never_raises(tmp_path):
    bad = tmp_path / "not-a-dir"
    bad.write_text("x")                                              # spool dir is a FILE
    url = Sub(_free_port()).url
    reporter._deliver(url, _kill(), bad)                             # must not raise
    assert reporter._drain_spool(bad) == 0


def test_end_to_end_the_worker_delivers_once_the_subscriber_comes_up(tmp_path, monkeypatch):
    port = _free_port()
    sub = Sub(port)
    client = tmp_path / "clients" / "kiroshi"
    client.mkdir(parents=True)
    (client / "m.json").write_text(json.dumps({"atfield_event_webhook": sub.url}))
    monkeypatch.setattr(reporter, "_cached_at", 0.0)
    monkeypatch.setattr(reporter, "_RETRY_MIN_S", 0.05)
    monkeypatch.setattr(reporter, "_RETRY_MAX_S", 0.05)

    class Action:
        rule_name, signal, threshold, kind, notify = "cpu-pkg-hot", "cpu", 90.0, "kill", False

    class Report:
        kill_root, succeeded, skipped_reason = None, 1, None
    reporter.report_kill(tmp_path, action=Action(), report=Report())
    deadline = time.time() + 5
    while reporter.spool_size(tmp_path) == 0 and time.time() < deadline:
        time.sleep(0.02)
    assert reporter.spool_size(tmp_path) == 1, "the failed POST was not spooled"
    sub.up()
    try:
        deadline = time.time() + 5
        while not sub.got and time.time() < deadline:
            time.sleep(0.02)
        time.sleep(0.3)                                              # a few more retry periods
    finally:
        sub.down()
    assert len(sub.got) == 1 and sub.got[0]["rule"] == "cpu-pkg-hot"
    assert reporter.spool_size(tmp_path) == 0
