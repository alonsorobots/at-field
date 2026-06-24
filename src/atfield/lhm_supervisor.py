"""LibreHardwareMonitor (LHM) child-process supervisor.

The LHM CLI ships as ``LibreHardwareMonitor.exe`` and exposes its
sensor-tree JSON document over HTTP when launched with ``--server`` /
``--port``. AT-Field needs LHM running to read VRAM-junction temp on
consumer NVIDIA GPUs and CPU package temp -- neither is reachable
through NVML alone.

Strategy
--------
At service startup, find a bundled or system-installed copy of LHM and
spawn it as a child of the AT-Field service. Restart on unexpected
exit with capped exponential backoff. On AT-Field shutdown, terminate
LHM cleanly so we don't leak a CPU-pinning sensor poll loop.

Why a supervisor instead of telling users to start LHM themselves
- Friction. The whole point of AT-Field is "install once, forget about
  it"; a manual second installer + a tray that the user has to
  remember to launch breaks that promise.
- Lifecycle. We want LHM running for as long as AT-Field is, period.
  Tying it to our own process tree means it dies with us instead of
  lingering in the background.
- Visibility. Crashes / restart loops surface in our audit log
  alongside everything else, instead of being a silent black box.

Why we don't reimplement LHM's sensor reads in Python
- LHM is a kernel-mode-driver-backed sensor library; reproducing it
  would require a substantial Windows driver development effort.
  Bundling it (MPL 2.0, no modifications) is the right cost/benefit.

Design notes
------------
* This module is platform-agnostic for testability. The actual binary
  resolution (find_lhm_executable) is Windows-specific in practice; the
  supervisor itself uses ``subprocess.Popen``-style calls that work
  cross-platform so tests can substitute a fake.
* All process I/O goes through an injected ``ProcessSpawner`` so unit
  tests don't have to spawn real LHM.exe.
* Backoff is *capped* at 60s. LHM crashing in a tight loop is a
  hardware/driver problem we can't fix; we want to keep retrying so
  that whoever fixes it gets data flowing again without restarting
  AT-Field, but not at a rate that pegs a CPU core.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

from atfield.lhm_config import ensure_lhm_config

__all__ = [
    "LhmStatus",
    "LhmSupervisor",
    "LhmSupervisorConfig",
    "ProcessHandle",
    "ProcessSpawner",
    "RealProcessSpawner",
    "find_lhm_executable",
    "probe_lhm_http",
]


_log = logging.getLogger("atfield.lhm_supervisor")


# ---------------------------------------------------------------------------
# Process abstraction (so tests can fake subprocess without monkeypatching)
# ---------------------------------------------------------------------------


class ProcessHandle(Protocol):
    """Minimal handle subset we actually use, mirrored after Popen."""

    pid: int

    def poll(self) -> int | None:
        """Return None if running, else the exit code."""
        ...

    def terminate(self) -> None:
        """Send SIGTERM / Win equivalent. Should not raise on dead procs."""
        ...

    def wait(self, timeout: float | None = None) -> int:
        ...

    def kill(self) -> None:
        ...


class ProcessSpawner(Protocol):
    def spawn(self, args: list[str]) -> ProcessHandle:
        """Spawn ``args`` and return a handle. Raises ``OSError`` if the
        executable is missing / unspawnable."""
        ...


@dataclass(frozen=True, slots=True)
class RealProcessSpawner:
    """Default spawner backed by :class:`subprocess.Popen`."""

    def spawn(self, args: list[str]) -> ProcessHandle:
        # No stdin; pipe stdout/stderr to DEVNULL so LHM's chattering
        # doesn't fill our parent log. LHM is silent on success and
        # writes startup banners we don't care about.
        #
        # ``__COMPAT_LAYER=RUNASINVOKER`` is critical when AT-Field runs
        # as a Windows Service. LHM's manifest declares
        # ``requestedExecutionLevel="requireAdministrator"``; CreateProcess
        # treats this as "trigger UAC elevation" -- but Session 0 (where
        # Windows services live) has no UAC prompt host. The result is
        # CreateProcess failing with WinError 740 even when the caller
        # is LocalSystem (which IS administrator). RUNASINVOKER tells
        # the loader to ignore the manifest's elevation request and
        # honor the caller's token, which is exactly what we want --
        # LocalSystem is already at SYSTEM integrity so LHM gets the
        # privileges it needs to load WinRing0 and read MSRs.
        #
        # In interactive (non-service) contexts this flag is a no-op
        # when the caller is already admin, so it's safe to always set.
        # When the caller is unprivileged, CreateProcess succeeds but
        # LHM itself fails to load its kernel driver -- which surfaces
        # cleanly via the supervisor's HTTP probe ("process started but
        # port did not open in 15s") rather than the obscure WinError 740.
        env = os.environ.copy()
        env["__COMPAT_LAYER"] = (
            (env.get("__COMPAT_LAYER", "") + " RUNASINVOKER").strip()
        )
        return subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            # CREATE_NO_WINDOW so a console doesn't flash on Windows.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


# ---------------------------------------------------------------------------
# Configuration + status
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LhmSupervisorConfig:
    """How the supervisor should run LHM.

    The defaults mirror the URL the LHM collector reads
    (``http://127.0.0.1:8085/data.json``) so the two halves stay in sync.
    """

    executable: Path
    """Path to ``LibreHardwareMonitor.exe`` (or another OS-equivalent)."""

    port: int = 8085
    """HTTP server port LHM should listen on."""

    extra_args: tuple[str, ...] = ()
    """Additional CLI args. LHM's CLI flags evolve across releases; this
    escape hatch lets the operator inject ``--config`` etc. without
    rebuilding."""

    backoff_initial_s: float = 1.0
    backoff_max_s: float = 60.0
    backoff_factor: float = 2.0

    shutdown_grace_s: float = 5.0
    """How long to wait for LHM's terminate() before kill() on shutdown."""

    http_ready_timeout_s: float = 15.0
    """How long to wait after spawn for LHM's HTTP server to start
    accepting connections on ``port``. LHM takes a few seconds on
    cold start (kernel driver load + sensor enumeration). When this
    timeout elapses without the port opening, the supervisor records
    a clear error on :attr:`LhmStatus.last_error` -- distinguishing
    "spawned but unreachable" from "exited cleanly" so the dashboard
    can show the right diagnosis."""

    manage_config: bool = True
    """Whether to call :func:`atfield.lhm_config.ensure_lhm_config`
    before each spawn. True in production -- LHM's persisted config
    might have been overwritten by the user / a prior LHM run /
    a previous AT-Field version, and we want the HTTP server reliably
    enabled. Disabled in tests where there's no real exe."""

    http_failure_restart_limit: int = 4
    """How many times to kill+respawn LHM when the process comes up but
    its HTTP server never binds. LHM attempts ``StartHttpListener()``
    exactly once at launch and swallows any failure, so a *transient*
    first-start fault -- the URL reservation landing a beat late, a
    Session-0 hiccup, a port held momentarily by a dying prior instance
    -- would otherwise wedge every LHM-backed rule until the next service
    restart. We give it a few fresh starts (each a brand-new process that
    re-runs StartHttpListener) before giving up and leaving the process
    alone, so we don't churn forever on a genuinely unfixable box. Set to
    0 to disable restart-on-no-HTTP entirely."""


@dataclass
class LhmStatus:
    """Mutable snapshot the API can render. All fields are best-effort
    and may briefly disagree with reality during transitions."""

    running: bool = False
    pid: int | None = None
    started_at: float | None = None
    restart_count: int = 0
    last_exit_code: int | None = None
    last_exit_at: float | None = None
    next_retry_at: float | None = None
    last_error: str | None = None
    # True once LHM's HTTP server has accepted at least one connection
    # since the last spawn. Distinguishes "process is alive but server
    # never came up" from "process is alive and serving data".
    http_ready: bool = False
    # Whether the supervisor is in shutdown -- distinguishes "LHM died,
    # we're respawning" from "we asked LHM to stop, expected".
    stopping: bool = field(default=False)


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


class LhmSupervisor:
    """Background thread that keeps a single LHM child process alive.

    Use:

        cfg = LhmSupervisorConfig(executable=Path("LHM.exe"))
        sup = LhmSupervisor(cfg)
        sup.start()
        ...
        sup.stop()  # blocks until LHM is dead

    The supervisor owns the spawn/respawn loop. The HTTP API reads the
    public :attr:`status` (under ``status_lock``) for the dashboard;
    callers should never poke at :attr:`_proc` directly.
    """

    def __init__(
        self,
        config: LhmSupervisorConfig,
        *,
        spawner: ProcessSpawner | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(config, LhmSupervisorConfig):
            raise TypeError(
                f"config must be LhmSupervisorConfig, got {type(config).__name__}"
            )
        self._config = config
        self._spawner: ProcessSpawner = spawner or RealProcessSpawner()
        self._clock = clock
        self._sleep = sleep

        self._status = LhmStatus()
        self.status_lock = threading.Lock()

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: ProcessHandle | None = None
        self._proc_lock = threading.Lock()

        # Count of consecutive "process up but HTTP never bound" faults.
        # Reset on a healthy spawn; capped by
        # ``config.http_failure_restart_limit`` so we stop churning on a
        # box where LHM can genuinely never bind.
        self._http_failures = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Begin supervising. Returns immediately; the supervise loop
        runs on a daemon thread so it dies with the parent process if
        ``stop()`` was never called."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._supervise_loop,
            name="atfield-lhm-supervisor",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, join_timeout_s: float = 10.0) -> None:
        """Signal the supervise loop to exit and terminate LHM.

        Idempotent; safe to call from a signal handler. Blocks for up
        to ``join_timeout_s`` waiting for the loop to drain. If the
        loop is wedged we proceed anyway -- the OS will reap the
        leftover child when we exit.
        """
        with self.status_lock:
            self._status.stopping = True
        self._stop_event.set()
        # Make sure any currently-running LHM process is signalled
        # immediately, not only when the supervise loop next ticks.
        self._terminate_proc()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout_s)

    def snapshot_status(self) -> LhmStatus:
        """Return a defensive copy of the public status."""
        with self.status_lock:
            return LhmStatus(
                running=self._status.running,
                pid=self._status.pid,
                started_at=self._status.started_at,
                restart_count=self._status.restart_count,
                last_exit_code=self._status.last_exit_code,
                last_exit_at=self._status.last_exit_at,
                next_retry_at=self._status.next_retry_at,
                last_error=self._status.last_error,
                http_ready=self._status.http_ready,
                stopping=self._status.stopping,
            )

    # ------------------------------------------------------------------
    # Supervise loop
    # ------------------------------------------------------------------

    def _supervise_loop(self) -> None:
        backoff = self._config.backoff_initial_s
        while not self._stop_event.is_set():
            try:
                self._spawn_once()
            except OSError as exc:
                # Most likely cause: executable missing. We log and
                # back off; the user can drop a binary into place
                # without restarting the whole AT-Field service.
                with self.status_lock:
                    self._status.last_error = str(exc)
                    self._status.running = False
                _log.warning("lhm spawn failed: %s; retrying in %.1fs", exc, backoff)
                self._wait_with_backoff(backoff)
                backoff = min(backoff * self._config.backoff_factor,
                              self._config.backoff_max_s)
                continue

            # The OS-level launch succeeded. But LHM tries
            # StartHttpListener() exactly once and swallows failures, so a
            # live process is NOT proof of a working web server. If the
            # probe in _spawn_once never saw the port open, treat it as a
            # restartable fault: kill LHM and respawn (a fresh process
            # re-runs StartHttpListener), with growing backoff, up to a
            # cap. This self-heals transient first-start failures -- the
            # URL reservation committing a beat late, a Session-0 hiccup,
            # a port briefly held by a dying prior instance -- which would
            # otherwise leave every LHM-backed rule dead until a manual
            # service restart.
            if self._config.http_ready_timeout_s > 0 and self._config.http_failure_restart_limit > 0:
                with self.status_lock:
                    http_ok = self._status.http_ready
                if http_ok:
                    self._http_failures = 0
                    backoff = self._config.backoff_initial_s
                else:
                    self._http_failures += 1
                    if self._http_failures <= self._config.http_failure_restart_limit:
                        _log.warning(
                            "LHM is up but its HTTP server never bound "
                            "(attempt %d/%d); killing and respawning in %.1fs",
                            self._http_failures,
                            self._config.http_failure_restart_limit,
                            backoff,
                        )
                        self._terminate_proc()
                        if self._stop_event.is_set():
                            break
                        self._wait_with_backoff(backoff)
                        backoff = min(backoff * self._config.backoff_factor,
                                      self._config.backoff_max_s)
                        continue
                    _log.error(
                        "LHM is up but its HTTP server still never bound after "
                        "%d respawns; leaving the process running and giving up "
                        "retries to avoid churn. Run `atf doctor` for the fix.",
                        self._http_failures,
                    )
                    backoff = self._config.backoff_initial_s
            else:
                backoff = self._config.backoff_initial_s

            # Wait for the child to exit.
            exit_code = self._wait_for_proc()

            if self._stop_event.is_set():
                # Shutdown requested while LHM was alive -- don't restart.
                break

            # Unexpected exit; record + back off + retry.
            with self.status_lock:
                self._status.running = False
                self._status.pid = None
                self._status.last_exit_code = exit_code
                self._status.last_exit_at = time.time()
                self._status.last_error = (
                    f"exited with code {exit_code}" if exit_code is not None
                    else "exited (no code)"
                )
            _log.warning("lhm exited (code=%s); restarting in %.1fs", exit_code, backoff)
            self._wait_with_backoff(backoff)
            backoff = min(backoff * self._config.backoff_factor,
                          self._config.backoff_max_s)

        # Drain: ensure LHM is dead even if the loop exited via Stop.
        self._terminate_proc()
        with self.status_lock:
            self._status.running = False
            self._status.pid = None

    def _spawn_once(self) -> None:
        """Spawn LHM and update status. Raises on spawn failure.

        LHM 0.9.x doesn't take CLI flags for server/port -- those are
        read from ``LibreHardwareMonitor.config`` (an XML file LHM
        persists next to its exe). LHM tends to rewrite that file from
        its in-memory settings on first boot, which has historically
        clobbered our pre-baked version (and broke us between v0.9.4
        and v0.9.6). The robust pattern is to *re-assert* the config
        on every spawn:

        1. Patch the config file to enforce our required keys
           (HTTP server enabled, port = our port, start minimized,
           no auto-update). User-set unrelated keys are preserved.
        2. Spawn the process.
        3. Probe the configured port for up to
           ``http_ready_timeout_s`` seconds. If we never see the
           server, log a clear error and let the supervise loop
           handle restart on the next iteration -- we don't kill
           the process here because a slow-starting LHM might still
           be useful once it comes up; subsequent restarts will
           catch a permanently broken state.
        """
        if self._config.manage_config:
            lhm_dir = Path(self._config.executable).parent
            try:
                ensure_lhm_config(lhm_dir, port=self._config.port)
            except OSError as exc:
                # Config write failures are not fatal -- LHM may have
                # a usable config from a previous run, or the user may
                # have set things via the GUI. Log loudly and proceed.
                _log.warning(
                    "could not ensure LHM config at %s: %s; "
                    "spawning anyway with whatever is on disk",
                    lhm_dir, exc,
                )
            # Reserve the wildcard URL so LHM's HttpListener can bind it
            # without being elevated. Best-effort; never fatal (logs status).
            ensure_url_reservation(self._config.port)

        args = [
            str(self._config.executable),
            *self._config.extra_args,
        ]
        _log.info("starting LHM: %s (port=%d via config)", " ".join(args), self._config.port)
        proc = self._spawner.spawn(args)
        with self._proc_lock:
            self._proc = proc
        with self.status_lock:
            self._status.running = True
            self._status.pid = proc.pid
            self._status.started_at = time.time()
            self._status.restart_count += (
                1 if self._status.last_exit_code is not None else 0
            )
            self._status.next_retry_at = None
            self._status.last_error = None
            self._status.http_ready = False

        # Best-effort port probe. Failures don't tear down the process
        # -- LHM might come up late, and an unreachable HTTP server is
        # surfaced via the collector's own probe path. What we want to
        # avoid is the dashboard reporting "LHM running" while the
        # collector reports "LHM unreachable", which is exactly what
        # silently broke between v0.9.4 and v0.9.6.
        #
        # ``http_ready_timeout_s <= 0`` skips the probe entirely. Useful
        # in tests with fake spawners that have no real port to probe.
        if self._config.http_ready_timeout_s > 0:
            if probe_lhm_http(
                host="127.0.0.1",
                port=self._config.port,
                timeout_s=self._config.http_ready_timeout_s,
                sleep=self._sleep,
                clock=self._clock,
                stop_event=self._stop_event,
            ):
                with self.status_lock:
                    self._status.http_ready = True
                _log.info("LHM HTTP server up on 127.0.0.1:%d", self._config.port)
            else:
                with self.status_lock:
                    self._status.last_error = (
                        f"LHM started (pid={proc.pid}) but its HTTP server did "
                        f"not come up on port {self._config.port} within "
                        f"{self._config.http_ready_timeout_s:.0f}s. "
                        "Check that the config file enables the web server -- "
                        "see docs/sensors.md."
                    )
                _log.warning(
                    "LHM spawned (pid=%d) but port %d did not open within %.0fs",
                    proc.pid, self._config.port, self._config.http_ready_timeout_s,
                )

    def _wait_for_proc(self) -> int | None:
        """Block until the current LHM proc exits or stop is requested.

        Returns the exit code (or None if we tore it down via stop()).
        We poll rather than ``Popen.wait`` so the supervise loop stays
        responsive to ``stop_event`` without needing per-platform
        signal plumbing.
        """
        while not self._stop_event.is_set():
            with self._proc_lock:
                proc = self._proc
            if proc is None:
                return None
            code = proc.poll()
            if code is not None:
                return code
            self._sleep(0.5)
        return None

    def _terminate_proc(self) -> None:
        """Best-effort terminate the current LHM proc, escalating to
        kill after the grace period. Safe to call when no proc is alive."""
        with self._proc_lock:
            proc = self._proc
            self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                deadline = self._clock() + self._config.shutdown_grace_s
                while proc.poll() is None and self._clock() < deadline:
                    self._sleep(0.1)
                if proc.poll() is None:
                    _log.warning("lhm did not exit on terminate; killing pid=%s", proc.pid)
                    proc.kill()
        except OSError as exc:
            # Process may have already died between our poll and
            # terminate; that's the OS's problem now.
            _log.debug("lhm terminate raced with exit: %s", exc)

    def _wait_with_backoff(self, seconds: float) -> None:
        """Wait up to ``seconds`` but break early on stop. Updates
        ``next_retry_at`` so the dashboard can render a countdown."""
        with self.status_lock:
            self._status.next_retry_at = time.time() + seconds
        # Wake every 100 ms so stop() returns promptly even mid-backoff.
        deadline = self._clock() + seconds
        while not self._stop_event.is_set() and self._clock() < deadline:
            self._sleep(0.1)


# ---------------------------------------------------------------------------
# Bundled-LHM resolution
# ---------------------------------------------------------------------------


# SDDL granting the well-known *Everyone* (World, ``WD``) principal
# GENERIC_EXECUTE (``GX``) -- the http.sys access right that lets a token
# register/listen on a reserved URL. Using the SID keeps this correct on
# non-English Windows (the literal name "Everyone" is localized).
_URLACL_SDDL: Final = "D:(A;;GX;;;WD)"


def ensure_url_reservation(port: int, *, host: str = "+") -> str:
    """Ensure http.sys lets LHM bind ``http://<host>:<port>/`` unelevated.

    Why this is needed
    ------------------
    LHM 0.9.6 (the latest stable) always binds the *strong-wildcard*
    prefix ``http://+:<port>/`` for its remote web server -- binding a
    specific IP such as ``127.0.0.1`` only landed in post-0.9.6 builds.
    http.sys refuses a wildcard prefix for any token that lacks an
    explicit URL reservation *or* administrator rights, so on a hardened
    box (and on recent Windows builds even for some service tokens) LHM's
    ``HttpListener.Start()`` throws ``Access is denied``, the exception is
    swallowed, and the process sits up with no HTTP server -- the
    ``process_up_no_http`` state that disables every LHM-backed rule.

    The fix is the documented one: reserve the URL for *Everyone*. The
    watchdog runs as LocalSystem, so it can create the reservation here on
    every spawn (self-healing if someone clears it). Once present, LHM
    binds regardless of its own privilege level.

    Best-effort and never fatal: if we're not elevated (dev ``atf run``),
    ``netsh`` is missing, or the box forbids it, we log and return a status
    string -- LHM is spawned anyway and the collector probe surfaces the
    result. Returns one of: ``"present"``, ``"added"``,
    ``"failed:<reason>"``, ``"skipped:<reason>"``.
    """
    if sys.platform != "win32":
        return "skipped:not-windows"

    url = f"http://{host}:{port}/"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    # 1. Already reserved? `show urlacl url=` is read-only and cheap, and
    #    avoids a redundant (admin-only) `add` on every single spawn.
    try:
        shown = subprocess.run(
            ["netsh", "http", "show", "urlacl", f"url={url}"],
            capture_output=True, text=True, timeout=10, creationflags=flags,
        )
        if url.lower() in (shown.stdout or "").lower():
            return "present"
    except Exception as exc:  # noqa: BLE001 -- diagnostics only, never fatal
        _log.debug("netsh show urlacl %s raised %r", url, exc)

    # 2. Add the reservation for Everyone (World SID, locale-independent).
    try:
        added = subprocess.run(
            ["netsh", "http", "add", "urlacl", f"url={url}", f"sddl={_URLACL_SDDL}"],
            capture_output=True, text=True, timeout=10, creationflags=flags,
        )
        if added.returncode == 0:
            _log.info("reserved URL ACL %s so LHM's web server can bind unelevated", url)
            return "added"
        detail = ((added.stdout or "") + (added.stderr or "")).strip().splitlines()
        reason = detail[-1].strip() if detail else f"rc={added.returncode}"
        _log.warning(
            "could not reserve URL ACL %s (%s); LHM's web server may fail to "
            "bind unless LHM runs elevated. Run elevated: "
            "netsh http add urlacl url=%s user=Everyone listen=yes",
            url, reason, url,
        )
        return f"failed:{reason}"
    except Exception as exc:  # noqa: BLE001 -- never let this break startup
        _log.warning("netsh add urlacl %s raised %r; spawning LHM anyway", url, exc)
        return f"failed:{exc!r}"


def find_lhm_executable(
    *,
    bundled_root: Path | None = None,
    extra_search_paths: tuple[Path, ...] = (),
) -> Path | None:
    """Locate ``LibreHardwareMonitor.exe``.

    Search order, first existing wins:

    1. Explicit ``ATFIELD_LHM_EXE`` env var (full path to the binary).
    2. ``bundled_root/LibreHardwareMonitor.exe`` (the installer drops a
       copy here next to the AT-Field binaries).
    3. Each path in ``extra_search_paths`` joined with the binary name.
    4. ``%PROGRAMFILES%/LibreHardwareMonitor/LibreHardwareMonitor.exe``
       (where the upstream installer lands).

    Returns ``None`` when nothing is found; the supervisor will then
    surface "LHM not installed" via :attr:`LhmStatus.last_error` instead
    of crashing the service.
    """
    binary = "LibreHardwareMonitor.exe"

    env_override = os.environ.get("ATFIELD_LHM_EXE")
    if env_override:
        p = Path(env_override)
        if p.is_file():
            return p

    candidates: list[Path] = []
    if bundled_root is not None:
        candidates.append(Path(bundled_root) / binary)
    for extra in extra_search_paths:
        candidates.append(Path(extra) / binary)

    program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    candidates.append(Path(program_files) / "LibreHardwareMonitor" / binary)

    for c in candidates:
        if c.is_file():
            return c
    return None


# ---------------------------------------------------------------------------
# Port probe
# ---------------------------------------------------------------------------


def probe_lhm_http(
    *,
    host: str,
    port: int,
    timeout_s: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    stop_event: threading.Event | None = None,
    poll_interval_s: float = 0.5,
) -> bool:
    """Poll ``host:port`` until a TCP connect succeeds or ``timeout_s``
    elapses. Returns True on success, False on timeout.

    A successful TCP connect means LHM's HTTP server is bound and ready
    to serve ``/data.json`` -- the collector will be able to read it on
    its next sample. We don't bother sending an HTTP GET as part of the
    probe; LHM doesn't open the listening socket until the server is
    fully initialized, so the TCP handshake is sufficient.

    Returns early if ``stop_event`` is set, so a supervisor shutdown
    during a slow LHM start doesn't have to wait the full timeout.
    """
    deadline = clock() + timeout_s
    while clock() < deadline:
        if stop_event is not None and stop_event.is_set():
            return False
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except (TimeoutError, OSError):
            pass
        sleep(poll_interval_s)
    return False
