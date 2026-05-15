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
    from atfield.collectors.lhm import LhmCollector
    from atfield.collectors.nvml import NvmlCollector
    from atfield.collectors.system import SystemCollector

    collectors = [SystemCollector(), NvmlCollector(), LhmCollector()]
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
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    app()


if __name__ == "__main__":
    main()
