"""AT-Field service main loop -- the entry point NSSM invokes.

What this does, end to end:

1. Load config (or fall back to safe-mode defaults if config is malformed,
   per PLANNING.md §5.4).
2. Configure logging to ``%ProgramData%\\ATField\\watchdog.log``.
3. Probe every built-in collector. Anything that fails to probe is logged
   and skipped; capability negotiation happens here.
4. Build a :class:`atfield.policy.PolicyEngine` from the union of the
   probed collectors' signals; rules whose signals aren't available are
   reported as disabled (and logged loudly).
5. Tick at ``cfg.general.tick_hz`` Hz: poll every healthy collector, feed
   the samples into the engine, and dispatch any returned actions to the
   :class:`atfield.actuator.Actuator`.
6. Write a heartbeat file every 10 s so ``atf status`` can confirm the
   service is alive.
7. Honor a pause sentinel file (written by ``atf pause``).
8. On SIGTERM/SIGINT (NSSM stop), drain in-flight, write a shutdown event,
   and exit cleanly.

The main loop is a plain function (``run_service``) rather than a class
so it's straightforward to drive from the CLI for ``atf test-kill`` and
the pytest integration tests. ``main()`` is the NSSM entry point.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from atfield import __version__
from atfield.actuator import Actuator
from atfield.audit import AuditWriter, configure_service_logging
from atfield.collectors import HealthState, ProbeResult
from atfield.collectors.lhm import LhmCollector
from atfield.collectors.nvml import PER_PROCESS_VRAM_KEY, NvmlCollector
from atfield.collectors.system import SystemCollector
from atfield.config import AtFieldConfig, ConfigError, default_config, load_config
from atfield.policy import PolicyEngine
from atfield.signals import Sample

__all__ = [
    "run_service",
    "main",
    "PAUSE_SENTINEL_FILENAME",
    "HEARTBEAT_FILENAME",
    "DEFAULT_CONFIG_PATH",
]


_log = logging.getLogger("atfield.service")


PAUSE_SENTINEL_FILENAME = "pause.sentinel"
HEARTBEAT_FILENAME = "heartbeat.txt"
DEFAULT_CONFIG_PATH = "config.toml"

_HEARTBEAT_INTERVAL_S = 10.0


# ---------------------------------------------------------------------------
# Config loading with safe-mode fallback (PLANNING.md §5.4)
# ---------------------------------------------------------------------------


def _load_config_safe(config_path: Path | None) -> tuple[AtFieldConfig, bool]:
    """Load config; on failure fall back to observe-only defaults.

    Returns ``(cfg, observe_only)`` where ``observe_only`` is True when
    the load failed and the service should never actually kill. The
    service downgrades all kill actions to "log" in observe-only mode.
    """
    try:
        return load_config(config_path), False
    except ConfigError as exc:
        _log.error("config load failed (%s); entering OBSERVE-ONLY mode", exc)
        return default_config(), True


# ---------------------------------------------------------------------------
# Pause sentinel
# ---------------------------------------------------------------------------


def _read_pause_sentinel(state_dir: Path) -> int | None:
    """Read ``pause.sentinel`` if present and not expired.

    File format: a single ISO-8601 UTC timestamp on the first line. A
    sentinel without a parseable timestamp is treated as a permanent pause
    (until the file is removed) so a corrupt sentinel never silently
    re-arms killing.

    Returns the monotonic_ns at which the pause expires, or None if no
    valid pause is in effect.
    """
    p = state_dir / PAUSE_SENTINEL_FILENAME
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8").strip().splitlines()[0]
        until = datetime.fromisoformat(text)
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        seconds_remaining = (until - datetime.now(timezone.utc)).total_seconds()
        if seconds_remaining <= 0:
            return None  # expired
        return time.monotonic_ns() + int(seconds_remaining * 1_000_000_000)
    except Exception:
        # Corrupt sentinel: pause forever (until removed). Better safe.
        return time.monotonic_ns() + (10**18)


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


def _write_heartbeat(state_dir: Path, *, observe_only: bool) -> None:
    payload = {
        "ts": time.time(),
        "ts_iso": datetime.now(timezone.utc).isoformat(),
        "version": __version__,
        "observe_only": observe_only,
    }
    try:
        (state_dir / HEARTBEAT_FILENAME).write_text(
            f"{payload['ts_iso']}\nversion={payload['version']}\nobserve_only={payload['observe_only']}\n",
            encoding="utf-8",
        )
    except Exception:
        # Heartbeat is best-effort -- never let it crash the loop.
        _log.debug("failed to write heartbeat", exc_info=True)


# ---------------------------------------------------------------------------
# Collector probing
# ---------------------------------------------------------------------------


def _probe_all_collectors(audit: AuditWriter) -> tuple[list[object], dict[str, ProbeResult]]:
    """Instantiate and probe every built-in collector.

    Returns the list of HEALTHY collectors (caller should poll only these)
    and a per-collector probe result dict (so the audit log records why
    any unavailable collector was rejected).
    """
    collectors_to_try = [SystemCollector(), NvmlCollector(), LhmCollector()]
    healthy: list[object] = []
    results: dict[str, ProbeResult] = {}
    for c in collectors_to_try:
        result = c.probe()
        results[c.name] = result
        if result.available:
            _log.info("collector %s OK -- %s", c.name, result.reason)
            healthy.append(c)
        else:
            _log.warning("collector %s unavailable -- %s", c.name, result.reason)
            audit.write_collector_health(c.name, "unavailable", result.reason)
    return healthy, results


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


class _StopFlag:
    """Cross-thread stop signal usable as `bool(flag)` and `flag.set()`."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def __bool__(self) -> bool:
        return self._event.is_set()

    def set(self) -> None:
        self._event.set()

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout)


def run_service(
    *,
    config_path: Path | None = None,
    state_dir: Path | None = None,
    max_ticks: int | None = None,
    stop_flag: _StopFlag | None = None,
) -> int:
    """The main loop. Returns a process exit code.

    Parameters
    ----------
    config_path :
        Path to ``config.toml``. If None, looks in ``state_dir / 'config.toml'``.
    state_dir :
        Override for the runtime state directory. If None, uses
        ``cfg.general.state_dir`` from the loaded config.
    max_ticks :
        Run at most N ticks then return. Used by ``atf test-kill`` and tests.
        ``None`` means run forever (until SIGTERM/SIGINT).
    stop_flag :
        External stop-event injection (for tests). If None, the service
        installs SIGINT/SIGTERM handlers itself.
    """
    cfg, observe_only = _load_config_safe(config_path)
    sd = state_dir or cfg.general.state_dir
    sd.mkdir(parents=True, exist_ok=True)

    configure_service_logging(sd, level=cfg.general.log_level)

    if observe_only:
        _log.warning("OBSERVE-ONLY mode: kill actions will be downgraded to log entries")
    _log.info("AT-Field %s starting in %s (state=%s)", __version__, "observe-only" if observe_only else "armed", sd)

    audit = AuditWriter(sd)

    # Probe collectors -> negotiate signal map
    collectors, probe_results = _probe_all_collectors(audit)
    available_signals: set[str] = set()
    for r in probe_results.values():
        if r.available:
            available_signals.update(r.signals)
    # Strip the special process-map channel from the policy view; it's used
    # by the actuator only.
    policy_signals = available_signals - {PER_PROCESS_VRAM_KEY}

    # Build engine + actuator
    engine = PolicyEngine(cfg, available_signals=policy_signals)
    actuator = Actuator(cfg)

    # Locate the NVML collector (if present) so we can hand its per-GPU
    # process map to the actuator on each kill.
    nvml: NvmlCollector | None = next(
        (c for c in collectors if isinstance(c, NvmlCollector)),
        None,
    )

    audit.write_startup(
        version=__version__,
        config_path=str(config_path) if config_path else None,
        available_signals=sorted(available_signals),
        disabled_rules=list(engine.disabled_rules),
        gpu_info={
            k: v for k, v in (probe_results.get("nvml").metadata.items() if probe_results.get("nvml") else [])
        },
    )

    # Stop signaling
    stop = stop_flag or _StopFlag()
    if stop_flag is None:
        def _on_signal(signum, _frame):
            _log.info("received signal %s; shutting down", signum)
            stop.set()
        signal.signal(signal.SIGINT, _on_signal)
        try:
            signal.signal(signal.SIGTERM, _on_signal)
        except (AttributeError, ValueError):
            # SIGTERM not available on Windows in some interpreters
            pass

    tick_period_s = 1.0 / max(cfg.general.tick_hz, 1)
    last_heartbeat_ns = 0
    last_pause_check_ns = 0
    pause_check_interval_ns = 5_000_000_000  # 5 s
    ticks = 0
    exit_code = 0

    try:
        while not stop:
            tick_started_at = time.monotonic()

            # Honor pause sentinel (re-checked every 5 s, not every tick).
            now_ns = time.monotonic_ns()
            if now_ns - last_pause_check_ns >= pause_check_interval_ns:
                pause_until = _read_pause_sentinel(sd)
                engine.set_paused(pause_until or 0)
                last_pause_check_ns = now_ns

            # Sample every healthy collector
            samples: dict[str, Sample] = {}
            for c in collectors:
                if c.health() is HealthState.FAILED:
                    continue
                try:
                    samples.update(c.sample())  # type: ignore[attr-defined]
                except Exception:
                    _log.exception("collector %s.sample() raised; treating tick as no-op for it", getattr(c, "name", "?"))
            samples.pop(PER_PROCESS_VRAM_KEY, None)

            # Evaluate
            try:
                actions = engine.tick(samples, now_ns=now_ns)
            except Exception:
                _log.exception("policy tick raised; skipping action dispatch this tick")
                actions = []

            # Dispatch
            for action in actions:
                effective = action
                if observe_only and action.kind == "kill":
                    # Safe-mode demotion (PLANNING.md §5.4)
                    effective = type(action)(
                        kind="log",
                        rule_name=action.rule_name,
                        base_rule_name=action.base_rule_name,
                        signal=action.signal,
                        threshold=action.threshold,
                        fraction_over=action.fraction_over,
                        samples_considered=action.samples_considered,
                        latest_value=action.latest_value,
                        triggered_at_ns=action.triggered_at_ns,
                        cooldown_seconds=action.cooldown_seconds,
                    )

                audit.write_action(effective)

                # Pick candidate PIDs for GPU rules from the NVML proc map.
                candidate_pids = None
                if nvml is not None and effective.signal.startswith("gpu."):
                    proc_map = nvml.process_map()
                    # Extract gpu index from signal name: gpu.<idx>.<metric>
                    try:
                        idx = int(effective.signal.split(".")[1])
                        candidate_pids = [pid for pid, _ in proc_map.get(idx, [])]
                    except (IndexError, ValueError):
                        candidate_pids = None

                report = actuator.execute(effective, candidate_pids=candidate_pids)
                audit.write_kill_report(report)
                if report.kill_root:
                    _log.warning(
                        "ACTION %s: rule=%s signal=%s killed=%d survived=%d",
                        effective.kind,
                        effective.rule_name,
                        effective.signal,
                        len(report.killed),
                        sum(1 for k in report.killed if k.survived),
                    )

            # Heartbeat
            if now_ns - last_heartbeat_ns >= int(_HEARTBEAT_INTERVAL_S * 1_000_000_000):
                _write_heartbeat(sd, observe_only=observe_only)
                last_heartbeat_ns = now_ns

            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break

            # Sleep the remainder of the tick period.
            elapsed = time.monotonic() - tick_started_at
            sleep_for = max(0.0, tick_period_s - elapsed)
            if sleep_for > 0:
                stop.wait(sleep_for)

    except Exception:
        _log.exception("service main loop crashed")
        exit_code = 1
    finally:
        for c in collectors:
            try:
                c.shutdown()  # type: ignore[attr-defined]
            except Exception:
                pass
        audit.write_shutdown("normal" if exit_code == 0 else "error")
        _log.info("AT-Field service stopped (exit=%d, ticks=%d)", exit_code, ticks)

    return exit_code


# ---------------------------------------------------------------------------
# NSSM entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """Console-script entry point (``atfield-service``).

    Picks the config from ``%PROGRAMDATA%\\ATField\\config.toml`` if it
    exists, otherwise falls back to defaults. NSSM invokes this directly.
    """
    from atfield.config import default_state_dir

    sd = default_state_dir()
    cfg_path = sd / DEFAULT_CONFIG_PATH
    return run_service(config_path=cfg_path if cfg_path.exists() else None)


if __name__ == "__main__":
    sys.exit(main())
