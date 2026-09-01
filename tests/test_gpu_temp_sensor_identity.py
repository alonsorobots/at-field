"""A signal must be named for the quantity that was actually measured.

MEASURED on two hosts, 2026-08-31, from ``atfield-sensors.exe --once``.

AURORA, two RTX 2070 SUPER (Turing, GDDR6) -- LHM publishes THREE GPU
temperatures per card, and two of them are one sensor::

    GPU Core            = 64
    GPU Hot Spot        = 77.188
    GPU Memory Junction = 77.188      <- identical to three decimals

Turing has no memory-junction sensor: that telemetry begins with GDDR6X on
the RTX 30-series. HWiNFO, reading the same cards independently, exposes
``GPU Hot Spot Temperature`` and NO memory-junction column at all.

CHRONOS, two RTX 5090 (Blackwell, GDDR7) -- the real thing::

    GPU Core = 25.16   GPU Memory Junction = 38     (delta +12.8)
    GPU Core = 54.66   GPU Memory Junction = 56     (delta  +1.3)

No hot-spot sensor is exposed, and the core->junction delta MOVES, which is
what an independent sensor looks like.

WHAT IT COST. ``_VRAM_JUNCTION_PATTERNS`` ended in ``gpu hot ?spot`` under the
comment "last resort: hot-spot is close". The hot spot runs 10-25 C above the
core by construction and 85-95 C is ordinary under load, so the 90 C
``vram-junction-hot`` rule sat inside the sensor's normal range. It killed work
on Aurora 63 times in three hours while ``gpu-core-hot`` (83 C) fired twice --
the cores were never in trouble. The host was parked as "thermally incapable"
on the strength of it.

Note the discriminator is NOT NVML: ``nvidia-smi --query-gpu=temperature.memory``
returns N/A on the RTX 5090 as well, so using it as the existence test would
have thrown away the one guard that works.
"""
import pytest

from atfield.collectors.lhm import synthetic_junction
from atfield.collectors.lhmlib import LhmLibCollector


def _t(sid, hw, name, value, hw_type="GpuNvidia"):
    return {"id": sid, "hwId": hw, "hwType": hw_type, "name": name,
            "type": "Temperature", "value": value}


# Verbatim from Aurora, both cards.
AURORA = [
    _t("/gpu-nvidia/0/temperature/0", "/gpu-nvidia/0", "GPU Core", 64.0),
    _t("/gpu-nvidia/0/temperature/1", "/gpu-nvidia/0", "GPU Hot Spot", 77.188),
    _t("/gpu-nvidia/0/temperature/2", "/gpu-nvidia/0", "GPU Memory Junction", 77.188),
    _t("/gpu-nvidia/1/temperature/0", "/gpu-nvidia/1", "GPU Core", 62.0),
    _t("/gpu-nvidia/1/temperature/1", "/gpu-nvidia/1", "GPU Hot Spot", 77.312),
    _t("/gpu-nvidia/1/temperature/2", "/gpu-nvidia/1", "GPU Memory Junction", 77.312),
]

# Verbatim from Chronos, both cards.
CHRONOS = [
    _t("/gpu-nvidia/0/temperature/0", "/gpu-nvidia/0", "GPU Core", 25.16),
    _t("/gpu-nvidia/0/temperature/1", "/gpu-nvidia/0", "GPU Memory Junction", 38.0),
    _t("/gpu-nvidia/1/temperature/0", "/gpu-nvidia/1", "GPU Core", 54.656),
    _t("/gpu-nvidia/1/temperature/1", "/gpu-nvidia/1", "GPU Memory Junction", 56.0),
]


def _signals(sensors):
    return set(LhmLibCollector.__new__(LhmLibCollector)
               ._resolve_gpu_temps(sensors).values())


class TestTuringHotSpotIsNotAMemoryJunction:
    def test_no_memory_junction_signal_is_published(self):
        sigs = _signals(AURORA)
        assert not [s for s in sigs if "mem_junction" in s], (
            "a card with no memory sensor is publishing a memory temperature; "
            "the 90 C VRAM rule will bind to it and kill healthy work"
        )

    def test_the_reading_survives_under_its_real_name(self):
        # The measurement is still useful -- it just isn't VRAM. Losing it
        # entirely would leave the host with no die-temperature guard.
        assert _signals(AURORA) == {"gpu.0.hotspot_temp_c", "gpu.1.hotspot_temp_c"}

    def test_both_cards_keep_distinct_indices(self):
        m = LhmLibCollector.__new__(LhmLibCollector)._resolve_gpu_temps(AURORA)
        assert m["/gpu-nvidia/0/temperature/1"] == "gpu.0.hotspot_temp_c"
        assert m["/gpu-nvidia/1/temperature/1"] == "gpu.1.hotspot_temp_c"


class TestRealMemoryJunctionIsUntouched:
    def test_blackwell_still_reports_a_junction(self):
        assert _signals(CHRONOS) == {"gpu.0.mem_junction_temp_c",
                                     "gpu.1.mem_junction_temp_c"}

    def test_index_assignment_is_unchanged_for_junction_only_devices(self):
        m = LhmLibCollector.__new__(LhmLibCollector)._resolve_gpu_temps(CHRONOS)
        assert m["/gpu-nvidia/0/temperature/1"] == "gpu.0.mem_junction_temp_c"
        assert m["/gpu-nvidia/1/temperature/1"] == "gpu.1.mem_junction_temp_c"


class TestDiscriminator:
    def test_equal_readings_mean_one_sensor(self):
        assert synthetic_junction(77.188, 77.188) is True

    @pytest.mark.parametrize("junction,hotspot", [(38.0, 25.16), (56.0, 54.656)])
    def test_a_moving_delta_is_a_real_sensor(self, junction, hotspot):
        assert synthetic_junction(junction, hotspot) is False

    def test_a_missing_side_is_not_evidence(self):
        # Chronos exposes no hot spot at all; that must not be read as proof
        # its junction is fake.
        assert synthetic_junction(38.0, None) is False
        assert synthetic_junction(None, 77.188) is False


class TestTheRuleExists:
    def test_a_hotspot_rule_guards_the_renamed_signal(self):
        from atfield.config import _default_rules
        rules = {r.name: r for r in _default_rules()}
        assert "gpu-hotspot-hot" in rules, (
            "renaming the signal without a rule for the new name leaves a "
            "host with no die-temperature guard at all"
        )
        r = rules["gpu-hotspot-hot"]
        assert r.signal == "gpu.*.hotspot_temp_c"
        # 85-95 C is ordinary for a hot spot under sustained load; a threshold
        # inside that band is the bug this whole file is about.
        assert r.threshold >= 96.0
