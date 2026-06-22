"""Tests for the HWiNFO Shared Memory collector.

The Windows shared-memory binding (``OpenFileMappingW`` / ``MapViewOfFile``)
is exercised only by integration on a host with HWiNFO running. Everything
that matters for correctness -- header decoding, sensor/reading parsing, and
the regex-driven signal discovery -- is pure-Python and is fully covered here
by feeding the parsers a synthetic buffer laid out exactly like HWiNFO's
``HWiNFO_SENSORS_SHARED_MEM2`` region.

The parsers are the part most likely to break silently against a malformed or
unexpected buffer, so they get the bulk of the attention. The collector's
``probe()`` is checked only for the "cleanly unavailable" contract that CI
runners (no HWiNFO) and non-Windows hosts must satisfy.
"""

from __future__ import annotations

import dataclasses
import struct

import pytest

from atfield.collectors import Collector, HealthState, ProbeResult
from atfield.collectors.hwinfo import (
    _HEADER_SIZE,
    _HWINFO_SIGNATURE,
    HwinfoCollector,
    discover_signals,
    parse_header,
    parse_readings,
    parse_sensors,
)

# ---------------------------------------------------------------------------
# Synthetic HWiNFO_SENSORS_SHARED_MEM2 buffer builder
# ---------------------------------------------------------------------------

# Reading-type constants (mirror HWiNFO's SDK header).
_RT_TEMP = 0
_RT_VOLT = 1
_RT_POWER = 4

_SENSOR_STRIDE = 264   # 8 (ids) + 128 (orig) + 128 (user)
_READING_STRIDE = 292  # 12 (ids) + 128 (orig) + 128 (user) + 16 (unit) + 8 (value)


def _cstr(text: str, size: int) -> bytes:
    """NUL-terminated, NUL-padded latin-1 field of exactly ``size`` bytes."""
    raw = text.encode("latin-1")[: size - 1]
    return raw + b"\x00" * (size - len(raw))


def _sensor_row(sensor_id: int, instance: int, name_orig: str, name_user: str = "") -> bytes:
    row = struct.pack("<II", sensor_id, instance)
    row += _cstr(name_orig, 128)
    row += _cstr(name_user, 128)
    assert len(row) == _SENSOR_STRIDE
    return row


def _reading_row(
    rtype: int,
    sensor_index: int,
    reading_id: int,
    label: str,
    unit: str,
    value: float,
) -> bytes:
    row = struct.pack("<III", rtype, sensor_index, reading_id)
    row += _cstr(label, 128)   # label_orig
    row += _cstr("", 128)      # label_user (blank -> falls back to orig)
    row += _cstr(unit, 16)
    row += struct.pack("<d", value)
    assert len(row) == _READING_STRIDE
    return row


def _build_buffer(sensors: list[bytes], readings: list[bytes]) -> bytes:
    """Assemble header + sensor section + reading section into one blob."""
    sensor_section_offset = _HEADER_SIZE
    sensor_section = b"".join(sensors)
    reading_section_offset = sensor_section_offset + len(sensor_section)
    reading_section = b"".join(readings)

    header = struct.pack(
        "<IIIqIIIIII",
        _HWINFO_SIGNATURE,
        2,                       # version
        1,                       # revision
        0,                       # poll_time_filetime
        sensor_section_offset,
        _SENSOR_STRIDE,
        len(sensors),
        reading_section_offset,
        _READING_STRIDE,
        len(readings),
    )
    # parse_header reads calcsize(44) but requires >= _HEADER_SIZE bytes.
    header += b"\x00" * (_HEADER_SIZE - len(header))
    return header + sensor_section + reading_section


@pytest.fixture
def sample_buffer() -> bytes:
    """A realistic two-sensor buffer: one NVIDIA GPU, one AMD CPU.

    Readings deliberately include a power reading (which discovery should
    ignore) so the "only temp + voltage are matched" contract is exercised.
    """
    sensors = [
        _sensor_row(0, 0, "NVIDIA GeForce RTX 5090"),
        _sensor_row(1, 0, "AMD Ryzen 9 9950X3D"),
    ]
    readings = [
        _reading_row(_RT_TEMP, 0, 100, "GPU Memory Junction", "\u00b0C", 64.0),
        _reading_row(_RT_TEMP, 1, 200, "CPU Package", "\u00b0C", 55.5),
        _reading_row(_RT_VOLT, 1, 201, "+12V", "V", 12.1),
        _reading_row(_RT_POWER, 0, 101, "GPU Power", "W", 305.0),
    ]
    return _build_buffer(sensors, readings)


# ---------------------------------------------------------------------------
# parse_header
# ---------------------------------------------------------------------------


class TestParseHeader:
    def test_decodes_fields(self, sample_buffer):
        h = parse_header(sample_buffer)
        assert h.signature == _HWINFO_SIGNATURE
        assert h.version == 2
        assert h.revision == 1
        assert h.sensor_section_offset == _HEADER_SIZE
        assert h.sensor_element_size == _SENSOR_STRIDE
        assert h.sensor_count == 2
        assert h.reading_element_size == _READING_STRIDE
        assert h.reading_count == 4

    def test_rejects_bad_signature(self, sample_buffer):
        corrupt = b"XXXX" + sample_buffer[4:]
        with pytest.raises(ValueError, match="signature mismatch"):
            parse_header(corrupt)

    def test_rejects_short_buffer(self):
        with pytest.raises(ValueError, match="expects"):
            parse_header(b"\x00" * (_HEADER_SIZE - 1))


# ---------------------------------------------------------------------------
# parse_sensors / parse_readings
# ---------------------------------------------------------------------------


class TestParseSensors:
    def test_returns_all_rows(self, sample_buffer):
        h = parse_header(sample_buffer)
        sensors = parse_sensors(sample_buffer, h)
        assert [s.display_name for s in sensors] == [
            "NVIDIA GeForce RTX 5090",
            "AMD Ryzen 9 9950X3D",
        ]

    def test_user_rename_takes_precedence(self):
        buf = _build_buffer([_sensor_row(0, 0, "vendor name", "My GPU")], [])
        sensors = parse_sensors(buf, parse_header(buf))
        assert sensors[0].display_name == "My GPU"

    def test_rejects_undersized_stride(self, sample_buffer):
        h = parse_header(sample_buffer)
        bad = dataclasses.replace(h, sensor_element_size=100)
        with pytest.raises(ValueError, match="sensor element size"):
            parse_sensors(sample_buffer, bad)


class TestParseReadings:
    def test_returns_all_rows_with_values(self, sample_buffer):
        h = parse_header(sample_buffer)
        readings = parse_readings(sample_buffer, h)
        assert len(readings) == 4
        assert readings[0].display_label == "GPU Memory Junction"
        assert readings[0].reading_type == _RT_TEMP
        assert readings[0].sensor_index == 0
        assert readings[0].value == pytest.approx(64.0)
        assert readings[2].display_label == "+12V"
        assert readings[2].reading_type == _RT_VOLT
        assert readings[2].value == pytest.approx(12.1)


# ---------------------------------------------------------------------------
# discover_signals -- the regex-driven mapping to AT-Field signal names
# ---------------------------------------------------------------------------


class TestDiscoverSignals:
    def test_maps_expected_signals(self, sample_buffer):
        h = parse_header(sample_buffer)
        sensors = parse_sensors(sample_buffer, h)
        readings = parse_readings(sample_buffer, h)
        discoveries = discover_signals(sensors, readings)

        by_name = {d.signal_name: d for d in discoveries}
        # GPU junction temp, CPU package temp, +12V rail -- power is ignored.
        assert set(by_name) == {
            "gpu.0.mem_junction_temp_c",
            "system.cpu_package_temp_c",
            "system.psu_12v_volts",
        }
        assert by_name["gpu.0.mem_junction_temp_c"].unit == "celsius"
        assert by_name["system.cpu_package_temp_c"].unit == "celsius"
        assert by_name["system.psu_12v_volts"].unit == "volts"

    def test_reading_indices_point_back_to_source_rows(self, sample_buffer):
        h = parse_header(sample_buffer)
        sensors = parse_sensors(sample_buffer, h)
        readings = parse_readings(sample_buffer, h)
        discoveries = discover_signals(sensors, readings)

        # Each discovery's reading_index must address the row it came from,
        # so sample() re-reads the right value every tick.
        for d in discoveries:
            assert 0 <= d.reading_index < len(readings)
        idx = {d.signal_name: d.reading_index for d in discoveries}
        assert idx["gpu.0.mem_junction_temp_c"] == 0
        assert idx["system.cpu_package_temp_c"] == 1
        assert idx["system.psu_12v_volts"] == 2

    def test_power_readings_are_not_matched(self, sample_buffer):
        h = parse_header(sample_buffer)
        sensors = parse_sensors(sample_buffer, h)
        readings = parse_readings(sample_buffer, h)
        discoveries = discover_signals(sensors, readings)
        # The GPU power reading (reading_index 3) must not appear.
        assert all(d.reading_index != 3 for d in discoveries)

    def test_empty_inputs_yield_no_discoveries(self):
        assert discover_signals([], []) == []

    def test_second_gpu_gets_index_one(self):
        sensors = [
            _sensor_row(0, 0, "NVIDIA GeForce RTX 5090"),
            _sensor_row(1, 1, "NVIDIA GeForce RTX 5090"),
        ]
        readings = [
            _reading_row(_RT_TEMP, 0, 100, "GPU Memory Junction", "\u00b0C", 60.0),
            _reading_row(_RT_TEMP, 1, 101, "GPU Memory Junction", "\u00b0C", 70.0),
        ]
        buf = _build_buffer(sensors, readings)
        h = parse_header(buf)
        discoveries = discover_signals(parse_sensors(buf, h), parse_readings(buf, h))
        names = {d.signal_name for d in discoveries}
        assert names == {
            "gpu.0.mem_junction_temp_c",
            "gpu.1.mem_junction_temp_c",
        }


# ---------------------------------------------------------------------------
# Collector contract
# ---------------------------------------------------------------------------


class TestCollectorContract:
    def test_satisfies_protocol(self):
        c = HwinfoCollector()
        assert isinstance(c, Collector)
        assert c.name == "hwinfo"

    def test_probe_is_cleanly_unavailable_without_hwinfo(self):
        """On CI (no HWiNFO) and on non-Windows hosts the probe must report
        unavailable with a reason and never raise."""
        c = HwinfoCollector()
        result = c.probe()
        assert isinstance(result, ProbeResult)
        if not result.available:
            assert result.reason
            assert result.signals == ()
            assert c.health() is HealthState.FAILED
            # sample() must be a safe no-op when probe failed.
            assert c.sample() == {}

    def test_shutdown_is_idempotent(self):
        c = HwinfoCollector()
        c.probe()
        c.shutdown()
        c.shutdown()  # second call must not raise
        assert c.health() is HealthState.FAILED
