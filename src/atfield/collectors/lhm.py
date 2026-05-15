"""LibreHardwareMonitor HTTP collector: VRAM-junction temp + CPU-package temp.

LHM exposes a sensor-tree JSON document at ``http://127.0.0.1:8085/data.json``
when its built-in HTTP server is enabled. We poll that and pattern-match on
sensor names to extract the two values NVML and psutil cannot give us:

* **GPU memory junction temperature** -- the VRAM die temp, which on
  NVIDIA consumer cards (RTX 30/40/50 series) is the actual temperature
  that controls thermal damage during heavy training, *not* the GPU core
  temp NVML reports.
* **CPU package temperature** -- the per-socket package thermal sensor.
  WMI's ``MSAcpi_ThermalZoneTemperature`` is unreliable on consumer
  motherboards (famously returns 27.85 C constant); LHM reads the actual
  CPU sensors via the WinRing0 driver.

This is the most fragile collector in the project. Things that go wrong:

* LHM not running -> probe fails, rules disable cleanly, watchdog continues.
* LHM running but HTTP server disabled -> probe fails with a clear hint.
* Sensor names drift between LHM versions (e.g. "GPU Memory Junction" vs
  "Memory Junction" vs "GPU Hot Spot"). We pattern-match a list of known
  aliases and log which one matched at probe time.
* Brief HTTP timeouts during sample() -> single failures are absorbed,
  three consecutive failures trip DEGRADED.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

import requests

from atfield.collectors import HealthState, ProbeResult
from atfield.signals import Sample, monotonic_ns

__all__ = ["LhmCollector"]


_NAME: Final = "lhm"
_DEFAULT_URL: Final = "http://127.0.0.1:8085/data.json"
_DEFAULT_TIMEOUT_S: Final = 1.5

# Patterns ordered by preference. First match per device wins.
# These are case-insensitive substring matches against the sensor's full
# path text (parent + sensor name) -- LHM nests sensors deeply.
_VRAM_JUNCTION_PATTERNS: Final = (
    re.compile(r"gpu memory junction", re.IGNORECASE),
    re.compile(r"memory junction temperature", re.IGNORECASE),
    re.compile(r"gpu hot ?spot", re.IGNORECASE),  # last resort: hot-spot is close
)
_CPU_PACKAGE_PATTERNS: Final = (
    re.compile(r"cpu package", re.IGNORECASE),
    re.compile(r"package temperature", re.IGNORECASE),
    re.compile(r"core \(tctl/tdie\)", re.IGNORECASE),  # AMD Ryzen variant
)


@dataclass(frozen=True, slots=True)
class _SensorPath:
    """Where in the LHM JSON tree to look up a sensor each tick."""

    signal_name: str
    full_path: str  # human-readable; for logging
    indices: tuple[int, ...]  # path through Children[i] arrays


class LhmCollector:
    """Collector for sensors only LibreHardwareMonitor exposes."""

    name: Final = _NAME

    def __init__(
        self,
        *,
        url: str = _DEFAULT_URL,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._url = url
        self._timeout_s = timeout_s
        self._health = HealthState.UNPROBED
        self._session: requests.Session | None = None
        self._sensor_paths: tuple[_SensorPath, ...] = ()
        self._consecutive_failures = 0
        self._max_consecutive = 3

    # -- Probe -------------------------------------------------------------

    def probe(self) -> ProbeResult:
        self._session = requests.Session()
        try:
            data = self._fetch_tree()
        except requests.exceptions.ConnectionError as exc:
            self._health = HealthState.FAILED
            return ProbeResult(
                available=False,
                reason=(
                    f"LibreHardwareMonitor HTTP not reachable at {self._url} ({exc.__class__.__name__}); "
                    "ensure LHM is installed, running, and 'Run web server' is enabled in Options"
                ),
                signals=(),
            )
        except requests.exceptions.Timeout:
            self._health = HealthState.FAILED
            return ProbeResult(
                available=False,
                reason=f"LHM HTTP timed out at {self._url} (>{self._timeout_s}s); is LHM responsive?",
                signals=(),
            )
        except Exception as exc:
            self._health = HealthState.FAILED
            return ProbeResult(
                available=False,
                reason=f"LHM HTTP probe failed: {exc!r}",
                signals=(),
            )

        # Walk tree, collect candidate temperature sensors per device.
        self._sensor_paths = tuple(self._discover_sensors(data))
        if not self._sensor_paths:
            self._health = HealthState.FAILED
            return ProbeResult(
                available=False,
                reason=(
                    "LHM HTTP reachable but no GPU memory junction or CPU package "
                    "temperature sensors found in tree; check sensor names in LHM UI"
                ),
                signals=(),
            )

        sigs = tuple(p.signal_name for p in self._sensor_paths)
        meta = {p.signal_name: p.full_path for p in self._sensor_paths}
        self._health = HealthState.HEALTHY
        return ProbeResult(
            available=True,
            reason=f"LHM HTTP OK at {self._url}; {len(sigs)} sensor(s) mapped",
            signals=sigs,
            metadata=meta,
        )

    # -- Sample ------------------------------------------------------------

    def sample(self) -> dict[str, Sample]:
        if not self._health.is_pollable or not self._sensor_paths:
            return {}
        try:
            data = self._fetch_tree()
        except Exception:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._max_consecutive:
                self._health = HealthState.DEGRADED
            return {}

        now = monotonic_ns()
        out: dict[str, Sample] = {}
        for path in self._sensor_paths:
            value = self._read_at(data, path.indices)
            if value is None:
                continue
            out[path.signal_name] = Sample(
                value=value, taken_at_ns=now, source_id=_NAME, unit="celsius"
            )

        if out:
            self._consecutive_failures = 0
            self._health = HealthState.HEALTHY
        return out

    # -- Tree walking helpers ---------------------------------------------

    def _fetch_tree(self) -> dict[str, Any]:
        assert self._session is not None
        resp = self._session.get(self._url, timeout=self._timeout_s)
        resp.raise_for_status()
        return resp.json()

    def _discover_sensors(self, root: dict[str, Any]) -> list[_SensorPath]:
        """Walk the LHM JSON tree, return sensor paths matching our patterns.

        For GPU memory junction temps we want one per GPU. For CPU package
        we want one per CPU socket. We track which devices we've matched
        so we don't double-count when LHM exposes both "Memory Junction"
        and "GPU Hot Spot" on the same device (we prefer the first
        pattern match, hence the ordered tuple above).
        """
        results: list[_SensorPath] = []
        gpu_idx = 0
        cpu_idx = 0
        # Track per-device whether we've already mapped that device's
        # junction or package sensor.
        matched_gpu_devices: set[str] = set()
        matched_cpu_devices: set[str] = set()

        def walk(node: dict[str, Any], path_text: list[str], indices: list[int]) -> None:
            nonlocal gpu_idx, cpu_idx
            text = node.get("Text", "")
            new_path = [*path_text, text]
            full_text = " / ".join(new_path)

            # Is this leaf a temperature sensor with a numeric value?
            if "Value" in node and isinstance(node.get("Value"), str):
                value_str = node["Value"]
                if "°C" in value_str or "C" in value_str:
                    # GPU junction match?
                    device_key = " / ".join(new_path[:-1])  # parent path
                    if "gpu" in device_key.lower() and device_key not in matched_gpu_devices:
                        for pat in _VRAM_JUNCTION_PATTERNS:
                            if pat.search(full_text):
                                results.append(
                                    _SensorPath(
                                        signal_name=f"gpu.{gpu_idx}.mem_junction_temp_c",
                                        full_path=full_text,
                                        indices=tuple(indices),
                                    )
                                )
                                matched_gpu_devices.add(device_key)
                                gpu_idx += 1
                                break
                    if "cpu" in device_key.lower() and device_key not in matched_cpu_devices:
                        for pat in _CPU_PACKAGE_PATTERNS:
                            if pat.search(full_text):
                                results.append(
                                    _SensorPath(
                                        signal_name=(
                                            "system.cpu_package_temp_c"
                                            if cpu_idx == 0
                                            else f"system.cpu{cpu_idx}_package_temp_c"
                                        ),
                                        full_path=full_text,
                                        indices=tuple(indices),
                                    )
                                )
                                matched_cpu_devices.add(device_key)
                                cpu_idx += 1
                                break

            for i, child in enumerate(node.get("Children", []) or []):
                walk(child, new_path, [*indices, i])

        walk(root, [], [])
        return results

    def _read_at(self, root: dict[str, Any], indices: tuple[int, ...]) -> float | None:
        """Read the value at a previously-discovered sensor path.

        Returns None if the path doesn't resolve (sensor disappeared,
        re-detected hardware, etc.) or the value can't be parsed.
        """
        node: Any = root
        for i in indices:
            kids = node.get("Children", []) if isinstance(node, dict) else []
            if not kids or i >= len(kids):
                return None
            node = kids[i]
        if not isinstance(node, dict):
            return None
        raw = node.get("Value")
        if not isinstance(raw, str):
            return None
        # LHM values look like "85.2 °C" or "85.2 C" -- pull the first float.
        m = re.search(r"-?\d+(?:\.\d+)?", raw)
        if not m:
            return None
        try:
            return float(m.group(0))
        except ValueError:
            return None

    # -- Health / lifecycle -------------------------------------------------

    def health(self) -> HealthState:
        return self._health

    def shutdown(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None
