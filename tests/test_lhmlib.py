"""Tests for :mod:`atfield.collectors.lhmlib`.

These exercise the pure mapping/discovery logic (no real subprocess) plus
helper discovery and graceful-probe-failure paths. The subprocess plumbing
is covered by the live end-to-end run during development; here we lock down
the parsing/mapping that turns helper JSON into AT-Field signals.
"""

from __future__ import annotations

from pathlib import Path

from atfield.collectors import HealthState
from atfield.collectors.lhmlib import (
    LhmLibCollector,
    _hw_prefix,
    find_sensor_helper,
)


def _sensor(id, hwType, name, type, value, hw="Dev", hwId=None):
    return {
        "id": id,
        "hw": hw,
        "hwId": hwId,
        "hwType": hwType,
        "name": name,
        "type": type,
        "value": value,
    }


class TestHwPrefix:
    def test_extracts_device_path(self):
        assert _hw_prefix("/gpu-nvidia/0/temperature/3") == "/gpu-nvidia/0"
        assert _hw_prefix("/amdcpu/0/temperature/2") == "/amdcpu/0"

    def test_short_id_returned_as_is(self):
        assert _hw_prefix("/lpc") == "/lpc"


class TestUsableValue:
    def test_none_and_nonpositive_rejected(self):
        f = LhmLibCollector._usable_value
        assert f({"value": None}, "celsius") is None
        assert f({"value": 0.0}, "celsius") is None
        assert f({"value": -5}, "celsius") is None
        assert f({"value": "nan-ish"}, "celsius") is None

    def test_positive_accepted(self):
        f = LhmLibCollector._usable_value
        assert f({"value": 38.0}, "celsius") == 38.0
        assert f({"value": 12.1}, "volts") == 12.1


class TestDiscover:
    def _collector(self):
        # exe path need not exist for pure-discovery tests.
        return LhmLibCollector(exe_path=Path("nonexistent.exe"))

    def test_two_identical_gpus_get_distinct_indices(self):
        """Regression: two RTX 5090s share the name 'NVIDIA GeForce RTX
        5090' -- they must map to gpu.0 and gpu.1, deduped by hardware
        identifier, not name."""
        c = self._collector()
        sensors = [
            _sensor("/gpu-nvidia/0/temperature/3", "GpuNvidia", "GPU Memory Junction",
                    "Temperature", 34.0, hw="NVIDIA GeForce RTX 5090", hwId="/gpu-nvidia/0"),
            _sensor("/gpu-nvidia/1/temperature/3", "GpuNvidia", "GPU Memory Junction",
                    "Temperature", 38.0, hw="NVIDIA GeForce RTX 5090", hwId="/gpu-nvidia/1"),
        ]
        m = c._discover(sensors)
        sigs = sorted(sig for sig, _ in m.values())
        assert sigs == ["gpu.0.mem_junction_temp_c", "gpu.1.mem_junction_temp_c"]

    def test_dedup_falls_back_to_id_prefix_without_hwId(self):
        c = self._collector()
        sensors = [
            _sensor("/gpu-nvidia/0/temperature/3", "GpuNvidia", "GPU Memory Junction",
                    "Temperature", 34.0, hw="RTX 5090", hwId=None),
            _sensor("/gpu-nvidia/1/temperature/3", "GpuNvidia", "GPU Memory Junction",
                    "Temperature", 38.0, hw="RTX 5090", hwId=None),
        ]
        m = c._discover(sensors)
        assert len(m) == 2

    def test_cpu_package_mapped(self):
        c = self._collector()
        sensors = [
            _sensor("/amdcpu/0/temperature/2", "Cpu", "Core (Tctl/Tdie)",
                    "Temperature", 52.3, hw="AMD Ryzen 9 9950X3D", hwId="/amdcpu/0"),
        ]
        m = c._discover(sensors)
        assert list(m.values()) == [("system.cpu_package_temp_c", "celsius")]

    def test_cpu_temp_zero_not_mapped(self):
        """Non-elevated => MSR driver absent => Tctl/Tdie reads 0; we must
        not advertise a bogus CPU temp signal."""
        c = self._collector()
        sensors = [
            _sensor("/amdcpu/0/temperature/2", "Cpu", "Core (Tctl/Tdie)",
                    "Temperature", 0.0, hw="AMD Ryzen", hwId="/amdcpu/0"),
        ]
        assert c._discover(sensors) == {}

    def test_voltage_rail_mapped_once(self):
        c = self._collector()
        sensors = [
            _sensor("/lpc/it8689e/voltage/1", "Motherboard", "+12V",
                    "Voltage", 12.1, hwId="/lpc/it8689e"),
            _sensor("/lpc/it8689e/voltage/9", "Motherboard", "+12V",
                    "Voltage", 12.0, hwId="/lpc/it8689e"),
        ]
        m = c._discover(sensors)
        sigs = [sig for sig, _ in m.values()]
        assert sigs == ["system.psu_12v_volts"]


class TestFindHelper:
    def test_env_override(self, tmp_path, monkeypatch):
        exe = tmp_path / "atfield-sensors.exe"
        exe.write_bytes(b"MZ")
        monkeypatch.setenv("ATFIELD_SENSOR_EXE", str(exe))
        assert find_sensor_helper() == exe

    def test_env_missing_file_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATFIELD_SENSOR_EXE", str(tmp_path / "nope.exe"))
        # Should not raise; returns None or a discovered path, never the bad one.
        result = find_sensor_helper()
        assert result != (tmp_path / "nope.exe")


class TestProbeFailsGracefully:
    def test_missing_helper_returns_unavailable_not_raise(self, monkeypatch):
        monkeypatch.delenv("ATFIELD_SENSOR_EXE", raising=False)
        c = LhmLibCollector(exe_path=Path("does-not-exist.exe"))
        pr = c.probe()
        assert pr.available is False
        assert "not found" in pr.reason
        assert c.health() == HealthState.FAILED
        assert c.sample() == {}
        c.shutdown()
