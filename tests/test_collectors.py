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

import sys

import pytest

from atfield.collectors import Collector, HealthState, ProbeResult
from atfield.collectors.lhm import LhmCollector
from atfield.collectors.nvml import PER_PROCESS_VRAM_KEY, NvmlCollector
from atfield.collectors.system import SystemCollector

# ---------------------------------------------------------------------------
# Protocol compliance (no I/O required)
# ---------------------------------------------------------------------------


class TestProtocolCompliance:
    @pytest.mark.parametrize(
        "factory",
        [SystemCollector, NvmlCollector, lambda: LhmCollector()],
        ids=["system", "nvml", "lhm"],
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
