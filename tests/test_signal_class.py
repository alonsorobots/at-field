"""Unit tests for :mod:`atfield.signal_class`."""
from __future__ import annotations

from atfield.signal_class import classify_signal


def test_temperature_signals_are_thermal():
    assert classify_signal("gpu.0.core_temp_c") == "thermal"
    assert classify_signal("gpu.0.mem_junction_temp_c") == "thermal"
    assert classify_signal("system.cpu_package_temp_c") == "thermal"


def test_utilization_signals_are_throughput():
    assert classify_signal("gpu.0.util_percent") == "throughput"
    assert classify_signal("system.cpu_used_percent") == "throughput"


def test_capacity_and_rate_signals_default_to_reservoir():
    assert classify_signal("system.ram_used_percent") == "reservoir"
    assert classify_signal("system.commit_percent") == "reservoir"
    assert classify_signal("gpu.0.vram_used_percent") == "reservoir"
    assert classify_signal("system.hard_fault_rate") == "reservoir"


def test_unknown_signal_defaults_to_reservoir():
    # Safe default: an unrecognized signal is treated as a limit to respect,
    # not a target to maximize.
    assert classify_signal("some.brand_new_signal") == "reservoir"
