"""System-level collector: RAM %, swap %, true commit charge %, hard-fault rate.

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
* **Hard-fault rate (Windows only), via PDH.** ``commit_percent`` measures how
  much virtual memory has been PROMISED, not whether the machine is actually
  paging -- a box can sit at 85% commit perfectly healthy or thrash at 70%.
  ``\\Memory\\Pages Input/sec`` (read via the PDH API, since psutil doesn't
  expose Windows perf counters) is the canonical "is it actually thrashing"
  signal. Best-effort: if the perf counter category is unavailable, this
  signal is simply omitted, never a collector failure.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Final, Optional

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
    "system.input_idle_s",
    "system.hard_fault_rate",
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


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("dwTime", wintypes.DWORD),
    ]


def _read_input_idle_seconds_windows() -> float:
    """Seconds since the last keyboard/mouse input, system-wide.

    ``GetLastInputInfo`` reports the tick count of the last input event
    (keyboard OR mouse -- Windows doesn't distinguish the two at this API),
    which is exactly the "is anyone physically at this machine" signal:
    session-lock and idle time both flow through it. ``GetTickCount64`` is
    used (not ``GetTickCount``) to avoid the ~49.7-day 32-bit wraparound.
    """
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        raise OSError("GetLastInputInfo returned 0")
    now_ms = ctypes.windll.kernel32.GetTickCount64()
    # dwTime is a 32-bit tick count (GetTickCount-scale); compare against the
    # low 32 bits of the 64-bit now to stay correct across the wrap.
    elapsed_ms = (now_ms & 0xFFFFFFFF) - info.dwTime
    if elapsed_ms < 0:
        elapsed_ms += 1 << 32
    return elapsed_ms / 1000.0


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
# Win32 hard-fault-rate reader (PDH)
# ---------------------------------------------------------------------------

_PDH_FMT_DOUBLE: Final = 0x00000200

def _load_pdh():
    """Load pdh.dll, or return None if unavailable.

    MUST NOT raise: this runs at import time, and ``service.py`` imports
    ``SystemCollector`` at module scope. An unguarded ``WinDLL`` failure here
    would fail the import of the Tier-1 always-available collector, which in
    turn fails the import of the whole service -- i.e. one optional bonus
    signal could take down the entire watchdog, leaving every machine with no
    thermal/memory protection at all. hard_fault_rate is strictly best-effort;
    losing it must cost exactly that signal and nothing more.
    """
    if sys.platform != "win32":
        return None
    try:
        return ctypes.WinDLL("pdh.dll")
    except Exception:  # noqa: BLE001 - any load failure => signal unavailable
        return None


class _PdhFmtCounterValue(ctypes.Structure):
    # Real PDH_FMT_COUNTERVALUE is {DWORD CStatus; union{...double...}}. We
    # only ever read doubleValue; ctypes pads CStatus to 8-byte alignment for
    # the following c_double exactly like the real union does, so the memory
    # layout PdhGetFormattedCounterValue writes into matches. (Verified:
    # sizeof == 16 and doubleValue.offset == 8, matching the Win32 x64 ABI.)
    _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]


def _bind_pdh_signatures(pdh) -> bool:
    """Declare argtypes/restypes. Returns False (rather than raising) if any
    symbol is missing -- same import-time-safety rule as :func:`_load_pdh`."""
    try:
        pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p,
                                      ctypes.POINTER(wintypes.HANDLE)]
        pdh.PdhOpenQueryW.restype = wintypes.LONG
        pdh.PdhAddEnglishCounterW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, ctypes.c_void_p,
                                              ctypes.POINTER(wintypes.HANDLE)]
        pdh.PdhAddEnglishCounterW.restype = wintypes.LONG
        pdh.PdhCollectQueryData.argtypes = [wintypes.HANDLE]
        pdh.PdhCollectQueryData.restype = wintypes.LONG
        pdh.PdhCloseQuery.argtypes = [wintypes.HANDLE]
        pdh.PdhCloseQuery.restype = wintypes.LONG
        pdh.PdhGetFormattedCounterValue.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(_PdhFmtCounterValue),
        ]
        pdh.PdhGetFormattedCounterValue.restype = wintypes.LONG
        return True
    except Exception:  # noqa: BLE001 - missing symbol => signal unavailable
        return False


_pdh = _load_pdh()
if _pdh is not None and not _bind_pdh_signatures(_pdh):
    _pdh = None  # symbols missing -> treat exactly like "PDH unavailable"


class _HardFaultRateReader:
    """Reads Windows' ``\\Memory\\Pages Input/sec`` perf counter via PDH.

    This is the canonical Windows "is the machine actually thrashing" signal:
    pages read back from disk because they weren't resident -- real paging
    pain, not just how much virtual memory has been PROMISED (which
    ``commit_percent`` already covers, and which can sit at 85% perfectly
    healthy or 70% and thrashing -- commit measures reservation, not pain).
    psutil does not expose this on Windows; PDH is the standard way to read
    a perf counter without spawning ``perfmon``/``typeperf``.

    ``PdhAddEnglishCounterW`` (not the localized ``PdhAddCounterW``) so the
    counter path is not locale-dependent on non-English Windows installs.

    A PDH *rate* counter needs two ``PdhCollectQueryData`` calls spaced apart
    in time to compute a rate; the very first read after opening has no prior
    sample to diff against and returns "no data yet". Best-effort: that's
    ``None``, not an error -- exactly the same prime-on-first-call pattern
    ``psutil.cpu_percent`` already uses above.
    """

    _COUNTER_PATH: Final = r"\Memory\Pages Input/sec"

    def __init__(self) -> None:
        self._hquery = wintypes.HANDLE()
        self._hcounter = wintypes.HANDLE()
        self._opened = False

    def open(self) -> None:
        if _pdh is None:
            raise OSError(
                "PDH unavailable: not on Windows, pdh.dll failed to load, or a "
                "required Pdh* symbol is missing (see _load_pdh/_bind_pdh_signatures)")
        rc = _pdh.PdhOpenQueryW(None, None, ctypes.byref(self._hquery))
        if rc != 0:
            raise OSError(f"PdhOpenQueryW failed: 0x{rc & 0xFFFFFFFF:08X}")
        rc = _pdh.PdhAddEnglishCounterW(
            self._hquery, self._COUNTER_PATH, None, ctypes.byref(self._hcounter))
        if rc != 0:
            _pdh.PdhCloseQuery(self._hquery)
            raise OSError(f"PdhAddEnglishCounterW({self._COUNTER_PATH!r}) failed: "
                          f"0x{rc & 0xFFFFFFFF:08X}")
        self._opened = True

    def read(self) -> Optional[float]:
        """Pages-input/sec since the previous call, or ``None`` if not yet
        available (not opened, or this is the priming call)."""
        if not self._opened:
            return None
        if _pdh.PdhCollectQueryData(self._hquery) != 0:
            return None
        value = _PdhFmtCounterValue()
        rc = _pdh.PdhGetFormattedCounterValue(
            self._hcounter, _PDH_FMT_DOUBLE, None, ctypes.byref(value))
        if rc != 0 or value.CStatus != 0:
            return None  # e.g. PDH_CSTATUS_INVALID_DATA on the priming call
        return float(value.doubleValue)

    def close(self) -> None:
        if self._opened and _pdh is not None:
            try:
                _pdh.PdhCloseQuery(self._hquery)
            except Exception:  # noqa: BLE001
                pass
            self._opened = False


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
        # Best-effort: if PDH is unavailable (odd Windows install, permissions,
        # the counter category not registered), hard_fault_rate is simply
        # omitted -- never fails the whole collector. See probe().
        self._hard_fault_reader: Optional[_HardFaultRateReader] = None

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
                # Best-effort: hard_fault_rate is a bonus signal, never a
                # reason to fail this collector. A box with the Memory perf
                # counter category unregistered (rare, e.g. `lodctr /R`
                # never run) just doesn't get the signal.
                try:
                    reader = _HardFaultRateReader()
                    reader.open()
                    self._hard_fault_reader = reader
                    meta["hard_fault_rate_source"] = "PDH \\Memory\\Pages Input/sec"
                except Exception as exc:  # noqa: BLE001
                    self._hard_fault_reader = None
                    meta["hard_fault_rate_source"] = f"unavailable ({exc!r})"
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

            out = {
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
            # Presence signal: seconds since the last keyboard/mouse input,
            # system-wide. Windows-only (no cross-platform equivalent here);
            # omitted off-Windows rather than faked, same policy as the
            # commit-charge fallback being an explicit approximation rather
            # than a silent one.
            if self._on_windows:
                try:
                    out["system.input_idle_s"] = Sample(
                        value=_read_input_idle_seconds_windows(),
                        taken_at_ns=now,
                        source_id=_NAME,
                        unit="count",
                    )
                except Exception:
                    pass
                if self._hard_fault_reader is not None:
                    # None on the priming call (first tick after probe()) or
                    # any transient PDH hiccup -- omit the sample rather than
                    # emit a fabricated 0, which would look like "no thrash"
                    # instead of "no reading".
                    rate = self._hard_fault_reader.read()
                    if rate is not None:
                        out["system.hard_fault_rate"] = Sample(
                            value=rate, taken_at_ns=now, source_id=_NAME, unit="count",
                        )
            return out
        except Exception:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._max_consecutive:
                self._health = HealthState.DEGRADED
            return {}

    def health(self) -> HealthState:
        return self._health

    def shutdown(self) -> None:
        if self._hard_fault_reader is not None:
            self._hard_fault_reader.close()
            self._hard_fault_reader = None
