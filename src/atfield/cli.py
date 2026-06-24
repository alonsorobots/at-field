"""AT-Field CLI: ``atf`` command surface.

Commands implemented (matches PLANNING.md §6 and §7-item-9):

* ``atf status``   -- show service health, working signal map, disabled rules
* ``atf inputs``   -- one-shot probe + sample dump (great for "is LHM up?")
* ``atf pause``    -- write the pause sentinel
* ``atf unpause``  -- remove the pause sentinel
* ``atf tail``     -- follow events.jsonl
* ``atf run``      -- run the service in the foreground (for debugging /
                       NSSM "as-process" testing)
* ``atf install``  -- delegate to scripts/install_service.ps1
* ``atf uninstall``-- delegate to scripts/uninstall_service.ps1
* ``atf version``  -- print the package version

The CLI never imports the service or actuator at module load -- those carry
heavy native dependencies (NVML, psutil) and we want ``atf --help`` to be
fast even on a box where NVIDIA drivers aren't installed yet.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from atfield import __version__
from atfield.config import default_state_dir

__all__ = ["app", "main"]


app = typer.Typer(
    name="atf",
    add_completion=False,
    no_args_is_help=True,
    help="AT-Field — Windows GPU/VRAM/RAM watchdog for AI workloads.",
)
console = Console()


# ---------------------------------------------------------------------------
# Common option helpers
# ---------------------------------------------------------------------------


def _state_dir_option() -> Path:
    return typer.Option(
        default_state_dir(),
        "--state-dir",
        "-s",
        help="State directory (heartbeat, events.jsonl, watchdog.log).",
    )


def _resolve_config_path(state_dir: Path) -> Path | None:
    """Resolve ``state_dir/config.toml`` if it exists."""
    p = state_dir / "config.toml"
    return p if p.exists() else None


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@app.command()
def status(
    state_dir: Path = _state_dir_option(),
) -> None:
    """Show service health and the negotiated signal map."""
    if not state_dir.exists():
        console.print(f"[yellow]state dir does not exist:[/] {state_dir}")
        console.print("Service has never run on this machine. Try [cyan]atf install[/].")
        raise typer.Exit(code=1)

    heartbeat = state_dir / "heartbeat.txt"
    if heartbeat.exists():
        try:
            text = heartbeat.read_text(encoding="utf-8").strip().splitlines()
            ts_iso = text[0]
            ts = datetime.fromisoformat(ts_iso)
            age = datetime.now(timezone.utc) - ts
            extra = {k: v for line in text[1:] for k, v in [line.split("=", 1)] if "=" in line}
            alive = age < timedelta(seconds=30)
            color = "green" if alive else "red"
            verdict = "ALIVE" if alive else "STALE"
            console.print(f"Service: [{color}]{verdict}[/] (last heartbeat {ts_iso}, {int(age.total_seconds())}s ago)")
            console.print(f"Version: {extra.get('version', '?')}")
            mode = "OBSERVE-ONLY" if extra.get("observe_only", "False").lower() == "true" else "ARMED"
            mode_color = "yellow" if mode == "OBSERVE-ONLY" else "green"
            console.print(f"Mode:    [{mode_color}]{mode}[/]")
        except Exception as exc:
            console.print(f"[red]could not parse heartbeat:[/] {exc}")
    else:
        console.print("[yellow]no heartbeat file found[/]; service may not be running")

    # Pause sentinel?
    sentinel = state_dir / "pause.sentinel"
    if sentinel.exists():
        try:
            until = datetime.fromisoformat(sentinel.read_text(encoding="utf-8").strip().splitlines()[0])
            console.print(f"Paused:  [yellow]yes[/], until {until.isoformat()}")
        except Exception:
            console.print("Paused:  [yellow]yes[/] (corrupt sentinel -- pause is permanent until removed)")
    else:
        console.print("Paused:  no")

    # Last startup event for working signal map
    events = state_dir / "events.jsonl"
    if events.exists():
        last_startup: dict | None = None
        for line in events.read_text(encoding="utf-8").splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "startup":
                last_startup = obj
        if last_startup:
            console.print()
            tbl = Table(title="Working signal map (last startup)", show_lines=False)
            tbl.add_column("Signal", style="cyan")
            for s in last_startup.get("available_signals", []):
                tbl.add_row(s)
            console.print(tbl)
            disabled = last_startup.get("disabled_rules", [])
            if disabled:
                console.print()
                tbl2 = Table(title="Disabled rules", show_lines=False)
                tbl2.add_column("Rule", style="yellow")
                tbl2.add_column("Signal")
                tbl2.add_column("Reason")
                for d in disabled:
                    tbl2.add_row(d["rule"], d["signal"], d["reason"])
                console.print(tbl2)


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------


@app.command()
def inputs() -> None:
    """One-shot probe + sample dump for every collector. Useful for setup verification."""
    # Imported lazily so `atf --help` doesn't pay the NVML/psutil cost.
    from atfield.collectors.lhmlib import LhmLibCollector
    from atfield.collectors.nvml import NvmlCollector
    from atfield.collectors.system import SystemCollector

    collectors = [SystemCollector(), NvmlCollector(), LhmLibCollector()]
    for c in collectors:
        result = c.probe()
        color = "green" if result.available else "red"
        console.print(f"\n[bold]{c.name}[/]: [{color}]{'OK' if result.available else 'unavailable'}[/]")
        console.print(f"  reason: {result.reason}")
        if result.metadata:
            for k, v in result.metadata.items():
                console.print(f"  {k}: {v}")
        if result.available:
            samples = c.sample()  # type: ignore[attr-defined]
            for k in sorted(samples):
                v = samples[k]
                console.print(f"    {k} = {v.value:.3f} {v.unit}")
        c.shutdown()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# pause / unpause
# ---------------------------------------------------------------------------


_DURATION_RE = re.compile(r"^(\d+)\s*(s|sec|secs|seconds|m|min|mins|minutes|h|hr|hrs|hours)?$", re.I)


def _parse_duration(s: str) -> timedelta:
    m = _DURATION_RE.match(s.strip())
    if not m:
        raise typer.BadParameter(f"could not parse duration: {s!r} (try '30m', '2h', '900s')")
    n = int(m.group(1))
    unit = (m.group(2) or "s").lower()
    if unit.startswith("s"):
        return timedelta(seconds=n)
    if unit.startswith("m"):
        return timedelta(minutes=n)
    if unit.startswith("h"):
        return timedelta(hours=n)
    raise typer.BadParameter(f"unknown duration unit: {unit!r}")


@app.command()
def pause(
    duration: str = typer.Argument(..., help="How long to pause (e.g. '30m', '2h', '600s')."),
    state_dir: Path = _state_dir_option(),
) -> None:
    """Pause kill actions. The watchdog continues to monitor and log."""
    state_dir.mkdir(parents=True, exist_ok=True)
    until = datetime.now(timezone.utc) + _parse_duration(duration)
    (state_dir / "pause.sentinel").write_text(until.isoformat() + "\n", encoding="utf-8")
    console.print(f"[yellow]Paused[/] until {until.isoformat()} ({duration})")


@app.command()
def unpause(
    state_dir: Path = _state_dir_option(),
) -> None:
    """Remove the pause sentinel. The watchdog will resume actions on its next pause-check tick (≤5 s)."""
    sentinel = state_dir / "pause.sentinel"
    if sentinel.exists():
        sentinel.unlink()
        console.print("[green]Unpaused[/]")
    else:
        console.print("(was not paused)")


# ---------------------------------------------------------------------------
# tail
# ---------------------------------------------------------------------------


@app.command()
def tail(
    state_dir: Path = _state_dir_option(),
    follow: bool = typer.Option(True, "--follow/--no-follow", "-f", help="Follow new events as they arrive."),
    lines: int = typer.Option(20, "--lines", "-n", help="How many existing lines to show first."),
) -> None:
    """Show recent events.jsonl entries; optionally follow new ones."""
    p = state_dir / "events.jsonl"
    if not p.exists():
        console.print(f"[yellow]no events file at[/] {p}")
        raise typer.Exit(code=1)

    existing = p.read_text(encoding="utf-8").splitlines()
    for line in existing[-lines:]:
        _print_event(line)

    if not follow:
        return
    pos = p.stat().st_size
    try:
        while True:
            time.sleep(0.5)
            try:
                size = p.stat().st_size
            except FileNotFoundError:
                continue
            if size <= pos:
                continue
            with p.open("rb") as fh:
                fh.seek(pos)
                chunk = fh.read().decode("utf-8", "replace")
                pos = fh.tell()
            for line in chunk.splitlines():
                if line.strip():
                    _print_event(line)
    except KeyboardInterrupt:
        pass


def _print_event(line: str) -> None:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        console.print(line)
        return
    t = obj.get("type", "?")
    ts = obj.get("ts_iso", "")
    if t == "action":
        kind = obj.get("kind", "?").upper()
        rule = obj.get("rule", "?")
        sig = obj.get("signal", "?")
        val = obj.get("latest_value", "?")
        console.print(f"[cyan]{ts}[/] [bold]{kind:8}[/] rule={rule} signal={sig} value={val}")
    elif t == "kill_report":
        n = len(obj.get("killed", []))
        survived = sum(1 for k in obj.get("killed", []) if k.get("survived"))
        if obj.get("succeeded"):
            console.print(f"[cyan]{ts}[/] [green]KILLED[/]  rule={obj.get('rule')} count={n}")
        else:
            console.print(f"[cyan]{ts}[/] [red]FAILED[/]  rule={obj.get('rule')} count={n} survived={survived} reason={obj.get('skipped_reason')}")
    elif t == "startup":
        n_avail = len(obj.get("available_signals", []))
        n_disabled = len(obj.get("disabled_rules", []))
        console.print(f"[cyan]{ts}[/] [green]START[/]   v={obj.get('version')} signals={n_avail} disabled_rules={n_disabled}")
    elif t == "shutdown":
        console.print(f"[cyan]{ts}[/] [yellow]STOP[/]    {obj.get('reason')}")
    elif t == "collector_health":
        console.print(f"[cyan]{ts}[/] [magenta]COLL[/]    {obj.get('collector')} -> {obj.get('state')} :: {obj.get('reason')}")
    elif t == "pause":
        console.print(f"[cyan]{ts}[/] [yellow]PAUSE[/]   until={obj.get('until')}")
    else:
        console.print(f"[cyan]{ts}[/] {t}: {obj}")


# ---------------------------------------------------------------------------
# run (foreground)
# ---------------------------------------------------------------------------


@app.command()
def run(
    state_dir: Path = _state_dir_option(),
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to config.toml. Defaults to <state-dir>/config.toml."),
    max_ticks: int | None = typer.Option(None, "--max-ticks", help="Run at most N ticks then exit (for debugging)."),
) -> None:
    """Run the watchdog in the foreground (for debugging or as a non-NSSM service)."""
    from atfield.service import run_service

    cfg_path = config or _resolve_config_path(state_dir)
    code = run_service(config_path=cfg_path, state_dir=state_dir, max_ticks=max_ticks)
    raise typer.Exit(code=code)


# ---------------------------------------------------------------------------
# install / uninstall (delegates to PowerShell scripts)
# ---------------------------------------------------------------------------


def _find_script(name: str) -> Path | None:
    """Find a packaged script by name. Looks under ``scripts/`` next to the source."""
    here = Path(__file__).resolve()
    # Editable install: scripts/ is at repo root, two above the package.
    repo_scripts = here.parent.parent.parent / "scripts" / name
    if repo_scripts.exists():
        return repo_scripts
    # Packaged install: scripts/ is shipped alongside the wheel data.
    try:
        with resources.as_file(resources.files("atfield") / "scripts" / name) as p:
            if p.exists():
                return p
    except (ModuleNotFoundError, FileNotFoundError):
        pass
    # PyInstaller-frozen install: data files live under sys._MEIPASS
    # (which is _internal/ for onedir builds, or a temp dir for onefile).
    # See packaging/pyinstaller/atfield.spec datas section.
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidate = Path(meipass) / "scripts" / name
            if candidate.exists():
                return candidate
        # Fallback: side-by-side with the exe (for installer layouts that
        # flatten the bundle).
        exe_dir = Path(sys.executable).resolve().parent
        for candidate in (exe_dir / "scripts" / name, exe_dir / name):
            if candidate.exists():
                return candidate
    return None


def _frozen_service_exe() -> Path | None:
    """When ``atf.exe`` was built by PyInstaller, return the sibling
    ``atfield-service.exe`` so ``atf install`` can hand it to NSSM
    instead of relying on a Python interpreter being on PATH.
    """
    if not getattr(sys, "frozen", False):
        return None
    candidate = Path(sys.executable).resolve().parent / "atfield-service.exe"
    return candidate if candidate.exists() else None


@app.command()
def install(
    state_dir: Path = _state_dir_option(),
) -> None:
    """Install AT-Field as a Windows Service (NSSM-based, runs as LocalSystem).

    If ``atf`` itself was built by PyInstaller (i.e. you're running the
    bundled binary, not ``pip install atfield``), the Windows Service is
    pointed at the sibling ``atfield-service.exe`` -- no Python
    interpreter required on the target machine.
    """
    if sys.platform != "win32":
        console.print("[red]install is Windows-only[/]")
        raise typer.Exit(code=1)
    script = _find_script("install_service.ps1")
    if script is None:
        console.print("[red]install_service.ps1 not found[/]; reinstall the package or run from a source checkout")
        raise typer.Exit(code=1)
    cmd: list[str] = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", str(script),
        "-StateDir", str(state_dir),
    ]
    bundled = _frozen_service_exe()
    if bundled is not None:
        console.print(f"[cyan]bundled mode:[/] using {bundled}")
        cmd += ["-BundledExe", str(bundled)]
    else:
        cmd += ["-PythonExe", sys.executable]
    console.print(f"[cyan]running:[/] {' '.join(cmd)}")
    rc = subprocess.call(cmd)
    raise typer.Exit(code=rc)


@app.command()
def uninstall() -> None:
    """Uninstall the AT-Field Windows Service."""
    if sys.platform != "win32":
        console.print("[red]uninstall is Windows-only[/]")
        raise typer.Exit(code=1)
    script = _find_script("uninstall_service.ps1")
    if script is None:
        console.print("[red]uninstall_service.ps1 not found[/]")
        raise typer.Exit(code=1)
    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", str(script),
    ]
    console.print(f"[cyan]running:[/] {' '.join(cmd)}")
    rc = subprocess.call(cmd)
    raise typer.Exit(code=rc)


# ---------------------------------------------------------------------------
# test-kill (dry run + diagnostic)
# ---------------------------------------------------------------------------


@app.command(name="test-kill")
def test_kill(
    pid: int = typer.Argument(..., help="PID to walk-up-to and dry-run a kill against."),
    dry_run: bool = typer.Option(True, "--dry-run/--for-real", help="Default dry-run; pass --for-real to actually terminate."),
) -> None:
    """Walk up the process tree from PID and show what a kill would target.

    Use this to verify the launcher walk-up logic against a real Python /
    torchrun / accelerate process without arming a kill rule. With
    ``--dry-run`` (default) it only enumerates and prints; ``--for-real``
    actually terminates.
    """
    from atfield.actuator import Actuator, PsutilProvider, find_kill_root
    from atfield.config import default_config

    cfg = default_config()
    provider = PsutilProvider()

    root = find_kill_root(
        pid,
        provider=provider,
        killable_names=frozenset(cfg.targeting.killable_names),
        launcher_names=frozenset(cfg.targeting.launcher_names),
    )
    if root is None:
        console.print(f"[yellow]PID {pid} did not match killable_names; nothing to walk up[/]")
        raise typer.Exit(code=2)
    console.print(f"kill root: PID {root.pid} {root.name}")
    descendants = provider.descendants(root.pid)
    tbl = Table(show_lines=False)
    tbl.add_column("PID")
    tbl.add_column("Name")
    tbl.add_column("RSS (MiB)", justify="right")
    tbl.add_row(str(root.pid), root.name, f"{root.rss_bytes / (1024*1024):.1f}")
    for d in descendants:
        tbl.add_row(str(d.pid), d.name, f"{d.rss_bytes / (1024*1024):.1f}")
    console.print(tbl)

    if dry_run:
        console.print("[green]dry-run only; no processes terminated[/]")
        return

    actuator = Actuator(cfg, provider=provider)
    from atfield.policy import Action

    fake_action = Action(
        kind="kill",
        rule_name="cli:test-kill",
        base_rule_name="cli:test-kill",
        signal="cli.manual",
        threshold=0,
        fraction_over=1.0,
        samples_considered=0,
        latest_value=0.0,
        triggered_at_ns=0,
        cooldown_seconds=0,
    )
    report = actuator.execute(fake_action, candidate_pids=[pid])
    console.print(f"[red]killed[/] {len(report.killed)}; survived {sum(1 for k in report.killed if k.survived)}")


# ---------------------------------------------------------------------------
# doctor -- interactive health check + setup helper
# ---------------------------------------------------------------------------


@app.command()
def doctor(
    state_dir: Path = _state_dir_option(),
) -> None:
    """Run a one-shot diagnostic over the whole AT-Field stack.

    This is the first thing to run when something seems off. It checks:

      * Service heartbeat freshness
      * Pause sentinel state
      * Last startup event (working signal map + disabled rules)
      * Each collector's current probe result
      * On-disk config validity

    For each problem found, prints a concrete suggested fix. Exits 0
    when everything is green, 1 when at least one warning fired.
    """
    problems: list[str] = []
    successes: list[str] = []

    # 1. State directory
    if not state_dir.exists():
        problems.append(
            f"state dir does not exist: {state_dir}\n"
            "  fix: install the service with `atf install` (elevated PowerShell)"
        )
    else:
        successes.append(f"state dir present: {state_dir}")

    # 2. Heartbeat
    heartbeat = state_dir / "heartbeat.txt"
    if state_dir.exists():
        if heartbeat.exists():
            try:
                first_line = heartbeat.read_text(encoding="utf-8").strip().splitlines()[0]
                ts = datetime.fromisoformat(first_line)
                age_s = (datetime.now(timezone.utc) - ts).total_seconds()
                if age_s < 30:
                    successes.append(f"heartbeat fresh ({age_s:.1f}s old)")
                else:
                    problems.append(
                        f"heartbeat stale ({age_s:.0f}s old, last={first_line})\n"
                        "  fix: restart the service:\n"
                        "       Stop-Service ATField; Start-Service ATField"
                    )
            except Exception as exc:
                problems.append(f"heartbeat unreadable: {exc}")
        else:
            problems.append(
                f"no heartbeat file at {heartbeat}\n"
                "  fix: service has never run on this box. Try `atf install`"
            )

    # 3. Pause sentinel
    sentinel = state_dir / "pause.sentinel"
    if sentinel.exists():
        try:
            until = sentinel.read_text(encoding="utf-8").strip().splitlines()[0]
            problems.append(
                f"AT-Field is PAUSED until {until}\n"
                "  fix: `atf unpause` if this is unintentional"
            )
        except Exception:
            problems.append(
                "pause sentinel exists but is unreadable -- pause is permanent\n"
                "  fix: `atf unpause`"
            )

    # 4. Last startup event
    events = state_dir / "events.jsonl"
    last_startup: dict | None = None
    if events.exists():
        for line in events.read_text(encoding="utf-8").splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "startup":
                last_startup = obj
        if last_startup is None:
            problems.append("events.jsonl exists but has no `startup` event yet")
        else:
            disabled = last_startup.get("disabled_rules") or []
            if disabled:
                lines = "\n".join(
                    f"      - {d['rule']} (signal {d['signal']}): {d['reason']}"
                    for d in disabled
                )
                problems.append(
                    f"{len(disabled)} rule(s) disabled at last startup:\n{lines}\n"
                    "  fix: install the missing collector. For LHM see docs/faq.md."
                )
            else:
                successes.append("all rules active per last startup")
    else:
        # First-run; not a problem if state_dir doesn't exist yet either
        if state_dir.exists():
            problems.append(
                f"no events.jsonl in {state_dir}\n"
                "  fix: start the service so it can produce its first event"
            )

    # 5. Live collector probes (we don't need a running service for this)
    try:
        from atfield.collectors.lhmlib import LhmLibCollector
        from atfield.collectors.nvml import NvmlCollector
        from atfield.collectors.system import SystemCollector

        for c in (SystemCollector(), NvmlCollector(), LhmLibCollector()):
            r = c.probe()
            if r.available:
                successes.append(f"collector {c.name}: OK ({r.reason})")
            else:
                fix = ""
                if c.name == "lhm":
                    fix = (
                        "\n  fix: ensure the bundled sensor helper is present "
                        "(scripts\\build_helper.ps1) or set ATFIELD_SENSOR_EXE; see docs/faq.md"
                    )
                elif c.name == "nvml":
                    fix = "\n  fix: install NVIDIA driver + reboot, or ignore if you have no NVIDIA GPU"
                problems.append(f"collector {c.name}: UNAVAILABLE -- {r.reason}{fix}")
            try:
                c.shutdown()  # type: ignore[attr-defined]
            except Exception:
                pass
    except ImportError as exc:
        problems.append(f"could not import collectors: {exc}")

    # 5c. Headless sensor helper (the LHM-library transport).
    # AT-Field reads CPU package / GPU memory-junction / PSU voltage
    # sensors via the bundled atfield-sensors.exe (-> LibreHardwareMonitorLib),
    # NOT LHM's GUI web server. Surface whether the helper binary is
    # discoverable, since that's the new precondition for those signals.
    try:
        from atfield.collectors.lhmlib import find_sensor_helper

        helper = find_sensor_helper()
        if helper is not None:
            successes.append(f"sensor helper discoverable at {helper}")
        else:
            problems.append(
                "sensor helper (atfield-sensors.exe) not found\n"
                "  This is fine if you don't want CPU-package / VRAM-junction "
                "temps or PSU voltages. Otherwise build it next to the bundled "
                "LibreHardwareMonitorLib.dll:\n"
                "      powershell -ExecutionPolicy Bypass -File scripts\\build_helper.ps1\n"
                "  or set ATFIELD_SENSOR_EXE to its full path, then restart the service."
            )
    except ImportError:
        pass

    # 6. Forensic buffer
    if state_dir.exists():
        from atfield.forensics import FORENSICS_FILENAME, FORENSICS_PREV_FILENAME
        forensics = state_dir / FORENSICS_FILENAME
        forensics_prev = state_dir / FORENSICS_PREV_FILENAME
        if forensics.exists():
            try:
                size_kb = forensics.stat().st_size / 1024
                age_s = time.time() - forensics.stat().st_mtime
                if age_s < 30:
                    successes.append(
                        f"forensic buffer fresh ({size_kb:.1f} KB, last write {age_s:.1f}s ago)"
                    )
                else:
                    problems.append(
                        f"forensic buffer stale ({size_kb:.1f} KB, last write {age_s:.0f}s ago)\n"
                        "  fix: service may not be sampling -- check `atf status`"
                    )
            except OSError:
                pass
        elif forensics_prev.exists():
            successes.append(
                "previous run's forensic buffer present "
                f"({forensics_prev.stat().st_size // 1024} KB) -- "
                "service hasn't started a new run yet"
            )
        else:
            # Not a hard error: a fresh install hasn't sampled yet.
            successes.append("forensic buffer not yet written (fresh install)")

    # 7. Config validity
    config_path = _resolve_config_path(state_dir)
    if config_path is None:
        successes.append("no config.toml found -- using locked-in defaults")
    else:
        try:
            from atfield.config import load_config
            load_config(config_path)
            successes.append(f"config valid: {config_path}")
        except Exception as exc:
            problems.append(
                f"config INVALID: {config_path}\n  {exc}\n"
                "  fix: revert to defaults by deleting the file, or correct the indicated key"
            )

    # ── Render report ────────────────────────────────────────────────
    console.print()
    console.print("[bold]AT-Field doctor[/]")
    console.print()
    if successes:
        console.print(f"[green]{len(successes)} check(s) passed:[/]")
        for s in successes:
            console.print(f"  [green]+[/] {s}")
        console.print()
    if problems:
        console.print(f"[yellow]{len(problems)} issue(s) found:[/]")
        for p in problems:
            console.print(f"  [yellow]![/] {p}")
        console.print()
        console.print(
            "[dim]Re-run after applying fixes. For deeper troubleshooting see docs/faq.md.[/]"
        )
        raise typer.Exit(code=1)
    console.print("[green]All clear.[/]")


# ---------------------------------------------------------------------------
# set-profile -- one-shot Aggressive / Normal / Relaxed across all rules
# ---------------------------------------------------------------------------


@app.command(name="set-profile")
def set_profile(
    profile: str = typer.Argument(
        ...,
        help="One of: aggressive, normal, relaxed",
    ),
    state_dir: Path = _state_dir_option(),
    config: Path = typer.Option(
        None,
        "--config",
        "-c",
        help="Config file to mutate (defaults to <state_dir>/config.toml)",
    ),
) -> None:
    """Apply a profile preset to every known rule's threshold.

    Mirrors the dashboard's preset buttons. Same atomic, comment-preserving
    rewrite -- safe to run while the service is live; the engine will pick
    up the new thresholds within ~1 second.
    """
    from atfield.config_writer import ConfigWriteError, update_rule_threshold
    from atfield.rule_profiles import PROFILE_PRESETS

    profile_lower = profile.lower()
    if profile_lower not in PROFILE_PRESETS:
        valid = sorted(PROFILE_PRESETS.keys())
        raise typer.BadParameter(
            f"unknown profile {profile!r}; pick one of {valid}"
        )

    target = config if config is not None else (state_dir / "config.toml")

    preset = PROFILE_PRESETS[profile_lower]
    console.print(f"Applying [bold cyan]{profile_lower}[/] preset to {target}")
    failures = 0
    for rule_name, threshold in preset.items():
        try:
            update_rule_threshold(target, rule_name, threshold)
            console.print(f"  [green]+[/] {rule_name} -> {threshold}")
        except ConfigWriteError as exc:
            failures += 1
            console.print(f"  [red]x[/] {rule_name}: {exc}")
    if failures:
        console.print(
            f"\n[yellow]{failures} rule(s) could not be updated.[/] "
            "If the service is running, it may not see the partial change; "
            "fix the cause and re-run."
        )
        raise typer.Exit(code=1)
    console.print(
        "\n[green]Done.[/] The service will reload within ~1s "
        "(or restart it manually with Stop-Service / Start-Service)."
    )


# ---------------------------------------------------------------------------
# setup -- interactive first-run wizard
# ---------------------------------------------------------------------------


@app.command()
def setup(
    state_dir: Path = _state_dir_option(),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Skip prompts and accept defaults. Useful for scripted installs.",
    ),
) -> None:
    """Interactive first-run wizard.

    Walks through the three decisions a new user has to make:

      1. Where the state directory lives.
      2. Which profile (Aggressive / Normal / Relaxed) to start with.
      3. Whether to start the watchdog in observe-only mode for the
         first session (recommended for the first hour so the user
         can see what it WOULD do before letting it act).

    Writes the resulting choices into ``<state_dir>/config.toml`` and
    prints next-step instructions. Safe to re-run; existing config is
    preserved unless the user explicitly opts to overwrite.
    """
    from atfield.config_writer import (
        materialize_default_config,
        update_rule_threshold,
    )
    from atfield.rule_profiles import PROFILE_PRESETS

    console.print("\n[bold cyan]AT-Field setup[/]\n")

    # 1. State directory
    console.print(f"State directory: [cyan]{state_dir}[/]")
    if not state_dir.exists():
        if yes or typer.confirm("Create it now?", default=True):
            state_dir.mkdir(parents=True, exist_ok=True)
            console.print(f"  [green]+[/] created {state_dir}")
        else:
            console.print("[red]aborted[/] -- state dir is required")
            raise typer.Exit(code=1)

    config_path = state_dir / "config.toml"

    # 2. Existing config?
    if config_path.exists():
        console.print(f"\nFound existing config at [cyan]{config_path}[/]")
        # --yes accepts the default for every prompt; the default for
        # "overwrite an existing config?" is no, so --yes preserves
        # the user's hand-tuned config rather than nuking it.
        overwrite = False if yes else typer.confirm(
            "Overwrite with the wizard's choices?", default=False,
        )
        if not overwrite:
            console.print("\n[yellow]Keeping existing config.[/] Edit it directly or use `atf set-profile`.")
            raise typer.Exit(code=0)

    # 3. Profile choice
    profiles = ["aggressive", "normal", "relaxed"]
    console.print("\n[bold]Profile presets[/] (you can change later from the dashboard or `atf set-profile`):")
    console.print("  [cyan]aggressive[/]  Lower thresholds. Fires earlier; protective.")
    console.print("  [cyan]normal[/]      Balanced default from PLANNING.md §3. Recommended.")
    console.print("  [cyan]relaxed[/]     Higher thresholds. Only fires on clear hardware distress.")

    if yes:
        profile = "normal"
    else:
        while True:
            answer = typer.prompt("Profile to start with", default="normal").strip().lower()
            if answer in profiles:
                profile = answer
                break
            console.print(f"  [red]not one of {profiles}[/]")

    # 4. Observe-only opt-in
    console.print("\n[bold]Observe-only mode[/]")
    console.print("  In observe-only mode, AT-Field logs what it WOULD do but never kills.")
    console.print("  Recommended for the first hour so you can see the system's verdicts before arming it.")
    # --yes accepts the displayed default (True = observe-only on).
    observe_only = True if yes else typer.confirm(
        "Start in observe-only mode?", default=True,
    )

    # 5. Materialize + apply
    console.print(f"\nWriting config to [cyan]{config_path}[/]…")
    materialize_default_config(config_path)
    if profile != "normal":
        for rule_name, threshold in PROFILE_PRESETS[profile].items():
            update_rule_threshold(config_path, rule_name, threshold)
    if observe_only:
        # Edit in-place to flip every rule's action to "log" -- no kill_writer
        # action exists yet, so the safe primitive is to switch actions to log.
        text = config_path.read_text(encoding="utf-8")
        text = text.replace('action = "kill"', 'action = "log"')
        text = text.replace('action = "throttle"', 'action = "log"')
        config_path.write_text(text, encoding="utf-8")

    console.print("[green]+[/] config written")

    # 6. Final summary
    console.print()
    tbl = Table(show_header=False, box=None, padding=(0, 1))
    tbl.add_column(style="cyan", justify="right")
    tbl.add_column()
    tbl.add_row("profile", profile)
    tbl.add_row("observe-only", "yes" if observe_only else "no")
    tbl.add_row("config", str(config_path))
    tbl.add_row("state dir", str(state_dir))
    console.print(tbl)

    console.print("\n[bold]Next steps:[/]")
    console.print("  1. Install the watchdog as a Windows service (elevated PowerShell):")
    console.print("     [cyan]atf install[/]")
    console.print("  2. Verify it's running:")
    console.print("     [cyan]atf status[/]")
    console.print("  3. Open the dashboard (after installing the tray app):")
    console.print("     left-click the AT-Field tray icon")
    if observe_only:
        console.print("\n[yellow]Observe-only mode is on.[/] Once you've seen a few hours of verdicts,")
        console.print("flip every rule's `action` back to `kill` (or `throttle`) in the config to arm it.")


# ---------------------------------------------------------------------------
# install-lhm -- self-service LHM install for dev/manual installs
# ---------------------------------------------------------------------------


# Pinned LHM release. Bumping this is a deliberate choice: every version
# change has a small chance of breaking the .config XML schema we ship.
#
# v0.9.6 (2026-02-14) was picked over v0.9.4 because it adds the NvAPI
# workaround for the RTX 5090 memory-junction sensor (NVIDIA removed
# the public API for it in the 50-series driver), exposes GPU core
# voltage as a first-class signal, and adds foundational support for
# MSI B850 / X870 / Z890 boards plus ASUS Astral 50-series GPUs. See
# docs/sensors.md for the full per-signal coverage matrix.
#
# Note the asset name changed in v0.9.5 from `LibreHardwareMonitor-net472.zip`
# to `LibreHardwareMonitor.zip` (the .NET Framework 4.7.2 build kept the
# same in-box-on-Win10+ compatibility properties; the filename is just
# different).
_LHM_VERSION = "v0.9.6"
_LHM_ZIP_URL = (
    f"https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/"
    f"download/{_LHM_VERSION}/LibreHardwareMonitor.zip"
)


@app.command(name="install-lhm")
def install_lhm(
    install_dir: Path = typer.Option(
        Path.home() / ".atfield" / "lhm",
        "--dir", "-d",
        help=(
            "Where to drop the LHM binaries. Defaults to ~/.atfield/lhm/. "
            "The supervisor's ATFIELD_LHM_EXE env var should point at "
            "<install-dir>/LibreHardwareMonitor.exe (or pass --no-env-hint "
            "to skip that suggestion)."
        ),
    ),
    force: bool = typer.Option(
        False, "--force", "-f",
        help="Reinstall even if LibreHardwareMonitor.exe is already present.",
    ),
    env_hint: bool = typer.Option(
        True, "--env-hint/--no-env-hint",
        help="Print the ATFIELD_LHM_EXE export hint after install.",
    ),
) -> None:
    """Download LibreHardwareMonitor for the current AT-Field install.

    The bundled NSIS installer ships LHM out of the box, but pip-installed
    or git-cloned dev installs need a manual fetch -- without LHM the
    watchdog can't read VRAM-junction temp on consumer GPUs and CPU package
    temp on Intel/AMD desktops, and the dashboard says "Degraded".

    This command:

      1. Downloads ``LHM ${_LHM_VERSION}`` from the official GitHub release.
      2. Extracts it into ``<install-dir>``.
      3. Drops a pre-baked ``LibreHardwareMonitor.config`` that turns on the
         web server on port 8085 (LHM 0.9.x has no CLI flag for that).
      4. Prints the env-var hint so the supervisor finds the binary on next
         service restart.
    """
    import shutil
    import tempfile
    import urllib.request
    import zipfile

    install_dir = install_dir.expanduser()
    binary = install_dir / "LibreHardwareMonitor.exe"

    if binary.is_file() and not force:
        console.print(f"[green]+[/] LHM already present at [cyan]{binary}[/]")
        console.print("[dim]Pass --force to reinstall.[/]")
        if env_hint:
            _print_lhm_env_hint(binary)
        return

    install_dir.mkdir(parents=True, exist_ok=True)

    console.print(f"Downloading LHM {_LHM_VERSION} from GitHub…")
    with tempfile.NamedTemporaryFile(
        suffix=".zip", delete=False, dir=str(install_dir),
    ) as tmpfile:
        try:
            with urllib.request.urlopen(_LHM_ZIP_URL, timeout=60) as resp:
                shutil.copyfileobj(resp, tmpfile)
            tmpfile.close()
            console.print("Extracting…")
            with zipfile.ZipFile(tmpfile.name) as z:
                z.extractall(install_dir)
        finally:
            try:
                Path(tmpfile.name).unlink()
            except OSError:
                pass

    if not binary.is_file():
        # Some LHM zips wrap their files in a "LibreHardwareMonitor/"
        # subdirectory; flatten if needed.
        nested = install_dir / "LibreHardwareMonitor" / "LibreHardwareMonitor.exe"
        if nested.is_file():
            for child in nested.parent.iterdir():
                target = install_dir / child.name
                if target.exists():
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                shutil.move(str(child), str(target))
            try:
                nested.parent.rmdir()
            except OSError:
                pass

    if not binary.is_file():
        console.print(f"[red]error[/] LHM zip extracted but {binary.name} missing")
        raise typer.Exit(code=2)

    # Drop a starter LibreHardwareMonitor.config with the AT-Field
    # required keys (web server on, minimize to tray, no auto-update)
    # so the very first LHM launch -- before the AT-Field service has
    # a chance to call ensure_lhm_config() itself -- still comes up
    # with the HTTP server enabled.
    #
    # Once the service is running, atfield.lhm_supervisor calls
    # ensure_lhm_config() before every spawn, so this starter file
    # only matters in the narrow window between `atf install-lhm`
    # and the next service start.
    from atfield.lhm_config import ensure_lhm_config
    try:
        ensure_lhm_config(install_dir)
    except OSError as exc:
        console.print(
            f"[yellow]warning[/] couldn't write LibreHardwareMonitor.config: {exc}. "
            "LHM web server may not be enabled on first launch. Start the "
            "AT-Field service and it will re-assert the config."
        )

    console.print(f"[green]+[/] LHM installed at [cyan]{binary}[/]")
    if env_hint:
        _print_lhm_env_hint(binary)


# ---------------------------------------------------------------------------
# forensics -- post-crash signal history reader
# ---------------------------------------------------------------------------


@app.command()
def forensics(
    state_dir: Path = _state_dir_option(),
    since: str = typer.Option(
        "10m",
        "--since",
        help="Time window from now: '5m', '1h', '24h', or 'all'",
    ),
    signal_filter: str = typer.Option(
        "",
        "--signal",
        help="Substring filter on signal name (e.g. 'gpu.0' or 'voltage')",
    ),
    include_prev: bool = typer.Option(
        True,
        "--include-prev/--no-include-prev",
        help="Include the previous run's archived buffer too "
             "(useful right after a crash)",
    ),
    output_format: str = typer.Option(
        "table",
        "--format",
        "-f",
        help="One of: table, jsonl, csv",
    ),
) -> None:
    """Read the rolling forensic buffer.

    The watchdog flushes every signal sample to
    ``%ProgramData%\\ATField\\forensics.jsonl`` every 5 seconds, with the
    previous run rotated to ``forensics-prev.jsonl``. This command makes
    that data easy to inspect after a crash.

    Examples:

        atf forensics --since 5m
        atf forensics --since 1h --signal gpu.0 --format csv > out.csv
        atf forensics --since all --signal voltage
    """
    from atfield.forensics import FORENSICS_FILENAME, FORENSICS_PREV_FILENAME

    cur = state_dir / FORENSICS_FILENAME
    prev = state_dir / FORENSICS_PREV_FILENAME

    sources: list[Path] = []
    if include_prev and prev.exists():
        sources.append(prev)
    if cur.exists():
        sources.append(cur)
    if not sources:
        console.print(f"[yellow]no forensic buffer at {state_dir}[/]")
        console.print(
            "[dim]start the service so it can begin sampling, then re-run.[/]"
        )
        raise typer.Exit(code=1)

    cutoff = _parse_since(since)
    needle = signal_filter.strip().lower()

    rows: list[tuple[float, str, float]] = []
    for src in sources:
        try:
            for line in src.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue  # torn last line after a crash; skip
                ts = obj.get("ts")
                samples = obj.get("samples")
                if not isinstance(ts, (int, float)) or not isinstance(samples, dict):
                    continue
                if cutoff is not None and ts < cutoff:
                    continue
                for sig, val in samples.items():
                    if needle and needle not in sig.lower():
                        continue
                    if isinstance(val, (int, float)):
                        rows.append((float(ts), sig, float(val)))
        except OSError as exc:
            console.print(f"[yellow]warning[/] could not read {src}: {exc}")

    if not rows:
        console.print("[yellow]no rows matched the filter window.[/]")
        raise typer.Exit(code=1)

    rows.sort(key=lambda r: (r[0], r[1]))

    fmt = output_format.lower()
    if fmt == "jsonl":
        for ts, sig, val in rows:
            print(json.dumps({"ts": ts, "signal": sig, "value": val}))
        return
    if fmt == "csv":
        print("ts,ts_iso,signal,value")
        for ts, sig, val in rows:
            iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            print(f"{ts},{iso},{sig},{val}")
        return

    # Default: a Rich table, but capped so we don't dump 10k rows to terminal.
    cap = 200
    table = Table(
        title=f"forensic samples ({len(rows)} match{'es' if len(rows) != 1 else ''}, showing last {min(cap, len(rows))})",
        show_lines=False,
    )
    table.add_column("ts (UTC)")
    table.add_column("signal")
    table.add_column("value", justify="right")
    for ts, sig, val in rows[-cap:]:
        iso = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        table.add_row(iso, sig, f"{val:.3f}")
    console.print(table)


def _parse_since(spec: str) -> float | None:
    """Convert '5m' / '1h' / '24h' / 'all' to a unix-epoch cutoff."""
    s = spec.strip().lower()
    if s in ("all", "*", ""):
        return None
    m = re.fullmatch(r"(\d+)\s*([smhd])", s)
    if not m:
        raise typer.BadParameter(
            f"invalid --since {spec!r}; use e.g. '30s', '5m', '2h', '1d', or 'all'"
        )
    n, unit = int(m.group(1)), m.group(2)
    seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return time.time() - n * seconds


def _print_lhm_env_hint(binary: Path) -> None:
    """Print the platform-appropriate env-var export so the supervisor
    finds the just-installed LHM binary."""
    console.print()
    console.print("[bold]Tell the watchdog where to find it:[/]")
    if sys.platform == "win32":
        console.print(f'  [cyan]setx ATFIELD_LHM_EXE "{binary}"[/]')
        console.print("  [dim](then restart the AT-Field service)[/]")
    else:
        console.print(f'  [cyan]export ATFIELD_LHM_EXE="{binary}"[/]')


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    app()


if __name__ == "__main__":
    main()
