"""The per-process RSS cap kill tells the OWNER too, like every other kill (0.4.25).

2026-10-25: process-rss-cap killed a 35.6-38.2 GB P-spline fit four times on Chronos and
DEMETER (pst50multv14/x100/global). The rule-dispatch kill path posts to the owning
session / names the owning Kiroshi worker (agent_inbox.notify); the cap path did not, so
the job saw only "child failed rc=15", the steward called it "numerically fragile" and
burned every retry, and the cause was found by reading service.stderr.log by hand.
Through run_service itself, same harness as test_owner_is_told.
"""
import json

from atfield.actuator import RSS_CAP_RULE
from tests.test_actuator import _FakeProc
from tests.test_owner_is_told import SA, SB, TOKEN, WORKER, run

GB = 1024 ** 3
CAP = "[kill]\nmax_process_rss_gb = 32.0"


def procs_session_hog():
    return [_FakeProc(10, 0, "claude.exe"), _FakeProc(11, 10, "bash.exe"),
            _FakeProc(100, 11, "python.exe", cmdline=("python.exe", "pspline_unit_cli.py"),
                      rss=38 * GB, env=SA),
            _FakeProc(200, 11, "python.exe", rss=2 * GB, env=SB)]


def procs_worker_hog():
    return [_FakeProc(10, 0, "claude.exe"), _FakeProc(11, 10, "bash.exe"),
            _FakeProc(500, 11, "pythonw.exe", env=SA),
            _FakeProc(501, 500, "python.exe", rss=3 * GB, env=SA),
            _FakeProc(502, 501, "python.exe", cmdline=("python.exe", "pspline_unit_cli.py"),
                      rss=38 * GB, env=SA)]


def test_a_cap_kill_tells_the_owning_session_why(tmp_path, monkeypatch):
    posts, payloads, events = run(tmp_path, monkeypatch, procs_session_hog(), 40.0, CAP)
    caps = [p for p in payloads if p["rule"] == RSS_CAP_RULE]
    assert len(caps) == 1 and caps[0]["kill_root"]["pid"] == 100
    assert [sid for sid, _ in posts] == ["aaaaaaaa-1111"]
    text = posts[0][1]
    # the WHY, in the units a reader acts on -- not a raw byte count and a "cool down" ask
    assert "38.0 GB" in text and "32.0 GB" in text and "max_process_rss_gb" in text
    assert "cools" not in text
    assert caps[0]["owners"] == [{"kind": "claude_session", "id": "aaaaaaaa", "share": None,
                                  "pids": 1, "notified": True}]
    assert TOKEN not in events and TOKEN not in json.dumps(payloads)


def test_a_cap_kill_inside_a_worker_names_the_worker(tmp_path, monkeypatch):
    posts, payloads, _ = run(tmp_path, monkeypatch, procs_worker_hog(), 40.0, CAP,
                             manifest=WORKER)
    (p,) = [p for p in payloads if p["rule"] == RSS_CAP_RULE]
    assert p["kill_root"]["pid"] == 502
    assert [(o["kind"], o["id"], o.get("job")) for o in p["owners"]] == [
        ("kiroshi_worker", "DEMETER-4c030c", "handgap-v2")]
    assert posts == []          # the worker's steward hears it via the webhook, not a pipe
