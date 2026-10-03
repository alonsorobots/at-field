"""A near-limit warning surfaces on /health WITHOUT masking a kill (0.4.19).

The tray pops a kill notification only when /health `last_action.kind == "kill"`, and
until 0.4.19 every action -- log and throttle too -- overwrote last_action. Rules fire in
config order within a tick, so a warning rule listed after its kill rule (cpu-pkg-warm at
86 C after cpu-pkg-hot at 90 C, both over the line at 95 C) would have erased the kill
pop-up. Pinned here, through the REAL policy engine:
  * a warning goes to `last_warning` {rule, signal, value, threshold, kill_rule,
    kill_threshold} and leaves last_action alone;
  * a plain (non-notify) log rule still records last_action as before;
  * an observe-mode demotion (kill -> log) is never a warning;
  * the subscriber payload carries `notify`.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from atfield import reporter  # noqa: E402
from atfield.config import load_config_from_dict  # noqa: E402
from atfield.http_api import ServiceState  # noqa: E402
from atfield.policy import PolicyEngine  # noqa: E402
from atfield.service import demote_for_observe_only, record_on_health  # noqa: E402
from atfield.signals import Sample  # noqa: E402

CPU = "system.cpu_package_temp_c"
_NS = 1_000_000_000
KILL = {"name": "cpu-pkg-hot", "signal": CPU, "threshold": 90.0, "window_s": 10,
        "min_fraction_over": 0.5, "action": "kill"}
WARN = {"name": "cpu-pkg-warm", "signal": CPU, "threshold": 86.0, "window_s": 10,
        "min_fraction_over": 0.5, "action": "log", "notify": True, "cooldown_s": 600}
PLAIN = {"name": "cpu-pkg-note", "signal": CPU, "threshold": 80.0, "window_s": 10,
         "min_fraction_over": 0.5, "action": "log"}


def _engine(*rules):
    cfg = load_config_from_dict({"general": {"tick_hz": 1}, "rules": [dict(r) for r in rules]})
    return PolicyEngine(cfg, available_signals={CPU})


def _first_firing_tick(eng, value):
    now = 10 * _NS
    for _ in range(60):
        fired = eng.tick({CPU: Sample(value, now, "test", "celsius")}, now_ns=now)
        if fired:
            return fired
        now += _NS
    raise AssertionError("nothing fired")


def _state(tmp_path):
    return ServiceState(version="0.0.0-test", observe_only=False, events_path=tmp_path / "e.jsonl",
                        watchdog_log_path=tmp_path / "w.log", state_dir=tmp_path)


def test_a_warning_fired_with_its_kill_leaves_the_kill_on_health(tmp_path):
    eng = _engine(KILL, WARN)                       # kill listed FIRST: the warning fires after it
    actions = _first_firing_tick(eng, 95.0)
    assert [a.base_rule_name for a in actions] == ["cpu-pkg-hot", "cpu-pkg-warm"]
    st = _state(tmp_path)
    for a in actions:
        record_on_health(st, a, eng.effective_rules)
    h = st.snapshot_health()
    assert h["last_action"]["kind"] == "kill", "the warning erased the kill pop-up"
    w = h["last_warning"]
    assert w["rule"] == "cpu-pkg-warm" and w["signal"] == CPU
    assert w["value"] == 95.0 and w["threshold"] == 86.0
    assert w["kill_rule"] == "cpu-pkg-hot" and w["kill_threshold"] == 90.0


def test_a_warning_alone_does_not_touch_last_action(tmp_path):
    eng = _engine(KILL, WARN)
    actions = _first_firing_tick(eng, 87.0)        # over the warning line, under the kill line
    assert [a.base_rule_name for a in actions] == ["cpu-pkg-warm"]
    st = _state(tmp_path)
    record_on_health(st, actions[0], eng.effective_rules)
    h = st.snapshot_health()
    assert h["last_action"] is None
    assert h["last_warning"]["rule"] == "cpu-pkg-warm"


def test_a_plain_log_rule_still_records_last_action(tmp_path):
    eng = _engine(KILL, PLAIN)
    actions = _first_firing_tick(eng, 85.0)
    st = _state(tmp_path)
    record_on_health(st, actions[0], eng.effective_rules)
    h = st.snapshot_health()
    assert h["last_action"]["kind"] == "log" and h["last_warning"] is None


def test_an_observe_mode_demotion_is_not_a_warning(tmp_path):
    eng = _engine(KILL)
    (kill,) = _first_firing_tick(eng, 95.0)
    demoted = demote_for_observe_only(kill)
    assert demoted.kind == "log" and demoted.notify is False
    st = _state(tmp_path)
    record_on_health(st, demoted, eng.effective_rules)
    h = st.snapshot_health()
    assert h["last_warning"] is None and h["last_action"]["kind"] == "log"


def test_the_subscriber_payload_carries_notify(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(reporter, "_webhooks", lambda sd: ["http://x/atfield/event"])
    monkeypatch.setattr(reporter, "_ensure_worker", lambda: None)

    class Q:
        def put_nowait(self, item):
            sent.append(item[1])
    monkeypatch.setattr(reporter, "_send_queue", Q())

    class Report:
        kill_root = None
        succeeded = 0
        skipped_reason = "log action"
    eng = _engine(KILL, WARN)
    actions = _first_firing_tick(eng, 95.0)
    for a in actions:
        reporter.report_kill(tmp_path, action=a, report=Report())
    assert [(p["rule"], p["notify"]) for p in sent] == [("cpu-pkg-hot", False), ("cpu-pkg-warm", True)]
    assert [p["value"] for p in sent] == [95.0, 95.0]


def test_a_non_finite_reading_is_sent_as_null_not_NaN():
    import json
    import math
    assert reporter._finite_or_none(float("nan")) is None
    assert reporter._finite_or_none(None) is None
    assert reporter._finite_or_none(87.5) == 87.5
    json.dumps({"value": reporter._finite_or_none(math.inf)}, allow_nan=False)


def test_the_warning_names_the_nearest_kill_line_on_its_own_signal():
    from atfield.policy import kill_line_for
    RAM = "system.ram_used_percent"
    cfg = load_config_from_dict({"general": {"tick_hz": 1}, "rules": [
        {**KILL, "name": "cpu-pkg-critical", "threshold": 97.0},
        dict(KILL),
        {**KILL, "name": "ram-full", "signal": RAM, "threshold": 88.0},
        dict(WARN)]})
    eng = PolicyEngine(cfg, available_signals={CPU, RAM})
    assert kill_line_for(eng.effective_rules, CPU, 86.0) == ("cpu-pkg-hot", 90.0)
    assert kill_line_for(eng.effective_rules, RAM, 95.0) == (None, None)

LOOP_CONFIG = """
[general]
tick_hz = 10

[api]
enabled = false

[[rules]]
name = "cpu-pkg-hot"
signal = "system.cpu_package_temp_c"
threshold = 90.0
window_s = 1
min_fraction_over = 0.5
action = "kill"

[[rules]]
name = "cpu-pkg-warm"
signal = "system.cpu_package_temp_c"
threshold = 86.0
window_s = 1
min_fraction_over = 0.5
action = "log"
notify = true
"""


def test_the_real_service_loop_keeps_the_kill_on_health(tmp_path, monkeypatch):
    """Through run_service itself: the masking tests above call record_on_health
    directly, so reverting the dispatch loop to 0.4.18's record_action left them
    green (reviewer finding F1, reproduced). The actuator is faked: nothing is killed."""
    import atfield.service as svc
    from atfield.actuator import KillReport
    from atfield.collectors import HealthState, ProbeResult
    from atfield.signals import monotonic_ns

    class Hot:
        name = "fake"

        def probe(self):
            return ProbeResult(available=True, reason="fake", signals=(CPU,))

        def health(self):
            return HealthState.HEALTHY

        def sample(self):
            return {CPU: Sample(95.0, monotonic_ns(), self.name, "celsius")}

    class NoKill:
        def __init__(self, cfg):
            pass

        def execute(self, action, candidate_pids=None):
            return KillReport(action=action, offender_pid=None, kill_root=None,
                              skipped_reason="test: nothing is killed")

        def enforce_rss_cap(self):
            return []

    states = []

    class Spy(ServiceState):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            states.append(self)

    c = Hot()
    monkeypatch.setattr(svc, "_probe_all_collectors", lambda audit: ([c], {c.name: c.probe()}))
    monkeypatch.setattr(svc, "Actuator", NoKill)
    monkeypatch.setattr(svc, "ServiceState", Spy)
    monkeypatch.setattr(svc, "report_kill", lambda *a, **kw: None)
    cfg = tmp_path / "config.toml"
    cfg.write_text(LOOP_CONFIG, encoding="utf-8")
    svc.run_service(config_path=cfg, state_dir=tmp_path / "state", max_ticks=15)
    h = states[0].snapshot_health()
    assert h["last_warning"] and h["last_warning"]["rule"] == "cpu-pkg-warm", h["last_warning"]
    assert h["last_action"]["kind"] == "kill", "the warning erased the kill pop-up in the real loop"

def test_a_non_finite_warning_value_reaches_health_as_null(tmp_path):
    """Reviewer F7: /health is plain json.dumps; NaN there would make the tray's
    strict serde parse fail for the WHOLE snapshot, kill pop-ups included."""
    import dataclasses
    import json
    eng = _engine(KILL, WARN)
    (warn,) = _first_firing_tick(eng, 87.0)
    st = _state(tmp_path)
    record_on_health(st, dataclasses.replace(warn, latest_value=float("nan")), eng.effective_rules)
    h = st.snapshot_health()
    assert h["last_warning"]["value"] is None
    json.dumps(h["last_warning"], allow_nan=False)

