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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from atfield.client_registry import discover_protected
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

    def get(self, pid: int) -> ProcInfo | None:
        """Return ProcInfo for ``pid``, or None if it no longer exists."""
        ...

    def parent(self, pid: int) -> ProcInfo | None:
        """Return ProcInfo of pid's parent, or None if absent / orphan."""
        ...

    def descendants(self, pid: int) -> list[ProcInfo]:
        """Return ProcInfo for every transitive descendant of ``pid``."""
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


def offender_axis(signal: str) -> str:
    """Which per-process metric identifies the offender for ``signal``.

    Returns ``"cpu"`` or ``"memory"``. This exists because ranking candidates by
    RSS for a *CPU* rule picks the wrong victim: a busy process can be tiny (a
    compute loop holding almost no memory) while an idle supervisor is fat, so
    RSS ranking silently kills the largest process rather than the one causing
    the heat.

    GPU rules don't appear here -- the caller already narrows those to the
    per-GPU process map, so the candidate set itself carries the attribution.
    """
    if "cpu" in signal:
        return "cpu"
    return "memory"


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

        # Throttle action support: tracks PIDs we've suspended so we can
        # resume them on shutdown (a service crash mid-throttle would
        # otherwise leave the workload paused indefinitely). Keyed by
        # PID; value is the threading.Timer that will resume it.
        self._throttle_lock = threading.Lock()
        self._active_throttles: dict[int, threading.Timer] = {}

    # -- Public entry point ------------------------------------------------

    def execute(
        self,
        action: Action,
        *,
        candidate_pids: Iterable[int] | None = None,
    ) -> KillReport:
        """Execute one policy action.

        ``log`` and ``throttle`` actions don't kill anything -- they just
        produce a report with no killed processes (the audit layer still
        writes the record). ``kill`` does the full walk-up + tree-kill.

        ``candidate_pids`` is the set of PIDs the caller (service) believes
        are candidates -- typically the per-GPU process map for GPU rules,
        or top-RSS python procs for RAM rules. If ``None``, the actuator
        scans all processes.
        """
        if action.kind == "throttle":
            return self._execute_throttle(action, candidate_pids)

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
        axis = offender_axis(action.signal)
        offender_pid = self._pick_offender(
            candidate_pids, axis=axis, protected=protected
        )
        if offender_pid is None:
            _log.warning(
                "kill-action for rule=%s but no eligible offender PID found",
                action.rule_name,
            )
            return KillReport(
                action=action,
                offender_pid=None,
                kill_root=None,
                skipped_reason="no eligible offender PID matched killable_names",
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

        if self._cfg.kill.mode == "aggressive":
            results = self._kill_immediate(targets)
        else:
            results = self._terminate_then_kill(targets)

        return KillReport(
            action=action,
            offender_pid=offender_pid,
            kill_root=root,
            killed=tuple(results),
            finished_at_ns=int(time.monotonic_ns()),
        )

    # -- Throttle (suspend/resume) ----------------------------------------

    def _execute_throttle(
        self,
        action: Action,
        candidate_pids: Iterable[int] | None,
    ) -> KillReport:
        """Suspend the offending tree for ``cfg.kill.throttle_duration_seconds``,
        then resume it.

        Returns a :class:`KillReport` even though no kill happens -- the
        report shape is the universal "what did the actuator do?"
        envelope. Suspended PIDs land in :attr:`_active_throttles` so
        :meth:`shutdown` can resume them if the service exits early.
        """
        protected = self._protected_pids()
        offender_pid = self._pick_offender(
            candidate_pids,
            axis=offender_axis(action.signal),
            protected=protected,
        )
        if offender_pid is None:
            _log.info(
                "throttle for rule=%s but no eligible offender PID found",
                action.rule_name,
            )
            return KillReport(
                action=action,
                offender_pid=None,
                kill_root=None,
                skipped_reason="no eligible offender PID matched killable_names",
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

    def _protected_pids(self) -> frozenset[int]:
        """PIDs no kill may target, from the client manifests on disk.

        Re-read per action rather than cached: kills are rare, and a stale cache
        here means either killing a supervisor that registered moments ago or
        sparing a job that has long since exited.
        """
        try:
            found = discover_protected(
                self._cfg.general.state_dir,
                provider=self._provider,
                protected_roles=self._protected_roles,
            )
        except Exception:
            # Never let manifest trouble block a response to real thermal
            # pressure -- fall back to unprotected rather than not acting.
            _log.exception("failed to read protected client manifests")
            return frozenset()
        for entry in found.values():
            _log.debug(
                "protecting pid %d (%s role=%s): %s",
                entry.pid, entry.client, entry.role, entry.reason,
            )
        return frozenset(found)

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
        * ``"memory"`` -- RSS, correct for RAM/commit/pagefile rules and a
          reasonable proxy within an already GPU-attributed candidate set.

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

        eligible = [
            info
            for info in candidates
            if info.name.lower() in self._killable
            and info.pid != self._own_pid
            and info.pid not in protected
            and not self._matches_never_cmdline(info)
        ]
        if not eligible:
            return None

        if axis == "cpu":
            usage = self._sample_cpu(eligible)
            if usage:
                return max(usage, key=lambda pid: usage[pid])
            # Measurement unavailable (provider can't sample, or every
            # candidate exited); fall through to RSS rather than doing nothing.
            _log.warning("cpu-axis offender ranking unavailable, falling back to RSS")

        return max(eligible, key=lambda info: info.rss_bytes).pid

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

    def _kill_immediate(self, targets: list[ProcInfo]) -> list[KilledProcess]:
        for t in targets:
            self._provider.kill(t.pid)
        self._sleep(0.05)
        return [
            KilledProcess(info=t, method="kill", survived=self._provider.is_alive(t.pid))
            for t in targets
        ]
