"""Through run_service itself: a firing reaches the session that OWNS the load (0.4.21).

The actuator is real; only the process table is fake (tests/test_actuator.FakeProvider)
and nothing real is killed. Pipe posts are recorded instead of written; the webhook
payload is captured from the real reporter. Pins: exactly one post to the owning session,
none to the idle one; one post per session across rules; a worker that owns the load is
named in the payload and gets no pipe post; a kill tells only the killed root's owner,
captured BEFORE the kill; the token is nowhere it could persist.
"""
import json
import logging

import pytest

import atfield.service as svc
from atfield import agent_inbox as ai
from atfield import reporter
from atfield.actuator import Actuator
from atfield.collectors import HealthState, ProbeResult
from atfield.signals import Sample, monotonic_ns
from tests.test_actuator import FakeProvider, _FakeProc

CPU = "system.cpu_package_temp_c"
TOKEN = "tok-SECRET-4419"


def env(sock, sid):
    return {"CLAUDE_CODE_MESSAGING_SOCKET": sock, "CLAUDE_CODE_MESSAGING_TOKEN": TOKEN,
            "CLAUDE_CODE_SESSION_ID": sid}


SA = env(r"\\.\pipe\LOCAL\cc-msg-a", "aaaaaaaa-1111")
SB = env(r"\\.\pipe\LOCAL\cc-msg-b", "bbbbbbbb-2222")


def rule(name, threshold, action, **kw):
    lines = [f'[[rules]]', f'name = "{name}"', f'signal = "{CPU}"', f"threshold = {threshold}",
             "window_s = 1", "min_fraction_over = 0.5", f'action = "{action}"']
    lines += [f"{k} = {json.dumps(v)}" for k, v in kw.items()]
    return "\n".join(lines)


KILL = rule("cpu-pkg-hot", 90.0, "kill")
WARN = rule("cpu-pkg-warm", 86.0, "log", notify=True)
WARN2 = rule("cpu-pkg-warmer", 85.0, "log", notify=True)


def procs_incident():
    out = [_FakeProc(10, 0, "claude.exe"), _FakeProc(11, 10, "bash.exe")]
    out += [_FakeProc(100 + i, 11, "python.exe", cpu=87.5, env=SA) for i in range(8)]
    out += [_FakeProc(200 + i, 11, "python.exe", cpu=0.0, env=SB) for i in range(2)]
    out += [_FakeProc(300 + i, 0, "python.exe", cpu=0.0) for i in range(2)]
    return out


def procs_both(w_each, sa):
    """Worker 500 (manifest) launched from SA's shell: its slots inherit SA's env."""
    out = [_FakeProc(10, 0, "claude.exe"), _FakeProc(11, 10, "bash.exe"),
           _FakeProc(500, 11, "pythonw.exe", env=SA)]
    out += [_FakeProc(501 + i, 500, "python.exe", cpu=w_each, env=SA) for i in range(3)]
    out += [_FakeProc(100, 11, "python.exe", cpu=sa, env=SA), _FakeProc(200, 11, "python.exe", env=SB)]
    return out


WORKER = {"pid": 500, "role": "worker", "name": "kiroshi", "atfield_never_kill": True,
          "worker_id": "DEMETER-4c030c", "slots": 22,
          "launch_command": "kiroshi join --task t:run --slots 22 --job handgap-v2"}


def run(tmp_path, monkeypatch, procs, value, *rules, manifest=None):
    """One run_service pass; returns (posts, webhook payloads, events.jsonl text)."""
    state = tmp_path / "state"
    if manifest:
        d = state / "clients" / "kiroshi"
        d.mkdir(parents=True)
        (d / "worker-500.json").write_text(json.dumps(manifest), encoding="utf-8")
    provider = FakeProvider.from_procs(procs)

    class Hot:
        name = "fake"

        def probe(self):
            return ProbeResult(available=True, reason="fake", signals=(CPU,))

        def health(self):
            return HealthState.HEALTHY

        def sample(self):
            return {CPU: Sample(value, monotonic_ns(), self.name, "celsius")}

    posts, sent = [], []
    c = Hot()
    monkeypatch.setattr(svc, "_probe_all_collectors", lambda audit: ([c], {c.name: c.probe()}))
    monkeypatch.setattr(svc, "Actuator", lambda cfg: Actuator(cfg, provider=provider,
                                                              sleep=lambda _s: None))
    monkeypatch.setattr(ai, "post", lambda inbox, text, **kw: posts.append((inbox.session_id, text)))
    monkeypatch.setattr(ai, "_LIMIT", ai.RateLimit(600))
    monkeypatch.setattr(reporter, "_webhooks", lambda sd: ["http://x/atfield/event"])
    monkeypatch.setattr(reporter, "_ensure_worker", lambda: None)

    class Q:
        def put_nowait(self, item):
            sent.append(item[1])
    monkeypatch.setattr(reporter, "_send_queue", Q())
    cfg = tmp_path / "config.toml"
    cfg.write_text(f"[general]\ntick_hz = 10\nstate_dir = {json.dumps(str(state))}\n\n"
                   "[api]\nenabled = false\n\n" + "\n\n".join(rules), encoding="utf-8")
    svc.run_service(config_path=cfg, state_dir=state, max_ticks=15)
    events = (state / "events.jsonl").read_text(encoding="utf-8")
    return posts, [p for p in sent if p.get("type") == "kill_report"], events


def owners_of(payloads, rule_name):
    return [[(o["kind"], o["id"], o["notified"]) for o in p["owners"]]
            for p in payloads if p["rule"] == rule_name]


def test_a_warning_tells_exactly_the_owning_session(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    posts, payloads, events = run(tmp_path, monkeypatch, procs_incident(), 87.0, KILL, WARN)
    assert [sid for sid, _ in posts] == ["aaaaaaaa-1111"]
    text = posts[0][1]
    assert "cpu-pkg-warm" in text and "100%" in text and "8 processes" in text
    assert owners_of(payloads, "cpu-pkg-warm") == [[("claude_session", "aaaaaaaa", True),
                                                   ("claude_session", "bbbbbbbb", False),
                                                   ("unattributed", "-", False)]]
    for blob in (events, json.dumps(payloads), caplog.text):
        assert TOKEN not in blob
    assert '"owners"' in events


def test_two_rules_firing_together_post_once(tmp_path, monkeypatch):
    posts, payloads, _ = run(tmp_path, monkeypatch, procs_incident(), 87.0, WARN, WARN2)
    assert sorted(p["rule"] for p in payloads) == ["cpu-pkg-warm", "cpu-pkg-warmer"]
    assert [sid for sid, _ in posts] == ["aaaaaaaa-1111"]


def test_both_case_session_ahead_is_told(tmp_path, monkeypatch):
    posts, payloads, _ = run(tmp_path, monkeypatch, procs_both(100.0, 600.0), 87.0, WARN,
                             manifest=WORKER)
    assert [sid for sid, _ in posts] == ["aaaaaaaa-1111"]
    assert owners_of(payloads, "cpu-pkg-warm") == [[("claude_session", "aaaaaaaa", True),
                                                   ("kiroshi_worker", "DEMETER-4c030c", False),
                                                   ("claude_session", "bbbbbbbb", False)]]


def test_both_case_flip_worker_ahead_gets_no_pipe_post(tmp_path, monkeypatch):
    posts, payloads, _ = run(tmp_path, monkeypatch, procs_both(200.0, 300.0), 87.0, WARN,
                             manifest=WORKER)
    assert posts == []
    (p,) = [p for p in payloads if p["rule"] == "cpu-pkg-warm"]
    assert p["owners"][0] == {"kind": "kiroshi_worker", "id": "DEMETER-4c030c",
                              "job": "handgap-v2", "slots": 22, "share": 0.667, "pids": 3,
                              "notified": True}
    assert [o["id"] for o in p["owners"]] == ["DEMETER-4c030c", "aaaaaaaa", "bbbbbbbb"]


def test_a_kill_tells_the_killed_roots_session(tmp_path, monkeypatch):
    procs = procs_incident()
    for p in procs:                       # SB is now the busy one; SA idle
        p.cpu = 500.0 if p.env is SB else 0.0
    posts, payloads, _ = run(tmp_path, monkeypatch, procs, 95.0, KILL)
    assert [sid for sid, _ in posts] == ["bbbbbbbb-2222"]
    assert "killed" in posts[0][1]
    (p,) = payloads
    assert p["owners"] == [{"kind": "claude_session", "id": "bbbbbbbb", "share": None,
                            "pids": 1, "notified": True}]


def test_a_killed_worker_slot_is_named_and_gets_no_pipe_post(tmp_path, monkeypatch):
    posts, payloads, _ = run(tmp_path, monkeypatch, procs_both(200.0, 0.0), 95.0, KILL,
                             manifest=WORKER)
    assert posts == []
    (p,) = payloads
    assert p["kill_root"]["pid"] in (501, 502, 503)
    assert [(o["kind"], o["id"], o.get("job")) for o in p["owners"]] == [
        ("kiroshi_worker", "DEMETER-4c030c", "handgap-v2")]
