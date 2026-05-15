"""Tests for the `atf doctor` and `atf set-profile` CLI commands.

We exercise them via Typer's :class:`CliRunner` (no subprocess; same
process so we can prefab the state dir and assert on stdout). The
collector-probe leg of `doctor` runs against the real environment and
just asserts the structure is sane (system collector is available
everywhere, NVML/LHM may or may not be).
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from atfield.cli import app
from atfield.config_writer import materialize_default_config

runner = CliRunner()


# ---------------------------------------------------------------------------
# atf doctor
# ---------------------------------------------------------------------------


class TestDoctor:
    def test_no_state_dir_reports_problem_and_exits_nonzero(self, tmp_path):
        # Point at a nonexistent dir so we exercise the "never installed" branch.
        result = runner.invoke(app, ["doctor", "--state-dir", str(tmp_path / "missing")])
        assert result.exit_code == 1, result.stdout
        assert "state dir does not exist" in result.stdout

    def test_with_recent_heartbeat_reports_alive(self, tmp_path):
        from datetime import datetime, timezone
        sd = tmp_path / "state"
        sd.mkdir()
        # Write a fresh heartbeat so the "stale" check passes.
        (sd / "heartbeat.txt").write_text(
            datetime.now(timezone.utc).isoformat() + "\n"
            "version=test\n"
            "observe_only=False\n",
            encoding="utf-8",
        )
        # Provide a startup event with one available + one disabled rule.
        (sd / "events.jsonl").write_text(json.dumps({
            "type": "startup",
            "ts": 0,
            "ts_iso": "1970-01-01T00:00:00+00:00",
            "version": "test",
            "available_signals": ["system.ram_used_percent"],
            "disabled_rules": [
                {"rule": "vram-junction-hot",
                 "signal": "gpu.0.mem_junction_temp_c",
                 "reason": "no NVML"},
            ],
        }) + "\n", encoding="utf-8")
        result = runner.invoke(app, ["doctor", "--state-dir", str(sd)])
        # 1 issue (the disabled rule) -> exit 1
        assert result.exit_code == 1, result.stdout
        assert "heartbeat fresh" in result.stdout
        assert "vram-junction-hot" in result.stdout
        assert "see docs/faq.md" in result.stdout

    def test_paused_state_surfaces_as_problem(self, tmp_path):
        from datetime import datetime, timedelta, timezone
        sd = tmp_path / "state"
        sd.mkdir()
        (sd / "heartbeat.txt").write_text(
            datetime.now(timezone.utc).isoformat() + "\n", encoding="utf-8",
        )
        (sd / "pause.sentinel").write_text(
            (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat() + "\n",
            encoding="utf-8",
        )
        result = runner.invoke(app, ["doctor", "--state-dir", str(sd)])
        assert result.exit_code == 1
        assert "PAUSED" in result.stdout
        assert "atf unpause" in result.stdout

    def test_invalid_config_is_flagged(self, tmp_path):
        from datetime import datetime, timezone
        sd = tmp_path / "state"
        sd.mkdir()
        (sd / "heartbeat.txt").write_text(
            datetime.now(timezone.utc).isoformat() + "\n", encoding="utf-8",
        )
        # Write a busted config.
        (sd / "config.toml").write_text(
            "[general]\ntick_hz = -1\n", encoding="utf-8",
        )
        result = runner.invoke(app, ["doctor", "--state-dir", str(sd)])
        assert result.exit_code == 1
        assert "config INVALID" in result.stdout


# ---------------------------------------------------------------------------
# atf set-profile
# ---------------------------------------------------------------------------


class TestSetProfile:
    def test_aggressive_writes_lower_thresholds(self, tmp_path):
        cfg = tmp_path / "config.toml"
        materialize_default_config(cfg)
        result = runner.invoke(app, [
            "set-profile", "aggressive",
            "--config", str(cfg),
        ])
        assert result.exit_code == 0, result.stdout
        # All five rules should have been touched.
        for name in ("gpu-core-hot", "vram-junction-hot", "ram-pressure",
                     "pagefile-pressure", "cpu-pkg-hot"):
            assert name in result.stdout
        # And the file should be loadable + reflect aggressive values.
        from atfield.config import load_config
        from atfield.rule_profiles import PROFILE_PRESETS
        loaded = load_config(cfg)
        for r in loaded.rules:
            expected = PROFILE_PRESETS["aggressive"].get(r.name)
            if expected is not None:
                assert r.threshold == expected, f"{r.name} expected {expected} got {r.threshold}"

    def test_unknown_profile_rejects(self, tmp_path):
        cfg = tmp_path / "config.toml"
        materialize_default_config(cfg)
        result = runner.invoke(app, [
            "set-profile", "yolo",
            "--config", str(cfg),
        ])
        assert result.exit_code != 0
        assert "yolo" in result.stdout or "yolo" in (result.stderr or "")

    def test_creates_default_config_when_target_missing(self, tmp_path):
        # No config exists yet; set-profile should materialize one then mutate.
        target = tmp_path / "first_run.toml"
        result = runner.invoke(app, [
            "set-profile", "relaxed",
            "--config", str(target),
        ])
        assert result.exit_code == 0, result.stdout
        assert target.exists()
        from atfield.config import load_config
        load_config(target)  # round-trips cleanly


# ---------------------------------------------------------------------------
# atf setup wizard
# ---------------------------------------------------------------------------


class TestSetupWizard:
    def test_yes_flag_writes_normal_observe_only_config(self, tmp_path):
        """--yes accepts every prompt's displayed default. The default
        for observe-only is True (we recommend it for the first hour),
        so --yes should land in observe-only mode."""
        sd = tmp_path / "state"
        result = runner.invoke(app, [
            "setup", "--state-dir", str(sd), "--yes",
        ])
        assert result.exit_code == 0, result.stdout
        cfg_path = sd / "config.toml"
        assert cfg_path.exists()
        from atfield.config import load_config
        cfg = load_config(cfg_path)
        for r in cfg.rules:
            assert r.action == "log", (
                f"--yes should accept observe-only default; "
                f"got {r.name}={r.action}"
            )

    def test_yes_flag_keeps_existing_config(self, tmp_path):
        sd = tmp_path / "state"
        sd.mkdir()
        cfg_path = sd / "config.toml"
        cfg_path.write_text("# pre-existing\n", encoding="utf-8")
        result = runner.invoke(app, [
            "setup", "--state-dir", str(sd), "--yes",
        ])
        assert result.exit_code == 0
        # --yes opts to NOT overwrite an existing config.
        assert cfg_path.read_text(encoding="utf-8") == "# pre-existing\n"

    def test_interactive_aggressive_observe_only_flow(self, tmp_path):
        sd = tmp_path / "state"
        # Stdin: create_dir? y, profile=aggressive, observe_only=y
        # Click prompts use yes/no toggles for confirms, free text for prompt().
        # The flow expects: state dir prompt is hardcoded so we just answer:
        #   - "Create it now?" y
        #   - profile prompt (typer.prompt) -> "aggressive"
        #   - "Start in observe-only mode?" y
        result = runner.invoke(app, [
            "setup", "--state-dir", str(sd),
        ], input="y\naggressive\ny\n")
        assert result.exit_code == 0, result.stdout
        cfg_path = sd / "config.toml"
        from atfield.config import load_config
        from atfield.rule_profiles import PROFILE_PRESETS
        cfg = load_config(cfg_path)
        # Aggressive thresholds applied
        for r in cfg.rules:
            preset = PROFILE_PRESETS["aggressive"].get(r.name)
            if preset is not None:
                assert r.threshold == preset, (
                    f"{r.name}: expected aggressive {preset}, got {r.threshold}"
                )
        # Observe-only flipped every rule to log
        for r in cfg.rules:
            assert r.action == "log", f"observe-only should set action=log; got {r.name}={r.action}"
