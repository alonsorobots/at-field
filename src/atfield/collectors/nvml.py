"""NVIDIA NVML collector: per-GPU core temp, util, VRAM, power, and per-process VRAM.

The most surface-area collector in the project, but also the most reliable
one once it probes successfully -- NVML is a stable, in-process C library
maintained by NVIDIA. The hard parts are (a) deciding what to do when it's
absent (no GPU, no driver, mismatched driver/library), and (b) per-process
VRAM enumeration, which is where consumer drivers historically diverge from
datacenter drivers.

Decisions for v0.1
------------------
* **No nvidia-smi fallback.** PLANNING.md §6 lists ``nvidia_smi.py`` as a
  fallback module, but maintaining a subprocess-based code path (with its
  own timeout/parsing/circuit-breaker logic) doubles the surface area of
  this layer. NVML's ``nvmlDeviceGetComputeRunningProcesses_v3`` works on
  current consumer drivers (>=535). If it ever fails on a real user box,
  per-process VRAM is the only thing we lose -- the kill targeting
  degrades to "highest-VRAM python process across all running GPU procs"
  rather than guessing wrong. Documented in the morning summary.
* **Per-GPU signals are flat-namespaced.** ``gpu.0.core_temp_c``,
  ``gpu.1.core_temp_c``, etc. The :class:`atfield.policy.PolicyEngine`
  expands ``gpu.*.X`` rules against the working signal map at startup.
* **Mem junction temp is NOT here.** NVML doesn't expose VRAM junction
  temperature on consumer cards; that signal lives in
  :mod:`atfield.collectors.lhm`.
"""

from __future__ import annotations

from typing import Any, Final

import logging
import os
import sys
import time

from atfield.collectors import HealthState, ProbeResult
from atfield.signals import Sample, is_plausible, monotonic_ns

_log = logging.getLogger("atfield.collectors.nvml")

# THERE IS NO IN-PROCESS NVML REBUILD, on purpose (2026-10-02).
#
# Every incident this collector has had was finally cured by a FRESH PROCESS (07-23:
# "a fresh process read 36 C immediately"; 09-02: a service restart), and the
# 2026-10-02 sleep/resume probe on Aurora (tools/nvml_resume_probe.py, 4 long-lived
# processes, SYSTEM and user) showed that an in-process rebuild can CAUSE the fault:
#   * entering sleep, every call returns garbage for a few seconds;
#   * a session left alone healed by itself: temperature/util/power 1 s after
#     resume, VRAM within (1 s, 62 s] (the wrapped value arrives as NVML_SUCCESS,
#     so the probe saw its heal only at the next 60-s heartbeat);
#   * a session that ran nvmlShutdown+nvmlInit during that garbage could never read
#     GetMemoryInfo again (v1 or v2, handles by index or by PCI id) -- while
#     temperature/utilisation/power came back -- because nvml.dll is PINNED in the
#     process (64 FreeLibrary calls, still mapped) and whatever broke lives in it.
# AT-Field 0.4.17 did exactly that on Aurora's 2026-09-26 sleep, then rebuilt every
# second for six days; the dead VRAM call held the collector DEGRADED, so the GPU
# core-temperature rules sat "suspect" and Kiroshi froze the host's tuning.
#
# So: failures are ridden out, and only a failure that PERSISTS is escalated -- to a
# process restart (wants_process_restart -> the service exits -> NSSM restarts it),
# bounded by the service's restart budget so a genuinely dead GPU cannot loop it.

# Continuous failure (exceptions or impossible values) before asking for a fresh
# process. Sleep-entry garbage lasts seconds and an untouched session healed within
# 62 s of resume (VRAM; 1 s for the rest), so the margin is >= 58 s -- about 2x; a poisoned or replaced
# session is cured a couple of minutes after it breaks instead of six days later.
_PERSISTENT_FAILURE_S: Final = 120.0

# A wall-clock jump this large between ticks means the machine slept, hibernated or
# resumed from a Fast Startup "shut down" (which hibernates session 0 -- services
# survive it, and Aurora's LastBootUpTime stayed 2026-09-26 through two power-offs).
# The failure clock restarts at a gap: what failed across it is the transition.
# (The service loop can run 2.7-6.7 s per tick on this fleet, so 30 s is far above
# any slow tick.)
_WALL_GAP_S: Final = 30.0

# How often to read the installed driver version off disk. A version-resource
# read is far more expensive than the metric calls (which total ~0.016 ms), so
# it does not belong on every tick -- and a driver swap that goes unnoticed for
# a minute is not the failure we are preventing. The one on Chronos went
# unnoticed for four days.
_DRIVER_CHECK_INTERVAL_NS: Final = 60_000_000_000



__all__ = ["PER_PROCESS_VRAM_KEY", "NvmlCollector", "encode_driver_version"]


def encode_driver_version(a: int, b: int, c: int, d: int) -> str:
    """Turn a Windows 4-part driver version into NVIDIA's own ``MMM.mm`` form.

    NVIDIA hides the driver number in the LAST TWO groups, and Windows shows
    two different prefixes for the same driver -- MEASURED on Chronos, both
    naming 616.56 on the same day:

        nvml.dll FileVersion            8.17.16.1656
        Win32_PnPSignedDriver           32.0.16.1656
        nvmlSystemGetDriverVersion()    "616.56"

    so the prefix is discarded and only ``(c, d)`` decide: last digit of ``c``,
    then ``d`` split into hundreds and remainder.
    """
    return f"{c % 10}{d // 100}.{d % 100:02d}"


_NAME: Final = "nvml"

# Signal name that carries the per-process VRAM map. Special-cased: its
# Sample.value is the count of GPU procs (a numeric for shape consistency),
# and the actual mapping lives in ``Sample.metadata`` -- but Sample is
# frozen and metadata-less by design. So we ship the map separately via
# the snapshot dict the service holds. The service imports this constant
# and reads the live map from the collector when an Action needs it.
#
# Concretely: this signal name is *never* referenced by a [[rules]] entry
# in config.toml -- it's a service-private channel for the actuator.
PER_PROCESS_VRAM_KEY: Final = "gpu.processes"


# ---------------------------------------------------------------------------
# Lazy import wrapper for pynvml
# ---------------------------------------------------------------------------


def _import_pynvml() -> Any:
    """Import pynvml lazily so the module is importable without the driver."""
    import pynvml  # type: ignore[import-not-found]
    return pynvml


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


def _wall_s() -> float:
    """Wall clock; a thin wrapper so tests can replay a sleep."""
    return time.time()


def _error_code(exc: BaseException) -> str:
    """NVML_ERROR_* code of an NVMLError (e.g. 'nvml:999'), else the exception type."""
    v = getattr(exc, "value", None)
    return f"nvml:{v}" if isinstance(v, int) else type(exc).__name__


def _fmt_failures(failures: dict[str, str]) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(failures.items())) or "none"


class NvmlCollector:
    """Collector for NVIDIA GPU signals via NVML.

    Holds NVML handles for the lifetime of the service. ``shutdown()`` calls
    ``nvmlShutdown()``; failing to do so leaks an NVML init refcount that
    can confuse later re-init in the same process (which the service does
    not do, but tests might).
    """

    name: Final = _NAME

    def __init__(self) -> None:
        self._health = HealthState.UNPROBED
        self._pynvml: Any = None
        self._handles: list[Any] = []
        self._gpu_count: int = 0
        self._driver_version: str = ""
        self._signals: tuple[str, ...] = ()
        self._consecutive_failures = 0
        self._max_consecutive = 3
        # Monotonic time the current run of failing ticks began (None while healthy),
        # the previous tick's wall clock (power-transition witness), and the last
        # failure set we logged -- NVML error codes are logged when they CHANGE, so
        # the next new failure shape is diagnosable from the log on day one.
        self._failing_since_ns: int | None = None
        self._last_wall_s: float | None = None
        self._last_failures: dict[str, str] = {}
        self._last_driver_check_ns: int | None = None
        # The replaced driver's version and when it was first seen -- a restart is
        # asked for only once the SAME version has stood for a check interval.
        self._swap_version: str | None = None
        self._swap_seen_ns: int | None = None
        #: True once the installed driver differs from the one this session
        #: opened against. Never cleared in-process: only a fresh process maps
        #: the new nvml.dll.
        self.driver_replaced = False
        #: Set to a human-readable reason when this process cannot be cured in
        #: place: the driver was replaced, or NVML has failed continuously for
        #: _PERSISTENT_FAILURE_S. The service loop reads it and exits so NSSM
        #: hands us a fresh process (AppExit=Restart), within its restart budget.
        self.wants_process_restart: str | None = None
        # Live per-GPU process map. Keyed by gpu_idx; value is a list of
        # (pid, used_vram_bytes) tuples. Refreshed on a slow cadence from
        # sample() (cheap dashboard count) and force-refreshed at kill time.
        self._gpu_processes: dict[int, list[tuple[int, int]]] = {}
        # Compute-process enumeration is by far the most expensive NVML call
        # (~0.9 ms vs ~0.006 ms for ALL the metric reads combined on a 2-GPU
        # box). It's only *consumed* when a kill fires, so we don't pay for it
        # every tick -- we refresh at most this often on the hot path, and
        # force a fresh enumeration at kill time via refresh_process_map().
        self._proc_map_interval_ns = 5_000_000_000  # 5 s
        self._last_proc_map_ns = 0

    # -- Probe -------------------------------------------------------------

    def probe(self) -> ProbeResult:
        try:
            pynvml = _import_pynvml()
        except ImportError as exc:
            self._health = HealthState.FAILED
            return ProbeResult(
                available=False,
                reason=f"pynvml import failed: {exc}; install nvidia-ml-py and an NVIDIA driver >= 535",
                signals=(),
            )

        try:
            pynvml.nvmlInit()
        except Exception as exc:  # NVMLError or OSError on missing DLL
            self._health = HealthState.FAILED
            return ProbeResult(
                available=False,
                reason=(
                    f"NVML init failed ({exc!r}); driver missing, version mismatch, "
                    "or no NVIDIA GPU on this box"
                ),
                signals=(),
            )

        try:
            self._pynvml = pynvml
            count = pynvml.nvmlDeviceGetCount()
            if count == 0:
                pynvml.nvmlShutdown()
                self._health = HealthState.FAILED
                return ProbeResult(
                    available=False,
                    reason="NVML reports 0 GPUs; nothing to monitor",
                    signals=(),
                )

            self._gpu_count = count
            self._handles = [pynvml.nvmlDeviceGetHandleByIndex(i) for i in range(count)]
            try:
                self._driver_version = pynvml.nvmlSystemGetDriverVersion()
                if isinstance(self._driver_version, bytes):
                    self._driver_version = self._driver_version.decode("utf-8", "replace")
            except Exception:
                self._driver_version = "unknown"

            # Build the per-GPU signal namespace.
            sigs: list[str] = [PER_PROCESS_VRAM_KEY]
            for i in range(count):
                sigs.extend(
                    [
                        f"gpu.{i}.core_temp_c",
                        f"gpu.{i}.util_percent",
                        f"gpu.{i}.vram_used_percent",
                        f"gpu.{i}.vram_used_bytes",
                        f"gpu.{i}.power_w",
                    ]
                )
            self._signals = tuple(sigs)

            gpu_names = []
            for h in self._handles:
                try:
                    nm = pynvml.nvmlDeviceGetName(h)
                    if isinstance(nm, bytes):
                        nm = nm.decode("utf-8", "replace")
                    gpu_names.append(nm)
                except Exception:
                    gpu_names.append("unknown")

            self._health = HealthState.HEALTHY
            return ProbeResult(
                available=True,
                reason=f"NVML driver {self._driver_version}, {count} GPU(s): {', '.join(gpu_names)}",
                signals=self._signals,
                metadata={
                    "driver_version": self._driver_version,
                    "gpu_count": str(count),
                    "gpu_names": "; ".join(gpu_names),
                },
            )
        except Exception as exc:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass
            self._health = HealthState.FAILED
            return ProbeResult(
                available=False,
                reason=f"NVML probe failed after init: {exc!r}",
                signals=(),
            )

    # -- Sample ------------------------------------------------------------

    def sample(self) -> dict[str, Sample]:
        if not self._health.is_pollable:
            return {}

        pynvml = self._pynvml
        out: dict[str, Sample] = {}
        now = monotonic_ns()
        # An empty handle list is a FAILING tick, not a quiet one: a collector
        # publishing nothing looks exactly like a quiet one, so it must still
        # reach the failure accounting (and the escalation) below.
        any_failure = not self._handles
        failures: dict[str, str] = {} if self._handles else {"handles": "none"}

        for i, handle in enumerate(self._handles):
            try:
                # Core temperature (gpu chip, not memory).
                temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
                out[f"gpu.{i}.core_temp_c"] = Sample(
                    value=float(temp), taken_at_ns=now, source_id=_NAME, unit="celsius"
                )
            except Exception as exc:
                any_failure = True
                failures[f"gpu.{i}.core_temp_c"] = _error_code(exc)

            try:
                util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                out[f"gpu.{i}.util_percent"] = Sample(
                    value=float(util.gpu), taken_at_ns=now, source_id=_NAME, unit="percent"
                )
            except Exception as exc:
                any_failure = True
                failures[f"gpu.{i}.util_percent"] = _error_code(exc)

            try:
                meminfo = pynvml.nvmlDeviceGetMemoryInfo(handle)
                if meminfo.total and meminfo.used > meminfo.total:
                    # Physically impossible, and it arrives as NVML_SUCCESS:
                    # entering sleep on Aurora (2026-10-02) used read as the
                    # wrapped uint64 18446744073673375744 on an 8 GB card.
                    # The bytes unit has no upper bound in is_plausible, so
                    # this is the only place that knows the card's total.
                    raise ValueError("vram used > total")
                used_bytes = float(meminfo.used)
                pct = (meminfo.used / meminfo.total) * 100.0 if meminfo.total else 0.0
                out[f"gpu.{i}.vram_used_bytes"] = Sample(
                    value=used_bytes, taken_at_ns=now, source_id=_NAME, unit="bytes"
                )
                out[f"gpu.{i}.vram_used_percent"] = Sample(
                    value=float(pct), taken_at_ns=now, source_id=_NAME, unit="percent"
                )
            except Exception as exc:
                any_failure = True
                failures[f"gpu.{i}.vram"] = _error_code(exc)

            try:
                power_mw = pynvml.nvmlDeviceGetPowerUsage(handle)  # milliwatts
                out[f"gpu.{i}.power_w"] = Sample(
                    value=float(power_mw) / 1000.0, taken_at_ns=now, source_id=_NAME, unit="watts"
                )
            except Exception:
                # Power query is unsupported on some cards; not a failure.
                pass

        # Refresh the (expensive) per-process map only on a slow cadence so
        # the hot path stays cheap. Kill targeting force-refreshes separately.
        if now - self._last_proc_map_ns >= self._proc_map_interval_ns:
            self._gpu_processes = self._enumerate_process_map()
            self._last_proc_map_ns = now

        # Sample carrying the GPU-proc count; the actual map is read via
        # process_map() / refresh_process_map() by the service/actuator.
        out[PER_PROCESS_VRAM_KEY] = Sample(
            value=float(sum(len(v) for v in self._gpu_processes.values())),
            taken_at_ns=now,
            source_id=_NAME,
            unit="count",
        )

        # --- NVML "SUCCESS with garbage" ------------------------------------
        # 2026-07-23 (RTX 5090, wedged NVDEC context): temperature 885510 C and
        # util/power both 260640043, every call NVML_SUCCESS -- nothing raised.
        # 2026-10-02 (Aurora, entering sleep): temperature 0 C and VRAM used a
        # wrapped uint64, also NVML_SUCCESS. Implausibility is the only witness:
        # the bad samples are withheld (their rules abstain instead of reading
        # "far over threshold") and the tick counts as failing.
        implausible = [k for k, s in out.items() if not is_plausible(s.value, s.unit)]
        if implausible:
            for k in implausible:
                out.pop(k, None)
                failures[k] = "implausible"
            any_failure = True
        self._log_failure_change(failures)

        # --- power transition: ride it out, touch nothing ------------------
        wall = _wall_s()
        if self._last_wall_s is not None and wall - self._last_wall_s > _WALL_GAP_S:
            _log.warning(
                "wall clock jumped %.0f s between ticks: the machine slept, hibernated "
                "or resumed from a Fast Startup shutdown. Leaving the NVML session "
                "untouched -- it heals by itself within seconds of resume, and a "
                "rebuild during the transition breaks it for the life of the process.",
                wall - self._last_wall_s)
            if self._failing_since_ns is not None:
                # what failed across the gap was the transition, not the session
                self._failing_since_ns = now
        self._last_wall_s = wall

        # --- the driver was swapped under us --------------------------------
        # Checked every tick (rate-limited inside) because it is the ONLY route
        # that sees a session whose calls all still succeed (Chronos 2026-09-02:
        # a constant 0.0 C for four days, health HEALTHY). Its cure is a fresh
        # process: this one keeps the old nvml.dll mapped.
        #
        # STABLE FOR ONE FULL CHECK INTERVAL before asking (review 2026-10-02,
        # F1): a driver install is not instant (Chronos 09-02: 19:14:55 ->
        # 19:15:38). A fresh process started mid-install can hit the library /
        # kernel-module mismatch in probe(), go FAILED, and is then never polled
        # again -- so it could never ask for the restart that would cure it. The
        # same new version on two checks >= _DRIVER_CHECK_INTERVAL_NS apart means
        # the install has settled.
        if (self._check_driver_swap(now) and self.wants_process_restart is None
                and self._swap_seen_ns is not None
                and now - self._swap_seen_ns >= _DRIVER_CHECK_INTERVAL_NS):
            self.wants_process_restart = (
                f"NVML session was opened against driver {self._driver_version} but "
                f"{self._swap_version} has been installed for over a minute; only a "
                f"fresh process maps the new nvml.dll")

        if any_failure:
            if self._failing_since_ns is None:
                self._failing_since_ns = now
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._max_consecutive:
                self._health = HealthState.DEGRADED
            failing_s = (now - self._failing_since_ns) / 1e9
            if failing_s >= _PERSISTENT_FAILURE_S and self.wants_process_restart is None:
                self.wants_process_restart = (
                    f"NVML has failed continuously for {failing_s:.0f} s "
                    f"({_fmt_failures(failures)}); an in-process rebuild cannot cure "
                    f"this (nvml.dll stays mapped) -- a fresh process does")
        else:
            self._failing_since_ns = None
            self._consecutive_failures = 0
            self._health = HealthState.HEALTHY

        return out

    def _log_failure_change(self, failures: dict[str, str]) -> None:
        """Log NVML failures when the SET changes -- never per tick, never silently.

        Until 2026-10-02 every exception here was swallowed, so a VRAM call that
        failed for six days left no error code anywhere."""
        if failures == self._last_failures:
            return
        if failures:
            _log.warning("NVML calls failing: %s", _fmt_failures(failures))
        else:
            _log.info("NVML calls recovered (were failing: %s)",
                      _fmt_failures(self._last_failures))
        self._last_failures = dict(failures)

    def _installed_driver_version(self) -> str | None:
        """The driver version currently ON DISK, read WITHOUT touching NVML.

        This is the only witness that can see the silent shape. When a session
        goes stale, every call it makes may still return NVML_SUCCESS with a
        plausible number (Chronos: 0.0 C and 0.041 W, for four days) -- so the
        session cannot be asked whether it is stale. Something outside it has
        to say so.

        ``nvmlSystemGetDriverVersion()`` was the obvious candidate and is NOT
        used, because it is unproven: the value we compared against was
        captured at probe time and never re-read on the wedged session, so
        whether a stale session reports the old or the new string is unknown.
        A detector that might be comparing a constant to itself is not a
        detector. The file on disk is not in doubt.

        Returns None on non-Windows, a missing DLL, or any read failure --
        never a guess. A wrong answer here would force a service restart, so
        "I cannot tell" must be distinguishable from "it changed".
        """
        if sys.platform != "win32":
            return None
        try:
            import ctypes
            from ctypes import wintypes

            path = os.path.join(
                os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvml.dll")
            if not os.path.exists(path):
                return None

            ver = ctypes.WinDLL("version")
            size = ver.GetFileVersionInfoSizeW(ctypes.c_wchar_p(path), None)
            if not size:
                return None
            buf = ctypes.create_string_buffer(size)
            if not ver.GetFileVersionInfoW(ctypes.c_wchar_p(path), 0, size, buf):
                return None

            block = ctypes.c_void_p()
            length = wintypes.UINT()
            if not ver.VerQueryValueW(buf, ctypes.c_wchar_p("\\"),
                                      ctypes.byref(block), ctypes.byref(length)):
                return None

            class _FFI(ctypes.Structure):
                _fields_ = [("dwSignature", wintypes.DWORD),
                            ("dwStrucVersion", wintypes.DWORD),
                            ("dwFileVersionMS", wintypes.DWORD),
                            ("dwFileVersionLS", wintypes.DWORD)]

            ffi = ctypes.cast(block, ctypes.POINTER(_FFI)).contents
            return encode_driver_version(ffi.dwFileVersionMS >> 16,
                                         ffi.dwFileVersionMS & 0xFFFF,
                                         ffi.dwFileVersionLS >> 16,
                                         ffi.dwFileVersionLS & 0xFFFF)
        except Exception:  # noqa: BLE001 - fail safe, never fire on a bad read
            _log.debug("could not read nvml.dll file version", exc_info=True)
            return None

    def _check_driver_swap(self, now_ns: int) -> bool:
        """Witness B. True if the installed driver no longer matches ours."""
        if not self._driver_version:
            # Probe never recorded one; there is nothing to compare against.
            return self.driver_replaced
        if (self._last_driver_check_ns is not None
                and now_ns - self._last_driver_check_ns < _DRIVER_CHECK_INTERVAL_NS):
            return self.driver_replaced
        self._last_driver_check_ns = now_ns
        installed = self._installed_driver_version()
        if installed is None:
            return self.driver_replaced
        if installed != self._driver_version:
            if installed != self._swap_version:
                # first sighting of THIS version (or the install moved on):
                # the stability clock starts here
                _log.error(
                    "NVIDIA driver was REPLACED under this session: it was opened "
                    "against %s and %s is now installed. Every reading from these "
                    "handles is suspect -- asking for a fresh process once the "
                    "install has been stable for a check interval.",
                    self._driver_version, installed)
                self._swap_version, self._swap_seen_ns = installed, now_ns
            self.driver_replaced = True
        return self.driver_replaced

    # -- Per-process VRAM accessor -----------------------------------------

    def _enumerate_process_map(self) -> dict[int, list[tuple[int, int]]]:
        """Enumerate compute processes on every GPU (the expensive call).

        Tries the modern ``_v3`` entry point first and falls back to the
        legacy one for older drivers; a GPU that refuses both contributes an
        empty list rather than failing the whole sweep.
        """
        pynvml = self._pynvml
        proc_map: dict[int, list[tuple[int, int]]] = {}
        for i, handle in enumerate(self._handles):
            try:
                procs = pynvml.nvmlDeviceGetComputeRunningProcesses_v3(handle)
            except Exception:
                try:
                    procs = pynvml.nvmlDeviceGetComputeRunningProcesses(handle)
                except Exception:
                    proc_map[i] = []
                    continue
            pairs: list[tuple[int, int]] = []
            for p in procs:
                used = getattr(p, "usedGpuMemory", None)
                # NVML returns ULLONG_MAX (0xFFFFFFFFFFFFFFFF) when not measurable.
                used_int = 0 if used is None or used == (1 << 64) - 1 else int(used)
                pairs.append((int(p.pid), used_int))
            proc_map[i] = pairs
        return proc_map

    def process_map(self) -> dict[int, list[tuple[int, int]]]:
        """Last-known per-GPU process map: ``{gpu_idx -> [(pid, bytes), ...]}``.

        Returned dict is a shallow copy so callers can't mutate the
        collector's state. This is the cadence-cached view (refreshed at most
        every few seconds by sample()); for kill targeting use
        :meth:`refresh_process_map` so the PIDs are current.
        """
        return {gpu: list(pairs) for gpu, pairs in self._gpu_processes.items()}

    def refresh_process_map(self) -> dict[int, list[tuple[int, int]]]:
        """Force a fresh compute-process enumeration and return a copy.

        Called by the service immediately before a GPU kill so targeting uses
        up-to-the-moment PIDs rather than the cadence-cached map. Also resets
        the cadence clock so sample() won't redundantly re-enumerate right
        after a kill.
        """
        if not self._health.is_pollable or not self._handles:
            return {}
        self._gpu_processes = self._enumerate_process_map()
        self._last_proc_map_ns = monotonic_ns()
        return {gpu: list(pairs) for gpu, pairs in self._gpu_processes.items()}

    # -- Health / lifecycle -------------------------------------------------

    def health(self) -> HealthState:
        return self._health

    def shutdown(self) -> None:
        if self._pynvml is None:
            return
        try:
            self._pynvml.nvmlShutdown()
        except Exception:
            pass
        self._handles = []
        self._pynvml = None
