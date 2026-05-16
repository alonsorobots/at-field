"""LibreHardwareMonitor HTTP collector: thermal + voltage sensors.

LHM exposes a sensor-tree JSON document at ``http://127.0.0.1:8085/data.json``
when its built-in HTTP server is enabled. We poll that and pattern-match on
sensor names to extract values NVML and psutil cannot give us:

* **GPU memory junction temperature** -- the VRAM die temp, which on
  NVIDIA consumer cards (RTX 30/40/50 series) is the actual temperature
  that controls thermal damage during heavy training, *not* the GPU core
  temp NVML reports.
* **CPU package temperature** -- the per-socket package thermal sensor.
  WMI's ``MSAcpi_ThermalZoneTemperature`` is unreliable on consumer
  motherboards (famously returns 27.85 C constant); LHM reads the actual
  CPU sensors via the WinRing0 driver.
* **PSU rail voltages** -- ``+12V``, ``+5V``, ``+3.3V`` rails as exposed
  by Super I/O voltage sensors on the motherboard. Catches PSU sag
  patterns that correlate with GPU TDR / Kernel-Power 41 events on
  high-transient cards (RTX 4090 / 5090). Also surfaces VCore (CPU core
  voltage) and GPU core voltage when reported.

This is the most fragile collector in the project. Things that go wrong:

* LHM not running -> probe fails, rules disable cleanly, watchdog continues.
* LHM running but HTTP server disabled -> probe fails with a clear hint.
* Sensor names drift between LHM versions (e.g. "GPU Memory Junction" vs
  "Memory Junction" vs "GPU Hot Spot"). We pattern-match a list of known
  aliases and log which one matched at probe time.
* Voltage rails on consumer boards may be labelled by Super I/O register
  ("Voltage #2", "Voltage #4") rather than rail name -- some boards
  expose nothing useful here. We emit only the labels we can confidently
  identify, never guessing.
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

# Substrings (case-insensitive) that mark a node as belonging to a GPU
# device. LHM names devices by vendor + model ("NVIDIA GeForce RTX 5090",
# "AMD Radeon RX 7900 XTX") rather than the literal word "GPU", so a
# naive ``"gpu" in name`` check misses real cards. We scope GPU sensor
# attribution by checking any of these markers in the parent path.
_GPU_DEVICE_MARKERS: Final = (
    "gpu",
    "nvidia",
    "geforce",
    "radeon",
    " rtx ",
    " gtx ",
    "quadro",
    "tesla ",
)

# Same idea for CPUs. LHM labels CPU nodes "Intel Core i9-13900K" or
# "AMD Ryzen 9 7950X3D" rather than the literal word "CPU".
_CPU_DEVICE_MARKERS: Final = (
    "cpu",
    "intel",
    " core ",
    " core(",  # "Core(TM)"
    "amd",
    "ryzen",
    "epyc",
    "threadripper",
    "xeon",
)


def _looks_like_gpu_device(path_text: str) -> bool:
    lower = " " + path_text.lower() + " "
    return any(marker in lower for marker in _GPU_DEVICE_MARKERS)


def _looks_like_cpu_device(path_text: str) -> bool:
    lower = " " + path_text.lower() + " "
    return any(marker in lower for marker in _CPU_DEVICE_MARKERS)


# Voltage rail patterns. Each entry maps a regex to the canonical signal
# suffix we'll emit. The system. prefix is added at discovery time. We
# intentionally only enumerate well-known PSU rails -- random Super I/O
# "Voltage #N" labels are too unreliable to wire into rules, even though
# users can still see them in LHM directly.
#
# Why these matter: PSU transients (sub-millisecond +12V sag during a
# GPU power burst) are the leading cause of NVIDIA TDR / Kernel-Power 41
# events on high-end consumer cards. Logging the rail voltage at 1 Hz
# won't catch the actual sag (those happen in microseconds) but a
# *baseline* +12V that drifts below 11.7 V or above 12.5 V is a red
# flag that the rail is loaded at the edge of its regulation envelope.
_RAIL_VOLTAGE_PATTERNS: Final = (
    # Order matters: more-specific labels first so "+12V" doesn't gobble
    # "+12V VR" or similar.
    (re.compile(r"\+?12\s*v(?:olts?)?$", re.IGNORECASE), "psu_12v_volts"),
    (re.compile(r"\+?12\s*v rail", re.IGNORECASE), "psu_12v_volts"),
    (re.compile(r"\+?5\s*v(?:olts?)?$", re.IGNORECASE), "psu_5v_volts"),
    (re.compile(r"\+?5\s*v rail", re.IGNORECASE), "psu_5v_volts"),
    (re.compile(r"\+?3\.3\s*v(?:olts?)?$", re.IGNORECASE), "psu_3v3_volts"),
    (re.compile(r"\+?3\.3\s*v rail", re.IGNORECASE), "psu_3v3_volts"),
    (re.compile(r"vcore", re.IGNORECASE), "cpu_vcore_volts"),
    (re.compile(r"cpu core voltage", re.IGNORECASE), "cpu_vcore_volts"),
)


@dataclass(frozen=True, slots=True)
class _SensorPath:
    """Where in the LHM JSON tree to look up a sensor each tick."""

    signal_name: str
    full_path: str  # human-readable; for logging
    indices: tuple[int, ...]  # path through Children[i] arrays
    unit: str = "celsius"  # "celsius" | "volts" -- governs Sample.unit


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
                value=value, taken_at_ns=now, source_id=_NAME, unit=path.unit
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
        we want one per CPU socket. For PSU rail voltages we want one per
        rail label, regardless of where in the tree it lives -- some
        boards put +12V under "Mainboard / IT8688E", others under "CPU /
        Voltages". We track which devices we've matched so we don't
        double-count when LHM exposes both "Memory Junction" and "GPU
        Hot Spot" on the same device (we prefer the first pattern match,
        hence the ordered tuple above).
        """
        results: list[_SensorPath] = []
        gpu_idx = 0
        cpu_idx = 0
        # Track per-device whether we've already mapped that device's
        # junction or package sensor.
        matched_gpu_devices: set[str] = set()
        matched_cpu_devices: set[str] = set()
        # Voltages: only emit each rail name once even if multiple sensors
        # claim the same label (LHM can expose +12V from both the SuperIO
        # and an EC, with different accuracy).
        emitted_voltage_signals: set[str] = set()

        def walk(node: dict[str, Any], path_text: list[str], indices: list[int]) -> None:
            nonlocal gpu_idx, cpu_idx
            text = node.get("Text", "")
            new_path = [*path_text, text]
            full_text = " / ".join(new_path)

            # Is this leaf a sensor with a numeric value?
            if "Value" in node and isinstance(node.get("Value"), str):
                value_str = node["Value"]
                # Temperature sensors carry a degree symbol or trailing C.
                if "°C" in value_str or "C" in value_str:
                    # GPU junction match?
                    device_key = " / ".join(new_path[:-1])  # parent path
                    if _looks_like_gpu_device(device_key) and device_key not in matched_gpu_devices:
                        for pat in _VRAM_JUNCTION_PATTERNS:
                            if pat.search(full_text):
                                results.append(
                                    _SensorPath(
                                        signal_name=f"gpu.{gpu_idx}.mem_junction_temp_c",
                                        full_path=full_text,
                                        indices=tuple(indices),
                                        unit="celsius",
                                    )
                                )
                                matched_gpu_devices.add(device_key)
                                gpu_idx += 1
                                break
                    if _looks_like_cpu_device(device_key) and device_key not in matched_cpu_devices:
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
                                        unit="celsius",
                                    )
                                )
                                matched_cpu_devices.add(device_key)
                                cpu_idx += 1
                                break

                # Voltage sensors carry a "V" suffix -- but we have to
                # avoid false-positives ("Voltage" in path text is fine,
                # "10.0 W" power readings end in W). Match strictly on
                # the value string suffix.
                stripped = value_str.strip()
                if stripped.endswith("V") or stripped.endswith("v"):
                    # Match against the leaf text (the sensor's own name)
                    # rather than full_text -- otherwise the parent path
                    # containing "+12V VRM" could spuriously match a child
                    # voltage sensor.
                    leaf_text = text
                    for pat, suffix in _RAIL_VOLTAGE_PATTERNS:
                        if pat.search(leaf_text):
                            sig = f"system.{suffix}"
                            if sig in emitted_voltage_signals:
                                break
                            results.append(
                                _SensorPath(
                                    signal_name=sig,
                                    full_path=full_text,
                                    indices=tuple(indices),
                                    unit="volts",
                                )
                            )
                            emitted_voltage_signals.add(sig)
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
