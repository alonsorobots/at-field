"""Tests for :mod:`atfield.rule_profiles`."""

from __future__ import annotations

import pytest

from atfield.rule_profiles import (
    PROFILE_PRESETS,
    RULE_PROFILES,
    classify,
    preset_threshold,
    rule_profile,
)


class TestRuleProfileMetadata:
    def test_all_known_rules_have_profiles(self):
        # Sanity: keep the rule list aligned with config.py defaults.
        # If new rules land in default_config(), they must get tier
        # metadata here too -- otherwise the slider falls back to
        # "custom" for them and operators lose preset support.
        from atfield.config import default_config
        defaults = {r.name for r in default_config().rules}
        missing = defaults - RULE_PROFILES.keys()
        assert not missing, f"default rules missing tier metadata: {missing}"

    def test_each_profile_min_lt_aggressive_lt_relaxed_lt_max(self):
        """Tier ordering invariant: misordered tier bands would silently
        misclassify thresholds."""
        for p in RULE_PROFILES.values():
            assert p.min < p.aggressive_max < p.relaxed_min < p.max, (
                f"{p.name}: bands out of order: {p}"
            )

    def test_preset_values_lie_in_their_tier_bands(self):
        for p in RULE_PROFILES.values():
            assert classify(p.name, p.aggressive_value) == "aggressive"
            assert classify(p.name, p.normal_value) == "normal"
            assert classify(p.name, p.relaxed_value) == "relaxed"

    def test_rule_profile_returns_none_for_unknown(self):
        assert rule_profile("does-not-exist") is None


class TestClassify:
    @pytest.mark.parametrize("value,expected", [
        (60.0, "aggressive"),
        (78.0, "aggressive"),  # exact aggressive_max boundary -> aggressive
        (79.0, "normal"),
        (86.0, "normal"),
        (87.0, "relaxed"),     # exact relaxed_min boundary -> relaxed
        (95.0, "relaxed"),
    ])
    def test_gpu_core_hot_classification(self, value, expected):
        assert classify("gpu-core-hot", value) == expected

    def test_unknown_rule_classifies_custom(self):
        assert classify("nonexistent", 50.0) == "custom"


class TestPresets:
    def test_presets_have_all_rules(self):
        for profile_name, mapping in PROFILE_PRESETS.items():
            for rule_name in RULE_PROFILES:
                assert rule_name in mapping, (
                    f"profile {profile_name!r} missing rule {rule_name!r}"
                )

    def test_aggressive_preset_lower_threshold_than_relaxed_for_each_rule(self):
        for rule_name in RULE_PROFILES:
            agg = PROFILE_PRESETS["aggressive"][rule_name]
            rel = PROFILE_PRESETS["relaxed"][rule_name]
            assert agg < rel, (
                f"{rule_name}: aggressive ({agg}) should be lower than "
                f"relaxed ({rel}) -- lower threshold means earlier kill"
            )

    def test_preset_threshold_returns_none_for_unknowns(self):
        assert preset_threshold("aggressive", "no-such-rule") is None
