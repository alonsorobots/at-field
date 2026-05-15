"""Tests for :mod:`atfield.policy`.

Pure-logic tests with synthetic Sample streams and injected timestamps. No
real time, no real collectors. The cornerstones being verified:

* Glob expansion against the working signal map.
* Disablement of rules whose signals aren't available (with a clear reason).
* Per-rule sliding window + cooldown gating.
* Pause sentinel suppresses actions but not evaluations.
* Stale collector causes rule to abstain (covered through PolicyEngine
  rather than directly through evaluate_window, to ensure the integration
  stays honest).
"""

from __future__ import annotations

import math

import pytest

from atfield.config import (
    ApiConfig,
    AtFieldConfig,
    GeneralConfig,
    KillConfig,
    RuleConfig,
    TargetingConfig,
    default_config,
)
from atfield.policy import DisabledRule, PolicyEngine, expand_rules
from atfield.signals import Sample

NS = 1_000_000_000


def _cfg_with_rule(**rule_overrides) -> AtFieldConfig:
    base = {
        "name": "test-rule",
        "signal": "system.ram_used_percent",
        "threshold": 80.0,
        "window_s": 10,
        "min_fraction_over": 0.5,
        "action": "kill",
    }
    base.update(rule_overrides)
    return AtFieldConfig(
        general=GeneralConfig(),
        targeting=TargetingConfig(),
        kill=KillConfig(),
        api=ApiConfig(),
        rules=(RuleConfig(**base),),
    )


# ---------------------------------------------------------------------------
# Rule expansion
# ---------------------------------------------------------------------------


class TestExpandRules:
    def test_literal_signal_in_map_enabled(self):
        cfg = _cfg_with_rule(signal="system.ram_used_percent")
        eff, dis = expand_rules(cfg.rules, {"system.ram_used_percent"})
        assert len(eff) == 1
        assert not dis
        assert eff[0].signal == "system.ram_used_percent"
        assert eff[0].name == "test-rule"

    def test_literal_signal_missing_disabled_with_reason(self):
        cfg = _cfg_with_rule(signal="system.ram_used_percent")
        eff, dis = expand_rules(cfg.rules, {"system.commit_percent"})
        assert not eff
        assert len(dis) == 1
        assert isinstance(dis[0], DisabledRule)
        assert "not provided" in dis[0].reason

    def test_glob_expands_per_concrete_signal(self):
        cfg = _cfg_with_rule(signal="gpu.*.core_temp_c")
        eff, dis = expand_rules(
            cfg.rules,
            {"gpu.0.core_temp_c", "gpu.1.core_temp_c", "gpu.2.core_temp_c", "gpu.0.power_w"},
        )
        assert not dis
        assert len(eff) == 3
        signals = sorted(r.signal for r in eff)
        assert signals == ["gpu.0.core_temp_c", "gpu.1.core_temp_c", "gpu.2.core_temp_c"]
        # Synthesized name carries the concrete signal
        assert all("test-rule[gpu." in r.name for r in eff)

    def test_glob_no_matches_disabled(self):
        cfg = _cfg_with_rule(signal="gpu.*.mem_junction_temp_c")
        eff, dis = expand_rules(cfg.rules, {"gpu.0.core_temp_c"})  # no junction sensor
        assert not eff
        assert len(dis) == 1
        assert "no available signals matched glob" in dis[0].reason

    def test_glob_does_not_match_through_dots(self):
        # gpu.*.core_temp_c should NOT match gpu.0.foo.core_temp_c
        cfg = _cfg_with_rule(signal="gpu.*.core_temp_c")
        eff, _ = expand_rules(
            cfg.rules,
            {"gpu.0.core_temp_c", "gpu.0.foo.core_temp_c"},
        )
        assert len(eff) == 1
        assert eff[0].signal == "gpu.0.core_temp_c"

    def test_min_samples_floor_uses_tick_hz(self):
        cfg = _cfg_with_rule(window_s=30, min_fraction_over=0.67)
        eff, _ = expand_rules(cfg.rules, {"system.ram_used_percent"}, tick_hz=1)
        assert eff[0].min_samples == math.ceil(30 * 1 * 0.67)
        eff2, _ = expand_rules(cfg.rules, {"system.ram_used_percent"}, tick_hz=2)
        assert eff2[0].min_samples == math.ceil(30 * 2 * 0.67)


# ---------------------------------------------------------------------------
# PolicyEngine end-to-end
# ---------------------------------------------------------------------------


class TestPolicyEngineEndToEnd:
    def _make(self, **rule_overrides) -> PolicyEngine:
        cfg = _cfg_with_rule(**rule_overrides)
        return PolicyEngine(cfg, available_signals={cfg.rules[0].signal})

    def test_no_actions_when_below_threshold(self):
        eng = self._make(threshold=80, window_s=10, min_fraction_over=0.5)
        for t in range(15):
            actions = eng.tick(
                {"system.ram_used_percent": Sample(50.0, t * NS, "test", "percent")},
                now_ns=t * NS,
            )
            assert not actions

    def test_triggers_on_sustained_over_threshold(self):
        eng = self._make(threshold=80, window_s=10, min_fraction_over=0.5)
        # Need ceil(10 * 1 * 0.5) = 5 over-samples to fire.
        first_fire = None
        for t in range(15):
            actions = eng.tick(
                {"system.ram_used_percent": Sample(95.0, t * NS, "test", "percent")},
                now_ns=t * NS,
            )
            if actions:
                first_fire = t
                break
        assert first_fire is not None
        assert first_fire == 4  # 5 samples at t=0..4

    def test_cooldown_prevents_rapid_re_fire(self):
        eng = self._make(threshold=80, window_s=5, min_fraction_over=0.5)
        # Stay hot the whole time. Cooldown is post_kill_cooldown_seconds = 60s default.
        action_times: list[int] = []
        for t in range(150):
            actions = eng.tick(
                {"system.ram_used_percent": Sample(95.0, t * NS, "test", "percent")},
                now_ns=t * NS,
            )
            for _ in actions:
                action_times.append(t)
        # At most one action per 60s cooldown window
        assert all(action_times[i + 1] - action_times[i] >= 60 for i in range(len(action_times) - 1))

    def test_per_rule_cooldown_overrides_global(self):
        cfg = _cfg_with_rule(window_s=3, min_fraction_over=0.5, cooldown_s=10)
        eng = PolicyEngine(cfg, available_signals={"system.ram_used_percent"})
        action_times: list[int] = []
        for t in range(40):
            actions = eng.tick(
                {"system.ram_used_percent": Sample(95.0, t * NS, "test", "percent")},
                now_ns=t * NS,
            )
            for _ in actions:
                action_times.append(t)
        # Cooldown is 10s now -- expect more frequent firings than the 60s default
        assert len(action_times) >= 2
        for i in range(len(action_times) - 1):
            assert action_times[i + 1] - action_times[i] >= 10

    def test_pause_suppresses_actions_but_not_evaluation(self):
        eng = self._make(threshold=80, window_s=5, min_fraction_over=0.5)
        eng.set_paused(until_ns=999 * NS)
        for t in range(20):
            actions = eng.tick(
                {"system.ram_used_percent": Sample(95.0, t * NS, "test", "percent")},
                now_ns=t * NS,
            )
            assert not actions, "no actions while paused"
        # Stats still updated -- evaluation continued
        stats = eng.stats_snapshot()
        rule_name = next(iter(stats))
        assert stats[rule_name]["last_verdict"] in ("TRIGGER", "BELOW", "INSUFFICIENT")

    def test_stale_collector_makes_rule_abstain(self):
        """If a collector goes silent mid-run, the rule must abstain.

        This is the policy-layer check that mirrors the signals-layer
        property tested in test_signals.py. End-to-end safety.
        """
        eng = self._make(threshold=80, window_s=10, min_fraction_over=0.5)
        # Feed 8 hot samples then go silent for 5s
        for t in range(8):
            eng.tick(
                {"system.ram_used_percent": Sample(95.0, t * NS, "test", "percent")},
                now_ns=t * NS,
            )
        # Now silent ticks
        for t in range(8, 20):
            actions = eng.tick({}, now_ns=t * NS)
            # At t=10 onward, latest sample (from t=7) is >2s old -> INSUFFICIENT
            if t >= 10:
                assert not actions, (
                    f"frozen collector at t={t} caused action -- silent samples must abstain"
                )

    def test_disabled_rules_show_in_engine_view(self):
        cfg = _cfg_with_rule(signal="gpu.*.mem_junction_temp_c")
        eng = PolicyEngine(cfg, available_signals=set())  # empty map
        assert not eng.effective_rules
        assert len(eng.disabled_rules) == 1
        assert eng.disabled_rules[0].base_rule_name == "test-rule"


# ---------------------------------------------------------------------------
# Action shape + audit-friendly fields
# ---------------------------------------------------------------------------


class TestActionShape:
    def test_action_carries_audit_fields(self):
        cfg = _cfg_with_rule(threshold=80, window_s=5, min_fraction_over=0.5)
        eng = PolicyEngine(cfg, available_signals={"system.ram_used_percent"})
        for t in range(10):
            actions = eng.tick(
                {"system.ram_used_percent": Sample(95.0, t * NS, "test", "percent")},
                now_ns=t * NS,
            )
            if actions:
                a = actions[0]
                assert a.kind == "kill"
                assert a.signal == "system.ram_used_percent"
                assert a.threshold == 80.0
                assert a.fraction_over == pytest.approx(1.0)
                assert a.samples_considered >= 3
                assert a.latest_value == 95.0
                assert a.cooldown_seconds == 60
                return
        pytest.fail("expected action to fire")


# ---------------------------------------------------------------------------
# Default config + realistic signal map (regression for the dev rig)
# ---------------------------------------------------------------------------


class TestDefaultConfigIntegration:
    def test_dev_rig_two_gpus_no_lhm_yields_4_rules_2_disabled(self):
        """The actual scenario from the dev box: 2 GPUs probed via NVML,
        LHM not running. Conservative profile should produce 4 enabled
        rules (gpu-core x 2, ram, pagefile) and 2 disabled rules (vram-
        junction, cpu-pkg)."""
        cfg = default_config()
        avail = {
            "gpu.0.core_temp_c", "gpu.0.util_percent", "gpu.0.vram_used_percent",
            "gpu.1.core_temp_c", "gpu.1.util_percent", "gpu.1.vram_used_percent",
            "system.ram_used_percent", "system.swap_used_percent", "system.commit_percent",
        }
        eng = PolicyEngine(cfg, available_signals=avail)
        eff_names = {r.name for r in eng.effective_rules}
        dis_names = {d.base_rule_name for d in eng.disabled_rules}
        assert dis_names == {"vram-junction-hot", "cpu-pkg-hot"}
        assert "gpu-core-hot[gpu.0.core_temp_c]" in eff_names
        assert "gpu-core-hot[gpu.1.core_temp_c]" in eff_names
        assert "ram-pressure" in eff_names
        assert "pagefile-pressure" in eff_names
