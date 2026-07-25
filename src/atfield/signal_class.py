"""Static signal -> class map for ``/headroom/detail`` (PHASE3.5 §3a).

Three classes, matching the two-axis resource model a consumer (Kiroshi's
spike-aware tuner) reserves margin against:

* ``reservoir``  -- a capacity that fails on the SPIKE, not the mean (RAM,
  pagefile-commit, VRAM, hard-fault rate). The safe default for anything
  unrecognized: treat an unclassified signal as a limit to respect, not a
  target to maximize.
* ``throughput`` -- a utilization percentage where saturating IS success
  (GPU/CPU util). Never a backoff trigger.
* ``thermal``    -- reacts to the sustained mean; thermal mass means it
  cannot spike instantaneously the way memory pressure can.

Classification is purely by signal-name shape (suffix/substring), zero
dependency on the rule engine -- a signal gets a class whether or not any
rule currently references it (that asymmetry, discovered 2026-07-24 field
measurement: vram_used_percent and hard_fault_rate exist in /signals but had
no kill rule, so were invisible to the old scalar /headroom), is the entire
reason this module exists.
"""
from __future__ import annotations

__all__ = ["SignalClass", "classify_signal"]

SignalClass = str  # "reservoir" | "throughput" | "thermal"

_THERMAL_MARKERS = ("temp_c",)
_THROUGHPUT_MARKERS = ("util_percent", "cpu_used_percent")


def classify_signal(signal: str) -> SignalClass:
    """Classify a raw signal id (e.g. ``gpu.0.vram_used_percent``).

    Suffix/substring match, case-sensitive (signal ids are already a fixed
    lower_snake_case vocabulary owned by the collectors). Unknown -> the
    conservative default, ``reservoir``.
    """
    for marker in _THERMAL_MARKERS:
        if marker in signal:
            return "thermal"
    for marker in _THROUGHPUT_MARKERS:
        if marker in signal:
            return "throughput"
    return "reservoir"
