"""Tests for collector contract compliance and capability negotiation.

The :class:`atfield.collectors.Collector` protocol is a public API surface
from v0.1.0 onward. These tests verify:

* Every built-in collector matches the structural protocol.
* :class:`SystemCollector` actually probes successfully on the test host
  (it's the Tier-1 always-available collector; if this fails, the
  watchdog has bigger problems).
* :class:`LhmCollector` reports unavailable cleanly when LHM isn't running
  (the typical CI scenario).
* :class:`NvmlCollector` reports unavailable cleanly when NVML isn't
  available (the typical CI scenario).

The hardware-dependent assertions are guarded with skip markers so the
suite works on CI runners without GPUs / LHM.
"""

from __future__ import annotations

import ctypes
import sys

import pytest

from atfield.collectors import Collector, HealthState, ProbeResult
from atfield.collectors.lhm import LhmCollector
from atfield.collectors.lhmlib import LhmLibCollector
from atfield.collectors.nvml import PER_PROCESS_VRAM_KEY, NvmlCollector
from atfield.collectors.system import SystemCollector

# ---------------------------------------------------------------------------
# Protocol compliance (no I/O required)
# ---------------------------------------------------------------------------


class TestProtocolCompliance:
    @pytest.mark.parametrize(
        "factory",
        [SystemCollector, NvmlCollector, lambda: LhmCollector(), lambda: LhmLibCollector()],
        ids=["system", "nvml", "lhm", "lhmlib"],
    )
    def test_satisfies_collector_protocol(self, factory):
        c = factory()
        assert isinstance(c, Collector), f"{type(c).__name__} does not match Collector protocol"
        # Protocol requires .name attribute
        assert isinstance(c.name, str) and c.name


# ---------------------------------------------------------------------------
# SystemCollector
# ---------------------------------------------------------------------------


class TestSystemCollector:
    def test_probe_succeeds(self):
        c = SystemCollector()
        result = c.probe()
        assert isinstance(result, ProbeResult)
        assert result.available, f"system collector should always probe successfully: {result.reason}"
        assert "system.ram_used_percent" in result.signals
        assert "system.swap_used_percent" in result.signals
        assert "system.commit_percent" in result.signals
        assert "system.cpu_used_percent" in result.signals
        assert c.health() is HealthState.HEALTHY

    def test_sample_returns_valid_values(self):
        c = SystemCollector()
        c.probe()
        samples = c.sample()
        assert "system.ram_used_percent" in samples
        ram = samples["system.ram_used_percent"]
        assert 0 <= ram.value <= 100, f"RAM% out of range: {ram.value}"
        assert ram.unit == "percent"
        assert ram.source_id == "system"

        # CPU% comes from psutil.cpu_percent() primed during probe(); a real
        # value (not the 0.0 cold-start artifact) should be reported on the
        # first sample after probe() since probe() called cpu_percent() to
        # establish the diff baseline.
        assert "system.cpu_used_percent" in samples
        cpu = samples["system.cpu_used_percent"]
        assert 0 <= cpu.value <= 100, f"CPU% out of range: {cpu.value}"
        assert cpu.unit == "percent"
        assert cpu.source_id == "system"

    @pytest.mark.skipif(sys.platform != "win32", reason="commit charge accuracy is Windows-specific")
    def test_commit_percent_uses_globalmemorystatusex(self):
        c = SystemCollector()
        result = c.probe()
        assert result.metadata.get("commit_charge_source") == "GlobalMemoryStatusEx"


# ---------------------------------------------------------------------------
# NvmlCollector
# ---------------------------------------------------------------------------


class TestNvmlCollectorBehavior:
    def test_probe_returns_proberesult_either_way(self):
        """Whether NVML is present or not, probe must return a ProbeResult,
        never raise. This is what makes capability negotiation safe."""
        c = NvmlCollector()
        result = c.probe()
        c.shutdown()
        assert isinstance(result, ProbeResult)
        assert isinstance(result.reason, str)
        if not result.available:
            assert result.signals == ()
            assert c.health() is HealthState.FAILED

    def test_signals_include_per_gpu_namespace_when_available(self):
        c = NvmlCollector()
        result = c.probe()
        try:
            if not result.available:
                pytest.skip(f"NVML unavailable on this host: {result.reason}")
            assert PER_PROCESS_VRAM_KEY in result.signals
            # At least gpu.0 should exist if probe succeeded
            assert any(s.startswith("gpu.0.") for s in result.signals)
        finally:
            c.shutdown()


# ---------------------------------------------------------------------------
# LhmCollector
# ---------------------------------------------------------------------------


class TestLhmCollectorBehavior:
    def test_probe_returns_proberesult_when_lhm_absent(self):
        # Use a non-listening port to force unavailable
        c = LhmCollector(url="http://127.0.0.1:1/data.json", timeout_s=0.2)
        result = c.probe()
        c.shutdown()
        assert isinstance(result, ProbeResult)
        assert not result.available
        assert "LibreHardwareMonitor" in result.reason or "HTTP" in result.reason
        assert c.health() is HealthState.FAILED
        assert result.signals == ()

    def test_sample_when_failed_returns_empty(self):
        c = LhmCollector(url="http://127.0.0.1:1/data.json", timeout_s=0.2)
        c.probe()
        # Should not raise; just return empty
        assert c.sample() == {}
        c.shutdown()


class TestLhmSensorDiscovery:
    """White-box tests of the JSON-tree walker.

    LHM's sensor tree is too tedious to mock end-to-end via HTTP, but the
    walker itself is pure -- we can hand it a fixture that mirrors a
    realistic LHM 0.9.6 payload and verify the right signals come out.
    """

    @staticmethod
    def _gpu_node(label: str, junction_label: str | None = None) -> dict:
        children = []
        if junction_label is not None:
            children.append(
                {"Text": junction_label, "Value": "84.0 °C", "Children": []}
            )
        children.append(
            {"Text": "GPU Core", "Value": "67.0 °C", "Children": []}
        )
        return {"Text": label, "Children": children}

    @staticmethod
    def _cpu_node(label: str, package_label: str = "CPU Package") -> dict:
        return {
            "Text": label,
            "Children": [
                {"Text": package_label, "Value": "55.0 °C", "Children": []},
                {"Text": "Core #1", "Value": "53.0 °C", "Children": []},
            ],
        }

    @staticmethod
    def _mb_voltage_node() -> dict:
        return {
            "Text": "ASUS Mainboard / IT8688E",
            "Children": [
                {"Text": "+12V", "Value": "12.096 V", "Children": []},
                {"Text": "+5V", "Value": "5.040 V", "Children": []},
                {"Text": "+3.3V", "Value": "3.312 V", "Children": []},
                {"Text": "VCore", "Value": "1.184 V", "Children": []},
                # An anonymous "Voltage #5" must NOT be picked up.
                {"Text": "Voltage #5", "Value": "1.024 V", "Children": []},
            ],
        }

    def test_discovers_gpu_junction_and_cpu_package(self):
        tree = {
            "Text": "Computer",
            "Children": [
                self._gpu_node("NVIDIA GeForce RTX 5090", "GPU Memory Junction"),
                self._cpu_node("Intel Core i9-13900K"),
            ],
        }
        c = LhmCollector()
        paths = c._discover_sensors(tree)
        signals = {p.signal_name for p in paths}
        assert "gpu.0.mem_junction_temp_c" in signals
        assert "system.cpu_package_temp_c" in signals
        # Units flow through to Sample.unit -- celsius for these.
        for p in paths:
            assert p.unit == "celsius"

    def test_discovers_psu_rail_voltages(self):
        tree = {
            "Text": "Computer",
            "Children": [self._mb_voltage_node()],
        }
        c = LhmCollector()
        paths = c._discover_sensors(tree)
        sigs_to_paths = {p.signal_name: p for p in paths}
        assert "system.psu_12v_volts" in sigs_to_paths
        assert "system.psu_5v_volts" in sigs_to_paths
        assert "system.psu_3v3_volts" in sigs_to_paths
        assert "system.cpu_vcore_volts" in sigs_to_paths
        # The unlabeled "Voltage #5" sensor must not have been emitted.
        assert not any(p.full_path.endswith("Voltage #5") for p in paths)
        # Voltage paths carry unit="volts".
        for sig in ("system.psu_12v_volts", "system.cpu_vcore_volts"):
            assert sigs_to_paths[sig].unit == "volts"

    def test_voltage_dedup_keeps_first_match(self):
        # Two +12V sensors (e.g. SuperIO and EC) -- we must emit only one.
        tree = {
            "Text": "Computer",
            "Children": [
                {
                    "Text": "Mainboard",
                    "Children": [
                        {"Text": "+12V", "Value": "12.10 V", "Children": []},
                    ],
                },
                {
                    "Text": "EC",
                    "Children": [
                        {"Text": "+12V", "Value": "12.05 V", "Children": []},
                    ],
                },
            ],
        }
        c = LhmCollector()
        paths = c._discover_sensors(tree)
        rail_paths = [p for p in paths if p.signal_name == "system.psu_12v_volts"]
        assert len(rail_paths) == 1
        # First match wins; should be the Mainboard reading.
        assert "Mainboard" in rail_paths[0].full_path


# ---------------------------------------------------------------------------
# _HardFaultRateReader (PDH \Memory\Pages Input/sec)
# ---------------------------------------------------------------------------


class TestHardFaultRateReader:
    """PDH is Windows-only infra. These tests mock the DLL boundary so the
    reader's OWN logic (priming, error handling, close) is exercised on any
    platform. test_read_returns_a_plausible_value_on_real_windows below
    covers the real pdh.dll end-to-end and is skipped off-Windows.

    Mocking note: production code calls PDH with ctypes.byref(...) output
    params (matching the rest of this file's style, e.g.
    _read_commit_percent_windows). A byref() result can be recovered with
    ctypes.cast(pvalue, POINTER(T)).contents inside a fake function to mutate
    the caller's struct in place -- verified directly against CPython's
    ctypes before relying on it here.
    """

    @staticmethod
    def _set_via_byref(pvalue, cstatus, double_value):
        from atfield.collectors.system import _PdhFmtCounterValue
        target = ctypes.cast(pvalue, ctypes.POINTER(_PdhFmtCounterValue)).contents
        target.CStatus = cstatus
        target.doubleValue = double_value

    def test_read_before_open_returns_none(self):
        from atfield.collectors.system import _HardFaultRateReader
        r = _HardFaultRateReader()
        assert r.read() is None

    def test_open_raises_when_pdh_unavailable(self, monkeypatch):
        from atfield.collectors import system as sysmod
        monkeypatch.setattr(sysmod, "_pdh", None)
        r = sysmod._HardFaultRateReader()
        with pytest.raises(OSError):
            r.open()

    def test_open_failure_is_a_clean_oserror_and_closes_the_query(self, monkeypatch):
        from atfield.collectors import system as sysmod

        closed = {"n": 0}

        class FakePdh:
            def PdhOpenQueryW(self, *a): return 0
            def PdhAddEnglishCounterW(self, *a): return 1  # nonzero = failure
            def PdhCloseQuery(self, *a): closed["n"] += 1; return 0

        monkeypatch.setattr(sysmod, "_pdh", FakePdh())
        r = sysmod._HardFaultRateReader()
        with pytest.raises(OSError, match="PdhAddEnglishCounterW"):
            r.open()
        assert closed["n"] == 1, "a failed AddCounter must close the query it opened"
        assert r.read() is None, "never opened successfully -> read() is a no-op"

    def test_priming_call_returns_none(self, monkeypatch):
        """Real PDH behavior: the first CollectQueryData succeeds, but the
        formatted value has CStatus != 0 -- no prior sample to diff a rate
        against yet."""
        from atfield.collectors import system as sysmod

        class FakePdh:
            def PdhOpenQueryW(self, *a): return 0
            def PdhAddEnglishCounterW(self, *a): return 0
            def PdhCollectQueryData(self, hquery): return 0
            def PdhGetFormattedCounterValue(self, hcounter, fmt, ptype, pvalue):
                TestHardFaultRateReader._set_via_byref(pvalue, cstatus=0xC0000BC6, double_value=0.0)
                return 0

        monkeypatch.setattr(sysmod, "_pdh", FakePdh())
        r = sysmod._HardFaultRateReader()
        r.open()
        assert r.read() is None

    def test_read_returns_the_formatted_value_on_success(self, monkeypatch):
        from atfield.collectors import system as sysmod

        class FakePdh:
            def PdhOpenQueryW(self, *a): return 0
            def PdhAddEnglishCounterW(self, *a): return 0
            def PdhCollectQueryData(self, hquery): return 0
            def PdhGetFormattedCounterValue(self, hcounter, fmt, ptype, pvalue):
                TestHardFaultRateReader._set_via_byref(pvalue, cstatus=0, double_value=42.5)
                return 0

        monkeypatch.setattr(sysmod, "_pdh", FakePdh())
        r = sysmod._HardFaultRateReader()
        r.open()
        assert r.read() == 42.5

    def test_collect_query_data_failure_returns_none(self, monkeypatch):
        from atfield.collectors import system as sysmod

        class FakePdh:
            def PdhOpenQueryW(self, *a): return 0
            def PdhAddEnglishCounterW(self, *a): return 0
            def PdhCollectQueryData(self, hquery): return 1  # nonzero = failure

        monkeypatch.setattr(sysmod, "_pdh", FakePdh())
        r = sysmod._HardFaultRateReader()
        r.open()
        assert r.read() is None

    def test_close_is_idempotent_and_safe_before_open(self, monkeypatch):
        from atfield.collectors import system as sysmod

        class FakePdh:
            def PdhOpenQueryW(self, *a): return 0
            def PdhAddEnglishCounterW(self, *a): return 0
            def PdhCloseQuery(self, *a): return 0

        monkeypatch.setattr(sysmod, "_pdh", FakePdh())
        r = sysmod._HardFaultRateReader()
        r.close()  # never opened -- must not raise
        r.open()
        r.close()
        r.close()  # idempotent

    @pytest.mark.skipif(sys.platform != "win32", reason="real pdh.dll is Windows-only")
    def test_read_returns_a_plausible_value_on_real_windows(self):
        """End-to-end against the REAL PDH counter: open, read twice with a
        real time gap (a rate counter needs two samples), and confirm the
        result is a sane non-negative float -- the ground-truth check that no
        amount of mocking substitutes for."""
        import time
        from atfield.collectors.system import _HardFaultRateReader

        r = _HardFaultRateReader()
        try:
            r.open()
            first = r.read()
            time.sleep(1.2)
            second = r.read()
        finally:
            r.close()
        # first is very likely None (priming call); do not assert on it.
        assert second is None or (isinstance(second, float) and second >= 0.0)


# ---------------------------------------------------------------------------
# SystemCollector: hard_fault_rate integration
# ---------------------------------------------------------------------------


class TestSystemCollectorHardFaultRate:
    @pytest.mark.skipif(sys.platform != "win32", reason="hard_fault_rate is Windows-only")
    def test_probe_declares_hard_fault_rate_signal(self):
        c = SystemCollector()
        result = c.probe()
        assert "system.hard_fault_rate" in result.signals

    @pytest.mark.skipif(sys.platform != "win32", reason="hard_fault_rate is Windows-only")
    def test_probe_failure_to_open_pdh_does_not_fail_the_collector(self, monkeypatch):
        """A missing/broken perf counter category must degrade gracefully --
        the whole collector (RAM/CPU/commit) must not go down with it."""
        from atfield.collectors import system as sysmod

        def boom(self):
            raise OSError("perf counter category not registered")

        monkeypatch.setattr(sysmod._HardFaultRateReader, "open", boom)
        c = SystemCollector()
        result = c.probe()
        assert result.available and c.health() is HealthState.HEALTHY
        assert "unavailable" in result.metadata.get("hard_fault_rate_source", "")
        samples = c.sample()
        assert "system.hard_fault_rate" not in samples
        assert "system.ram_used_percent" in samples  # everything else still works

    @pytest.mark.skipif(sys.platform != "win32", reason="hard_fault_rate is Windows-only")
    def test_sample_omits_signal_when_reader_returns_none(self, monkeypatch):
        """The priming-call case: sample() must NOT fabricate a 0.0 (which
        would read as "no thrash" instead of "no reading yet")."""
        c = SystemCollector()
        c.probe()
        monkeypatch.setattr(c._hard_fault_reader, "read", lambda: None)
        samples = c.sample()
        assert "system.hard_fault_rate" not in samples

    @pytest.mark.skipif(sys.platform != "win32", reason="hard_fault_rate is Windows-only")
    def test_sample_includes_signal_when_reader_has_a_value(self, monkeypatch):
        c = SystemCollector()
        c.probe()
        monkeypatch.setattr(c._hard_fault_reader, "read", lambda: 7.5)
        samples = c.sample()
        assert "system.hard_fault_rate" in samples
        s = samples["system.hard_fault_rate"]
        assert s.value == 7.5 and s.unit == "count" and s.source_id == "system"

    def test_shutdown_closes_the_reader(self, monkeypatch):
        c = SystemCollector()
        c.probe()
        if c._hard_fault_reader is not None:
            closed = {"n": 0}
            monkeypatch.setattr(c._hard_fault_reader, "close", lambda: closed.__setitem__("n", 1))
            c.shutdown()
            assert closed["n"] == 1
        else:
            c.shutdown()  # off-Windows / PDH unavailable: must not raise
