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



# 0.4.22. MEASURED 2026-10-17 on Aurora, 0.4.21, from /signals/history (1 h, 3,584 paired
# samples per card, idle): the "junction" equals the hot spot EXACTLY in only 2.0% (gpu.0)
# and 44.7% (gpu.1) of samples; mean |d| 0.336 / 0.083 C, max 0.907 / 0.625 C. Deciding
# from exact equality in ONE startup sample was therefore a coin flip: four service
# restarts that day published the duplicate on 1, 2, 2, 2 cards, and vram-junction-hot/warm
# then watched the die hot spot.
# Widest measured pairs, verbatim (hot spot, "junction"):
AURORA_NOISY = [
    _t("/gpu-nvidia/0/temperature/0", "/gpu-nvidia/0", "GPU Core", 40.0),
    _t("/gpu-nvidia/0/temperature/2", "/gpu-nvidia/0", "GPU Hot Spot", 51.969),
    _t("/gpu-nvidia/0/temperature/3", "/gpu-nvidia/0", "GPU Memory Junction", 51.062),
    _t("/gpu-nvidia/1/temperature/0", "/gpu-nvidia/1", "GPU Core", 41.0),
    _t("/gpu-nvidia/1/temperature/2", "/gpu-nvidia/1", "GPU Hot Spot", 52.531),
    _t("/gpu-nvidia/1/temperature/3", "/gpu-nvidia/1", "GPU Memory Junction", 51.906),
]

# `atfield-sensors.exe --once`, 2026-10-17, verbatim GPU temperatures. The 5090s expose
# NO hot spot, so no comparison can ever hide their junction.
CHRONOS_TODAY = [
    _t("/gpu-nvidia/0/temperature/0", "/gpu-nvidia/0", "GPU Core", 28.227),
    _t("/gpu-nvidia/0/temperature/3", "/gpu-nvidia/0", "GPU Memory Junction", 42.0),
    _t("/gpu-nvidia/1/temperature/0", "/gpu-nvidia/1", "GPU Core", 31.844),
    _t("/gpu-nvidia/1/temperature/3", "/gpu-nvidia/1", "GPU Memory Junction", 42.0),
]
DEMETER_TODAY = [
    _t("/gpu-nvidia/0/temperature/0", "/gpu-nvidia/0", "GPU Core", 35.312),
    _t("/gpu-nvidia/0/temperature/3", "/gpu-nvidia/0", "GPU Memory Junction", 44.0),
    _t("/gpu-nvidia/1/temperature/0", "/gpu-nvidia/1", "GPU Core", 26.531),
    _t("/gpu-nvidia/1/temperature/3", "/gpu-nvidia/1", "GPU Memory Junction", 38.0),
]


def _with(sensors, **values):
    """A copy of ``sensors`` with some readings replaced, keyed by sensor id."""
    return [dict(s, value=values.get(s["id"], s["value"])) for s in sensors]


def _history_signals(snapshots):
    return set(LhmLibCollector.__new__(LhmLibCollector)
               ._resolve_gpu_temps(snapshots[-1], history=snapshots).values())


class TestTheDuplicateIsFoundDeterministically:
    def test_the_widest_measured_noise_is_still_one_sensor(self):
        assert _signals(AURORA_NOISY) == {"gpu.0.hotspot_temp_c", "gpu.1.hotspot_temp_c"}

    def test_a_first_sample_outlier_does_not_decide(self):
        # the startup sample catches the two reads 1.6 C apart (wider than anything
        # measured); the following samples track -- the median decides
        spike = _with(AURORA_NOISY, **{"/gpu-nvidia/0/temperature/3": 50.3})
        snaps = [spike] + [AURORA_NOISY] * 4
        assert _history_signals(snaps) == {"gpu.0.hotspot_temp_c", "gpu.1.hotspot_temp_c"}

    @pytest.mark.parametrize("dump", [CHRONOS_TODAY, DEMETER_TODAY])
    def test_the_real_5090_junctions_survive(self, dump):
        assert _history_signals([dump] * 5) == {"gpu.0.mem_junction_temp_c",
                                                "gpu.1.mem_junction_temp_c"}

    def test_a_real_junction_beside_a_hot_spot_survives(self):
        # GDDR6X-style card: both sensors real, a few degrees apart in most samples
        real = _with(AURORA_NOISY, **{"/gpu-nvidia/0/temperature/3": 58.0})
        snaps = [_with(real, **{"/gpu-nvidia/0/temperature/3": 52.5})] + [real] * 4
        assert "gpu.0.mem_junction_temp_c" in _history_signals(snaps)


def test_probe_decides_from_several_samples_not_the_first(tmp_path, monkeypatch):
    """Through probe() and the real reader loop: the helper's FIRST sample has the two
    reads 1.6 C apart; four more track. The duplicate must not be published."""
    import json
    import threading
    import time

    exe = tmp_path / "atfield-sensors.exe"
    exe.write_text("")
    spike = _with(AURORA_NOISY, **{"/gpu-nvidia/0/temperature/3": 50.3})
    lines = [json.dumps({"event": "ready", "elevated": True})]
    lines += [json.dumps({"event": "sample", "sensors": s}) for s in [spike] + [AURORA_NOISY] * 6]

    class Proc:
        returncode = None

        def poll(self):
            return None

        @property
        def stdout(self):
            for line in lines:
                time.sleep(0.05)
                yield line

    c = LhmLibCollector(exe_path=exe, startup_timeout_s=5.0)

    def spawn():
        threading.Thread(target=c._read_loop, args=(Proc(),), daemon=True).start()
        return True
    monkeypatch.setattr(c, "_spawn", spawn)
    sigs = set(c.probe().signals)
    assert {s for s in sigs if s.startswith("gpu.")} == {"gpu.0.hotspot_temp_c",
                                                         "gpu.1.hotspot_temp_c"}


def test_a_short_sample_window_is_said_out_loud(tmp_path, monkeypatch, caplog):
    """Review F1: at the startup deadline with fewer than 5 samples, probe() still decides
    (from what it has) -- and must SAY how many samples that decision rests on."""
    import json
    import logging
    import threading
    import time

    exe = tmp_path / "atfield-sensors.exe"
    exe.write_text("")
    lines = [json.dumps({"event": "sample", "sensors": AURORA_NOISY})] * 2

    class Proc:
        returncode = None

        def poll(self):
            return None

        @property
        def stdout(self):
            yield from lines
            time.sleep(5)                           # the helper stalls after 2 samples

    c = LhmLibCollector(exe_path=exe, startup_timeout_s=1.0)

    def spawn():
        threading.Thread(target=c._read_loop, args=(Proc(),), daemon=True).start()
        return True
    monkeypatch.setattr(c, "_spawn", spawn)
    caplog.set_level(logging.WARNING, logger="atfield.collectors.lhmlib")
    assert c.probe().available
    assert [r.getMessage() for r in caplog.records if "samples" in r.getMessage()] == [
        "GPU temperature discovery decided from 2 of 5 samples (startup deadline)"]
