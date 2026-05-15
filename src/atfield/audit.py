"""Audit logging: events.jsonl writer + rotating service log.

Two destinations, one purpose: explain after the fact why the watchdog did
what it did.

* ``events.jsonl`` -- one JSON object per significant event (kill, log,
  throttle, rule disabled at startup, collector health change). Append-only,
  human and grep friendly. The "after-action report" the operator opens
  when they ask "why was my training job killed at 3 AM?"
* ``watchdog.log`` -- a rotating text log of INFO+ events, intended for
  debugging the watchdog itself (collector errors, NVML init failures,
  pause sentinel, etc.). Standard ``logging.handlers.RotatingFileHandler``.

Both files live under ``cfg.general.state_dir`` (default
``%ProgramData%\\ATField\\``).
"""

from __future__ import annotations

import json
import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from atfield.actuator import KillReport
from atfield.policy import Action, DisabledRule

__all__ = [
    "AuditWriter",
    "configure_service_logging",
    "EVENTS_FILENAME",
    "WATCHDOG_LOG_FILENAME",
]


EVENTS_FILENAME = "events.jsonl"
WATCHDOG_LOG_FILENAME = "watchdog.log"

# Rotating log defaults: 5 MB per file, 5 archives. ~25 MB ceiling; nothing
# in the watchdog should be log-heavy enough to outrun that at 1 Hz.
_LOG_MAX_BYTES = 5 * 1024 * 1024
_LOG_BACKUP_COUNT = 5


# ---------------------------------------------------------------------------
# Service log setup
# ---------------------------------------------------------------------------


def configure_service_logging(state_dir: Path, *, level: str = "INFO") -> None:
    """Wire ``logging`` to a rotating file under ``state_dir/watchdog.log``.

    Idempotent: removes any prior AT-Field handlers before attaching new
    ones, so re-calling on config reload doesn't double-log.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = state_dir / WATCHDOG_LOG_FILENAME

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Drop any existing AT-Field handlers (idempotency).
    for h in list(root.handlers):
        if getattr(h, "_atfield_handler", False):
            root.removeHandler(h)

    handler = RotatingFileHandler(
        log_path,
        maxBytes=_LOG_MAX_BYTES,
        backupCount=_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    handler._atfield_handler = True  # type: ignore[attr-defined]
    root.addHandler(handler)


# ---------------------------------------------------------------------------
# Events writer
# ---------------------------------------------------------------------------


class AuditWriter:
    """Append-only JSONL writer for audit events.

    Each method writes exactly one line. We open the file in append mode on
    each call rather than holding a long-lived handle: this trades a tiny
    amount of performance for the property that the file can be safely
    rotated externally (logrotate-style) and read concurrently by ``atf
    tail`` without locking weirdness on Windows.
    """

    def __init__(self, state_dir: Path) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        self._path = state_dir / EVENTS_FILENAME

    @property
    def path(self) -> Path:
        return self._path

    # -- High-level event helpers (one per event type) ---------------------

    def write_startup(
        self,
        *,
        version: str,
        config_path: str | None,
        available_signals: list[str],
        disabled_rules: list[DisabledRule],
        gpu_info: dict[str, str] | None = None,
    ) -> None:
        self._write(
            {
                "type": "startup",
                "version": version,
                "config_path": config_path,
                "available_signals": sorted(available_signals),
                "disabled_rules": [
                    {"rule": d.base_rule_name, "signal": d.signal, "reason": d.reason}
                    for d in disabled_rules
                ],
                "gpu_info": gpu_info or {},
            }
        )

    def write_action(self, action: Action) -> None:
        self._write(
            {
                "type": "action",
                "kind": action.kind,
                "rule": action.rule_name,
                "base_rule": action.base_rule_name,
                "signal": action.signal,
                "threshold": action.threshold,
                "fraction_over": action.fraction_over,
                "samples_considered": action.samples_considered,
                "latest_value": action.latest_value,
                "cooldown_seconds": action.cooldown_seconds,
            }
        )

    def write_kill_report(self, report: KillReport) -> None:
        self._write(
            {
                "type": "kill_report",
                "rule": report.action.rule_name,
                "signal": report.action.signal,
                "offender_pid": report.offender_pid,
                "kill_root": (
                    {
                        "pid": report.kill_root.pid,
                        "name": report.kill_root.name,
                        "cmdline": list(report.kill_root.cmdline),
                    }
                    if report.kill_root
                    else None
                ),
                "killed": [
                    {
                        "pid": k.info.pid,
                        "name": k.info.name,
                        "cmdline": list(k.info.cmdline),
                        "method": k.method,
                        "survived": k.survived,
                    }
                    for k in report.killed
                ],
                "succeeded": report.succeeded,
                "skipped_reason": report.skipped_reason,
            }
        )

    def write_collector_health(self, name: str, state: str, reason: str = "") -> None:
        self._write(
            {
                "type": "collector_health",
                "collector": name,
                "state": state,
                "reason": reason,
            }
        )

    def write_pause(self, until_iso: str | None) -> None:
        self._write({"type": "pause", "until": until_iso})

    def write_shutdown(self, reason: str) -> None:
        self._write({"type": "shutdown", "reason": reason})

    # -- Low-level ---------------------------------------------------------

    def _write(self, payload: dict[str, Any]) -> None:
        payload.setdefault("ts", time.time())
        payload.setdefault("ts_iso", time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()))
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        # On Windows, opening in append-binary and writing UTF-8 + newline is
        # the cleanest concurrent-safe pattern. Standard text mode would do
        # \r\n line endings, which break grep on tail readers.
        with self._path.open("ab") as fh:
            fh.write(line.encode("utf-8") + b"\n")
