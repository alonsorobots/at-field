"""Tests for :mod:`atfield.audit`."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from atfield.actuator import KilledProcess, KillReport, ProcInfo
from atfield.audit import (
    EVENTS_FILENAME,
    WATCHDOG_LOG_FILENAME,
    AuditWriter,
    configure_service_logging,
)
from atfield.policy import Action, DisabledRule


def _read_lines(p: Path) -> list[dict]:
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def _action(kind: str = "kill", rule: str = "test") -> Action:
    return Action(
        kind=kind,
        rule_name=rule,
        base_rule_name=rule,
        signal="system.ram_used_percent",
        threshold=80.0,
        fraction_over=0.9,
        samples_considered=10,
        latest_value=92.0,
        triggered_at_ns=1234,
        cooldown_seconds=60,
    )


class TestAuditWriter:
    def test_creates_state_dir_and_file_path(self, tmp_path):
        sd = tmp_path / "fresh"
        w = AuditWriter(sd)
        assert sd.exists()
        assert w.path.name == EVENTS_FILENAME

    def test_write_action_produces_one_line(self, tmp_path):
        w = AuditWriter(tmp_path)
        w.write_action(_action())
        lines = _read_lines(w.path)
        assert len(lines) == 1
        ev = lines[0]
        assert ev["type"] == "action"
        assert ev["kind"] == "kill"
        assert ev["rule"] == "test"
        assert ev["latest_value"] == 92.0
        assert "ts" in ev and "ts_iso" in ev

    def test_multiple_writes_append(self, tmp_path):
        w = AuditWriter(tmp_path)
        for i in range(5):
            w.write_action(_action(rule=f"r{i}"))
        lines = _read_lines(w.path)
        assert len(lines) == 5
        assert [ev["rule"] for ev in lines] == [f"r{i}" for i in range(5)]

    def test_write_kill_report_full_shape(self, tmp_path):
        w = AuditWriter(tmp_path)
        action = _action()
        root = ProcInfo(pid=10, ppid=1, name="torchrun", cmdline=("torchrun", "train.py"))
        child = ProcInfo(pid=20, ppid=10, name="python.exe", cmdline=("python", "worker.py"))
        report = KillReport(
            action=action,
            offender_pid=20,
            kill_root=root,
            killed=(
                KilledProcess(info=root, method="terminate", survived=False),
                KilledProcess(info=child, method="terminate", survived=False),
            ),
            finished_at_ns=99,
        )
        w.write_kill_report(report)
        ev = _read_lines(w.path)[0]
        assert ev["type"] == "kill_report"
        assert ev["rule"] == "test"
        assert ev["offender_pid"] == 20
        assert ev["kill_root"]["pid"] == 10
        assert ev["kill_root"]["name"] == "torchrun"
        assert len(ev["killed"]) == 2
        assert ev["succeeded"] is True

    def test_kill_report_includes_script_name_at_top_level(self, tmp_path):
        """The headline name -- what the operator sees first when reading
        events.jsonl -- should be the script the launcher was running, not
        the launcher itself."""
        w = AuditWriter(tmp_path)
        root = ProcInfo(pid=10, ppid=1, name="python.exe", cmdline=("python.exe", "-u", "scripts/train.py"))
        worker = ProcInfo(pid=20, ppid=10, name="python.exe", cmdline=("python.exe", "worker.py"))
        report = KillReport(
            action=_action(),
            offender_pid=10,
            kill_root=root,
            killed=(
                KilledProcess(info=root, method="terminate", survived=False),
                KilledProcess(info=worker, method="terminate", survived=False),
            ),
            finished_at_ns=99,
        )
        w.write_kill_report(report)
        ev = _read_lines(w.path)[0]
        assert ev["script"] == "train.py", "top-level script == kill_root's script for headline use"
        assert ev["kill_root"]["script"] == "train.py"
        assert ev["killed"][0]["script"] == "train.py"
        assert ev["killed"][1]["script"] == "worker.py"

    def test_kill_report_script_field_handles_module_mode(self, tmp_path):
        w = AuditWriter(tmp_path)
        root = ProcInfo(
            pid=10, ppid=1, name="python.exe",
            cmdline=("python.exe", "-m", "torch.distributed.run", "--nproc-per-node=2"),
        )
        report = KillReport(
            action=_action(),
            offender_pid=10,
            kill_root=root,
            killed=(KilledProcess(info=root, method="terminate", survived=False),),
            finished_at_ns=99,
        )
        w.write_kill_report(report)
        ev = _read_lines(w.path)[0]
        assert ev["script"] == "torch.distributed.run"

    def test_kill_report_skipped_has_null_script(self, tmp_path):
        """When no kill happens, there's nothing to attribute the script to."""
        w = AuditWriter(tmp_path)
        report = KillReport(
            action=_action(kind="log"),
            offender_pid=None,
            kill_root=None,
            skipped_reason="action.kind == 'log'",
            finished_at_ns=99,
        )
        w.write_kill_report(report)
        ev = _read_lines(w.path)[0]
        assert ev["script"] is None
        assert ev["kill_root"] is None
        assert ev["skipped_reason"]

    def test_write_startup_includes_disabled_rules(self, tmp_path):
        w = AuditWriter(tmp_path)
        w.write_startup(
            version="0.1.0",
            config_path=None,
            available_signals=["system.ram_used_percent", "gpu.0.core_temp_c"],
            disabled_rules=[
                DisabledRule(
                    base_rule_name="vram-junction-hot",
                    signal="gpu.*.mem_junction_temp_c",
                    reason="no available signals matched",
                )
            ],
        )
        ev = _read_lines(w.path)[0]
        assert ev["type"] == "startup"
        assert ev["available_signals"] == ["gpu.0.core_temp_c", "system.ram_used_percent"]
        assert len(ev["disabled_rules"]) == 1
        assert ev["disabled_rules"][0]["rule"] == "vram-junction-hot"

    def test_lines_are_unix_endings_for_grep(self, tmp_path):
        """Critical: must be \\n, not \\r\\n. tail -f and grep break on CRLF."""
        w = AuditWriter(tmp_path)
        w.write_action(_action())
        w.write_action(_action(rule="r2"))
        raw = w.path.read_bytes()
        assert b"\r\n" not in raw, "events.jsonl must use Unix line endings"
        assert raw.count(b"\n") == 2

    def test_collector_health_round_trip(self, tmp_path):
        w = AuditWriter(tmp_path)
        w.write_collector_health("nvml", "degraded", "3 consecutive failures")
        ev = _read_lines(w.path)[0]
        assert ev["type"] == "collector_health"
        assert ev["collector"] == "nvml"
        assert ev["state"] == "degraded"


class TestServiceLogging:
    def test_configure_creates_log_file(self, tmp_path):
        configure_service_logging(tmp_path, level="DEBUG")
        log = logging.getLogger("atfield.test")
        log.info("hello")
        log_path = tmp_path / WATCHDOG_LOG_FILENAME
        # RotatingFileHandler may not flush immediately; force it
        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:
                pass
        assert log_path.exists()
        text = log_path.read_text(encoding="utf-8")
        assert "hello" in text

    def test_idempotent(self, tmp_path):
        """Re-calling configure should not double-log."""
        configure_service_logging(tmp_path, level="INFO")
        configure_service_logging(tmp_path, level="INFO")
        configure_service_logging(tmp_path, level="INFO")
        log = logging.getLogger("atfield.test2")
        log.info("once")
        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:
                pass
        text = (tmp_path / WATCHDOG_LOG_FILENAME).read_text(encoding="utf-8")
        # "once" should appear exactly once, not three times
        assert text.count("once") == 1
