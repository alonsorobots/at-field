"""Rule profile metadata: tier classification, slider ranges, presets.

Single source of truth for "what's an Aggressive vs Normal vs Relaxed value
for this rule?". Read by the HTTP API to decorate `/rules` responses (so the
tray slider can render its tooltip without re-deriving the bands), by the
`atf set-profile` CLI, and by the rule patch endpoint to validate the
slider's input range.

Why hardcoded here rather than user-configurable: the bands are a *meaning*
question (what does "aggressive" mean for VRAM-junction temp?) that's
informed by hardware physics, not user preference. The user's preference is
expressed by where they SET the threshold, not by what we call each band.
Hardware-specific guidance lives in docs/tuning.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

__all__ = [
    "PROFILE_PRESETS",
    "RULE_PROFILES",
    "Profile",
    "ProfileTier",
    "RuleProfile",
    "classify",
    "preset_threshold",
    "rule_profile",
]


ProfileTier = Literal["aggressive", "normal", "relaxed", "custom"]
Profile = Literal["aggressive", "normal", "relaxed"]


@dataclass(frozen=True, slots=True)
class RuleProfile:
    """Per-rule slider metadata.

    `min`/`max` define the sane slider extents -- AT-Field rejects PATCH
    requests outside this range. The `aggressive_max` and `relaxed_min`
    tier boundaries split the slider into three zones for the tooltip.
    Threshold preset values are pinned by `preset_threshold()`.

    The aggressive_max < normal < relaxed_min ordering means lower
    threshold = more aggressive (will fire sooner / kill earlier), and
    higher threshold = more relaxed (gives the workload more headroom).
    """

    name: str
    unit: str  # display unit, "°C" / "%"
    min: float
    aggressive_max: float
    relaxed_min: float
    max: float
    aggressive_value: float
    normal_value: float
    relaxed_value: float
    step: float = 1.0


# Per-rule tier definitions. Numbers reflect the consensus "this hardware
# starts being unhappy at X" values from PLANNING.md §3 + community
# benchmarks for the RTX 30/40/50 series. Update docs/tuning.md when
# changing these so the tier story stays coherent.
RULE_PROFILES: Final[dict[str, RuleProfile]] = {
    "gpu-core-hot": RuleProfile(
        name="gpu-core-hot", unit="°C",
        min=60.0, aggressive_max=78.0, relaxed_min=87.0, max=95.0,
        aggressive_value=78.0, normal_value=83.0, relaxed_value=88.0,
    ),
    "vram-junction-hot": RuleProfile(
        name="vram-junction-hot", unit="°C",
        min=80.0, aggressive_max=88.0, relaxed_min=98.0, max=110.0,
        aggressive_value=85.0, normal_value=92.0, relaxed_value=100.0,
    ),
    "ram-pressure": RuleProfile(
        name="ram-pressure", unit="%",
        min=50.0, aggressive_max=80.0, relaxed_min=92.0, max=99.0,
        aggressive_value=78.0, normal_value=85.0, relaxed_value=93.0,
    ),
    "pagefile-pressure": RuleProfile(
        name="pagefile-pressure", unit="%",
        min=60.0, aggressive_max=85.0, relaxed_min=95.0, max=99.0,
        aggressive_value=83.0, normal_value=90.0, relaxed_value=96.0,
    ),
    "cpu-pkg-hot": RuleProfile(
        name="cpu-pkg-hot", unit="°C",
        min=60.0, aggressive_max=85.0, relaxed_min=95.0, max=100.0,
        aggressive_value=82.0, normal_value=90.0, relaxed_value=96.0,
    ),
}


# Preset profiles. Each maps `(rule_name) -> threshold`. Used by
# `atf set-profile aggressive` and the UI's preset buttons.
PROFILE_PRESETS: Final[dict[Profile, dict[str, float]]] = {
    "aggressive": {n: p.aggressive_value for n, p in RULE_PROFILES.items()},
    "normal":     {n: p.normal_value     for n, p in RULE_PROFILES.items()},
    "relaxed":    {n: p.relaxed_value    for n, p in RULE_PROFILES.items()},
}


def rule_profile(rule_name: str) -> RuleProfile | None:
    """Return the profile metadata for a rule, or None if unknown.

    Unknown rules get no slider in the UI -- they may be custom rules the
    user defined that don't map to our canonical tiers.
    """
    return RULE_PROFILES.get(rule_name)


def classify(rule_name: str, threshold: float) -> ProfileTier:
    """Classify a threshold into Aggressive / Normal / Relaxed / Custom.

    Returns ``"custom"`` for unknown rules (we have no tier definition,
    so there's no honest classification to give).
    """
    p = RULE_PROFILES.get(rule_name)
    if p is None:
        return "custom"
    if threshold <= p.aggressive_max:
        return "aggressive"
    if threshold >= p.relaxed_min:
        return "relaxed"
    return "normal"


def preset_threshold(profile: Profile, rule_name: str) -> float | None:
    """Return the canonical threshold for ``rule_name`` under ``profile``.

    None if either the profile or the rule is unknown.
    """
    return PROFILE_PRESETS.get(profile, {}).get(rule_name)
