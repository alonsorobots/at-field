"""HWiNFO Shared Memory collector (Windows-only, autodetect).

When the user runs HWiNFO64 with its **Shared Memory Support** option
enabled (Sensors → Settings → Shared Memory Support) HWiNFO publishes
the full sensor tree to a named shared-memory section called
``Global\\HWiNFO_SENS_SM2``. AT-Field opens that section read-only,
parses the documented v2 ABI, and emits the same signals the
:mod:`atfield.collectors.lhm` collector would. When HWiNFO is running,
this collector is *preferred* over LHM:

* HWiNFO's vendor-licensed kernel driver covers more sensors than LHM's
  WinRing0 (notably +12V VR / VRM voltages on most Z-/X-series boards
  where Super I/O probes alone are blind).
* HWiNFO is well-known and trusted by Windows enthusiasts who run ML
  workloads — many of them already have it open. Reading their existing
  feed is a free-of-friction win compared to bundling our own LHM.

Why we don't *bundle* HWiNFO
----------------------------
HWiNFO's license forbids redistribution. We can read its public SHM
interface (which the author explicitly designed for third-party
consumption — see the SDK at ``hwinfo.com/forum/``), but we can't ship
the binary. So this collector is opportunistic — present when the user
has HWiNFO running, otherwise the bundled LHM remains the source.

ABI we parse
------------
Header (52 bytes, little-endian, V2 layout used by HWiNFO 6.x+)::

    DWORD  Signature          // 'HWiS' = 0x53694857
    DWORD  Version            // 2
    DWORD  Revision
    INT64  PollTime           // FILETIME of last poll
    DWORD  OffsetOfSensorSection
    DWORD  SizeOfSensorElement
    DWORD  NumSensorElements
    DWORD  OffsetOfReadingSection
    DWORD  SizeOfReadingElement
    DWORD  NumReadingElements

Sensor element (288 bytes)::

    DWORD SensorID
    DWORD SensorInst
    char  SensorNameOrig[128]
    char  SensorNameUser[128]

Reading element (variable, but 65-byte header + payload, total 344
bytes in HWiNFO 6.x+)::

    DWORD  ReadingType  // 0=temp 1=volt 2=fan 3=current 4=power
                        // 5=clock 6=usage 7=other
    DWORD  SensorIndex  // index into sensor table
    DWORD  ReadingID
    char   LabelOrig[128]
    char   LabelUser[128]
    char   Unit[16]
    DOUBLE Value
    DOUBLE ValueMin
    DOUBLE ValueMax
    DOUBLE ValueAvg

The format has been frozen since HWiNFO 6.40 (2020); we tolerate
unknown trailing fields by reading exactly ``SizeOfReadingElement``
bytes per row and ignoring anything past our struct.

Why ctypes not :mod:`mmap`
--------------------------
Python's :mod:`mmap` on Windows can open named shared-memory sections
via the ``tagname`` argument, but only when the size is known in
advance. HWiNFO's section size depends on the number of sensors
detected at HWiNFO startup; we don't know it before we look. The
clean path is :func:`OpenFileMappingW` + :func:`MapViewOfFile` from
``kernel32.dll``, which gives us the full mapping irrespective of
size. This is the same pattern the official HWiNFO SDK examples use.
"""

from __future__ import annotations

import ctypes
import logging
import struct
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import Final

from atfield.collectors import HealthState, ProbeResult

# Reuse the LHM collector's regex patterns + device markers verbatim.
# HWiNFO and LHM use very similar labelling conventions, so the same
# patterns work; if HWiNFO drifts we'll add HWiNFO-specific overrides
# rather than duplicate the (much larger) LHM list.
from atfield.collectors.lhm import (
    _CPU_PACKAGE_PATTERNS,
    _RAIL_VOLTAGE_PATTERNS,
    _VRAM_JUNCTION_PATTERNS,
    _looks_like_cpu_device,
    _looks_like_gpu_device,
)
from atfield.signals import Sample, monotonic_ns

__all__ = ["HwinfoCollector", "parse_header", "parse_readings", "parse_sensors"]


_log = logging.getLogger("atfield.collectors.hwinfo")

_NAME: Final = "hwinfo"
_SECTION_NAME: Final = r"Global\HWiNFO_SENS_SM2"
_HWINFO_SIGNATURE: Final = 0x53694857  # "HWiS" in little-endian
_HEADER_SIZE: Final = 52
# Conservative upper bound; the section is typically 200-500 KB, never
# more than a few MB even on monster boards. We map a 16 MB window to
# be safe -- pages are committed lazily so unused tail costs nothing.
_MAX_VIEW_SIZE: Final = 16 * 1024 * 1024


# ---------------------------------------------------------------------------
# Pure-Python parsers (Windows-independent, fully unit-testable)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Header:
    """Decoded HWiNFO_SENSORS_SHARED_MEM2 header."""

    signature: int
    version: int
    revision: int
    poll_time_filetime: int
    sensor_section_offset: int
    sensor_element_size: int
    sensor_count: int
    reading_section_offset: int
    reading_element_size: int
    reading_count: int


@dataclass(frozen=True, slots=True)
class _Sensor:
    """Decoded HWiNFO_SENSORS_SENSOR_ELEMENT."""

    sensor_id: int
    sensor_instance: int
    name_orig: str
    name_user: str

    @property
    def display_name(self) -> str:
        """Prefer the user-renamed label when present (HWiNFO lets users
        rename sensors); fall back to the vendor-supplied name."""
        return self.name_user.strip() or self.name_orig.strip()


@dataclass(frozen=True, slots=True)
class _Reading:
    """Decoded HWiNFO_SENSORS_READING_ELEMENT."""

    reading_type: int
    sensor_index: int
    reading_id: int
    label_orig: str
    label_user: str
    unit: str
    value: float

    @property
    def display_label(self) -> str:
        return self.label_user.strip() or self.label_orig.strip()


# Reading-type constants from HWiNFO's SDK header.
_RT_TEMP: Final = 0
_RT_VOLT: Final = 1
_RT_POWER: Final = 4


def parse_header(buf: bytes) -> _Header:
    """Decode the first :data:`_HEADER_SIZE` bytes of an HWiNFO SHM view.

    Raises :class:`ValueError` when the signature doesn't match
    ``"HWiS"`` -- protects against accidentally pointing this at
    a non-HWiNFO mapping.
    """
    if len(buf) < _HEADER_SIZE:
        raise ValueError(
            f"HWiNFO header expects >= {_HEADER_SIZE} bytes, got {len(buf)}"
        )
    fields = struct.unpack_from("<IIIqIIIIII", buf, 0)
    sig = fields[0]
    if sig != _HWINFO_SIGNATURE:
        raise ValueError(
            f"HWiNFO signature mismatch: expected {_HWINFO_SIGNATURE:#x}, "
            f"got {sig:#x} -- target is not an HWiNFO_SENS_SM2 mapping"
        )
    return _Header(
        signature=fields[0],
        version=fields[1],
        revision=fields[2],
        poll_time_filetime=fields[3],
        sensor_section_offset=fields[4],
        sensor_element_size=fields[5],
        sensor_count=fields[6],
        reading_section_offset=fields[7],
        reading_element_size=fields[8],
        reading_count=fields[9],
    )


def _decode_cstring(raw: bytes) -> str:
    """HWiNFO's char[N] fields are NUL-terminated CP1252-encoded strings.

    We use latin-1 (a superset for our purposes) so we never raise on
    decode -- some board vendors put rare characters in sensor labels.
    """
    end = raw.find(b"\x00")
    if end >= 0:
        raw = raw[:end]
    return raw.decode("latin-1", errors="replace")


def parse_sensors(buf: bytes, header: _Header) -> list[_Sensor]:
    """Walk the sensor section and return one :class:`_Sensor` per row.

    Tolerates ``sensor_element_size > 264`` (the documented size) by
    skipping any trailing bytes per row -- HWiNFO has historically
    appended fields without breaking the prefix layout.
    """
    out: list[_Sensor] = []
    base = header.sensor_section_offset
    stride = header.sensor_element_size
    if stride < 264:
        raise ValueError(
            f"HWiNFO sensor element size {stride} < documented 264"
        )
    for i in range(header.sensor_count):
        off = base + i * stride
        if off + 264 > len(buf):
            raise ValueError(
                f"HWiNFO sensor row {i} extends past mapped region"
            )
        sensor_id, sensor_inst = struct.unpack_from("<II", buf, off)
        name_orig = _decode_cstring(buf[off + 8 : off + 8 + 128])
        name_user = _decode_cstring(buf[off + 8 + 128 : off + 8 + 256])
        out.append(_Sensor(
            sensor_id=sensor_id,
            sensor_instance=sensor_inst,
            name_orig=name_orig,
            name_user=name_user,
        ))
    return out


def parse_readings(buf: bytes, header: _Header) -> list[_Reading]:
    """Walk the reading section and return one :class:`_Reading` per row."""
    out: list[_Reading] = []
    base = header.reading_section_offset
    stride = header.reading_element_size
    # Documented prefix layout = 12 + 128 + 128 + 16 = 284 bytes header,
    # then 8 bytes value (we ignore Min/Max/Avg trailing 24 bytes for
    # speed; rules read the live value).
    prefix_size = 284 + 8
    if stride < prefix_size:
        raise ValueError(
            f"HWiNFO reading element size {stride} < {prefix_size}"
        )
    for i in range(header.reading_count):
        off = base + i * stride
        if off + prefix_size > len(buf):
            raise ValueError(
                f"HWiNFO reading row {i} extends past mapped region"
            )
        rtype, sidx, rid = struct.unpack_from("<III", buf, off)
        label_orig = _decode_cstring(buf[off + 12 : off + 12 + 128])
        label_user = _decode_cstring(buf[off + 140 : off + 140 + 128])
        unit_raw = _decode_cstring(buf[off + 268 : off + 268 + 16])
        (value,) = struct.unpack_from("<d", buf, off + 284)
        out.append(_Reading(
            reading_type=rtype,
            sensor_index=sidx,
            reading_id=rid,
            label_orig=label_orig,
            label_user=label_user,
            unit=unit_raw,
            value=value,
        ))
    return out


# ---------------------------------------------------------------------------
# Signal discovery: map (sensor, reading) tuples to AT-Field signal names
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Discovery:
    """A reading we matched to an AT-Field signal name. The
    ``reading_index`` is the row in the readings table to re-read on
    each :meth:`HwinfoCollector.sample` call, so we do the regex
    matching exactly once at probe time."""

    signal_name: str
    reading_index: int
    unit: str  # "celsius" | "volts" | "watts" | other


def discover_signals(
    sensors: list[_Sensor],
    readings: list[_Reading],
) -> list[_Discovery]:
    """Match HWiNFO readings against the LHM-derived patterns.

    Returns one :class:`_Discovery` per matched reading. GPU and CPU
    devices are enumerated by index (gpu.0, gpu.1, ...) in the order
    HWiNFO reports them.
    """
    out: list[_Discovery] = []
    matched_gpu_devices: dict[int, int] = {}   # sensor_idx -> gpu_n
    matched_cpu_devices: dict[int, int] = {}
    matched_voltage_signals: set[str] = set()  # one per rail name

    for reading_idx, r in enumerate(readings):
        if r.sensor_index < 0 or r.sensor_index >= len(sensors):
            continue
        device = sensors[r.sensor_index]
        device_text = device.display_name
        full_text = f"{device_text} {r.display_label}".strip()

        # Temperatures
        if r.reading_type == _RT_TEMP:
            # GPU memory junction
            if _looks_like_gpu_device(device_text) and any(
                p.search(full_text) for p in _VRAM_JUNCTION_PATTERNS
            ):
                if r.sensor_index not in matched_gpu_devices:
                    matched_gpu_devices[r.sensor_index] = len(matched_gpu_devices)
                gpu_n = matched_gpu_devices[r.sensor_index]
                out.append(_Discovery(
                    signal_name=f"gpu.{gpu_n}.mem_junction_temp_c",
                    reading_index=reading_idx,
                    unit="celsius",
                ))
                continue
            # CPU package
            if _looks_like_cpu_device(device_text) and any(
                p.search(full_text) for p in _CPU_PACKAGE_PATTERNS
            ):
                if r.sensor_index not in matched_cpu_devices:
                    matched_cpu_devices[r.sensor_index] = len(matched_cpu_devices)
                cpu_n = matched_cpu_devices[r.sensor_index]
                # Multi-socket boxes: cpu0 vs cpuN naming -- matches
                # the LHM collector's convention.
                signal = (
                    "system.cpu_package_temp_c"
                    if cpu_n == 0
                    else f"system.cpu{cpu_n}_package_temp_c"
                )
                out.append(_Discovery(
                    signal_name=signal,
                    reading_index=reading_idx,
                    unit="celsius",
                ))
                continue

        # Voltage rails
        if r.reading_type == _RT_VOLT:
            for pattern, suffix in _RAIL_VOLTAGE_PATTERNS:
                if pattern.search(r.display_label):
                    signal_name = f"system.{suffix}"
                    if signal_name in matched_voltage_signals:
                        # First match wins; HWiNFO often reports the
                        # same rail through multiple Super I/O / VR
                        # probes and we don't want to fight ourselves
                        # over which one is canonical.
                        break
                    matched_voltage_signals.add(signal_name)
                    out.append(_Discovery(
                        signal_name=signal_name,
                        reading_index=reading_idx,
                        unit="volts",
                    ))
                    break
    return out


# ---------------------------------------------------------------------------
# Windows shared memory binding (kept tightly scoped; tested via integration)
# ---------------------------------------------------------------------------


_INVALID_HANDLE_VALUE: Final = ctypes.c_void_p(-1).value
_FILE_MAP_READ: Final = 0x0004


def _is_windows() -> bool:
    return sys.platform == "win32"


def _open_shm() -> tuple[int | None, ctypes.c_void_p | None, str]:
    """Open the HWiNFO shared-memory section.

    Returns ``(handle, view_address, error_message)``. On success
    ``error_message`` is empty. On failure both handle and view are
    None and ``error_message`` describes why.

    Caller is responsible for ``UnmapViewOfFile`` + ``CloseHandle``
    via :func:`_close_shm`.
    """
    if not _is_windows():
        return None, None, "HWiNFO Shared Memory is Windows-only"

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenFileMappingW.restype = wintypes.HANDLE
    kernel32.MapViewOfFile.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t,
    ]
    kernel32.MapViewOfFile.restype = ctypes.c_void_p

    handle = kernel32.OpenFileMappingW(_FILE_MAP_READ, False, _SECTION_NAME)
    if not handle:
        err = ctypes.get_last_error() or ctypes.GetLastError()
        # ERROR_FILE_NOT_FOUND (2) = HWiNFO not running OR shared-memory
        # support not enabled in HWiNFO settings. Both are expected, not
        # bug states; the probe surfaces them with actionable text.
        if err in (0, 2):
            return None, None, (
                "HWiNFO not running, or 'Shared Memory Support' is "
                "disabled in HWiNFO Sensors → Settings."
            )
        return None, None, f"OpenFileMappingW failed: WinError {err}"

    view = kernel32.MapViewOfFile(handle, _FILE_MAP_READ, 0, 0, 0)
    if not view:
        err = ctypes.get_last_error() or ctypes.GetLastError()
        kernel32.CloseHandle(handle)
        return None, None, f"MapViewOfFile failed: WinError {err}"
    return handle, ctypes.c_void_p(view), ""


def _close_shm(handle: int | None, view: ctypes.c_void_p | None) -> None:
    if not _is_windows():
        return
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    if view is not None:
        kernel32.UnmapViewOfFile(view)
    if handle:
        kernel32.CloseHandle(handle)


def _read_view(view: ctypes.c_void_p, length: int) -> bytes:
    """Copy ``length`` bytes out of the mapped view into a Python bytes."""
    return ctypes.string_at(view, length)


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


class HwinfoCollector:
    """Collector that reads HWiNFO64's named shared-memory sensor feed."""

    name: Final = _NAME

    def __init__(self) -> None:
        self._health = HealthState.UNPROBED
        self._discoveries: list[_Discovery] = []
        # We keep the SHM mapping open across ticks. HWiNFO updates the
        # shared memory in-place on its own poll interval (default 2s);
        # re-opening on every sample wastes syscalls. The handle is
        # released in shutdown().
        self._handle: int | None = None
        self._view: ctypes.c_void_p | None = None
        self._consecutive_failures = 0
        self._max_consecutive = 3

    # -- Probe -------------------------------------------------------------

    def probe(self) -> ProbeResult:
        if not _is_windows():
            self._health = HealthState.FAILED
            return ProbeResult(
                available=False,
                reason="HWiNFO Shared Memory is Windows-only",
                signals=(),
            )

        handle, view, err = _open_shm()
        if handle is None or view is None:
            self._health = HealthState.FAILED
            return ProbeResult(available=False, reason=err, signals=())

        try:
            # Read the header to learn how big the actual section is.
            header_bytes = _read_view(view, _HEADER_SIZE)
            header = parse_header(header_bytes)
            section_end = max(
                header.sensor_section_offset + header.sensor_element_size * header.sensor_count,
                header.reading_section_offset + header.reading_element_size * header.reading_count,
            )
            full_bytes = _read_view(view, min(section_end, _MAX_VIEW_SIZE))
            sensors = parse_sensors(full_bytes, header)
            readings = parse_readings(full_bytes, header)
        except (ValueError, OSError) as exc:
            _close_shm(handle, view)
            self._health = HealthState.FAILED
            return ProbeResult(
                available=False,
                reason=f"HWiNFO SHM parse failed: {exc}",
                signals=(),
            )

        self._discoveries = discover_signals(sensors, readings)
        self._handle = handle
        self._view = view
        self._health = HealthState.HEALTHY
        sig_names = tuple(d.signal_name for d in self._discoveries)
        return ProbeResult(
            available=True,
            reason=(
                f"HWiNFO SHM v{header.version}.{header.revision}, "
                f"{header.sensor_count} sensors, {header.reading_count} "
                f"readings, {len(sig_names)} matched"
            ),
            signals=sig_names,
            metadata={
                "sensor_count": str(header.sensor_count),
                "reading_count": str(header.reading_count),
                "version": f"{header.version}.{header.revision}",
            },
        )

    # -- Sample ------------------------------------------------------------

    def sample(self) -> dict[str, Sample]:
        if self._health is HealthState.FAILED or self._view is None:
            return {}

        try:
            header_bytes = _read_view(self._view, _HEADER_SIZE)
            header = parse_header(header_bytes)
            section_end = (
                header.reading_section_offset
                + header.reading_element_size * header.reading_count
            )
            buf = _read_view(self._view, min(section_end, _MAX_VIEW_SIZE))
        except (ValueError, OSError) as exc:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._max_consecutive:
                self._health = HealthState.DEGRADED
                _log.warning("HWiNFO sample failed %dx: %s",
                             self._consecutive_failures, exc)
            return {}

        out: dict[str, Sample] = {}
        ts = monotonic_ns()
        # The reading layout can shift if HWiNFO re-enumerates sensors
        # (rare; happens on plug/unplug events). We trust the indices
        # we discovered at probe time but bound-check before reading.
        for d in self._discoveries:
            base = (
                header.reading_section_offset
                + d.reading_index * header.reading_element_size
            )
            value_off = base + 284
            if value_off + 8 > len(buf):
                continue
            (value,) = struct.unpack_from("<d", buf, value_off)
            out[d.signal_name] = Sample(
                value=float(value),
                taken_at_ns=ts,
                source_id=_NAME,
                unit=d.unit,
            )
        if out:
            self._consecutive_failures = 0
            if self._health is HealthState.DEGRADED:
                self._health = HealthState.HEALTHY
        return out

    def health(self) -> HealthState:
        return self._health

    def shutdown(self) -> None:
        """Release the shared-memory mapping. Idempotent (Collector protocol)."""
        _close_shm(self._handle, self._view)
        self._handle = None
        self._view = None
        self._health = HealthState.FAILED
