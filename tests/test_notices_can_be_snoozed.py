"""Every AT-Field -> agent notice can be SNOOZED or MUTED, and silencing never changes an action (0.4.27).

User, 2026-10-25: "we are trying to stay away from heuristics. so if we add anything, is that
we reach out to warn agents about things the heuristics are seeing. they should be able to
snooze or for time increments or mute for job ... This should be a standard with any
notifications from AT-field to agents." Same day the per-process RSS cap killed a legitimate
35.6-38.2 GB fit four times; the cap is now a WARNING by default ("is it smart to have a HARD
lock on mem?? I wouldn't think so").

Through run_service with the test_owner_is_told harness (real actuator, fake process table,
pipe posts and webhook payloads captured). Pins:
  * a snoozed session is not posted to until the snooze expires, then it is;
  * a job mute silences that job's worker notices and nobody else's;
  * every pipe message ends with the exact command that silences that owner for that rule;
  * a suppressed KILL still kills and is still in events.jsonl, marked suppressed;
  * the cap in its default (warn) mode kills nothing and tells the owner;
  * prefs persist across a restart; a live drill is delivered, then suppressed once snoozed.
"""
import json

import pytest

import atfield.service as svc
from atfield import agent_inbox as ai
from atfield import notify_prefs as np_
from atfield.actuator import RSS_CAP_RULE
from atfield.http_api import ServiceState
from tests.test_owner_is_told import (KILL, SA, SB, WORKER, procs_both, procs_incident, rule,
                                      run)
from tests.test_owner_is_told import every_warning_needs_action  # noqa: F401 -- autouse
from tests.test_rss_cap_owner_is_told import procs_session_hog

T0 = 1_800_000_000.0
WARN0 = rule("cpu-pkg-warm", 86.0, "log", notify=True, cooldown_s=0)



@pytest.fixture
def clock(monkeypatch):
    t = [T0]
    monkeypatch.setattr(np_, "_now", lambda: t[0])
    return t


def prefs_at(tmp_path):
    return np_.NotifyPrefs(tmp_path / "state" / np_.FILENAME)


def kill_reports(events):
    return [json.loads(line) for line in events.splitlines()
            if line.strip() and json.loads(line).get("type") == "kill_report"]


def told(payload, owner_id):
    (o,) = [o for o in payload["owners"] if o["id"] == owner_id]
    return o


def test_a_snoozed_session_is_not_posted_to_until_expiry_then_is(tmp_path, monkeypatch, clock):
    prefs_at(tmp_path).snooze(session="aaaaaaaa-1111", minutes=1, rule="cpu-pkg-warm",
                              by="session aaaaaaaa")

    def value(tick):                       # every tick is 5 s later on the prefs clock
        clock[0] = T0 + 5.0 * tick
        return 87.0
    posts, payloads, events = run(tmp_path, monkeypatch, procs_incident(), value, WARN0,
                                  ticks=30, limit_s=0)
    states = [("posted" if told(p, "aaaaaaaa")["notified"] else
               "snoozed" if (told(p, "aaaaaaaa").get("suppressed") or {}).get("by") == "snooze"
               else "other") for p in payloads]
    assert "snoozed" in states and "posted" in states and "other" not in states, states
    first_post = states.index("posted")
    assert set(states[:first_post]) == {"snoozed"} and set(states[first_post:]) == {"posted"}
    assert len(posts) == states.count("posted")
    sup = told(payloads[0], "aaaaaaaa")["suppressed"]
    assert sup == {"by": "snooze", "until": T0 + 60.0, "set_by": "session aaaaaaaa",
                   "set_at": T0, "rule": "cpu-pkg-warm"}
    assert '"suppressed"' in events


def test_a_snooze_for_another_rule_or_session_does_not_silence(tmp_path, monkeypatch, clock):
    p = prefs_at(tmp_path)
    p.snooze(session="aaaaaaaa", minutes=60, rule="cpu-pkg-hot")
    p.snooze(session="bbbbbbbb", minutes=60)
    posts, _, _ = run(tmp_path, monkeypatch, procs_incident(), 87.0, WARN0)
    assert [sid for sid, _ in posts] == ["aaaaaaaa-1111"]


def test_a_job_mute_silences_that_jobs_worker_notices_only(tmp_path, monkeypatch, clock):
    prefs_at(tmp_path).mute(job="handgap-v2", by="steward")
    # worker ahead: its notice is suppressed by the mute
    _, payloads, _ = run(tmp_path, monkeypatch, procs_both(200.0, 300.0), 87.0, WARN0,
                         manifest=WORKER)
    w = told(payloads[0], "DEMETER-4c030c")
    assert w["notified"] is False and w["suppressed"]["by"] == "mute"
    assert w["suppressed"]["until"] is None


def test_a_job_mute_leaves_the_session_told(tmp_path, monkeypatch, clock):
    prefs_at(tmp_path).mute(job="handgap-v2")
    posts, payloads, _ = run(tmp_path, monkeypatch, procs_both(100.0, 600.0), 87.0, WARN0,
                             manifest=WORKER)
    assert [sid for sid, _ in posts] == ["aaaaaaaa-1111"]
    assert "suppressed" not in told(payloads[0], "aaaaaaaa")


def test_a_mute_for_another_job_does_not_silence_the_worker(tmp_path, monkeypatch, clock):
    prefs_at(tmp_path).mute(job="some-other-job")
    _, payloads, _ = run(tmp_path, monkeypatch, procs_both(200.0, 300.0), 87.0, WARN0,
                         manifest=WORKER)
    w = told(payloads[0], "DEMETER-4c030c")
    assert w["notified"] is True and "suppressed" not in w


def test_every_message_ends_with_the_exact_silence_command(tmp_path, monkeypatch):
    posts, payloads, _ = run(tmp_path, monkeypatch, procs_incident(), 87.0, WARN0)
    line = (f"To silence (on {ai._HOST}): atf snooze --session aaaaaaaa --rule cpu-pkg-warm "
            "--minutes 60 (or 15 / 240; drop --rule for every rule).")
    (text,) = [t for _, t in posts]
    assert text.endswith("\n" + line)
    assert told(payloads[0], "aaaaaaaa")["silence"] == line


def test_a_workers_payload_carries_the_job_silence_command(tmp_path, monkeypatch):
    _, payloads, _ = run(tmp_path, monkeypatch, procs_both(200.0, 300.0), 87.0, WARN0,
                         manifest=WORKER)
    assert told(payloads[0], "DEMETER-4c030c")["silence"] == (
        f"To silence (on {ai._HOST}): atf snooze --job handgap-v2 --rule cpu-pkg-warm "
        "--minutes 60 (or 15 / 240; drop --rule for every rule), or atf mute --job "
        "handgap-v2 --rule cpu-pkg-warm until the job ends.")


def test_a_suppressed_kill_still_kills_and_is_recorded_suppressed(tmp_path, monkeypatch, clock):
    prefs_at(tmp_path).snooze(session="bbbbbbbb", minutes=60, by="session bbbbbbbb")
    procs = procs_incident()
    for p in procs:                        # SB is the busy one: its root gets killed
        p.cpu = 500.0 if p.env is SB else 0.0
    prov = []
    posts, payloads, events = run(tmp_path, monkeypatch, procs, 95.0, KILL, provider_out=prov)
    assert posts == []
    (p,) = payloads
    assert p["succeeded"] is True and p["action"] == "kill"
    assert prov[0].killed or prov[0].terminated, "silencing changed the action"
    (o,) = p["owners"]
    assert o["notified"] is False and o["suppressed"]["by"] == "snooze"
    (k,) = kill_reports(events)
    assert k["killed"] and k["succeeded"] is True
    assert k["owners"][0]["suppressed"]["set_by"] == "session bbbbbbbb"


def test_the_cap_by_default_warns_the_owner_and_kills_nothing(tmp_path, monkeypatch):
    prov = []
    posts, payloads, events = run(tmp_path, monkeypatch, procs_session_hog(), 40.0,
                                  "[kill]\nmax_process_rss_gb = 32.0", provider_out=prov)
    (p,) = [p for p in payloads if p["rule"] == RSS_CAP_RULE]
    assert p["action"] == "log" and p["notify"] is True and p["kill_root"] is None
    assert prov[0].killed == [] and prov[0].terminated == []
    assert all(pr.alive for pr in prov[0].procs.values())
    assert [sid for sid, _ in posts] == ["aaaaaaaa-1111"]
    text = posts[0][1]
    assert "pid 100" in text and "38.0 GB" in text and "32.0 GB" in text
    assert "Nothing was killed" in text and "--rule process-rss-cap" in text
    (k,) = [k for k in kill_reports(events) if k["rule"] == RSS_CAP_RULE]
    assert k["killed"] == [] and k["owners"][0]["notified"] is True


def test_a_snoozed_cap_warning_is_still_recorded(tmp_path, monkeypatch, clock):
    prefs_at(tmp_path).snooze(session="aaaaaaaa", minutes=240, rule=RSS_CAP_RULE)
    posts, payloads, _ = run(tmp_path, monkeypatch, procs_session_hog(), 40.0,
                             "[kill]\nmax_process_rss_gb = 32.0")
    assert posts == []
    (p,) = [p for p in payloads if p["rule"] == RSS_CAP_RULE]
    assert p["owners"][0]["suppressed"]["rule"] == RSS_CAP_RULE


def test_prefs_survive_a_restart(tmp_path, monkeypatch, clock):
    prefs_at(tmp_path).snooze(session="aaaaaaaa", minutes=60)
    for _ in range(2):                     # two service lifetimes over the same state dir
        posts, payloads, _ = run(tmp_path, monkeypatch, procs_incident(), 87.0, WARN0)
        assert posts == [] and told(payloads[0], "aaaaaaaa")["suppressed"]["by"] == "snooze"
    assert [p["kind"] for p in prefs_at(tmp_path).snapshot()] == ["snooze"]


def test_a_live_drill_is_delivered_then_suppressed_once_snoozed(tmp_path, monkeypatch, clock):
    queued = [[100], [], [], [], [100]]   # drill on tick 1 and tick 5

    def consume(self):
        if len(queued) == 2:               # between the drills, the agent snoozes itself
            self.notify_prefs.snooze(session="aaaaaaaa", minutes=15, rule="notify-drill")
        return queued.pop(0) if queued else []
    monkeypatch.setattr(ServiceState, "consume_drills", consume)
    posts, payloads, events = run(tmp_path, monkeypatch, procs_incident(), 20.0, WARN0)
    assert payloads == []                  # a drill never reaches the webhook
    assert [sid for sid, _ in posts] == ["aaaaaaaa-1111"]
    assert "TEST notice" in posts[0][1]
    drills = [k for k in kill_reports(events) if k["rule"] == "notify-drill"]
    assert [(d["owners"][0]["notified"], "suppressed" in d["owners"][0]) for d in drills] == [
        (True, False), (False, True)]
    assert all(d["killed"] == [] for d in drills)


# -- the store itself ---------------------------------------------------------------


def test_a_job_mute_ends_an_hour_after_the_job_leaves_the_host(tmp_path, clock):
    p = prefs_at(tmp_path)
    p.mute(job="J")
    clock[0] += 3000
    p.observe_jobs({"J"})                  # still running: last seen moves up
    clock[0] += 3000
    assert [x["kind"] for x in p.snapshot()] == ["mute"]
    clock[0] += 700                        # 3700 s since last seen
    assert p.snapshot() == []


def test_unmute_and_validation(tmp_path, clock):
    p = prefs_at(tmp_path)
    p.snooze(job="J", minutes=15, rule="r1")
    p.mute(job="J")
    p.snooze(session="aaaaaaaa", minutes=15)
    assert p.unmute(job="J", rule="r1") == 1
    assert p.unmute(job="J") == 1
    assert [x["scope"] for x in prefs_at(tmp_path).snapshot()] == ["session"]
    for bad in (dict(minutes=0), dict(minutes=-5), dict(minutes="60"), dict(minutes=1e9)):
        with pytest.raises(ValueError):
            p.snooze(session="aaaaaaaa", **bad)
    with pytest.raises(ValueError):
        p.snooze(session="aaaa", minutes=5)        # too short to name one session
    with pytest.raises(ValueError):
        p.snooze(session="aaaaaaaa", job="J", minutes=5)
    with pytest.raises(ValueError):
        p.mute(job="")


def test_a_corrupt_prefs_file_starts_empty(tmp_path):
    f = tmp_path / "state" / np_.FILENAME
    f.parent.mkdir(parents=True)
    f.write_text("{not json", encoding="utf-8")
    assert np_.NotifyPrefs(f).snapshot() == []


# -- the localhost API ---------------------------------------------------------------


def test_the_api_snoozes_mutes_lists_unmutes_and_queues_a_drill(tmp_path, clock):
    from atfield.http_api import ApiServer
    from tests.test_http_api import _get, _make_state, _post
    state = _make_state(tmp_path)
    state.notify_prefs = np_.NotifyPrefs(tmp_path / np_.FILENAME)
    srv = ApiServer(state, host="127.0.0.1", port=0)
    srv.start()
    h, port = srv.address
    try:
        st, out = _post(h, port, "/notify/snooze",
                        {"session": "aaaaaaaa", "minutes": 15, "rule": "cpu-pkg-warm", "by": "me"})
        assert st == 200 and out["snoozed"]["until"] == T0 + 900
        st, out = _post(h, port, "/notify/mute", {"job": "J"})
        assert st == 200 and out["muted"]["kind"] == "mute"
        st, out = _get(h, port, "/notify/prefs")
        assert st == 200 and sorted(p["kind"] for p in out["prefs"]) == ["mute", "snooze"]
        assert (st := _post(h, port, "/notify/mute", {"session": "aaaaaaaa"})[0]) == 400
        assert _post(h, port, "/notify/snooze", {"session": "aaaaaaaa", "minutes": 0})[0] == 400
        assert _post(h, port, "/notify/snooze", {"minutes": 5})[0] == 400
        assert _post(h, port, "/notify/unmute", {"job": "J"}) == (200, {"removed": 1})
        assert _post(h, port, "/notify/drill", {"pid": "x"})[0] == 400
        assert _post(h, port, "/notify/drill", {"pid": 4242}) == (200, {"drill_queued": 4242})
        assert state.consume_drills() == [4242] and state.consume_drills() == []
        # persisted: a fresh store over the same file sees the snooze
        assert [p["kind"] for p in np_.NotifyPrefs(tmp_path / np_.FILENAME).snapshot()] == ["snooze"]
    finally:
        srv.stop()


def test_the_cap_action_defaults_to_warn_and_kill_is_an_explicit_opt_in():
    from atfield.config import ConfigError, default_config, load_config_from_dict
    assert default_config().kill.max_process_rss_action == "warn"
    assert load_config_from_dict({"kill": {"max_process_rss_action": "kill"}}
                                 ).kill.max_process_rss_action == "kill"
    with pytest.raises(ConfigError, match="max_process_rss_action"):
        load_config_from_dict({"kill": {"max_process_rss_action": "terminate"}})


# -- review 2026-10-25 (Fable, 0.4.28) -------------------------------------------------


def test_a_cap_warning_right_after_a_thermal_warning_is_still_told(tmp_path, monkeypatch):
    """Review F1: the cap warning shared the session's 10-min WARNING budget, so a session
    told about cpu-pkg-warm was then told nothing about its 38 GB process for 30 min. The
    cap has its own per-process limit (30 min) and bypasses the session budget."""
    import atfield.actuator as act_mod
    monkeypatch.setattr(act_mod, "_RSS_CAP_MIN_INTERVAL_S", 0.0)
    prov = []

    def value(tick):
        if tick == 10 and prov:                    # the fit balloons AFTER the warm notice
            prov[0].procs[100].rss = 38 * 1024 ** 3
        return 87.0
    posts, _, _ = run(tmp_path, monkeypatch, procs_incident(), value, WARN0,
                      "[kill]\nmax_process_rss_gb = 32.0", provider_out=prov, ticks=15)
    assert [("process-rss-cap" in t) for _, t in posts] == [False, True]


def test_a_suppressed_notice_spends_no_rate_limit_budget(tmp_path, monkeypatch, clock):
    """Review F3: with the real 10-min limit on, notices suppressed by a 1-min snooze must
    not use up the budget -- the first one after expiry is posted."""
    prefs_at(tmp_path).snooze(session="aaaaaaaa", minutes=1, rule="cpu-pkg-warm")

    def value(tick):
        clock[0] = T0 + 5.0 * tick
        return 87.0
    posts, payloads, _ = run(tmp_path, monkeypatch, procs_incident(), value, WARN0, ticks=30)
    assert any(told(p, "aaaaaaaa").get("suppressed") for p in payloads)
    assert len(posts) == 1


def test_a_job_mute_holds_while_the_job_is_still_on_the_host(tmp_path, monkeypatch, clock):
    """Review F2: the tick loop must refresh a mute from the LIVE worker manifests; without
    that every job mute lapses an hour after it was set, job running or not."""
    prefs_at(tmp_path).mute(job="handgap-v2")      # set at T0

    def value(tick):                               # observed alive at T0+3500, read at T0+3700
        clock[0] = T0 + (3500.0 if tick < 3 else 3700.0)
        return 87.0
    _, payloads, _ = run(tmp_path, monkeypatch, procs_both(200.0, 300.0), value, WARN0,
                         manifest=WORKER)
    late = told(payloads[-1], "DEMETER-4c030c")
    assert late["notified"] is False and late["suppressed"]["by"] == "mute"


def test_the_cap_warning_names_the_exact_owner_not_a_ranking(tmp_path, monkeypatch):
    """Review F4: the warn path carries the process's own owner; it must not fall back to
    ranking the machine's memory users (which could name another session)."""
    from atfield.actuator import Actuator
    monkeypatch.setattr(Actuator, "load_owners", lambda *a, **kw: [])
    posts, _, _ = run(tmp_path, monkeypatch, procs_session_hog(), 40.0,
                      "[kill]\nmax_process_rss_gb = 32.0")
    assert [sid for sid, _ in posts] == ["aaaaaaaa-1111"]
