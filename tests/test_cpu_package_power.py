"""CPU package POWER is the variable that explains CPU temperature.

AT-Field published `system.cpu_package_temp_c` and nothing about power, so the
tuner throttled on temperature while blind to what was driving it -- and so was
I. Reasoning from utilization instead of power produced a confident,
hardware-touching wrong answer:

    "Yes -- something is wrong with Chronos's CPU cooling, and it isn't the
     workload. [...] In likelihood order: AIO pump failing, pumped-out or dried
     paste on the 9950X3D, or an uneven cooler mount. [...] Worth servicing the
     cooler before that stage."

All wrong. The chip pulls **80-126 W at 11-27% "CPU usage"** -- utilization is
an average over 32 threads, and a handful of cores boosting to 5215 MHz shows
up as a low percentage and high power. Temperature tracks power. The user's
HWiNFO CSV showed the pump rock-steady at ~2975 RPM and CCD1 at 39-56 C, which
no failing pump could produce; measured under equal load afterwards, Chronos
and DEMETER were one degree apart.

The post-mortem named this exact gap twice: *"AT-Field publishes
cpu_package_temp_c but no CPU package power -- and power is the variable that
actually explains the temperature. [...] would have let me get this right in
one step instead of chasing a pump."* The helper already reads it. This wires
it up.

Sensor identity, measured on Chronos 2026-09-08 (`atfield-sensors.exe --once`):

    {"id":"/amdcpu/0/power/0", "hwType":"Cpu", "name":"Package",
     "type":"Power", "value":77.384}

77.4 W with the machine nearly idle -- the shape of the whole misdiagnosis, in
one reading.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from atfield.collectors.lhmlib import LhmLibCollector  # noqa: E402

# Verbatim from the live helper on Chronos.
CHRONOS_SENSORS = [
    {"id": "/amdcpu/0/power/0", "hw": "AMD Ryzen 9 9950X3D", "hwId": "/amdcpu/0",
     "hwType": "Cpu", "name": "Package", "type": "Power", "value": 77.384},
    {"id": "/amdcpu/0/power/1", "hw": "AMD Ryzen 9 9950X3D", "hwId": "/amdcpu/0",
     "hwType": "Cpu", "name": "Core #1 (SMU)", "type": "Power", "value": 0.901},
    {"id": "/amdcpu/0/temperature/2", "hw": "AMD Ryzen 9 9950X3D", "hwId": "/amdcpu/0",
     "hwType": "Cpu", "name": "Core (Tctl/Tdie)", "type": "Temperature", "value": 61.375},
    # A GPU package power sensor exists too -- it must NOT be taken for the CPU's.
    {"id": "/gpu-nvidia/0/power/0", "hw": "NVIDIA GeForce RTX 5090",
     "hwId": "/gpu-nvidia/0", "hwType": "GpuNvidia", "name": "GPU Package",
     "type": "Power", "value": 40.154},
    {"id": "/gpu-amd/0/power/0", "hw": "AMD Radeon(TM) Graphics", "hwId": "/gpu-amd/0",
     "hwType": "GpuAmd", "name": "GPU Core", "type": "Power", "value": 33.0},
    # SYNTHETIC, and the only sensor here that is not verbatim from the host.
    # LHM's naming is not stable across vendors, and a GPU sensor named exactly
    # "Package" is the one case where the name pattern alone cannot save us --
    # `hwType == "Cpu"` is the only thing that rejects it. Without this entry
    # the guard is untested: dropping it left every assertion green, because
    # "GPU Package" already failed the ^package$ pattern.
    {"id": "/gpu-nvidia/1/power/0", "hw": "NVIDIA GeForce RTX 5090",
     "hwId": "/gpu-nvidia/1", "hwType": "GpuNvidia", "name": "Package",
     "type": "Power", "value": 8.377},
]


def _discover(sensors):
    c = LhmLibCollector.__new__(LhmLibCollector)
    return c._discover(sensors)


def test_the_cpu_package_power_sensor_is_published():
    """RED before this change: no power signal existed at all."""
    mapping = _discover(CHRONOS_SENSORS)
    assert mapping.get("/amdcpu/0/power/0") == ("system.cpu_package_power_w", "watts")


def test_a_PER_CORE_power_sensor_is_not_mistaken_for_the_package():
    """`Core #1 (SMU)` is 0.9 W. Publishing it as package power would understate
    the real 77 W by ~85x and reproduce the original error with extra steps.

    The per-core sensor is deliberately placed FIRST here. With package-first
    ordering the per-device dedupe claims the right sensor and then blocks the
    wrong one, so this assertion passed even with the name pattern widened to
    match anything -- the dedupe was doing the work and the pattern was
    untested. Caught by mutation.
    """
    first = [s for s in CHRONOS_SENSORS if s["id"] == "/amdcpu/0/power/1"]
    rest = [s for s in CHRONOS_SENSORS if s["id"] != "/amdcpu/0/power/1"]
    reordered = first + rest
    mapping = _discover(reordered)
    assert "/amdcpu/0/power/1" not in mapping
    assert mapping.get("/amdcpu/0/power/0") == ("system.cpu_package_power_w", "watts")


def test_a_GPU_package_power_sensor_is_not_mistaken_for_the_CPU():
    """Both are named "...Package" and both are type Power. Only `hwType: Cpu`
    separates them, and GPU power already arrives from NVML."""
    mapping = _discover(CHRONOS_SENSORS)
    # Asserted as ABSENT, not merely differently named. Checking only the name
    # let a dropped `hwType == "Cpu"` guard through: the GPU sensor was then
    # claimed as `system.cpu1_package_power_w` -- a second "CPU" whose watts
    # are a graphics card. Caught by mutation.
    for sid in ("/gpu-nvidia/0/power/0", "/gpu-amd/0/power/0",
                "/gpu-nvidia/1/power/0"):
        assert sid not in mapping, f"{sid} was published as a CPU signal"
    assert [v[0] for v in mapping.values()].count("system.cpu_package_power_w") == 1
    assert not [v for v in mapping.values() if v[0].startswith("system.cpu1_")]


def test_temperature_mapping_is_unaffected():
    """The refusing direction: adding a power channel must not disturb the
    signal the kill rule actually acts on."""
    mapping = _discover(CHRONOS_SENSORS)
    assert mapping.get("/amdcpu/0/temperature/2") == ("system.cpu_package_temp_c", "celsius")


def test_a_machine_with_no_cpu_power_sensor_is_fine():
    """Aurora's i7-9700K may expose no package power at all. Absence must be
    silent -- this is a diagnostic channel, not a guard."""
    mapping = _discover([s for s in CHRONOS_SENSORS if s["type"] != "Power"])
    assert not [v for v in mapping.values() if v[0] == "system.cpu_package_power_w"]
    assert mapping.get("/amdcpu/0/temperature/2") == ("system.cpu_package_temp_c", "celsius")


def test_the_signal_is_classified_as_a_diagnostic_not_a_constraint():
    """It must never become a backoff constraint by accident.

    Power has no operator-authored wall, and inventing one would be a second
    set of limits -- the thing the headroom work exists to remove. The consumer
    classifies by name; `_w` must not read as a reservoir with a threshold.
    """
    from atfield.signal_class import classify_signal
    assert classify_signal("system.cpu_package_power_w") != "thermal"


def test_a_SECOND_physical_cpu_gets_its_own_signal():
    """Dual-socket naming, and the only thing the per-device dedupe guards.

    With a single socket the dedupe is unreachable -- one sensor matches
    `^package$` and there is nothing to deduplicate -- so removing it left
    every other test green. This is the case that distinguishes it: two
    packages must become two signals, not one signal claimed twice.
    """
    two_sockets = [
        {"id": "/amdcpu/0/power/0", "hwId": "/amdcpu/0", "hwType": "Cpu",
         "name": "Package", "type": "Power", "value": 77.384},
        {"id": "/amdcpu/1/power/0", "hwId": "/amdcpu/1", "hwType": "Cpu",
         "name": "Package", "type": "Power", "value": 64.2},
    ]
    mapping = _discover(two_sockets)
    assert mapping["/amdcpu/0/power/0"] == ("system.cpu_package_power_w", "watts")
    assert mapping["/amdcpu/1/power/0"] == ("system.cpu1_package_power_w", "watts")


def test_the_same_device_reporting_Package_twice_yields_ONE_signal():
    """The dedupe's actual job: LHM occasionally exposes duplicate entries for
    one device. Two readings of the same package must not become two CPUs."""
    dupes = [
        {"id": "/amdcpu/0/power/0", "hwId": "/amdcpu/0", "hwType": "Cpu",
         "name": "Package", "type": "Power", "value": 77.384},
        {"id": "/amdcpu/0/power/99", "hwId": "/amdcpu/0", "hwType": "Cpu",
         "name": "Package", "type": "Power", "value": 77.384},
    ]
    mapping = _discover(dupes)
    assert [v[0] for v in mapping.values()] == ["system.cpu_package_power_w"]
