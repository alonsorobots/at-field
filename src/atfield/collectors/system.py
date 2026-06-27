"""System-level collector: RAM %, swap %, true commit charge %.

This is the Tier-1 always-available collector. psutil is a hard dependency
of the package, and ``GlobalMemoryStatusEx`` is in every Windows version
back to XP. If this collector ever fails to probe, the watchdog has bigger
problems than missing data.

Design notes
------------
* **True commit charge on Windows.** ``psutil.swap_memory()`` reports
  pagefile-only usage, which is *not* the same thing as Windows' commit
  charge (which is committed virtual memory across RAM + pagefile). The
  commit charge is what actually triggers OOM-class failures on Windows,
  so we read it via ``GlobalMemoryStatusEx`` (Win32) when available.
  On non-Windows, we fall back to ``swap_used_percent`` for the same signal
  name -- which keeps tests cross-platform but means the
  ``system.commit_percent`` rule is a soft approximation off-Windows.
* **Cheap probe.** psutil import is the entire probe. No subprocess, no
  HTTP, no file I/O. Always ``HEALTHY`` after a successful probe.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Final

import psutil

from atfield.collectors import HealthState, ProbeResult
from atfield.signals import Sample, monotonic_ns

__all__ = ["SystemCollector"]


_NAME: Final = "system"

# Signal names this collector provides. Kept as a module-level tuple so the
# probe and sample paths can't drift.
_SIGNALS: Final = (
    "system.ram_used_percent",
    "system.swap_used_percent",
    "system.commit_percent",
    "system.cpu_used_percent",
)


# ---------------------------------------------------------------------------
# Win32 commit-charge reader
# ---------------------------------------------------------------------------


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _read_commit_percent_windows() -> float:
    """Return Windows commit-charge usage as a percentage.

    Commit charge = total committed virtual memory (RAM + pagefile-backed).
    The fields here are ``ullTotalPageFile`` and ``ullAvailPageFile``, which
    -- contrary to their names -- report the system commit limit and
    available commit, *not* the pagefile alone. (See MSDN
    ``MEMORYSTATUSEX``.) This is the canonical way to read commit charge
    without spawning ``perfmon``.
    """
    mem = _MEMORYSTATUSEX()
    mem.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
        raise OSError("GlobalMemoryStatusEx returned 0")
    total = mem.ullTotalPageFile
    avail = mem.ullAvailPageFile
    if total == 0:
        return 0.0
    return ((total - avail) / total) * 100.0


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


class SystemCollector:
    """Collector for system memory pressure signals.

    Implements :class:`atfield.collectors.Collector` structurally.
    """

    name: Final = _NAME

    def __init__(self) -> None:
        self._health = HealthState.UNPROBED
        self._on_windows = sys.platform == "win32"
        self._consecutive_failures = 0
        self._max_consecutive = 3  # 3 strikes -> DEGRADED

    def probe(self) -> ProbeResult:
        try:
            psutil.virtual_memory()
            psutil.swap_memory()
            # Prime psutil's CPU-percent ticker. The first non-blocking call
            # always returns 0.0 because it has no prior reading to diff
            # against; calling it here means the FIRST real sample() already
            # returns a meaningful percent (avoiding an initial "0% CPU" tile
            # that's just an artifact of the probe ordering).
            psutil.cpu_percent(interval=None)
            if self._on_windows:
                _read_commit_percent_windows()
                reason = "psutil + Win32 GlobalMemoryStatusEx OK"
                meta = {"commit_charge_source": "GlobalMemoryStatusEx"}
            else:
                reason = "psutil OK; commit_percent approximated by swap_used_percent off-Windows"
                meta = {"commit_charge_source": "swap_memory (fallback)"}
            self._health = HealthState.HEALTHY
            return ProbeResult(
                available=True,
                reason=reason,
                signals=_SIGNALS,
                metadata=meta,
            )
        except Exception as exc:
            self._health = HealthState.FAILED
            return ProbeResult(
                available=False,
                reason=f"system collector probe failed: {exc!r}",
                signals=(),
            )

    def sample(self) -> dict[str, Sample]:
        if self._health is HealthState.FAILED:
            return {}
        try:
            now = monotonic_ns()
            vm = psutil.virtual_memory()
            sw = psutil.swap_memory()
            commit = _read_commit_percent_windows() if self._on_windows else float(sw.percent)
            # Non-blocking: returns % busy averaged over interval since the
            # last call (or since probe() primed the ticker). Cheap -- no
            # interval sleep, no subprocess. System-wide aggregate; per-core
            # breakdown is available via psutil.cpu_percent(percpu=True) but
            # the dashboard only needs the headline number.
            cpu = float(psutil.cpu_percent(interval=None))

            self._consecutive_failures = 0
            self._health = HealthState.HEALTHY

            return {
                "system.ram_used_percent": Sample(
                    value=float(vm.percent),
                    taken_at_ns=now,
                    source_id=_NAME,
                    unit="percent",
                ),
                "system.swap_used_percent": Sample(
                    value=float(sw.percent),
                    taken_at_ns=now,
                    source_id=_NAME,
                    unit="percent",
                ),
                "system.commit_percent": Sample(
                    value=commit,
                    taken_at_ns=now,
                    source_id=_NAME,
                    unit="percent",
                ),
                "system.cpu_used_percent": Sample(
                    value=cpu,
                    taken_at_ns=now,
                    source_id=_NAME,
                    unit="percent",
                ),
            }
        except Exception:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._max_consecutive:
                self._health = HealthState.DEGRADED
            return {}

    def health(self) -> HealthState:
        return self._health

    def shutdown(self) -> None:
        # No resources to release; psutil maintains its own caches.
        return None
