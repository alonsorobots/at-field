"""LibreHardwareMonitor *library* collector (the robust transport).

This replaces the old HTTP-based :class:`atfield.collectors.lhm.LhmCollector`,
which drove LHM's optional GUI web server from a background service. That
path proved fragile in the field and unfixable from our side:

* It binds ``http://+:<port>/`` (a strong wildcard), which http.sys refuses
  without a URL reservation, and recent Windows http.sys updates broke it
  outright (LHM issues #1855, #2374).
* LHM is a WinForms GUI app; launched by a Session-0 LocalSystem service it
  would not reliably start its listener, and it swallows the exception, so
  the process sits up with no server and every LHM-backed rule silently dies.

Instead we talk to ``LibreHardwareMonitorLib`` directly -- the same library
LHM's GUI uses, and its documented headless use case. A tiny bundled .NET
helper (``atfield-sensors.exe``, see ``helper/AtfieldSensors.cs``) opens the
sensor tree and streams readings to us as JSON lines on stdout. No GUI, no
web server, no http.sys, no URL ACL, no Session-0 footgun. Driver-backed
sensors (CPU package temp via MSR) need elevation; the watchdog runs as
LocalSystem, so it gets them. GPU memory-junction temp needs neither.

The collector owns the helper subprocess: it spawns it at :meth:`probe`,
reads its output on a background thread, and re-spawns it (with a small
backoff) if it dies, so a crashed helper self-heals without taking the
watchdog down.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Final

from atfield.collectors import HealthState, ProbeResult
from atfield.collectors.lhm import (
    _CPU_PACKAGE_PATTERNS,
    _RAIL_VOLTAGE_PATTERNS,
    _VRAM_JUNCTION_PATTERNS,
)
from atfield.signals import Sample, monotonic_ns

__all__ = ["LhmLibCollector", "find_sensor_helper"]

_log = logging.getLogger(__name__)

_NAME: Final = "lhm"
_HELPER_EXE: Final = "atfield-sensors.exe"
_GPU_HW_TYPES: Final = frozenset({"GpuNvidia", "GpuAmd", "GpuIntel"})

# A sample older than this means the helper has stalled (it should emit at
# its --interval, default 1 Hz). We trip DEGRADED and try to re-spawn.
_MAX_STALE_S: Final = 6.0
# Don't hammer re-spawns when the helper is genuinely broken.
_RESPAWN_BACKOFF_S: Final = 5.0


def _hw_prefix(sensor_id: str) -> str:
    """Derive the owning-device identifier from a sensor identifier.

    LHM identifiers look like ``/gpu-nvidia/0/temperature/3``; the device
    is the first two path segments (``/gpu-nvidia/0``). Used as a fallback
    when the helper didn't supply ``hwId``.
    """
    parts = sensor_id.split("/")
    return "/".join(parts[:3]) if len(parts) >= 3 else sensor_id


def find_sensor_helper(
    *, bundled_root: Path | None = None
) -> Path | None:
    """Locate ``atfield-sensors.exe`` across dev and installed layouts.

    The helper is built into the same directory as the bundled
    ``LibreHardwareMonitorLib.dll`` (it loads that DLL at runtime), so we
    search the same places AT-Field looks for LHM. Resolution order:

    1. ``ATFIELD_SENSOR_EXE`` env var (explicit override; what the
       installer bakes into the service).
    2. Next to the discovered LibreHardwareMonitor binary.
    3. ``bundled_root`` and ``sys.executable``'s directory.
    4. A sibling ``dist/atfield/`` tree (checked-out repo dev layout).
    5. ``%PROGRAMDATA%\\ATField\\lhm`` (per-machine install).
    """
    env = os.environ.get("ATFIELD_SENSOR_EXE")
    if env and Path(env).is_file():
        return Path(env)

    candidate_dirs: list[Path] = []

    # (2) next to the discovered LHM binary, using the same robust search
    # the service uses for LHM (env var + bundled + sibling dist + ProgramData).
    try:
        from atfield.lhm_supervisor import find_lhm_executable

        extra: list[Path] = []
        try:
            here = Path(sys.executable).resolve().parent
            cur = here
            for _ in range(4):
                cur = cur.parent
                cand = cur / "dist" / "atfield"
                if cand.is_dir():
                    extra.append(cand)
                    break
        except Exception:  # noqa: BLE001
            pass
        program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        extra.append(Path(program_data) / "ATField" / "lhm")

        root = bundled_root
        if root is None:
            try:
                root = Path(sys.executable).parent
            except Exception:  # noqa: BLE001
                root = None

        lhm = find_lhm_executable(bundled_root=root, extra_search_paths=tuple(extra))
        if lhm is not None:
            candidate_dirs.append(lhm.parent)
        candidate_dirs.extend(extra)
    except Exception:  # noqa: BLE001 -- discovery must never raise
        pass

    # (3) bundled_root / sys.executable dir
    if bundled_root is not None:
        candidate_dirs.append(bundled_root)
    try:
        candidate_dirs.append(Path(sys.executable).parent)
    except Exception:  # noqa: BLE001
        pass

    for d in candidate_dirs:
        cand = d / _HELPER_EXE
        if cand.is_file():
            return cand
    return None


class LhmLibCollector:
    """Reads CPU/GPU/PSU sensors via the bundled LHM-library helper."""

    name: Final = _NAME

    def __init__(
        self,
        *,
        exe_path: Path | None = None,
        interval_s: float = 1.0,
        startup_timeout_s: float = 12.0,
    ) -> None:
        self._exe = exe_path if exe_path is not None else find_sensor_helper()
        self._interval_s = interval_s
        self._startup_timeout_s = startup_timeout_s

        self._health = HealthState.UNPROBED
        self._proc: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()
        self._latest: list[dict[str, Any]] = []
        self._latest_ts: float = 0.0
        self._ready = False
        self._elevated: bool | None = None
        self._last_spawn_at: float = 0.0
        self._stop = False

        # id -> (signal_name, unit). Built once at probe; read each tick.
        self._sensor_map: dict[str, tuple[str, str]] = {}

    # -- Probe -------------------------------------------------------------

    def probe(self) -> ProbeResult:
        if self._exe is None or not Path(self._exe).is_file():
            self._health = HealthState.FAILED
            return ProbeResult(
                available=False,
                reason=(
                    f"sensor helper {_HELPER_EXE} not found "
                    "(build it with scripts/build_helper.ps1, or set "
                    "ATFIELD_SENSOR_EXE). LHM-backed signals (CPU package "
                    "temp, GPU memory-junction temp, PSU voltages) disabled."
                ),
                signals=(),
            )

        if not self._spawn():
            self._health = HealthState.FAILED
            return ProbeResult(
                available=False,
                reason=f"could not start sensor helper {self._exe}",
                signals=(),
            )

        # Wait for the first sample so we can discover sensors.
        deadline = time.monotonic() + self._startup_timeout_s
        while time.monotonic() < deadline:
            with self._lock:
                latest = list(self._latest)
            if latest:
                break
            if self._proc is not None and self._proc.poll() is not None:
                self._health = HealthState.FAILED
                return ProbeResult(
                    available=False,
                    reason=(
                        f"sensor helper exited (code "
                        f"{self._proc.returncode}) before emitting a sample"
                    ),
                    signals=(),
                )
            time.sleep(0.1)
        else:
            self._health = HealthState.FAILED
            return ProbeResult(
                available=False,
                reason=(
                    f"sensor helper produced no sample within "
                    f"{self._startup_timeout_s:.0f}s"
                ),
                signals=(),
            )

        self._sensor_map = self._discover(latest)
        if not self._sensor_map:
            self._health = HealthState.FAILED
            elev = "" if self._elevated else " (helper not elevated -- CPU MSR sensors need admin)"
            return ProbeResult(
                available=False,
                reason=(
                    "sensor helper running but no GPU memory-junction / CPU "
                    f"package / PSU voltage sensors had usable values{elev}"
                ),
                signals=(),
            )

        self._health = HealthState.HEALTHY
        sigs = tuple(sig for sig, _ in self._sensor_map.values())
        meta = {sig: f"{sid}" for sid, (sig, _) in self._sensor_map.items()}
        meta["helper"] = str(self._exe)
        meta["elevated"] = str(bool(self._elevated))
        return ProbeResult(
            available=True,
            reason=(
                f"LHM library helper OK; {len(sigs)} sensor(s) mapped "
                f"(elevated={bool(self._elevated)})"
            ),
            signals=sigs,
            metadata=meta,
        )

    # -- Sample ------------------------------------------------------------

    def sample(self) -> dict[str, Sample]:
        if not self._health.is_pollable or not self._sensor_map:
            return {}

        # Re-spawn a dead helper (bounded by backoff) so it self-heals.
        if self._proc is None or self._proc.poll() is not None:
            self._health = HealthState.DEGRADED
            if time.monotonic() - self._last_spawn_at >= _RESPAWN_BACKOFF_S:
                _log.warning("sensor helper not running; re-spawning")
                self._spawn()
            return {}

        with self._lock:
            latest = list(self._latest)
            age = time.monotonic() - self._latest_ts

        if not latest or age > _MAX_STALE_S:
            self._health = HealthState.DEGRADED
            return {}

        now = monotonic_ns()
        out: dict[str, Sample] = {}
        for s in latest:
            sid = s.get("id")
            if sid is None:
                continue
            mapped = self._sensor_map.get(sid)
            if mapped is None:
                continue
            signal_name, unit = mapped
            value = self._usable_value(s, unit)
            if value is None:
                continue
            out[signal_name] = Sample(
                value=value, taken_at_ns=now, source_id=_NAME, unit=unit
            )

        if out:
            self._health = HealthState.HEALTHY
        return out

    # -- Discovery ---------------------------------------------------------

    def _discover(self, sensors: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
        """Map helper sensors to AT-Field signal names using the same
        name patterns as the legacy HTTP collector, but keyed on the
        authoritative ``hwType`` the library reports."""
        mapping: dict[str, tuple[str, str]] = {}
        gpu_idx = 0
        cpu_idx = 0
        matched_gpu_hw: set[str] = set()
        matched_cpu_hw: set[str] = set()
        emitted_voltage: set[str] = set()

        for s in sensors:
            sid = s.get("id")
            if not sid:
                continue
            stype = s.get("type")
            hw_type = s.get("hwType", "")
            name = s.get("name", "")
            # Dedupe per physical device by hardware *identifier*, not name:
            # two identical GPUs share the name "NVIDIA GeForce RTX 5090" but
            # have distinct identifiers (/gpu-nvidia/0 vs /gpu-nvidia/1).
            hw = s.get("hwId") or _hw_prefix(sid)

            if stype == "Temperature":
                value = self._usable_value(s, "celsius")
                if value is None:
                    continue
                if hw_type in _GPU_HW_TYPES and hw not in matched_gpu_hw:
                    if any(p.search(name) for p in _VRAM_JUNCTION_PATTERNS):
                        mapping[sid] = (f"gpu.{gpu_idx}.mem_junction_temp_c", "celsius")
                        matched_gpu_hw.add(hw)
                        gpu_idx += 1
                        continue
                if hw_type == "Cpu" and hw not in matched_cpu_hw:
                    if any(p.search(name) for p in _CPU_PACKAGE_PATTERNS):
                        sig = (
                            "system.cpu_package_temp_c"
                            if cpu_idx == 0
                            else f"system.cpu{cpu_idx}_package_temp_c"
                        )
                        mapping[sid] = (sig, "celsius")
                        matched_cpu_hw.add(hw)
                        cpu_idx += 1
                        continue

            elif stype == "Voltage":
                value = self._usable_value(s, "volts")
                if value is None:
                    continue
                for pat, suffix in _RAIL_VOLTAGE_PATTERNS:
                    if pat.search(name):
                        sig = f"system.{suffix}"
                        if sig not in emitted_voltage:
                            mapping[sid] = (sig, "volts")
                            emitted_voltage.add(sig)
                        break

        return mapping

    @staticmethod
    def _usable_value(sensor: dict[str, Any], unit: str) -> float | None:
        """Return a physically-plausible reading, or None.

        A temperature of <= 0 means the sensor exists but isn't being fed
        (e.g. the CPU MSR driver isn't loaded because we're not elevated) --
        we treat it as "no value" so the rule abstains cleanly instead of
        showing a bogus 0 C. Same for non-positive voltages.
        """
        raw = sensor.get("value")
        if raw is None:
            return None
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return None
        if v <= 0.0:
            return None
        return v

    # -- Subprocess lifecycle ---------------------------------------------

    def _spawn(self) -> bool:
        self._last_spawn_at = time.monotonic()
        self._terminate_proc()
        if self._exe is None:
            return False
        try:
            self._proc = subprocess.Popen(
                [str(self._exe), "--interval", f"{self._interval_s:g}"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            _log.warning("failed to spawn sensor helper %s: %s", self._exe, exc)
            self._proc = None
            return False

        self._reader = threading.Thread(
            target=self._read_loop,
            args=(self._proc,),
            name="atfield-sensor-helper-reader",
            daemon=True,
        )
        self._reader.start()
        return True

    def _read_loop(self, proc: subprocess.Popen[str]) -> None:
        stdout = proc.stdout
        if stdout is None:
            return
        try:
            for line in stdout:
                if self._stop:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    continue
                event = obj.get("event")
                if event == "sample":
                    sensors = obj.get("sensors") or []
                    with self._lock:
                        self._latest = sensors
                        self._latest_ts = time.monotonic()
                elif event == "ready":
                    with self._lock:
                        self._ready = True
                        self._elevated = bool(obj.get("elevated"))
                elif event == "error":
                    _log.warning("sensor helper error: %s", obj.get("message"))
        except Exception:  # noqa: BLE001 -- reader thread must not crash service
            pass

    def _terminate_proc(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()  # signal graceful shutdown
                except OSError:
                    pass
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:  # noqa: BLE001
            pass

    # -- Health / lifecycle ------------------------------------------------

    def health(self) -> HealthState:
        return self._health

    def shutdown(self) -> None:
        self._stop = True
        self._terminate_proc()
