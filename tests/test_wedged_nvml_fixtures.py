"""The captured payloads from the 2026-09-02 wedged-NVML incident, pinned.

Chronos booted 2026-09-02 19:04 and AT-Field, a boot-start service, opened an
NVML session against driver 610.88. Ten minutes later Windows PnP re-attached
the NVIDIA driver as 616.56 underneath the running process (UserPnp event
20003, three times, 19:14:55-19:15:38). From then on GPU 1's NVML calls RAISED
-- its six signals froze for 4.2 days -- while GPU 0's calls returned
NVML_SUCCESS with ``core_temp_c = 0.0`` and ``power_w = 0.041``.

These two directories are the live ``/signals``, ``/health``, ``/headroom`` and
``/headroom/detail`` payloads from that service, captured 2026-09-06 23:57
just before the curing restart, plus the same four from the healthy service
afterwards. Later phases test the liveness gate against them rather than
against synthetic data, for the reason ``c79d1c3`` records: a drift guard
written against constant input passes on the one input where the bug is
invisible.

What these tests pin is not "the JSON parses". It is the three shapes that
make the wedged capture *useful as evidence*, any one of which a careless
re-capture would lose:

1. A signal that is FROZEN but still published (``gpu.1.core_temp_c``, 4.2 days
   stale, carrying a real-looking 32.0 C).
2. A signal that is FRESH but not credible (``gpu.0.core_temp_c`` at 0.0 C with
   a current timestamp) -- the shape an age-only check cannot see.
3. The two headroom endpoints DISAGREEING about which rules exist: the scalar
   publishes ``gpu-core-hot[gpu.0.core_temp_c]: 1.0`` (perfect headroom, from
   the frozen zero) and silently omits ``gpu-core-hot[gpu.1.core_temp_c]``
   entirely, while ``/headroom/detail`` lists both as though current.

The healthy capture is the control: the same host, same rules, twelve GPU
signals matching ``nvidia-smi``, so a gate that condemns it is over-firing.
"""
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
WEDGED = FIXTURES / "wedged_nvml_20260906"
HEALTHY = FIXTURES / "healthy_20260907"

ENDPOINTS = ("signals", "health", "headroom", "headroom_detail")

# The two cards' core-temp rules. Both are action=kill at 88 C on Chronos.
GPU0_CORE = "gpu.0.core_temp_c"
GPU1_CORE = "gpu.1.core_temp_c"
GPU0_RULE = f"gpu-core-hot[{GPU0_CORE}]"
GPU1_RULE = f"gpu-core-hot[{GPU1_CORE}]"

# 4.2 days. Kept loose (> 3 days) so a re-capture at a different hour still
# passes, but far enough above any plausible tick interval that a fresh
# payload cannot satisfy it by accident.
MIN_FROZEN_AGE_S = 3 * 86400


def _load(directory: Path, endpoint: str) -> dict:
    return json.loads((directory / f"{endpoint}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("directory", [WEDGED, HEALTHY], ids=["wedged", "healthy"])
@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_every_captured_endpoint_parses(directory: Path, endpoint: str) -> None:
    assert _load(directory, endpoint)


def test_wedged_holds_a_frozen_but_published_signal() -> None:
    """Shape 1: arriving stopped, publishing did not."""
    detail = _load(WEDGED, "headroom_detail")
    sig = detail["per_signal"][GPU1_CORE]
    age = detail["ts"] - sig["latest_ts"]
    assert age > MIN_FROZEN_AGE_S, f"{GPU1_CORE} is only {age:.0f}s stale"
    # Still carries a plausible-looking temperature and a live kill threshold,
    # which is precisely why nothing downstream noticed.
    assert sig["latest"] == 32.0
    assert sig["threshold"] == 88.0
    assert sig["class"] == "thermal"


def test_wedged_holds_a_fresh_but_incredible_signal() -> None:
    """Shape 2: a current timestamp on a value that cannot be real.

    An age-only staleness check is blind to this one. 0.0 C sits inside
    ``is_plausible``'s celsius range (-50..150), so the collector's own
    garbage detector did not drop it either.
    """
    detail = _load(WEDGED, "headroom_detail")
    sig = detail["per_signal"][GPU0_CORE]
    age = detail["ts"] - sig["latest_ts"]
    assert age < 60, f"{GPU0_CORE} should be FRESH in this capture, was {age:.0f}s old"
    assert sig["latest"] == 0.0
    assert sig["threshold"] == 88.0


def test_wedged_headroom_endpoints_disagree_about_which_rules_exist() -> None:
    """Shape 3: the scalar and detail describe different machines.

    The scalar publishes perfect headroom for a rule that cannot fire, and
    drops the other card's rule with no record of why. Detail lists both as
    though current. Phase 3's "endpoints cannot disagree" gate is written
    against exactly this.
    """
    scalar = _load(WEDGED, "headroom")["per_rule"]
    detail = _load(WEDGED, "headroom_detail")["per_signal"]

    assert scalar[GPU0_RULE] == 1.0, "the poisoned zero should read as perfect headroom"
    assert GPU1_RULE not in scalar, "the starved rule should be silently absent"
    # ...while detail lists both signals, indistinguishable from live ones.
    assert detail[GPU0_CORE]["rule"] == GPU0_RULE
    assert detail[GPU1_CORE]["rule"] == GPU1_RULE


def test_wedged_health_reports_the_collector_but_not_the_damage() -> None:
    health = _load(WEDGED, "health")
    by_name = {c["name"]: c for c in health["collectors"]}
    assert by_name["nvml"]["health"] == "DEGRADED"
    # The probe string still advertises the driver the session was opened
    # against -- the session outlived it by 4.2 days.
    assert "610.88" in by_name["nvml"]["reason"]
    # Only ONE rule is flagged: GPU 0's starvation was cleared 17.5 h in when
    # the constant zero began arriving.
    assert health["rules_starved"] == 1


def test_healthy_capture_is_a_clean_control() -> None:
    """A gate that condemns this payload is over-firing."""
    health = _load(HEALTHY, "health")
    assert health["rules_starved"] == 0
    assert all(c["health"] == "HEALTHY" for c in health["collectors"])
    by_name = {c["name"]: c for c in health["collectors"]}
    assert "616.56" in by_name["nvml"]["reason"], "post-restart session is on the new driver"

    # Both core-temp rules are back in the fold, and every GPU signal is fresh.
    scalar = _load(HEALTHY, "headroom")["per_rule"]
    assert GPU0_RULE in scalar and GPU1_RULE in scalar

    detail = _load(HEALTHY, "headroom_detail")
    gpu_signals = [k for k in detail["per_signal"] if k.startswith("gpu.")]
    assert len(gpu_signals) == 12
    for name in gpu_signals:
        age = detail["ts"] - detail["per_signal"][name]["latest_ts"]
        assert age < 60, f"{name} is {age:.0f}s old in the HEALTHY capture"


def test_neither_capture_carries_thermal_band_c() -> None:
    """Both were taken from the 09-02 binary, so 4a7e799 is not in them.

    Recorded here because Phase 5's deploy check reads the opposite: once
    0.4.11 is running, every thermal signal with a rule carries a numeric
    ``thermal_band_c``. If a re-capture starts carrying it, these fixtures
    came from a newer service and no longer pin the pre-fix behaviour.
    """
    for directory in (WEDGED, HEALTHY):
        for sig in _load(directory, "headroom_detail")["per_signal"].values():
            assert sig.get("thermal_band_c") is None
