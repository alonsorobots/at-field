"""AT-Field actuator: process-tree-aware kill with launcher walk-up.

This is the half of the watchdog that does damage-prevention work, so the
behavior is defined precisely and exercised by tests against a fake
process tree (see ``tests/test_actuator.py``). The real psutil calls live
behind a thin :class:`ProcessProvider` indirection so production uses live
processes and tests use a deterministic mock.

Algorithm (matches PLANNING.md §5.3)
------------------------------------
1. Identify candidate offender PIDs. For GPU rules, use the per-GPU
   process map captured by :mod:`atfield.collectors.nvml`. For RAM/commit
   rules, scan all running processes whose name is in
   ``targeting.killable_names`` and pick the largest by RSS.
2. For each offender PID, walk up the parent chain: while the parent's
   name is in ``killable_names | launcher_names``, climb. Stop at the
   highest python-or-launcher whose parent is *not* itself one. That PID
   is the kill root.
3. Enumerate the kill root and all its descendants. Filter out anything
   in ``never_kill_names`` (and the watchdog's own PID).
4. Mode ``graceful``: send SIGTERM-equivalent (``terminate()`` on Windows
   becomes ``TerminateProcess`` -> immediate, but with a brief drain
   window). Wait ``grace_seconds`` for the tree to exit. Anything still
   alive: ``kill()`` (force).
5. Mode ``aggressive``: skip the grace window and ``kill()`` immediately.
6. Return a :class:`KillReport` describing what was found, what was sent,
   and what (if anything) survived. The audit log writes one record per
   report regardless of outcome.

Self-protection rules:
* The service's own PID is filtered out unconditionally.
* ``never_kill_names`` is filtered out (defaults include ``explorer.exe``,
  ``services.exe``, ``code.exe``, the watchdog itself, etc).
* If the kill-root walk produces no killable PIDs after filtering, the
  actuator returns a report with ``skipped_reason`` populated rather than
  performing a destructive action -- the caller (audit log) records the
  miss for operator visibility.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from atfield import agent_inbox
from atfield.client_registry import (
    discover_protected,
    live_manifests,
    is_absolute_protection,
    request_stop,
)
from atfield.config import AtFieldConfig
from atfield.policy import Action

__all__ = [
    "Actuator",
    "KillReport",
    "KilledProcess",
    "ProcInfo",
    "ProcessProvider",
    "PsutilProvider",
    "find_kill_root",
    "offender_axis",
    "processes_over_rss_cap",
    "RSS_CAP_RULE",
    "script_name_from_cmdline",
]


_log = logging.getLogger("atfield.actuator")

# Window used to measure per-process CPU when ranking offenders for a CPU rule.
# Long enough to be meaningful, short enough not to delay a thermal response --
# and small next to the graceful-kill grace period that follows it.
_CPU_SAMPLE_INTERVAL_S = 0.3


# ---------------------------------------------------------------------------
# Cmdline → "what was actually running" extraction
# ---------------------------------------------------------------------------


# Python flags that take NO value (so the next argv item is the script).
# Source: `python --help` short flags. Conservative: when in doubt we treat
# unknown short flags as value-less; the alternative (consuming the next arg)
# can mis-identify a script as a flag value.
_PY_VALUELESS_SHORT_FLAGS = frozenset(
    "BOdEhiIqRsSuvVx"
)
# Python flags that DO take a value as the next argv item.
_PY_VALUE_TAKING_SHORT_FLAGS = frozenset(("-W", "-X", "-c", "--check-hash-based-pycs"))


def _basename(path: str) -> str:
    """Cross-platform basename without the os.path round-trip.

    psutil cmdlines on Windows can contain forward OR back slashes depending
    on how the launcher was invoked, so we strip both.
    """
    if not path:
        return path
    last_sep = max(path.rfind("\\"), path.rfind("/"))
    return path[last_sep + 1 :] if last_sep >= 0 else path


def script_name_from_cmdline(cmdline: tuple[str, ...] | list[str] | None) -> str | None:
    """Best-effort 'what was the user actually running?' from a cmdline.

    Returns ``None`` if the cmdline doesn't carry useful info (empty, or
    it's just an interactive interpreter like ``python.exe`` with no args).

    Examples::

        ('python.exe', 'train.py', '--lr', '1e-4')             → 'train.py'
        ('python.exe', '-u', 'scripts/train.py')               → 'train.py'
        ('python.exe', '-m', 'torch.distributed.run', '--np')  → 'torch.distributed.run'
        ('python.exe', '-c', 'import torch; ...')              → '<inline -c>'
        ('powershell.exe', '-File', 'C:\\\\foo\\\\bar.ps1')    → 'bar.ps1'
        ('cmd.exe', '/c', 'run.bat')                           → 'run.bat'
        ('node.exe', 'server.mjs')                             → 'server.mjs'
        ('python.exe',)                                        → None

    The intent is to pick the single most informative token to put in front
    of an operator who's reading "why was my training job killed?". The
    launcher executable name (``python.exe``, ``node.exe``) is uninformative
    and lives elsewhere in the kill report; this is the script *behind* it.
    """
    if not cmdline or len(cmdline) < 2:
        return None

    argv = list(cmdline[1:])
    i = 0
    while i < len(argv):
        arg = argv[i]
        # Inline-code modes
        if arg in ("-c", "-Command"):
            return "<inline -c>" if arg == "-c" else "<inline -Command>"
        # Module mode: next token IS the script identifier
        if arg == "-m":
            return argv[i + 1] if i + 1 < len(argv) else None
        # PowerShell -File / -f
        if arg.lower() in ("-file", "-f") and i + 1 < len(argv):
            return _basename(argv[i + 1])
        # cmd.exe /c /k
        if arg in ("/c", "/k", "/C", "/K") and i + 1 < len(argv):
            return _basename(argv[i + 1])
        # End-of-options: anything past `--` is positional
        if arg == "--":
            i += 1
            if i < len(argv):
                return _basename(argv[i])
            return None
        # Long flags: --foo, --foo=bar
        if arg.startswith("--"):
            i += 1
            continue
        # Short Python flags
        if arg.startswith("-") and len(arg) >= 2:
            # -W / -X take an argument
            if arg in _PY_VALUE_TAKING_SHORT_FLAGS:
                i += 2
                continue
            # Bundled short flags like -uOO or -u: peel off any value-less
            # combo and move on.
            stripped = arg.lstrip("-")
            if stripped and all(c in _PY_VALUELESS_SHORT_FLAGS for c in stripped):
                i += 1
                continue
            # Unknown short flag: skip just this token (don't risk eating a
            # script name as a flag value).
            i += 1
            continue
        # First positional token = the script
        return _basename(arg)
    return None


# ---------------------------------------------------------------------------
# Process abstraction (so tests don't have to spawn real processes)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProcInfo:
    """Snapshot of a process for audit logging.

    A frozen dataclass rather than a live psutil.Process so we can capture
    name/cmdline before sending the terminate signal -- once a process is
    gone, psutil can no longer query it.
    """

    pid: int
    ppid: int
    name: str
    cmdline: tuple[str, ...]
    rss_bytes: int = 0
    # Process start time, used to detect a client manifest whose PID has been
    # recycled (see :mod:`atfield.client_registry`).
    create_time: float = 0.0


class ProcessProvider(Protocol):
    """Indirection over psutil so the actuator is testable with a mock tree."""

    def own_pid(self) -> int: ...

    def list_all(self) -> list[ProcInfo]:
        """Return a snapshot of every process visible to the service."""
        ...

    def list_all_lean(self) -> list[ProcInfo]:
        """Like :meth:`list_all` but WITHOUT cmdline -- entries carry ``()``.

        MEASURED on a 466-process box, ELEVATED (how the service runs):
        cmdline() is 2,060 ms of a 2,091 ms walk; without it, 32.7 ms.
        Unelevated the numbers invert, so measure in the right context.

        Callers MUST NOT make a kill decision from these entries: cmdline is a
        safety filter (``never_kill_cmdline_patterns``). Re-fetch the full
        record with :meth:`get` for the few processes that matter.
        """
        ...

    def get(self, pid: int) -> ProcInfo | None:
        """Return ProcInfo for ``pid``, or None if it no longer exists."""
        ...

    def parent(self, pid: int) -> ProcInfo | None:
        """Return ProcInfo of pid's parent, or None if absent / orphan."""
        ...

    def descendants(self, pid: int) -> list[ProcInfo]:
        """Return ProcInfo for every transitive descendant of ``pid``."""
        ...

    def environ(self, pid: int) -> dict[str, str] | None:
        """pid's environment, or None if unreadable / gone."""
        ...

    def terminate(self, pid: int) -> None:
        """Best-effort polite termination (SIGTERM-equivalent)."""
        ...

    def kill(self, pid: int) -> None:
        """Force-kill (SIGKILL-equivalent)."""
        ...

    def is_alive(self, pid: int) -> bool: ...

    def suspend(self, pid: int) -> bool:
        """Pause execution of ``pid``. Returns True on success, False if
        the OS / process is gone or the operation isn't supported.
        Used by the ``throttle`` action."""
        ...

    def resume(self, pid: int) -> bool:
        """Inverse of :meth:`suspend`. Best-effort; returns True on
        success."""
        ...

    def sample_cpu_percent(self, pids: Sequence[int], interval: float) -> dict[int, float]:
        """CPU busy-ness of each PID over ``interval``, percent of one core.

        Deliberately a separate call rather than a :class:`ProcInfo` field:
        measuring CPU needs two samples separated in time, which is far too
        expensive to do for every process on every scan. PIDs that vanish
        mid-sample are simply absent from the result.
        """
        ...


# ---------------------------------------------------------------------------
# Real psutil-backed provider (used in production)
# ---------------------------------------------------------------------------


class PsutilProvider:
    """ProcessProvider implementation backed by ``psutil``."""

    def __init__(self) -> None:
        import psutil
        self._psutil = psutil

    def own_pid(self) -> int:
        return os.getpid()

    def _to_info(self, p: Any) -> ProcInfo | None:
        try:
            with p.oneshot():
                name = p.name()
                ppid = p.ppid()
                try:
                    cmdline = tuple(p.cmdline() or ())
                except Exception:
                    cmdline = ()
                try:
                    rss = int(p.memory_info().rss)
                except Exception:
                    rss = 0
                try:
                    created = float(p.create_time())
                except Exception:
                    created = 0.0
            return ProcInfo(
                pid=p.pid,
                ppid=ppid,
                name=name,
                cmdline=cmdline,
                rss_bytes=rss,
                create_time=created,
            )
        except Exception:
            return None

    def list_all(self) -> list[ProcInfo]:
        out: list[ProcInfo] = []
        for p in self._psutil.process_iter(attrs=None):
            info = self._to_info(p)
            if info is not None:
                out.append(info)
        return out

    def list_all_lean(self) -> list[ProcInfo]:
        """Every process, minus the one field that costs 98.5% of the walk."""
        out: list[ProcInfo] = []
        for p in self._psutil.process_iter(attrs=None):
            try:
                with p.oneshot():
                    name = p.name()
                    ppid = p.ppid()
                    try:
                        rss = int(p.memory_info().rss)
                    except Exception:
                        rss = 0
                    try:
                        created = float(p.create_time())
                    except Exception:
                        created = 0.0
                out.append(ProcInfo(pid=p.pid, ppid=ppid, name=name,
                                    cmdline=(), rss_bytes=rss,
                                    create_time=created))
            except Exception:
                continue
        return out

    def get(self, pid: int) -> ProcInfo | None:
        try:
            return self._to_info(self._psutil.Process(pid))
        except Exception:
            return None

    def parent(self, pid: int) -> ProcInfo | None:
        try:
            p = self._psutil.Process(pid).parent()
            if p is None:
                return None
            return self._to_info(p)
        except Exception:
            return None

    def descendants(self, pid: int) -> list[ProcInfo]:
        try:
            children = self._psutil.Process(pid).children(recursive=True)
        except Exception:
            return []
        out: list[ProcInfo] = []
        for c in children:
            info = self._to_info(c)
            if info is not None:
                out.append(info)
        return out

    def environ(self, pid: int) -> dict[str, str] | None:
        try:
            return self._psutil.Process(pid).environ()
        except Exception:
            return None

    def terminate(self, pid: int) -> None:
        try:
            self._psutil.Process(pid).terminate()
        except Exception:
            pass

    def kill(self, pid: int) -> None:
        try:
            self._psutil.Process(pid).kill()
        except Exception:
            pass

    def is_alive(self, pid: int) -> bool:
        try:
            return self._psutil.Process(pid).is_running()
        except Exception:
            return False

    def suspend(self, pid: int) -> bool:
        # psutil.Process.suspend() works on Windows (NtSuspendProcess)
        # and Linux (SIGSTOP). May raise AccessDenied if we don't have
        # the right privileges; that's not a programming error so we
        # swallow + return False so the actuator can report it cleanly.
        try:
            self._psutil.Process(pid).suspend()
            return True
        except Exception:
            return False

    def resume(self, pid: int) -> bool:
        try:
            self._psutil.Process(pid).resume()
            return True
        except Exception:
            return False

    def sample_cpu_percent(self, pids: Sequence[int], interval: float) -> dict[int, float]:
        # psutil's Process.cpu_percent() is relative to the previous call on
        # that same object, so it returns 0.0 the first time -- useless here,
        # where every scan builds fresh Process objects. Two explicit
        # cpu_times() snapshots give a real measurement instead.
        if not pids or interval <= 0:
            return {}

        def snapshot() -> dict[int, float]:
            out: dict[int, float] = {}
            for pid in pids:
                try:
                    t = self._psutil.Process(pid).cpu_times()
                    out[pid] = float(t.user) + float(t.system)
                except Exception:
                    continue
            return out

        first = snapshot()
        if not first:
            return {}
        start = time.monotonic()
        time.sleep(interval)
        elapsed = time.monotonic() - start or interval
        second = snapshot()

        result: dict[int, float] = {}
        for pid, before in first.items():
            after = second.get(pid)
            if after is None:
                continue  # exited mid-sample
            result[pid] = max(0.0, (after - before) / elapsed * 100.0)
        return result


# ---------------------------------------------------------------------------
# Kill targeting
# ---------------------------------------------------------------------------


def _host_ram_bytes() -> int:
    import psutil
    return int(psutil.virtual_memory().total)


def offender_axis(signal: str) -> str:
    """Which per-process metric identifies the offender for ``signal``.

    Returns ``"cpu"`` or ``"memory"``. This exists because ranking candidates by
    RSS for a *CPU* rule picks the wrong victim: a busy process can be tiny (a
    compute loop holding almost no memory) while an idle supervisor is fat, so
    RSS ranking silently kills the largest process rather than the one causing
    the heat.

    ``"gpu"`` for every ``gpu.*`` signal: the target is the process with MEASURED GPU use
    (``gpu_use`` from the NVML collector), or nobody. Never RSS -- 2026-10-18 an LHM junction
    rule fell back to RSS and killed an idle viewer while the heater ran on.
    """
    if signal.startswith("gpu."):
        return "gpu"
    if "cpu" in signal:
        return "cpu"
    return "memory"


# A GPU kill needs a process at or over this mean SM % (NVML process utilization, ~10 s).
# Measured on Chronos 2026-10-18: desktop processes read 0-2, an idle CUDA context is
# absent; a matmul heater ~100. 20 still catches two heaters sharing one card (~50 each).
GPU_USE_FLOOR = 20.0
_NO_ELIGIBLE = "no eligible offender PID matched killable_names"


RSS_CAP_RULE = "process-rss-cap"

# How often the per-process RSS cap may walk the process table.
#
# NOT every tick, which is what it used to do. list_all() builds a ProcInfo per
# process INCLUDING cmdline, and on Windows that measured 0.70 s median / 4.32 s
# worst over ~400 processes -- against a 1.0 s tick budget. The tick stretched
# to ~4.7 s, and because min_samples was sized from the CONFIGURED tick rate,
# every kill rule silently went to INSUFFICIENT and stopped guarding. A CPU then
# ran >=90 C for an hour with a 90 C rule armed and inert (2026-08-30).
#
# 5 s costs nothing that matters: this cap exists to catch a process whose RSS
# has run away, and RSS runs away over seconds-to-minutes, not milliseconds. The
# guard it was suppressing is the one that protects the whole machine, so the
# trade is not close.
_RSS_CAP_MIN_INTERVAL_S = 5.0
# warn mode: re-tell the owner of a process still over the line this often (snooze/mute
# silence it sooner; the repeat is what makes a long-lived oversized process visible).
_RSS_WARN_REPEAT_S = 1800.0


def processes_over_rss_cap(
    processes: Iterable[ProcInfo], cap_bytes: int
) -> list[ProcInfo]:
    """Processes holding more than ``cap_bytes`` of RSS, largest first.

    Pure and separately testable, because the interesting part is the policy
    question -- *is one process unreasonable* -- not the process walking.

    This is the complement of the system-wide rules, not a duplicate of them.
    A rule on ``system.ram_used_percent`` can only answer "is the machine in
    danger", which is necessarily late: the memory is already committed by the
    time the percentage moves, and the guard then races the allocator. A cap on
    a single process is answerable while the machine is still healthy -- one
    worker holding 67 GB is wrong at 60% system RAM, not merely at 97%.
    """
    if cap_bytes <= 0:
        return []
    over = [p for p in processes if p.rss_bytes > cap_bytes]
    return sorted(over, key=lambda p: p.rss_bytes, reverse=True)


class _TableProvider:
    """``get`` / ``parent`` over a process table already in hand, for ranking whole kill trees without one more
    psutil call (each re-reads a cmdline, ~1.7 ms) per ancestor while the machine is at the memory wall. A parent
    that started after its child is a recycled PID, not the parent (psutil's own ``parent()`` check)."""

    def __init__(self, procs: Iterable[ProcInfo]) -> None:
        self._by = {p.pid: p for p in procs}

    def get(self, pid: int) -> ProcInfo | None:
        return self._by.get(pid)

    def parent(self, pid: int) -> ProcInfo | None:
        child = self._by.get(pid)
        par = None if child is None else self._by.get(child.ppid)
        if par is None or par.pid == child.pid:
            return None
        if par.create_time and child.create_time and par.create_time > child.create_time:
            return None
        return par


def find_kill_root(
    pid: int,
    *,
    provider: ProcessProvider,
    killable_names: frozenset[str],
    launcher_names: frozenset[str],
    protected_pids: frozenset[int] = frozenset(),
) -> ProcInfo | None:
    """Walk up the parent chain to the highest python-or-launcher ancestor.

    Stops at the topmost process whose name is in
    ``killable_names | launcher_names`` *and* whose parent is not. This is
    the dispatcher: terminating its tree takes self-healing workers down
    with it (the "many jobs have coordinators with self-healing workers"
    case from the bootstrap chat).

    ``protected_pids`` bounds that climb. A long-lived supervisor (a job
    coordinator, an MCP server, a tray icon) is itself ``python.exe``, so
    without this the walk-up sails straight past the job it should be targeting
    and returns the supervisor -- killing an entire service to shed one job's
    load. When the parent is protected we stop and return the child, which is
    the root of the job subtree. When the offender is *itself* protected there is
    no legitimate target, so this returns ``None``.

    Returns ``None`` if ``pid`` doesn't exist anymore or never matched
    a killable/launcher name in the first place.
    """
    keepers = killable_names | launcher_names
    seen: set[int] = set()
    cursor = provider.get(pid)
    if cursor is None:
        return None

    # If the offender itself is not a python/launcher, we have no killable
    # candidate. Refuse to walk up arbitrary processes (e.g. don't climb
    # into svchost.exe just because a python child of it spiked GPU temp).
    if cursor.name.lower() not in {n.lower() for n in keepers}:
        return None

    if cursor.pid in protected_pids:
        return None

    while True:
        if cursor.pid in seen:
            return cursor  # cycle guard (shouldn't happen, but defense in depth)
        seen.add(cursor.pid)
        parent = provider.parent(cursor.pid)
        if parent is None:
            return cursor
        if parent.pid in protected_pids:
            return cursor  # stop below the supervisor: this is the job root
        if parent.name.lower() not in {n.lower() for n in keepers}:
            return cursor
        cursor = parent


# ---------------------------------------------------------------------------
# Kill report
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KilledProcess:
    info: ProcInfo
    method: str       # "terminate" | "kill" | "skipped"
    survived: bool


@dataclass(frozen=True, slots=True)
class KillReport:
    """Outcome of one actuator invocation, written to events.jsonl."""

    action: Action
    offender_pid: int | None
    kill_root: ProcInfo | None
    killed: tuple[KilledProcess, ...] = ()
    skipped_reason: str | None = None
    finished_at_ns: int = 0
    # The kill root's parent chain, (pid, name) nearest first, captured BEFORE the
    # kill (psutil cannot report an exited process's parent). Lets whoever launched
    # it recognise its own work: an agent session's process is in this chain.
    ancestors: tuple[tuple[int, str], ...] = ()
    # Who owns the kill root (agent_inbox.Owner, captured pre-kill like ancestors) and the
    # ranked owners agent_inbox.notify() attributed; serialised only via Owner.public().
    owner: Any = None
    owners: tuple[Any, ...] = ()

    @property
    def succeeded(self) -> bool:
        return (
            self.kill_root is not None
            and not any(k.survived for k in self.killed)
        )


# ---------------------------------------------------------------------------
# Actuator
# ---------------------------------------------------------------------------


class Actuator:
    """Executes :class:`Action` objects produced by :class:`atfield.policy.PolicyEngine`."""

    def __init__(
        self,
        cfg: AtFieldConfig,
        *,
        provider: ProcessProvider | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self._cfg = cfg
        self._provider = provider or PsutilProvider()
        self._sleep = sleep

        self._killable = frozenset(n.lower() for n in cfg.targeting.killable_names)
        self._launchers = frozenset(n.lower() for n in cfg.targeting.launcher_names)
        self._never = frozenset(n.lower() for n in cfg.targeting.never_kill_names)
        self._never_cmdline = tuple(cfg.targeting.never_kill_cmdline_patterns)
        self._protected_roles = tuple(cfg.targeting.protected_client_roles)
        self._own_pid = self._provider.own_pid()
        # Last time the RSS cap walked the process table (see
        # _RSS_CAP_MIN_INTERVAL_S). Monotonic ns; 0 means "never, run now".
        self._last_rss_cap_ns = 0
        self._rss_warned: dict[tuple[int, Any], int] = {}   # (pid, create_time) -> last warned

        # Throttle action support: tracks PIDs we've suspended so we can
        # resume them on shutdown (a service crash mid-throttle would
        # otherwise leave the workload paused indefinitely). Keyed by
        # PID; value is the threading.Timer that will resume it.
        self._throttle_lock = threading.Lock()
        self._active_throttles: dict[int, threading.Timer] = {}

        # Escalation state: protected owner PID -> how many of its children we
        # have killed without the pressure going away. See _escalate().
        # owner pid -> (futile kill count, monotonic time of the last one)
        self._futile_kills: dict[int, tuple[int, float]] = {}
        # Owners we have already asked to stop, so we ask once and then escalate
        # rather than re-dropping the same file forever.
        self._stop_requested: set[int] = set()
        # Populated by _protected_pids() so escalation can address the OWNER,
        # not just avoid it. Discovery already parses these manifests; carrying
        # the objects costs nothing and re-globbing would risk a different view
        # of the world between deciding and acting.
        self._clients_by_pid: dict[int, Any] = {}
        # Every live Kiroshi worker manifest (pid -> manifest), protected or not: the
        # load owner of its slots. Refreshed with _clients_by_pid.
        self._workers: dict[int, dict[str, Any]] = {}

    # -- Public entry point ------------------------------------------------

    def enforce_rss_cap(self, *, now_ns: int | None = None) -> list[KillReport]:
        """Kill any single process holding more than ``kill.max_process_rss_gb``.

        Returns one report per process killed; empty when the cap is disabled
        (the default) or nothing exceeds it.

        DOES NOT WALK UP TO THE KILL ROOT, and that is the whole point. The
        system-pressure path walks up deliberately: when the machine is in
        danger you want to shed the whole job. Here the offender is one process
        that has individually misbehaved, and its siblings are innocent -- so
        killing just it (plus its own descendants) turns the incident into ONE
        failed unit of work instead of a dead job or a dead machine. Walking up
        would take out every sibling worker and lose all of their progress.

        Deliberately NOT rate-limited by the kill cooldown. The cooldown exists
        to stop a *system-wide* rule thrashing on one noisy signal; this fires
        on a specific process crossing a specific number, so there is nothing to
        flap about -- the process either is over the cap or it is not, and if
        three workers are each over it, all three are individually wrong.
        """
        cap_gb = self._cfg.kill.max_process_rss_gb
        if cap_gb <= 0:
            return []
        # Rate-limit the process walk itself. The caller runs every tick; the
        # scan must not. See _RSS_CAP_MIN_INTERVAL_S -- this is a tick-budget
        # guard, not a debounce on the decision.
        now_mono_ns = now_ns if now_ns is not None else int(time.monotonic_ns())
        if self._last_rss_cap_ns and (
            now_mono_ns - self._last_rss_cap_ns
            < int(_RSS_CAP_MIN_INTERVAL_S * 1_000_000_000)
        ):
            return []
        self._last_rss_cap_ns = now_mono_ns
        cap_bytes = int(cap_gb * (1024 ** 3))

        # THREE THINGS ARE EXPENSIVE HERE, AND A HEALTHY MACHINE PAYS FOR NONE.
        #
        # `_protected_pids()` calls the FULL list_all() -- with cmdline --
        # whenever never_kill_cmdline_patterns is configured, and the candidate
        # scan called it again. On Chronos, which HAS those patterns set, that
        # is two 2-second walks per run: measured `SLOW TICK: 4367.0ms against
        # a 1000.0ms budget -- rss_cap 4364.6ms`, 6,557 times, holding the loop
        # at 0.36 Hz and its thermal rules at 2.4x margin. DEMETER and Aurora
        # configure no patterns, pay one walk, and sit at 0.78 Hz -- which is
        # the entire reason Chronos looked uniquely slow.
        #
        # This is the same component that starved the loop to 0.22 Hz on
        # 2026-09-03 and let the machine run an hour at Tjmax.
        #
        # So: sweep cheap FIRST (no cmdline, 33 ms), and reach for the
        # expensive facts only if something is actually over the cap. In 1,812
        # recorded events across three hosts, nothing real ever has been.
        scan = getattr(self._provider, "list_all_lean", self._provider.list_all)
        lean = [p for p in scan() if p.name.lower() in self._killable]
        over = processes_over_rss_cap(lean, cap_bytes)
        warn = self._cfg.kill.max_process_rss_action != "kill"
        if warn:
            # In warn mode the process STAYS over (kill mode removed it), so one already
            # told within the repeat window must cost only this lean sweep -- not the
            # 2-7 s of full facts below every 5 s (the 2026-09-03 starvation shape).
            repeat_ns = int(_RSS_WARN_REPEAT_S * 1_000_000_000)
            live = {(o.pid, o.create_time) for o in over}
            self._rss_warned = {k: t for k, t in self._rss_warned.items() if k in live}
            over = [o for o in over if now_mono_ns - self._rss_warned.get(
                (o.pid, o.create_time), -repeat_ns) >= repeat_ns]
            for o in over:      # also those the full facts then skip (protected, never-kill)
                self._rss_warned[(o.pid, o.create_time)] = now_mono_ns
        if not over:
            return []

        # Something IS oversized: now pay for the facts a kill needs. The lean
        # entries carry no cmdline, so deciding from them would make the scan
        # fast by making it BLIND -- and this path kills a process AND every
        # descendant.
        protected = self._protected_pids()
        candidates: list[ProcInfo] = []
        for lean_hit in over:
            if lean_hit.pid in protected:
                continue
            full = self._provider.get(lean_hit.pid)
            if full is None:
                continue        # exited between the sweep and the fetch
            if self._is_killable(full):
                candidates.append(full)

        reports: list[KillReport] = []
        if warn:
            return self._warn_rss_cap(processes_over_rss_cap(candidates, cap_bytes), cap_bytes,
                                      now_mono_ns)
        for offender in processes_over_rss_cap(candidates, cap_bytes):
            action = Action(
                kind="kill",
                rule_name=RSS_CAP_RULE,
                base_rule_name=RSS_CAP_RULE,
                signal="process.rss_bytes",
                threshold=float(cap_bytes),
                fraction_over=1.0,
                samples_considered=1,
                latest_value=float(offender.rss_bytes),
                triggered_at_ns=now_ns if now_ns is not None else int(time.monotonic_ns()),
                cooldown_seconds=0,
            )
            targets = [offender, *self._provider.descendants(offender.pid)]
            targets = [
                t for t in targets if self._is_killable(t) and t.pid not in protected
            ]
            if not targets:
                continue
            # Capture the owner while the offender is still alive; psutil cannot
            # report the parent of an exited process.
            owner = self._provider.parent(offender.pid)
            ancestors = self._ancestors(offender.pid)
            # Who to TELL, captured pre-kill like the rule path (the environ is gone after).
            root_owner = self._owner_of(offender, ancestors)
            _log.warning(
                "process-rss-cap: pid=%s name=%s holds %.1f GB (cap %.1f GB); killing it",
                offender.pid, offender.name, offender.rss_bytes / (1024 ** 3), cap_gb,
            )
            # Over-cap is already pathological, so do not grant a grace window
            # that lets it allocate further while we wait politely.
            results = self._kill_immediate(targets)
            # A cap kill counts as futile too. Without this, a pool whose every
            # respawned worker balloons past the cap is killed forever: the
            # machine survives, the job never progresses, and nothing escalates
            # -- a cheaper treadmill than the one that took Chronos down, but
            # still a loop with no exit. Routing both kill paths through the
            # same accounting is what makes the deterrence general.
            self._escalate(offender, owner, protected, action)
            reports.append(
                KillReport(
                    action=action,
                    offender_pid=offender.pid,
                    kill_root=offender,
                    killed=tuple(results),
                    finished_at_ns=int(time.monotonic_ns()),
                    ancestors=ancestors,
                    owner=root_owner and replace(root_owner, pids=len(targets)),
                )
            )
        return reports


    def _warn_rss_cap(self, over: list[ProcInfo], cap_bytes: int,
                      now_ns: int) -> list[KillReport]:
        """``max_process_rss_action = "warn"`` (the default): tell the owner, kill nothing.
        One notice per process per ``_RSS_WARN_REPEAT_S`` while it stays over the line
        (the caller already dropped those told within it, before paying for full facts)."""
        reports: list[KillReport] = []
        for offender in over:
            action = Action(
                kind="log", rule_name=RSS_CAP_RULE, base_rule_name=RSS_CAP_RULE,
                signal="process.rss_bytes", threshold=float(cap_bytes), fraction_over=1.0,
                samples_considered=1, latest_value=float(offender.rss_bytes),
                triggered_at_ns=now_ns, cooldown_seconds=0, notify=True,
            )
            ancestors = self._ancestors(offender.pid)
            owner = self._owner_of(offender, ancestors)
            _log.warning(
                "process-rss-cap (warn): pid=%s name=%s holds %.1f GB (line %.1f GB); "
                "telling its owner, killing nothing", offender.pid, offender.name,
                offender.rss_bytes / (1024 ** 3), cap_bytes / (1024 ** 3))
            reports.append(KillReport(
                action=action, offender_pid=offender.pid, kill_root=None,
                skipped_reason='warn only (kill.max_process_rss_action = "warn")',
                finished_at_ns=int(time.monotonic_ns()), ancestors=ancestors,
                owner=owner and replace(owner, pids=1)))
        return reports

    def _ancestors(self, pid: int, cap: int = 16) -> tuple[tuple[int, str], ...]:
        """(pid, name) of pid's parent, grandparent, ... up to ``cap`` levels."""
        out: list[tuple[int, str]] = []
        p = self._provider.parent(pid)
        while p is not None and len(out) < cap:
            out.append((p.pid, p.name))
            p = self._provider.parent(p.pid)
        return tuple(out)

    # -- Escalation --------------------------------------------------------

    # How many of one owner's children we will kill before concluding that
    # killing children is not working. One is a coincidence; two is a pattern,
    # and waiting longer costs a machine.
    _ESCALATE_ASK_AFTER = 2
    # After asking politely this many times over, take the owner down.
    _ESCALATE_KILL_AFTER = 3
    # Futility is only evidence while it is RECENT. Without a window the count
    # is cumulative over the service's whole life, so a job that legitimately
    # lost one worker this morning and another next week would be escalated on
    # the strength of two unrelated events. Sized against the incident it is
    # drawn from: those kills came 6-26 minutes apart, so half an hour still
    # sees a genuine loop as one episode.
    _FUTILITY_WINDOW_S = 1800.0

    def _escalate(
        self,
        root: ProcInfo,
        owner: ProcInfo | None,
        protected: frozenset[int],
        action: Action,
    ) -> None:
        """Address the OWNER when killing its children is achieving nothing.

        THE FAILURE THIS EXISTS FOR. ``find_kill_root`` stops just below a
        protected process and returns the child, so pressure sheds "the job,
        not the service". That reasoning holds for a one-shot child. It is
        exactly wrong for a self-healing worker pool: the supervisor respawns
        whatever we kill, so the memory comes straight back and the guard is
        firing into a loop it cannot win.

        Measured 2026-08-30: twelve ram-pressure kills over four hours, every
        one of them a ``multiprocessing.spawn`` child of the same few protected
        kiroshi runners, RAM recovering each time and climbing back. The
        machine bugchecked with the guard still dutifully killing children.

        So: count futile kills per owner. At ``_ESCALATE_ASK_AFTER``, use the
        control channel the owner itself advertised -- clients publish
        ``control.graceful_stop`` in their manifest and AT-Field had never once
        read it. At ``_ESCALATE_KILL_AFTER``, stop asking and take the owner's
        tree down, because a supervisor that will not stop spawning IS the
        problem. An explicit ``atfield_never_kill`` opt-in is still honoured;
        a role listed in config is an operator's routine preference and yields
        to keeping the machine alive.
        """
        if owner is None or owner.pid not in protected:
            self._futile_kills.pop(root.pid, None)
            return  # nothing above us was spared; the normal path did its job

        parent = owner
        owner_pid = owner.pid
        now = time.monotonic()
        prev_count, prev_at = self._futile_kills.get(owner_pid, (0, 0.0))
        if now - prev_at > self._FUTILITY_WINDOW_S:
            prev_count = 0          # stale episode; this is a fresh one
            self._stop_requested.discard(owner_pid)
        count = prev_count + 1
        self._futile_kills[owner_pid] = (count, now)
        client = self._clients_by_pid.get(owner_pid)
        who = f"{client.client} {client.role}" if client is not None else parent.name

        if count < self._ESCALATE_ASK_AFTER:
            return

        if count >= self._ESCALATE_KILL_AFTER:
            if client is not None and is_absolute_protection(client):
                _log.error(
                    "escalation blocked: %s (pid %s) has respawned through %d "
                    "kills but declares atfield_never_kill; rule=%s signal=%s. "
                    "Pressure cannot be shed by AT-Field alone.",
                    who, owner_pid, count, action.rule_name, action.signal,
                )
                return
            _log.error(
                "escalating to OWNER: %s (pid %s) respawned through %d kills; "
                "killing its tree to stop the spawn loop (rule=%s)",
                who, owner_pid, count, action.rule_name,
            )
            targets = [parent, *self._provider.descendants(owner_pid)]
            targets = [t for t in targets if self._is_killable(t)]
            if targets:
                self._kill_immediate(targets)
            self._futile_kills.pop(owner_pid, None)
            self._stop_requested.discard(owner_pid)
            return

        if client is not None and owner_pid not in self._stop_requested:
            if request_stop(client):
                self._stop_requested.add(owner_pid)
                _log.warning(
                    "asked %s (pid %s) to stop after %d futile child kills "
                    "(rule=%s)", who, owner_pid, count, action.rule_name,
                )
                return
        _log.warning(
            "%s (pid %s) has now respawned through %d kills and offers no "
            "usable stop channel; will take its tree next time (rule=%s)",
            who, owner_pid, count, action.rule_name,
        )

    def execute(
        self,
        action: Action,
        *,
        candidate_pids: Iterable[int] | None = None,
        gpu_use: Mapping[int, float] | None = None,
    ) -> KillReport:
        """Execute one policy action.

        ``log`` and ``throttle`` actions don't kill anything -- they just
        produce a report with no killed processes (the audit layer still
        writes the record). ``kill`` does the full walk-up + tree-kill.

        ``candidate_pids`` is the set of PIDs the caller (service) believes
        are candidates -- typically the per-GPU process map for GPU rules,
        or top-RSS python procs for RAM rules. If ``None``, the actuator
        scans all processes. ``gpu_use`` ({pid: mean SM %}, None = not measurable)
        is the only input a ``gpu.*`` rule targets by.
        """
        if action.kind == "throttle":
            return self._execute_throttle(action, candidate_pids, gpu_use)

        if action.kind != "kill":
            _log.info(
                "action %s for rule=%s signal=%s value=%g (no kill performed)",
                action.kind, action.rule_name, action.signal, action.latest_value,
            )
            return KillReport(
                action=action,
                offender_pid=None,
                kill_root=None,
                skipped_reason=f"action.kind == {action.kind!r}",
                finished_at_ns=int(time.monotonic_ns()),
            )

        protected = self._protected_pids()
        offender_pid, why = self._offender(action.signal, candidate_pids, protected, gpu_use)
        if offender_pid is None:
            _log.warning("kill-action for rule=%s skipped: %s", action.rule_name, why)
            return KillReport(
                action=action,
                offender_pid=None,
                kill_root=None,
                skipped_reason=why,
                finished_at_ns=int(time.monotonic_ns()),
            )

        root = find_kill_root(
            offender_pid,
            provider=self._provider,
            killable_names=frozenset(self._cfg.targeting.killable_names),
            launcher_names=frozenset(self._cfg.targeting.launcher_names),
            protected_pids=protected,
        )
        if root is None:
            return KillReport(
                action=action,
                offender_pid=offender_pid,
                kill_root=None,
                skipped_reason=(
                    f"PID {offender_pid} no longer exists, did not match keepers, "
                    "or is a protected client process"
                ),
                finished_at_ns=int(time.monotonic_ns()),
            )

        # Resolve the owner BEFORE killing anything. Once the child exits,
        # psutil can no longer report its parent (the fake provider models this
        # faithfully and caught it), so a post-kill lookup silently returns None
        # and escalation never fires.
        owner = self._provider.parent(root.pid)
        ancestors = self._ancestors(root.pid)
        root_owner = self._owner_of(root, ancestors)

        # Build the full target set: root + descendants, minus protected. A
        # protected client can also sit *below* the root (a supervisor started by
        # the process we're about to kill), so filter by PID as well as name.
        targets: list[ProcInfo] = [root, *self._provider.descendants(root.pid)]
        targets = [
            t for t in targets if self._is_killable(t) and t.pid not in protected
        ]

        if not targets:
            return KillReport(
                action=action,
                offender_pid=offender_pid,
                kill_root=root,
                skipped_reason="all candidates filtered by never_kill_names / self-protection",
                finished_at_ns=int(time.monotonic_ns()),
            )

        # Near the ceiling, grace is a luxury. Graceful termination is correct
        # at the threshold, where there is time for a process to close cleanly;
        # it is wrong at 97%, where the 5-second courtesy window is most of the
        # time remaining before the store manager fails. Measured: a kill action
        # fired at 06:17:13 and the machine bugchecked at 06:17:16.
        ceiling = self._cfg.kill.hard_ceiling_percent
        immediate = self._cfg.kill.mode == "aggressive" or (
            ceiling > 0
            and action.signal.endswith("_percent")
            and action.latest_value >= ceiling
        )
        if immediate:
            results = self._kill_immediate(targets)
        else:
            results = self._terminate_then_kill(targets)

        # Killing this tree may not have shed the work at all -- see _escalate.
        self._escalate(root, owner, protected, action)

        return KillReport(
            action=action,
            offender_pid=offender_pid,
            kill_root=root,
            killed=tuple(results),
            finished_at_ns=int(time.monotonic_ns()),
            ancestors=ancestors,
            owner=root_owner and replace(root_owner, pids=len(targets)),
        )

    # -- Owners (agent_inbox) ------------------------------------------------

    def _environ(self, pid: int) -> dict[str, str] | None:
        return getattr(self._provider, "environ", lambda _p: None)(pid)

    def _owner_of(self, root: ProcInfo, ancestors: tuple[tuple[int, str], ...]) -> Any:
        """The worker whose manifest pid is the root or above it, else the root's session.
        On the kill path, so it never raises (attribution must not cost a kill)."""
        try:
            for pid in (root.pid, *(p for p, _ in ancestors)):
                if pid in self._workers:
                    return agent_inbox.worker_owner(self._workers[pid])
            return agent_inbox.session_owner(agent_inbox.inbox_of(self._environ(root.pid)))
        except Exception:  # noqa: BLE001
            _log.exception("kill-root owner attribution failed")
            return None

    def owner_of_pid(self, pid: int) -> Any:
        """Who a notice about ``pid`` would address (the notify drill). None if gone."""
        self._protected_pids()                 # refresh the worker manifests
        info = self._provider.get(pid)
        return None if info is None else self._owner_of(info, self._ancestors(pid))

    def live_worker_jobs(self) -> set[str]:
        """Jobs carried by a live Kiroshi worker on this host (ends a job mute)."""
        manifests = live_manifests(self._cfg.general.state_dir, provider=self._provider)
        return {j for _p, d in manifests.values() if d.get("role") == "worker"
                for j in [agent_inbox.worker_owner(d).job] if j}

    def load_owners(self, signal: str, gpu_use: Mapping[int, float] | None = None) -> list[Any]:
        """Ranked owners of the killable load on ``signal``'s axis. Reads env of the
        eligible (killable-named) processes only; called only on a firing."""
        procs = self._provider.list_all()     # ~2 s elevated on 480 procs; walk it once
        eligible = self._eligible(procs, self._protected_pids(procs))
        axis = offender_axis(signal)
        if axis == "gpu":
            use, floor = dict(gpu_use or {}), GPU_USE_FLOOR         # mean SM %
        elif axis == "cpu":
            use, floor = self._sample_cpu(eligible), 100.0          # one core
        else:
            use = {i.pid: float(i.rss_bytes) for i in eligible}
            floor = 0.10 * _host_ram_bytes()                       # 10% of RAM
        return agent_inbox.owners(
            {i.pid: (use.get(i.pid, 0.0), self._environ(i.pid), i.name) for i in eligible},
            workers=self._workers, floor=floor,
            descendants_of=lambda p: [d.pid for d in self._provider.descendants(p)])

    # -- Throttle (suspend/resume) ----------------------------------------

    def _execute_throttle(
        self,
        action: Action,
        candidate_pids: Iterable[int] | None,
        gpu_use: Mapping[int, float] | None = None,
    ) -> KillReport:
        """Suspend the offending tree for ``cfg.kill.throttle_duration_seconds``,
        then resume it.

        Returns a :class:`KillReport` even though no kill happens -- the
        report shape is the universal "what did the actuator do?"
        envelope. Suspended PIDs land in :attr:`_active_throttles` so
        :meth:`shutdown` can resume them if the service exits early.
        """
        protected = self._protected_pids()
        offender_pid, why = self._offender(action.signal, candidate_pids, protected, gpu_use)
        if offender_pid is None:
            _log.info("throttle for rule=%s skipped: %s", action.rule_name, why)
            return KillReport(
                action=action,
                offender_pid=None,
                kill_root=None,
                skipped_reason=why,
                finished_at_ns=int(time.monotonic_ns()),
            )

        root = find_kill_root(
            offender_pid,
            provider=self._provider,
            killable_names=frozenset(self._cfg.targeting.killable_names),
            launcher_names=frozenset(self._cfg.targeting.launcher_names),
            protected_pids=protected,
        )
        if root is None:
            return KillReport(
                action=action,
                offender_pid=offender_pid,
                kill_root=None,
                skipped_reason=(
                    f"PID {offender_pid} no longer exists, did not match keepers, "
                    "or is a protected client process"
                ),
                finished_at_ns=int(time.monotonic_ns()),
            )

        targets: list[ProcInfo] = [root, *self._provider.descendants(root.pid)]
        targets = [
            t for t in targets if self._is_killable(t) and t.pid not in protected
        ]
        if not targets:
            return KillReport(
                action=action,
                offender_pid=offender_pid,
                kill_root=root,
                skipped_reason="all candidates filtered by never_kill_names",
                finished_at_ns=int(time.monotonic_ns()),
            )

        # Suspend the tree. We re-use :class:`KilledProcess` to record
        # which procs we touched, with method="suspend" instead of
        # "terminate"/"kill". `survived=True` here means "still
        # alive" (which is exactly what we want for a throttle).
        suspended: list[KilledProcess] = []
        for t in targets:
            ok = self._provider.suspend(t.pid)
            suspended.append(KilledProcess(
                info=t,
                method="suspend" if ok else "skipped",
                survived=True,  # throttle deliberately keeps procs alive
            ))

        # Schedule resume. If the service stops cleanly before this
        # fires, shutdown() will resume them itself.
        duration = float(self._cfg.kill.throttle_duration_seconds)
        succeeded_pids = [k.info.pid for k in suspended if k.method == "suspend"]
        if succeeded_pids:
            timer = threading.Timer(
                duration,
                self._resume_pids,
                args=(succeeded_pids,),
            )
            timer.daemon = True
            with self._throttle_lock:
                # If the same PID is already throttled (a second rule
                # firing on the same offender), cancel the prior resume
                # so the longest stay wins.
                for pid in succeeded_pids:
                    prior = self._active_throttles.pop(pid, None)
                    if prior is not None:
                        prior.cancel()
                    self._active_throttles[pid] = timer
            timer.start()
            _log.warning(
                "THROTTLE: rule=%s signal=%s root=%s pid=%d "
                "(suspended %d procs for %.0fs)",
                action.rule_name, action.signal, root.name, root.pid,
                len(succeeded_pids), duration,
            )
        return KillReport(
            action=action,
            offender_pid=offender_pid,
            kill_root=root,
            killed=tuple(suspended),
            finished_at_ns=int(time.monotonic_ns()),
        )

    def _resume_pids(self, pids: list[int]) -> None:
        with self._throttle_lock:
            for pid in pids:
                # Only forget the pid if its tracking timer is the one
                # that fired (or is missing). A racing second throttle
                # may have overwritten the entry.
                self._active_throttles.pop(pid, None)
        for pid in pids:
            self._provider.resume(pid)
        _log.info("throttle resume: %d pid(s)", len(pids))

    def shutdown(self) -> None:
        """Cancel all pending throttle resumes and resume any still-suspended
        procs. The service should call this in its `finally:` block so we
        don't leave training jobs paused indefinitely on crashes."""
        with self._throttle_lock:
            timers = list(self._active_throttles.values())
            pids = list(self._active_throttles.keys())
            self._active_throttles.clear()
        for t in timers:
            t.cancel()
        for pid in pids:
            self._provider.resume(pid)
        if pids:
            _log.info("actuator shutdown: resumed %d throttled pid(s)", len(pids))

    # -- Helpers -----------------------------------------------------------

    def _protected_pids(self, procs: list[ProcInfo] | None = None) -> frozenset[int]:
        """PIDs no kill may target, and that the kill-root walk-up may not pass.

        Two sources, and both have to reach ``find_kill_root``: the client
        manifests on disk, and ``never_kill_cmdline_patterns`` for the
        supervisors that register no manifest. Honouring the patterns only when
        filtering the final target list spares the supervisor process itself but
        still lets the walk-up return it as the kill root, which takes down
        every one of its children instead of just the offending job's subtree.

        Re-read per action rather than cached: kills are rare, and a stale cache
        here means either killing a supervisor that registered moments ago or
        sparing a job that has long since exited.
        """
        pids: set[int] = set()
        try:
            manifests = live_manifests(self._cfg.general.state_dir, provider=self._provider)
            self._workers = {pid: d for pid, (_p, d) in manifests.items()
                             if d.get("role") == "worker"}
            found = discover_protected(
                self._cfg.general.state_dir,
                provider=self._provider,
                protected_roles=self._protected_roles,
                manifests=manifests,
            )
        except Exception:
            # Never let manifest trouble block a response to real thermal
            # pressure -- fall back to unprotected rather than not acting.
            _log.exception("failed to read protected client manifests")
        else:
            self._clients_by_pid = dict(found)
            for entry in found.values():
                _log.debug(
                    "protecting pid %d (%s role=%s): %s",
                    entry.pid, entry.client, entry.role, entry.reason,
                )
            pids.update(found)

        if self._never_cmdline:
            try:
                pids.update(
                    info.pid
                    for info in (self._provider.list_all() if procs is None else procs)
                    if self._matches_never_cmdline(info)
                )
            except Exception:
                _log.exception("failed to match never_kill_cmdline_patterns")

        return frozenset(pids)

    def _matches_never_cmdline(self, info: ProcInfo) -> bool:
        if not self._never_cmdline:
            return False
        joined = " ".join(info.cmdline)
        return any(fnmatch.fnmatch(joined, pat) for pat in self._never_cmdline)

    def _is_killable(self, info: ProcInfo) -> bool:
        if info.pid == self._own_pid:
            return False
        if info.name.lower() in self._never:
            return False
        return not self._matches_never_cmdline(info)

    def _offender(self, signal: str, candidate_pids: Iterable[int] | None,
                  protected: frozenset[int], gpu_use: Mapping[int, float] | None
                  ) -> tuple[int | None, str]:
        """(pid, "") or (None, why). A gpu.* rule: the top MEASURED GPU user if it is at
        or over GPU_USE_FLOOR and killable -- the loader or nobody."""
        axis = offender_axis(signal)
        if axis != "gpu":
            pid = self._pick_offender(candidate_pids, axis=axis, protected=protected)
            return pid, ("" if pid is not None else _NO_ELIGIBLE)
        if gpu_use is None:
            return None, ("GPU use not measurable (NVML unavailable, or per-process "
                          "utilization unsupported here)")
        top = max(((p, v) for p, v in gpu_use.items() if self._provider.get(p)),  # exited: skip
                  key=lambda kv: kv[1], default=None)
        info = self._provider.get(top[0]) if top else None
        who = f"pid {top[0]} {info.name if info else '?'} {top[1]:.0f}%" if top else "none"
        if top is None or top[1] < GPU_USE_FLOOR:
            return None, (f"no process with measured GPU use >= {GPU_USE_FLOOR:.0f}% (source "
                          f"not identified; {len(gpu_use)} measured, top: {who})")
        if info is None or not self._eligible([info], protected):
            return None, f"top GPU user {who} is not in killable_names (or is protected)"
        return top[0], ""

    def _pick_offender(
        self,
        candidate_pids: Iterable[int] | None,
        *,
        axis: str = "memory",
        protected: frozenset[int] = frozenset(),
    ) -> int | None:
        """Pick the PID we treat as the trigger.

        Candidates are the processes in ``killable_names`` -- either the caller's
        ``candidate_pids`` (the per-GPU process map for a GPU rule) or a full
        scan. They are then ranked on ``axis``:

        * ``"cpu"`` -- measured busy-ness. Required for CPU/thermal rules: a
          compute loop can hold almost no memory, so RSS ranking overlooks it
          entirely and picks the fattest idle process instead.
        * ``"memory"`` -- RSS summed over each KILL TREE (the candidates that
          ``find_kill_root`` maps to the same root), and the fattest tree wins.
          The kill takes a whole tree, so the ranking must too: 2026-10-25 a
          pagefile rule at 99.7 % commit killed a 3 GB viewer while 45 GB sat in
          ONE job slot spread over 30 children of 1.5 GB each, invisible to a
          per-process ranking.

        Returns None if nothing is eligible, and ``execute()`` logs a miss.
        """
        candidates: list[ProcInfo] = []
        if candidate_pids is not None:
            for pid in candidate_pids:
                info = self._provider.get(pid)
                if info is not None:
                    candidates.append(info)
        else:
            candidates = self._provider.list_all()

        eligible = self._eligible(candidates, protected)
        if not eligible:
            return None

        if axis == "cpu":
            usage = self._sample_cpu(eligible)
            if usage:
                return max(usage, key=lambda pid: usage[pid])
            # Measurement unavailable (provider can't sample, or every
            # candidate exited); fall through to RSS rather than doing nothing.
            _log.warning("cpu-axis offender ranking unavailable, falling back to RSS")

        table = _TableProvider(candidates) if candidate_pids is None else self._provider
        trees: dict[int, list[ProcInfo]] = {}
        for info in eligible:
            root = find_kill_root(
                info.pid,
                provider=table,
                killable_names=self._killable,
                launcher_names=self._launchers,
                protected_pids=protected,
            )
            trees.setdefault(root.pid if root is not None else info.pid, []).append(info)
        # Name the tree's ROOT, not its fattest member: execute() re-walks from this pid with the live provider,
        # and a short-lived child can exit during the seconds the scan takes -- the whole kill was then skipped
        # while the tree still held the memory. The root's own walk returns itself.
        return max(trees, key=lambda root: sum(i.rss_bytes for i in trees[root]))

    def _eligible(self, candidates: Iterable[ProcInfo], protected: frozenset[int]) -> list[ProcInfo]:
        return [
            info
            for info in candidates
            if info.name.lower() in self._killable
            and info.pid != self._own_pid
            and info.pid not in protected
            and not self._matches_never_cmdline(info)
        ]

    def _sample_cpu(self, candidates: list[ProcInfo]) -> dict[int, float]:
        sampler = getattr(self._provider, "sample_cpu_percent", None)
        if sampler is None:
            return {}
        try:
            return sampler([c.pid for c in candidates], _CPU_SAMPLE_INTERVAL_S)
        except Exception:
            _log.exception("cpu sampling failed during offender selection")
            return {}

    def _terminate_then_kill(self, targets: list[ProcInfo]) -> list[KilledProcess]:
        for t in targets:
            self._provider.terminate(t.pid)
        self._sleep(self._cfg.kill.grace_seconds)
        results: list[KilledProcess] = []
        for t in targets:
            if self._provider.is_alive(t.pid):
                self._provider.kill(t.pid)
                # Brief drain after force-kill to update is_alive.
                self._sleep(0.05)
                results.append(KilledProcess(info=t, method="kill", survived=self._provider.is_alive(t.pid)))
            else:
                results.append(KilledProcess(info=t, method="terminate", survived=False))
        return results

    # How long to WATCH for a killed process to disappear before calling it a
    # survivor. Not a grace period -- the kill has already been sent; this is
    # only about observing the result honestly.
    _REAP_TIMEOUT_S = 2.0
    _REAP_POLL_S = 0.05

    def _kill_immediate(self, targets: list[ProcInfo]) -> list[KilledProcess]:
        """Kill now, then wait long enough to report the outcome truthfully.

        The old single 0.05 s check was too eager. TerminateProcess is
        asynchronous, and tearing down a large working set is not instant, so a
        process that was dying still read as alive. Observed live: the RSS cap
        killed a 34 GB probe, logged ``survived=True``, and the process was
        gone moments later.

        That matters most exactly here. The cap kills processes selected FOR
        being huge, which are the slowest to disappear, so the report would
        have been wrong on essentially every cap kill -- and a kill log that
        cries "survived" when it did not is how operators learn to disbelieve
        the audit trail.

        Polls instead of sleeping a flat interval so the common case (small
        process, gone in milliseconds) stays fast.
        """
        for t in targets:
            self._provider.kill(t.pid)
        pending = {t.pid: t for t in targets}
        deadline = self._REAP_TIMEOUT_S
        waited = 0.0
        while pending and waited < deadline:
            self._sleep(self._REAP_POLL_S)
            waited += self._REAP_POLL_S
            for pid in [p for p in pending if not self._provider.is_alive(p)]:
                del pending[pid]
        return [
            KilledProcess(info=t, method="kill", survived=t.pid in pending)
            for t in targets
        ]
