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
* **Input idle time must come from the interactive session, not ours.**
  ``GetLastInputInfo`` is scoped to the *calling process's* session. AT-Field
  normally runs as a service in session 0, which receives no keyboard or
  mouse input ever -- so the call succeeds and returns a number that just
  counts up from service start, forever. That is the worst possible failure
  shape for a presence signal: it is not missing, it is confidently wrong,
  and it always says "nobody is here". We therefore query the *active console
  session* via ``WTSQuerySessionInformationW(WTSSessionInfoEx)`` and fall back
  to ``GetLastInputInfo`` only when we are ourselves running interactively.
  If neither source applies, the signal is omitted -- an absent signal makes
  presence-gated rules abstain, a fabricated one makes them act on a lie.
"""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Callable
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
    """Seconds since the last keyboard/mouse input *in our own session*.

    ``GetLastInputInfo`` reports the tick count of the last input event
    (keyboard OR mouse -- Windows doesn't distinguish the two at this API).
    ``GetTickCount64`` is used (not ``GetTickCount``) to avoid the ~49.7-day
    32-bit wraparound.

    Only meaningful when this process runs *inside* the interactive session.
    In a session-0 service it returns "time since the service started",
    monotonically, forever -- see :func:`_resolve_input_idle_reader`.
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


# ---------------------------------------------------------------------------
# Win32 WTS reader: idle time of the *interactive* session, from a service
# ---------------------------------------------------------------------------

# WTSQuerySessionInformationW info class. ``WTSSessionInfoEx`` (25) yields a
# WTSINFOEXW whose Level-1 payload carries both LastInputTime and CurrentTime,
# so idle time needs no clock of our own -- the terminal-services subsystem
# hands us both endpoints from the same clock, immune to skew.
_WTS_SESSION_INFO_EX: Final = 25
_WTS_CURRENT_SERVER_HANDLE: Final = 0  # NULL == this server
_WTS_INFO_EX_LEVEL1: Final = 1
_WTS_NO_ACTIVE_SESSION: Final = 0xFFFFFFFF  # WTSGetActiveConsoleSessionId sentinel

# String field sizes straight out of wtsapi32.h. They are load-bearing: the
# LARGE_INTEGERs we actually want sit *after* these arrays, so an off-by-one
# here silently shifts LastInputTime and yields garbage idle values.
_WINSTATIONNAME_LENGTH: Final = 32
_USERNAME_LENGTH: Final = 20
_DOMAIN_LENGTH: Final = 17

# 100-nanosecond FILETIME ticks per second.
_FILETIME_TICKS_PER_S: Final = 10_000_000


class _WTSINFOEX_LEVEL1_W(ctypes.Structure):
    _fields_ = [
        ("SessionId", wintypes.ULONG),
        ("SessionState", ctypes.c_int),  # WTS_CONNECTSTATE_CLASS
        ("SessionFlags", wintypes.LONG),
        ("WinStationName", wintypes.WCHAR * (_WINSTATIONNAME_LENGTH + 1)),
        ("UserName", wintypes.WCHAR * (_USERNAME_LENGTH + 1)),
        ("DomainName", wintypes.WCHAR * (_DOMAIN_LENGTH + 1)),
        ("LogonTime", ctypes.c_longlong),
        ("ConnectTime", ctypes.c_longlong),
        ("DisconnectTime", ctypes.c_longlong),
        ("LastInputTime", ctypes.c_longlong),
        ("CurrentTime", ctypes.c_longlong),
        ("IncomingBytes", wintypes.DWORD),
        ("OutgoingBytes", wintypes.DWORD),
        ("IncomingFrames", wintypes.DWORD),
        ("OutgoingFrames", wintypes.DWORD),
        ("IncomingCompressedBytes", wintypes.DWORD),
        ("OutgoingCompressedBytes", wintypes.DWORD),
    ]


class _WTSINFOEX_W(ctypes.Structure):
    # The real type has a union for Data; only Level 1 is ever defined, so a
    # plain struct member is equivalent. ctypes places Data at offset 8 (not 4)
    # because the c_longlong members give the payload 8-byte alignment --
    # matching the C layout, sizeof == 232.
    _fields_ = [
        ("Level", wintypes.DWORD),
        ("Data", _WTSINFOEX_LEVEL1_W),
    ]


def _process_session_id() -> int | None:
    """Session ID of the current process, or None if it can't be determined."""
    sid = wintypes.DWORD()
    ok = ctypes.windll.kernel32.ProcessIdToSessionId(
        ctypes.windll.kernel32.GetCurrentProcessId(), ctypes.byref(sid)
    )
    return int(sid.value) if ok else None


def _read_active_session_idle_seconds() -> float:
    """Seconds since last input in the *active console session*.

    Works from session 0, which is the whole point. Raises on any condition
    that would otherwise force us to invent a number:

    * no session currently attached to the console (nobody logged on),
    * ``LastInputTime == 0``, which WTS reports for sessions that have not
      registered input yet -- indistinguishable from "1601-01-01", so
      treating it as a real timestamp would report ~13 million hours idle.

    The active session is re-read on every call rather than cached: fast user
    switching and RDP reconnects both move it, and at 1 Hz the syscall cost is
    irrelevant next to being wrong about who is at the machine.
    """
    wtsapi = ctypes.windll.wtsapi32
    session_id = ctypes.windll.kernel32.WTSGetActiveConsoleSessionId()
    if session_id == _WTS_NO_ACTIVE_SESSION:
        raise OSError("no active console session")

    buf = ctypes.c_void_p()
    nbytes = wintypes.DWORD()
    ok = wtsapi.WTSQuerySessionInformationW(
        ctypes.c_void_p(_WTS_CURRENT_SERVER_HANDLE),
        wintypes.DWORD(session_id),
        ctypes.c_int(_WTS_SESSION_INFO_EX),
        ctypes.byref(buf),
        ctypes.byref(nbytes),
    )
    if not ok or not buf:
        # Read GetLastError explicitly: ctypes.windll handles are not created
        # with use_last_error, so ctypes.get_last_error() would report 0 here.
        raise OSError(
            "WTSQuerySessionInformationW(WTSSessionInfoEx) failed for session "
            f"{session_id}, GetLastError={ctypes.windll.kernel32.GetLastError()}"
        )
    try:
        if nbytes.value < ctypes.sizeof(_WTSINFOEX_W):
            raise OSError(
                f"WTSSessionInfoEx returned {nbytes.value} bytes, "
                f"expected >= {ctypes.sizeof(_WTSINFOEX_W)}"
            )
        info = ctypes.cast(buf, ctypes.POINTER(_WTSINFOEX_W)).contents
        if info.Level != _WTS_INFO_EX_LEVEL1:
            raise OSError(f"unexpected WTSINFOEX level {info.Level}")
        data = info.Data
        last_input = int(data.LastInputTime)
        current = int(data.CurrentTime)
        if last_input <= 0 or current <= 0:
            raise OSError("WTS reported no LastInputTime for the active session")
        # Clamp at zero: the two fields are sampled a hair apart, so a user
        # typing at that instant can yield a tiny negative delta.
        return max(0.0, (current - last_input) / _FILETIME_TICKS_PER_S)
    finally:
        wtsapi.WTSFreeMemory(buf)


def _resolve_input_idle_reader() -> tuple[Callable[[], float] | None, str]:
    """Pick the input-idle source valid for *this* process, once, at probe.

    Returns ``(reader, source_description)``; ``reader`` is None when no
    trustworthy source exists, in which case ``system.input_idle_s`` is simply
    not published. Deciding here (rather than per sample) keeps the hot path
    branch-free and puts the chosen source in the probe metadata, so
    ``atf status`` can show which one is live.
    """
    if sys.platform != "win32":
        return None, "unavailable (not Windows)"

    session_id = _process_session_id()

    # Preferred everywhere: asks the terminal-services subsystem about the
    # console session instead of assuming ours is it.
    try:
        _read_active_session_idle_seconds()
        return (
            _read_active_session_idle_seconds,
            "WTSQuerySessionInformationW(WTSSessionInfoEx).LastInputTime",
        )
    except Exception as exc:  # any failure => fall through to the fallback
        wts_error = repr(exc)

    # Fallback only when we are *in* an interactive session, where
    # GetLastInputInfo is scoped to input we can actually observe. Session 0
    # is explicitly excluded: there the call succeeds and lies.
    if session_id is not None and session_id != 0:
        try:
            _read_input_idle_seconds_windows()
            return (
                _read_input_idle_seconds_windows,
                f"GetLastInputInfo (session {session_id}); WTS unavailable: {wts_error}",
            )
        except Exception as exc:
            return None, f"unavailable (WTS: {wts_error}; GetLastInputInfo: {exc!r})"

    return None, (
        f"unavailable (session {session_id} is non-interactive and "
        f"WTS failed: {wts_error})"
    )


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
        # Resolved at probe(): which API can honestly answer "is a human at
        # this machine?" from the session we happen to be running in. None
        # means nothing can, and the signal is withheld rather than faked.
        self._input_idle_reader: Callable[[], float] | None = None

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
                # Same best-effort contract as hard_fault_rate: losing the
                # presence signal costs exactly that signal.
                self._input_idle_reader, idle_source = _resolve_input_idle_reader()
                meta["input_idle_source"] = idle_source
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
            # Presence signal: seconds since the last keyboard/mouse input in
            # the interactive session. Windows-only (no cross-platform
            # equivalent here); omitted off-Windows rather than faked, same
            # policy as the commit-charge fallback being an explicit
            # approximation rather than a silent one. Also omitted when probe()
            # found no session whose input we can legitimately observe.
            if self._input_idle_reader is not None:
                try:
                    out["system.input_idle_s"] = Sample(
                        value=self._input_idle_reader(),
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
