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

from atfield.collectors import HealthState, ProbeResult
from atfield.signals import Sample, monotonic_ns

__all__ = ["PER_PROCESS_VRAM_KEY", "NvmlCollector"]


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
        if not self._health.is_pollable or not self._handles:
            return {}

        pynvml = self._pynvml
        out: dict[str, Sample] = {}
        now = monotonic_ns()
        any_failure = False

        for i, handle in enumerate(self._handles):
            try:
                # Core temperature (gpu chip, not memory).
                temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
                out[f"gpu.{i}.core_temp_c"] = Sample(
                    value=float(temp), taken_at_ns=now, source_id=_NAME, unit="celsius"
                )
            except Exception:
                any_failure = True

            try:
                util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                out[f"gpu.{i}.util_percent"] = Sample(
                    value=float(util.gpu), taken_at_ns=now, source_id=_NAME, unit="percent"
                )
            except Exception:
                any_failure = True

            try:
                meminfo = pynvml.nvmlDeviceGetMemoryInfo(handle)
                used_bytes = float(meminfo.used)
                pct = (meminfo.used / meminfo.total) * 100.0 if meminfo.total else 0.0
                out[f"gpu.{i}.vram_used_bytes"] = Sample(
                    value=used_bytes, taken_at_ns=now, source_id=_NAME, unit="bytes"
                )
                out[f"gpu.{i}.vram_used_percent"] = Sample(
                    value=float(pct), taken_at_ns=now, source_id=_NAME, unit="percent"
                )
            except Exception:
                any_failure = True

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

        if any_failure:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._max_consecutive:
                self._health = HealthState.DEGRADED
        else:
            self._consecutive_failures = 0
            self._health = HealthState.HEALTHY

        return out

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
