"""Tests for :mod:`atfield.config`.

Covers the conservative-profile defaults from PLANNING.md §3 / §8 and every
documented validation path. Validation tests use ``load_config_from_dict``
to avoid TOML round-tripping; a single end-to-end ``load_config(path)`` test
exercises the file-reading path.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from atfield.config import (
    AtFieldConfig,
    ConfigError,
    GeneralConfig,
    KillConfig,
    RuleConfig,
    TargetingConfig,
    default_config,
    default_state_dir,
    load_config,
    load_config_from_dict,
)


# ---------------------------------------------------------------------------
# Defaults (conservative profile -- locked-in)
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_default_config_matches_locked_in_profile(self):
        cfg = default_config()
        assert isinstance(cfg, AtFieldConfig)
        assert cfg.general.tick_hz == 1
        assert cfg.general.log_level == "INFO"
        assert cfg.kill.mode == "graceful"
        assert cfg.kill.grace_seconds == 5
        assert cfg.kill.post_kill_cooldown_seconds == 60

    def test_default_rules_are_the_five_locked_in(self):
        cfg = default_config()
        names = {r.name for r in cfg.rules}
        assert names == {
            "vram-junction-hot",
            "gpu-core-hot",
            "ram-pressure",
            "pagefile-pressure",
            "cpu-pkg-hot",
        }

    @pytest.mark.parametrize(
        "name, signal, threshold, window_s, min_fraction",
        [
            ("vram-junction-hot", "gpu.*.mem_junction_temp_c", 90.0, 20, 0.67),
            ("gpu-core-hot", "gpu.*.core_temp_c", 83.0, 30, 0.67),
            ("ram-pressure", "system.ram_used_percent", 85.0, 60, 0.75),
            ("pagefile-pressure", "system.commit_percent", 90.0, 60, 0.75),
            ("cpu-pkg-hot", "system.cpu_package_temp_c", 90.0, 30, 0.67),
        ],
    )
    def test_each_default_rule_locked(self, name, signal, threshold, window_s, min_fraction):
        rule = next(r for r in default_config().rules if r.name == name)
        assert rule.signal == signal
        assert rule.threshold == threshold
        assert rule.window_s == window_s
        assert rule.min_fraction_over == min_fraction
        assert rule.action == "kill"

    def test_default_state_dir_uses_program_data(self, monkeypatch):
        monkeypatch.setenv("PROGRAMDATA", r"C:\TestProgramData")
        assert default_state_dir() == Path(r"C:\TestProgramData") / "ATField"

    def test_default_state_dir_falls_back_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("PROGRAMDATA", raising=False)
        assert default_state_dir() == Path(r"C:\ProgramData\ATField")

    def test_cooldown_for_falls_back_to_global(self):
        cfg = default_config()
        rule = cfg.rules[0]
        assert rule.cooldown_s is None
        assert cfg.cooldown_for(rule) == cfg.kill.post_kill_cooldown_seconds


# ---------------------------------------------------------------------------
# Per-section validation
# ---------------------------------------------------------------------------


class TestGeneralValidation:
    def test_valid_partial_override_merges_with_defaults(self):
        cfg = load_config_from_dict({"general": {"tick_hz": 2, "log_level": "debug"}})
        assert cfg.general.tick_hz == 2
        assert cfg.general.log_level == "DEBUG"

    @pytest.mark.parametrize("bad", [0, -1, True, "1", 1.5])
    def test_rejects_bad_tick_hz(self, bad):
        with pytest.raises(ConfigError, match="tick_hz"):
            load_config_from_dict({"general": {"tick_hz": bad}})

    def test_rejects_unknown_log_level(self):
        with pytest.raises(ConfigError, match="log_level"):
            load_config_from_dict({"general": {"log_level": "VERBOSE"}})

    def test_rejects_unknown_key(self):
        with pytest.raises(ConfigError, match="unknown key"):
            load_config_from_dict({"general": {"verbose": True}})


class TestTargetingValidation:
    def test_partial_override_keeps_other_defaults(self):
        cfg = load_config_from_dict({"targeting": {"killable_names": ["python.exe"]}})
        assert cfg.targeting.killable_names == ("python.exe",)
        assert "torchrun" in cfg.targeting.launcher_names

    def test_empty_killable_names_rejected(self):
        with pytest.raises(ConfigError, match="killable_names"):
            load_config_from_dict({"targeting": {"killable_names": []}})

    def test_killable_never_kill_overlap_rejected(self):
        with pytest.raises(ConfigError, match="overlap"):
            load_config_from_dict(
                {
                    "targeting": {
                        "killable_names": ["python.exe"],
                        "never_kill_names": ["python.exe"],
                    }
                }
            )


class TestKillValidation:
    @pytest.mark.parametrize("mode", ["graceful", "aggressive"])
    def test_valid_modes_accepted(self, mode):
        cfg = load_config_from_dict({"kill": {"mode": mode}})
        assert cfg.kill.mode == mode

    def test_invalid_mode_rejected(self):
        with pytest.raises(ConfigError, match="kill.mode"):
            load_config_from_dict({"kill": {"mode": "nuclear"}})

    def test_grace_seconds_must_be_non_negative(self):
        with pytest.raises(ConfigError, match="grace_seconds"):
            load_config_from_dict({"kill": {"grace_seconds": -1}})

    def test_bool_rejected_for_int_field(self):
        with pytest.raises(ConfigError, match="grace_seconds"):
            load_config_from_dict({"kill": {"grace_seconds": True}})


class TestRulesValidation:
    def _rule(self, **overrides):
        base = {
            "name": "r",
            "signal": "system.ram_used_percent",
            "threshold": 85,
            "window_s": 10,
            "min_fraction_over": 0.5,
            "action": "kill",
        }
        base.update(overrides)
        return base

    def test_minimal_valid_rule(self):
        cfg = load_config_from_dict({"rules": [self._rule()]})
        assert len(cfg.rules) == 1
        assert cfg.rules[0].name == "r"

    def test_per_rule_cooldown_overrides_global(self):
        cfg = load_config_from_dict(
            {"kill": {"post_kill_cooldown_seconds": 60}, "rules": [self._rule(cooldown_s=120)]}
        )
        assert cfg.cooldown_for(cfg.rules[0]) == 120

    def test_missing_required_key_rejected(self):
        with pytest.raises(ConfigError, match="missing required"):
            load_config_from_dict({"rules": [{"name": "r", "signal": "system.x"}]})

    def test_duplicate_rule_name_rejected(self):
        with pytest.raises(ConfigError, match="duplicate"):
            load_config_from_dict({"rules": [self._rule(name="dup"), self._rule(name="dup")]})

    @pytest.mark.parametrize(
        "bad_signal",
        ["BAD!", "system", "", "GPU.0.temp", "gpu..temp", "gpu.0.", ".system.x"],
    )
    def test_malformed_signal_rejected(self, bad_signal):
        with pytest.raises(ConfigError, match="signal"):
            load_config_from_dict({"rules": [self._rule(signal=bad_signal)]})

    @pytest.mark.parametrize(
        "good_signal",
        ["system.ram_used_percent", "gpu.0.core_temp_c", "gpu.*.mem_junction_temp_c",
         "gpu.1.power_w", "system.cpu_package_temp_c"],
    )
    def test_valid_signal_grammar_accepted(self, good_signal):
        cfg = load_config_from_dict({"rules": [self._rule(signal=good_signal)]})
        assert cfg.rules[0].signal == good_signal

    @pytest.mark.parametrize("bad_frac", [0.0, -0.5, 1.5])
    def test_fraction_out_of_range_rejected(self, bad_frac):
        with pytest.raises(ConfigError, match="min_fraction_over"):
            load_config_from_dict({"rules": [self._rule(min_fraction_over=bad_frac)]})

    def test_invalid_action_rejected(self):
        with pytest.raises(ConfigError, match="action"):
            load_config_from_dict({"rules": [self._rule(action="hibernate")]})

    def test_empty_rules_array_rejected(self):
        with pytest.raises(ConfigError, match="empty"):
            load_config_from_dict({"rules": []})

    def test_no_rules_section_falls_back_to_defaults(self):
        cfg = load_config_from_dict({"general": {"tick_hz": 2}})
        assert len(cfg.rules) == len(default_config().rules)


# ---------------------------------------------------------------------------
# Top-level + file-reading
# ---------------------------------------------------------------------------


class TestTopLevel:
    def test_unknown_section_rejected(self):
        with pytest.raises(ConfigError, match="unknown top-level section"):
            load_config_from_dict({"targetting": {}})  # typo

    def test_load_none_returns_defaults(self):
        cfg = load_config(None)
        assert cfg.general.tick_hz == default_config().general.tick_hz

    def test_load_missing_file_returns_defaults(self, tmp_path):
        cfg = load_config(tmp_path / "does-not-exist.toml")
        assert len(cfg.rules) == len(default_config().rules)

    def test_load_valid_toml_file(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(textwrap.dedent("""
            [general]
            tick_hz = 2
            log_level = "DEBUG"

            [kill]
            mode = "aggressive"

            [[rules]]
            name = "test"
            signal = "system.ram_used_percent"
            threshold = 90
            window_s = 30
            min_fraction_over = 0.8
            action = "log"
            cooldown_s = 30
        """))
        cfg = load_config(p)
        assert cfg.general.tick_hz == 2
        assert cfg.kill.mode == "aggressive"
        assert cfg.rules[0].name == "test"
        assert cfg.rules[0].cooldown_s == 30

    def test_load_malformed_toml_raises_config_error(self, tmp_path):
        p = tmp_path / "broken.toml"
        p.write_text("this is = not = valid = toml")
        with pytest.raises(ConfigError, match="invalid TOML"):
            load_config(p)


# ---------------------------------------------------------------------------
# Frozen dataclass invariants (defense in depth)
# ---------------------------------------------------------------------------


class TestFrozenInvariants:
    def test_general_frozen(self):
        with pytest.raises(Exception):
            default_config().general.tick_hz = 99  # type: ignore[misc]

    def test_kill_frozen(self):
        with pytest.raises(Exception):
            default_config().kill.mode = "aggressive"  # type: ignore[misc]

    def test_rule_frozen(self):
        with pytest.raises(Exception):
            default_config().rules[0].threshold = 0  # type: ignore[misc]

    def test_dataclass_types_exist(self):
        # surface-area smoke test
        assert GeneralConfig and TargetingConfig and KillConfig and RuleConfig
