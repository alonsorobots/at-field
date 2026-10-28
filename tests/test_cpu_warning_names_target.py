"""The cpu-pkg-warm notice states THIS machine's CPU, Tjmax and the AVERAGE target (0.4.30).

2026-10-27: "rule cpu-pkg-warm: ... = 87.25 ... Please reduce or pause YOUR OWN local
compute ... until the machine cools" made an agent over-throttle at 88 C. The user's limit
is the AVERAGE package temp (9950X3D: <= 92 C, Tjmax 95); spikes above are fine. User:
"change warning to clarify target. give CPU type and safe ranges including spikes. keep it
super concise" -- and "make sure this is per machine".

0.4.31 (user 2026-10-28): the "Info only" notice under the target still cost the agent a
turn -- "that is terrible". Under the target nobody is told; the event is still recorded.
"""
from types import SimpleNamespace

import pytest

from atfield import agent_inbox as ai
from atfield import cpu_limits as cl
from atfield.policy import Action

OWNER = ai.Owner("claude_session", "aaaaaaaa", share=1.0, pids=2,
                 top=((101, "python.exe", 98.0), (102, "python.exe", 90.0)))
REPORT = SimpleNamespace(kill_root=None, killed=())


def warn(latest, mean, signal="system.cpu_package_temp_c", kill=92.0):
    return Action(kind="log", rule_name="cpu-pkg-warm", base_rule_name="cpu-pkg-warm",
                  signal=signal, threshold=86.0, fraction_over=0.8, samples_considered=30,
                  latest_value=latest, triggered_at_ns=0, cooldown_seconds=600, notify=True,
                  window_mean=mean, window_s=30, kill_threshold=kill)


@pytest.fixture
def host(monkeypatch):
    def use(model):
        monkeypatch.setattr(ai, "_HOST", "Chronos")
        monkeypatch.setattr(ai.cpu_limits, "this_cpu", lambda: cl.limits_for(model))
    return use


def body(action):
    return ai._body(action, OWNER, REPORT)


def test_under_the_average_target_nobody_is_told(host, monkeypatch):
    host("AMD Ryzen 9 9950X3D 16-Core Processor")
    sent = []
    monkeypatch.setattr(ai, "post", lambda inbox, text: sent.append(text) or True)
    owner = ai.Owner("claude_session", "aaaaaaaa", share=1.0, pids=2, notified=True,
                     inbox=ai.Inbox("aaaaaaaa-1111", "pipe-a", "tok"))
    (o,) = ai.deliver([owner], warn(91.0, 92.0), REPORT, limit=False)   # mean == target
    assert sent == [] and not o.notified and o.suppressed["by"] == "below_target"
    (o,) = ai.deliver([owner], warn(93.0, 92.1), REPORT, limit=False)
    assert len(sent) == 1 and o.notified and o.suppressed is None
    assert "Average is over 92 C" in sent[0]


def test_only_cpu_warnings_are_gated_never_kills_or_other_rules(host):
    import dataclasses
    host("AMD Ryzen 9 9950X3D 16-Core Processor")
    assert ai.needs_action(warn(88.0, 88.0, signal="gpu.0.core_temp_c"))
    assert ai.needs_action(dataclasses.replace(warn(88.0, 88.0), kind="kill"))
    host("Some Future CPU 64-Core Processor")          # no target: the kill line gates
    assert not ai.needs_action(warn(91.0, 91.0)) and ai.needs_action(warn(93.0, 92.5))


def test_the_webhook_event_says_whether_it_is_actionable(host, monkeypatch, tmp_path):
    from atfield import reporter
    host("AMD Ryzen 9 9950X3D 16-Core Processor")
    q = []
    monkeypatch.setattr(reporter, "_webhooks", lambda d: ["http://x/e"])
    monkeypatch.setattr(reporter, "_ensure_worker", lambda: None)
    monkeypatch.setattr(reporter._send_queue, "put_nowait", q.append)
    rep = SimpleNamespace(kill_root=None, succeeded=False, skipped_reason=None, owners=())
    reporter.report_kill(tmp_path, action=warn(88.0, 88.0), report=rep)
    reporter.report_kill(tmp_path, action=warn(93.0, 92.5), report=rep)
    assert [e[1]["actionable"] for e in q] == [False, True]


def test_over_the_average_target_asks_to_reduce(host):
    host("AMD Ryzen 9 9950X3D 16-Core Processor")
    text = body(warn(93.0, 92.6))
    assert "Average is over 92 C: reduce YOUR OWN local compute" in text


def test_target_comes_from_this_hosts_cpu_and_never_exceeds_the_kill_line(host):
    host("Intel(R) Core(TM) i7-9700K CPU @ 3.60GHz")   # Tjmax 100 -> 97, but kills at 92
    text = body(warn(88.0, 88.0))
    assert "(Intel Core i7-9700K, Tjmax 100 C). Target: average <= 92 C" in text
    assert "Target: average <= 97 C" in body(warn(88.0, 88.0, kill=None))
    host("AMD Ryzen 9 9950X3D 16-Core Processor")         # Demeter: kill line 90
    assert "Target: average <= 90 C" in body(warn(88.0, 88.0, kill=90.0))


def test_an_unknown_cpu_says_so_and_borrows_no_other_hosts_limit(host):
    host("Some Future CPU 64-Core Processor")
    text = body(warn(88.0, 87.0))
    assert "(Some Future CPU, Tjmax unknown)" in text
    assert "95" not in text and "Target" not in text
    assert "AT-Field kills at a sustained 92 C" in text


def test_the_policy_engine_carries_the_window_mean():
    from atfield.config import RuleConfig, default_config
    from atfield.policy import PolicyEngine
    from atfield.signals import Sample
    import dataclasses
    rule = RuleConfig(name="cpu-pkg-warm", signal="system.cpu_package_temp_c", threshold=86.0,
                      window_s=5, min_fraction_over=0.5, action="log", notify=True)
    kill = RuleConfig(name="cpu-pkg-hot", signal="system.cpu_package_temp_c", threshold=92.0,
                      window_s=5, min_fraction_over=0.9, action="kill")
    cfg = dataclasses.replace(default_config(), rules=(rule, kill))
    eng = PolicyEngine(cfg, available_signals={"system.cpu_package_temp_c"})
    seen, out = [], []
    for i, v in enumerate([80.0, 89.0, 90.0, 91.0, 92.0]):
        t = (i + 1) * 1_000_000_000
        seen.append(v)
        fired = eng.tick({"system.cpu_package_temp_c": Sample(v, t, "x", "celsius")}, now_ns=t)
        out += [(a, sum(seen) / len(seen)) for a in fired]
    (action, mean), = out
    assert action.window_mean == pytest.approx(mean)
    assert (action.window_s, action.kill_threshold) == (5, 92.0)   # the live kill line
