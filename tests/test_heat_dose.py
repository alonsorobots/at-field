"""The CPU heat rule is a DOSE, not a window (0.4.33).

User 2026-11-04: "usually processes have a spike in heat and I THINK that should be ok just
as long as it's not sustained ... the amount of time depends on the heat? like total heat
aggregated since it entered the dangerous stage?" The 30-s / 67% window killed a CPU at
92.5 C as fast as one at 96 C, and forgot everything after 30 s. A dose is an
inverse-time breaker: degree-seconds over the line build up, drain below it, and the rule
fires at a budget -- 300 at 92 C = ~5 min at 93 C, ~100 s at 95 C, a spike never.

Replayed on 91 h of Chronos forensics (2026-10-31..11-04): the window rule fired once
(11-04 01:37:36, the real kill); the dose peaked at 68 of 300 on that same event.
"""
import math

import pytest

from atfield import agent_inbox
from atfield.actuator import KilledProcess, KillReport, ProcInfo
from atfield.agent_inbox import Owner
from atfield.config import ConfigError, default_config, load_config, load_config_from_dict
from atfield.config_writer import _render_default_config
from atfield.policy import PolicyEngine
from atfield.signals import Sample

NS = 1_000_000_000
SIG = "system.cpu_package_temp_c"
HOT = {"name": "cpu-pkg-hot", "signal": SIG, "threshold": 92.0, "window_s": 30,
       "action": "kill", "dose_budget": 300.0}
WARM = {"name": "cpu-pkg-warm", "signal": SIG, "threshold": 86.0, "window_s": 30,
        "min_fraction_over": 0.67, "action": "log", "notify": True, "cooldown_s": 600}


def _cfg(*rules):
    return load_config_from_dict({"rules": [dict(r) for r in rules]})


def _run(temps, *rules, start=0):
    """Feed one reading per second; return [(second, action)]."""
    eng = PolicyEngine(_cfg(*(rules or (HOT,))), available_signals={SIG})
    out = []
    for i, v in enumerate(temps):
        t = start + i
        out += [(t, a) for a in eng.tick({SIG: Sample(v, t * NS, "test", "celsius")},
                                         now_ns=t * NS)]
    return eng, out


def kills(acts):
    return [t for t, a in acts if a.kind == "kill"]


def warnings(acts):
    return [(t, a) for t, a in acts if a.kind == "log" and a.notify]


# -- config ---------------------------------------------------------------------------

def test_a_dose_rule_needs_no_min_fraction_and_round_trips(tmp_path):
    r = _cfg(HOT).rules[0]
    assert (r.dose_budget, r.dose_drain_per_s, r.dose_warn_fraction) == (300.0, 5.0, 0.5)
    p = tmp_path / "config.toml"
    p.write_text(_render_default_config(_cfg(HOT, WARM)), encoding="utf-8")
    back = {x.name: x for x in load_config(p).rules}
    assert back["cpu-pkg-hot"].dose_budget == 300.0 and back["cpu-pkg-warm"].dose_budget is None


@pytest.mark.parametrize("bad, msg", [
    ({"dose_budget": 0}, "dose_budget must be > 0"),
    ({"dose_drain_per_s": -1}, "dose_drain_per_s must be >= 0"),
    ({"dose_warn_fraction": 1.0}, r"dose_warn_fraction must be in \(0, 1\)"),
])
def test_bad_dose_values_are_refused(bad, msg):
    with pytest.raises(ConfigError, match=msg):
        _cfg({**HOT, **bad})


def test_a_window_rule_still_needs_min_fraction_and_dose_options_need_a_budget():
    no_dose = {k: v for k, v in HOT.items() if k != "dose_budget"}
    with pytest.raises(ConfigError, match="min_fraction_over"):
        _cfg(no_dose)
    with pytest.raises(ConfigError, match="without dose_budget"):
        _cfg({**no_dose, "min_fraction_over": 0.67, "dose_drain_per_s": 2.0})


def test_the_default_cpu_rule_is_a_dose_rule():
    hot = {r.name: r for r in default_config().rules}["cpu-pkg-hot"]
    assert hot.threshold == 92.0 and hot.dose_budget == 300.0


# -- the curve --------------------------------------------------------------------------

def test_a_spike_never_kills():
    # 30 s at 95 C (dose 90), then back to 85 C for ten minutes.
    eng, acts = _run([85.0] * 5 + [95.0] * 30 + [85.0] * 600)
    assert kills(acts) == [] and warnings(acts) == []
    assert eng.stats_snapshot()["cpu-pkg-hot"]["dose"] == 0.0


@pytest.mark.parametrize("temp, kill_at", [(95.0, 100), (93.0, 300), (96.0, 75)])
def test_time_to_kill_falls_as_the_heat_rises(temp, kill_at):
    _, acts = _run([temp] * 700)
    first = kills(acts)[0]
    # The first reading only starts the clock: dose after n seconds = (temp - 92) * n.
    assert first == kill_at, (temp, first)


def test_the_warning_comes_at_half_the_budget_with_the_time_left():
    _, acts = _run([95.0] * 200)
    (t, w), = warnings(acts)[:1]
    assert t == 50 and w.dose == pytest.approx(150.0)
    assert w.dose_eta_s == pytest.approx(50.0) and w.dose_budget == 300.0
    assert kills(acts)[0] == 100


def test_one_warning_per_episode_rearmed_when_the_dose_drains_to_zero():
    hot, cool = [95.0] * 60, [80.0] * 60          # dose 177 -> drained (5/s) in 36 s
    _, acts = _run(hot + cool + hot)
    assert [t for t, _ in warnings(acts)] == [50, 169]   # clock already running at 120
    _, acts = _run(hot + [80.0] * 10 + hot)       # not drained: no second warning
    assert [t for t, _ in warnings(acts)] == [50]


def test_firing_spends_the_dose_so_sustained_heat_kills_once_per_budget():
    _, acts = _run([95.0] * 360)
    assert kills(acts) == [100, 200, 300]


def test_heat_in_between_spikes_adds_up():
    # 40 s at 96 C (+160), 10 s at 88 C (-50), repeated: net +110 per 50 s -> crosses 300.
    _, acts = _run(([96.0] * 40 + [88.0] * 10) * 4)
    assert kills(acts) and kills(acts)[0] < 200


def test_a_gap_in_the_readings_only_drains():
    eng = PolicyEngine(_cfg(HOT), available_signals={SIG})
    for t in range(60):
        eng.tick({SIG: Sample(95.0, t * NS, "test", "celsius")}, now_ns=t * NS)
    before = eng.stats_snapshot()["cpu-pkg-hot"]["dose"]
    t = 59 + 30                                   # a 30-s sleep, then one hot reading
    eng.tick({SIG: Sample(95.0, t * NS, "test", "celsius")}, now_ns=t * NS)
    assert eng.stats_snapshot()["cpu-pkg-hot"]["dose"] == pytest.approx(max(0.0, before - 150))


def test_pause_holds_the_kill():
    eng = PolicyEngine(_cfg(HOT), available_signals={SIG})
    eng.set_paused(10_000 * NS)
    acts = []
    for t in range(200):
        acts += eng.tick({SIG: Sample(95.0, t * NS, "test", "celsius")}, now_ns=t * NS)
    assert acts == []


# The real event behind the last window-rule kill (Chronos, 2026-11-04 01:36:30, 1 Hz).
NOV4 = [93, 93, 92, 92, 92, 92, 92, 92, 92, 92, 92, 92, 92, 92, 92, 91, 92, 92, 92, 92, 91, 91,
        91, 91, 91, 91, 91, 91, 91, 91, 91, 92, 91, 91, 91, 91, 91, 91, 91, 91, 91, 91, 91, 91,
        93, 94, 94, 94, 95, 95, 95, 95, 96, 96, 96, 96, 96, 96, 96, 96, 96, 96, 96, 96, 96, 89]


def test_the_nov4_event_the_window_rule_killed_for_is_under_the_dose_budget():
    window_kill = {**HOT, "min_fraction_over": 0.67}
    window_kill.pop("dose_budget")
    _, acts = _run([float(v) for v in NOV4], window_kill)
    assert kills(acts), "the old rule fired on this event"
    eng, acts = _run([float(v) for v in NOV4])
    assert kills(acts) == []
    assert eng.stats_snapshot()["cpu-pkg-hot"]["dose"] < 100


# -- messages to the owners ----------------------------------------------------------------

def _owner():
    return Owner("claude_session", "abcd1234", share=0.8, pids=12,
                 top=((101, "python.exe", 640.0),), notified=True)


def test_the_warning_tells_the_owner_the_time_left_and_asks_for_less_compute():
    _, acts = _run([95.0] * 60)
    _, w = warnings(acts)[0]
    assert agent_inbox.needs_action(w)
    body = agent_inbox._body(w, _owner(), KillReport(action=w, offender_pid=None, kill_root=None))
    assert "heat dose 150 of 300" in body and "~50 s" in body
    assert "Reduce YOUR OWN local compute" in body and "Spikes are fine" in body
    assert "not the user" in body


def test_the_kill_says_it_was_sustained_and_that_a_retry_heats_again():
    _, acts = _run([95.0] * 120)
    k = next(a for _, a in acts if a.kind == "kill")
    root = ProcInfo(pid=4242, ppid=1, name="python.exe", cmdline=("python.exe", "x.py"))
    rep = KillReport(action=k, offender_pid=4242, kill_root=root,
                     killed=(KilledProcess(root, "terminate", False),))
    body = agent_inbox._body(k, _owner(), rep)
    assert "killed your process tree rooted at pid 4242" in body
    assert "heat dose reached 300" in body and "Retrying at the same width" in body


def test_the_window_warning_under_a_dose_rule_does_not_also_message_agents():
    # cpu-pkg-warm stays for the tray and the log; the dose warning is what agents get.
    _, acts = _run([95.0] * 40, HOT, WARM)
    warm = [a for _, a in acts if a.rule_name == "cpu-pkg-warm"]
    assert warm and warm[0].dose_guarded and not agent_inbox.needs_action(warm[0])
    out = agent_inbox.deliver([_owner()], warm[0], None)
    assert out[0].notified is False and out[0].suppressed["by"] == "dose_rule_warns"


def test_the_webhook_carries_the_dose(monkeypatch, tmp_path):
    from atfield import reporter
    sent = []
    monkeypatch.setattr(reporter, "_webhooks", lambda sd: ["http://x/atfield/event"])
    monkeypatch.setattr(reporter, "_ensure_worker", lambda: None)
    monkeypatch.setattr(reporter._send_queue, "put_nowait", lambda item: sent.append(item))
    _, acts = _run([95.0] * 60)
    _, w = warnings(acts)[0]
    reporter.report_kill(tmp_path, action=w,
                         report=KillReport(action=w, offender_pid=None, kill_root=None))
    payload = sent[0][1]
    assert payload["dose"] == pytest.approx(150.0) and payload["dose_budget"] == 300.0
    assert payload["dose_eta_s"] == pytest.approx(50.0)
    assert payload["actionable"] is True and payload["notify"] is True
    assert all(math.isfinite(v) for v in (payload["dose"], payload["dose_eta_s"]))
