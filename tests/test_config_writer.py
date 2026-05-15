"""Tests for :mod:`atfield.config_writer`.

The writer's contract: for every kind of config file we may encounter on
disk (existing user-edited, default-rendered, edge cases), a slider
threshold update should round-trip cleanly through `load_config()` and
preserve every byte we don't intend to touch.
"""

from __future__ import annotations

import re

import pytest

from atfield.config import load_config
from atfield.config_writer import (
    ConfigWriteError,
    materialize_default_config,
    update_rule_threshold,
    verify_roundtrip,
)


def _read(path):
    return path.read_text(encoding="utf-8")


class TestMaterializeDefaultConfig:
    def test_writes_a_loadable_config(self, tmp_path):
        path = tmp_path / "config.toml"
        materialize_default_config(path)
        cfg = verify_roundtrip(path)
        # The default profile has these five rules.
        names = {r.name for r in cfg.rules}
        assert "vram-junction-hot" in names
        assert "ram-pressure" in names
        assert "cpu-pkg-hot" in names

    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "config.toml"
        materialize_default_config(path)
        assert path.exists()
        verify_roundtrip(path)


class TestUpdateRuleThreshold:
    def test_updates_existing_user_config(self, tmp_path):
        # Hand-written config with comments + odd formatting (which the
        # writer must NOT clobber).
        config = tmp_path / "config.toml"
        config.write_text(
            "# I have notes here\n"
            "[general]\n"
            "tick_hz = 1\n"
            "\n"
            "[[rules]]\n"
            "# comment about ram-pressure\n"
            'name = "ram-pressure"\n'
            'signal = "system.ram_used_percent"\n'
            "threshold = 85.0   # don't touch this comment\n"
            "window_s = 60\n"
            "min_fraction_over = 0.75\n"
            'action = "kill"\n'
            "\n"
            "[[rules]]\n"
            'name = "gpu-core-hot"\n'
            'signal = "gpu.*.core_temp_c"\n'
            "threshold = 83.0\n"
            "window_s = 30\n"
            "min_fraction_over = 0.67\n"
            'action = "kill"\n',
            encoding="utf-8",
        )

        update_rule_threshold(config, "ram-pressure", 78.0)

        text = _read(config)
        # Updated value
        assert "threshold = 78.0   # don't touch this comment" in text
        # Untouched value
        assert "threshold = 83.0\n" in text
        # Comments preserved
        assert "# I have notes here" in text
        assert "# comment about ram-pressure" in text

        # Round-trips through canonical parser
        cfg = verify_roundtrip(config)
        ram = next(r for r in cfg.rules if r.name == "ram-pressure")
        gpu = next(r for r in cfg.rules if r.name == "gpu-core-hot")
        assert ram.threshold == 78.0
        assert gpu.threshold == 83.0

    def test_creates_default_when_file_missing(self, tmp_path):
        config = tmp_path / "config.toml"
        # File doesn't exist -- writer should materialize defaults first,
        # then update.
        update_rule_threshold(config, "ram-pressure", 92.0)
        assert config.exists()
        cfg = verify_roundtrip(config)
        ram = next(r for r in cfg.rules if r.name == "ram-pressure")
        assert ram.threshold == 92.0

    def test_unknown_rule_raises_with_helpful_message(self, tmp_path):
        config = tmp_path / "config.toml"
        materialize_default_config(config)
        with pytest.raises(ConfigWriteError) as exc:
            update_rule_threshold(config, "nonexistent-rule", 50.0)
        assert "nonexistent-rule" in str(exc.value)

    def test_preserves_other_rules_byte_for_byte(self, tmp_path):
        config = tmp_path / "config.toml"
        materialize_default_config(config)
        original = _read(config)

        # Capture the gpu-core-hot block before
        gpu_block_before = re.search(
            r"\[\[rules\]\]\nname = \"gpu-core-hot\".*?(?=\n\[\[|\Z)",
            original, re.DOTALL,
        )
        assert gpu_block_before is not None

        update_rule_threshold(config, "ram-pressure", 79.0)
        after = _read(config)

        gpu_block_after = re.search(
            r"\[\[rules\]\]\nname = \"gpu-core-hot\".*?(?=\n\[\[|\Z)",
            after, re.DOTALL,
        )
        assert gpu_block_after is not None
        assert gpu_block_after.group(0) == gpu_block_before.group(0), (
            "non-target rule blocks must be byte-identical"
        )

    def test_handles_integer_threshold_in_source(self, tmp_path):
        """User wrote `threshold = 85` (no decimal); writer should still
        find and replace it."""
        config = tmp_path / "config.toml"
        config.write_text(
            "[[rules]]\n"
            'name = "ram-pressure"\n'
            'signal = "system.ram_used_percent"\n'
            "threshold = 85\n"  # integer
            "window_s = 60\n"
            "min_fraction_over = 0.75\n"
            'action = "kill"\n',
            encoding="utf-8",
        )
        update_rule_threshold(config, "ram-pressure", 91.0)
        cfg = verify_roundtrip(config)
        assert next(r for r in cfg.rules if r.name == "ram-pressure").threshold == 91.0

    def test_threshold_renders_with_decimal(self, tmp_path):
        """Whole-number new values get `.0` appended for hand-edit
        consistency (matches the schema's number parser tolerating both)."""
        config = tmp_path / "config.toml"
        materialize_default_config(config)
        update_rule_threshold(config, "ram-pressure", 80.0)
        text = _read(config)
        assert "threshold = 80.0" in text

    def test_atomic_write_does_not_leave_tmp_files(self, tmp_path):
        config = tmp_path / "config.toml"
        materialize_default_config(config)
        update_rule_threshold(config, "ram-pressure", 81.0)
        leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == [], f"leftover tmp files: {leftovers}"

    def test_round_trip_load_after_update_matches_intent(self, tmp_path):
        config = tmp_path / "config.toml"
        materialize_default_config(config)
        for new_value in (75.5, 80.0, 92.25):
            update_rule_threshold(config, "ram-pressure", new_value)
            cfg = load_config(config)
            ram = next(r for r in cfg.rules if r.name == "ram-pressure")
            assert ram.threshold == new_value

    def test_updates_only_first_threshold_line_per_block(self, tmp_path):
        """If a user weirdly puts two threshold lines in one block, we
        update the first and leave the second alone (the second would
        cause a TOML duplicate-key error on load, which is not the
        writer's problem to fix)."""
        config = tmp_path / "config.toml"
        config.write_text(
            "[[rules]]\n"
            'name = "ram-pressure"\n'
            'signal = "system.ram_used_percent"\n'
            "threshold = 85.0\n"
            "threshold = 99.0\n"  # weird but should be left alone
            "window_s = 60\n"
            "min_fraction_over = 0.75\n"
            'action = "kill"\n',
            encoding="utf-8",
        )
        update_rule_threshold(config, "ram-pressure", 70.0)
        text = _read(config)
        assert text.index("threshold = 70.0") < text.index("threshold = 99.0")
