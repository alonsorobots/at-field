"""AMD GPU collector via the rocm-smi CLI.

Status: experimental. Ships disabled by default (the service has to
explicitly construct an :class:`AmdCollector`); we keep it out of the
default collector set because the CI rig only has NVIDIA cards and the
team can't smoke-test live AMD GPUs end-to-end yet. AMD users opt in via
their ``config.toml``::

    [[collectors]]
    name = "amd"

The intent is feature parity with the NVML collector for AMD Radeon /
Instinct cards: per-GPU core temp, VRAM usage percent, util percent,
power, and -- where the driver exposes it -- VRAM junction temperature
(``mem_junction_temp_c``), which is the marquee signal AT-Field cares
about most. Consumer Radeon (RDNA 3+) cards do report VRAM junction temp
through rocm-smi on Linux; on Windows it's a moving target between AMD
Adrenalin driver versions and may or may not be present. When absent
we just don't emit that signal and the matching rule gets disabled at
startup with a clear reason.

Why rocm-smi (subprocess) instead of ADLX (in-process SDK)
----------------------------------------------------------
ADLX is the modern AMD SDK and would in principle be lower-overhead,
but:

1. Its Windows distribution is a separate installer and ships only with
   recent Adrenalin builds. Telling Windows users "install the ADLX SDK
   first" defeats the install-once-and-forget goal.
2. There's no maintained Python binding. We'd have to write ctypes
   shims against the C++ ABI; significant ongoing maintenance for a
   tier-3 collector.
3. ``rocm-smi`` (or ``rocm-smi.exe`` on Windows AMD installs) is the
   officially supported AMD analog of nvidia-smi. Polling once per tick
   on a long-lived machine is well within its budget. The subprocess
   cost (~30 ms per invocation typical) is negligible against the
   1 Hz tick.

Falling back to ADLX is documented as a future enhancement (PLANNING.md
§6); when an in-process binding becomes viable we'll add it as a sibling
collector and let the service prefer it over rocm-smi when both probe
successfully.

Implementation status
---------------------
This file ships the **probe + protocol scaffolding** so that:

* third-party plugin authors can study the shape and write their own
  collectors against the same patterns;
* AMD users can enable it and see exactly which signals it would
  publish (or, if rocm-smi isn't installed, get an actionable error);
* CI lint passes on the module on every PR;

…but the actual ``sample()`` parsing of ``rocm-smi --json`` output is
left as a v0.3 feature once we can validate against a live AMD rig.
Right now ``sample()`` raises :class:`NotImplementedError` so a user
who enables the collector gets a clear error rather than silent zeros.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any, Final

from atfield.collectors import HealthState, ProbeResult
from atfield.signals import Sample

__all__ = ["AmdCollector", "find_rocm_smi"]


_NAME: Final = "amd"

# Per-GPU signals this collector aspires to emit. Names mirror the
# NVML collector so dashboards / Prometheus / rules don't need to special-case
# vendor. The service expands ``gpu.*.X`` rules against whichever signals
# both the AMD and NVML collectors actually advertise.
_AMD_SIGNALS_TEMPLATE: Final[tuple[str, ...]] = (
    "gpu.{idx}.core_temp_c",
    "gpu.{idx}.util_percent",
    "gpu.{idx}.vram_used_percent",
    "gpu.{idx}.vram_used_bytes",
    "gpu.{idx}.power_w",
    # Optional: only emitted when rocm-smi reports a junction temp on
    # this card. Probe will include it in `signals` only if the first
    # rocm-smi --json call returned it.
    "gpu.{idx}.mem_junction_temp_c",
)

# Probe timeout: rocm-smi can stall for a couple of seconds on cold start
# while it enumerates devices. We allow a generous one-shot here; the
# steady-state sampling timeout is much tighter (see _SAMPLE_TIMEOUT_S).
_PROBE_TIMEOUT_S: Final[float] = 5.0
_SAMPLE_TIMEOUT_S: Final[float] = 0.75


def find_rocm_smi() -> str | None:
    """Locate ``rocm-smi`` on PATH (or ``rocm-smi.exe`` on Windows).

    Returns the absolute path to the executable, or ``None`` when the
    AMD driver tools aren't installed. The probe layer treats ``None``
    as "AMD support unavailable on this box" -- expected on NVIDIA-only
    rigs.
    """
    return shutil.which("rocm-smi") or shutil.which("rocm-smi.exe")


class AmdCollector:
    """Tier-3 AMD GPU collector.

    See module docstring for status caveats. The probe is real (it
    attempts to invoke rocm-smi and parse its JSON output) so users get
    accurate "would AT-Field be able to see my AMD GPU?" feedback even
    while ``sample()`` is still under construction.
    """

    name: str = _NAME

    def __init__(self) -> None:
        self._exe: str | None = None
        self._gpu_indices: tuple[int, ...] = ()
        self._signals: tuple[str, ...] = ()
        self._health: HealthState = HealthState.UNPROBED

    def probe(self) -> ProbeResult:
        exe = find_rocm_smi()
        if exe is None:
            return ProbeResult(
                available=False,
                reason=(
                    "rocm-smi not found on PATH; install the AMD ROCm/Adrenalin "
                    "tools (Windows: included with recent AMD Software; Linux: "
                    "`sudo apt install rocm-smi-lib` or distro equivalent)."
                ),
            )
        try:
            payload = _run_rocm_smi_json(exe, _PROBE_TIMEOUT_S)
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as exc:
            self._health = HealthState.FAILED
            return ProbeResult(
                available=False,
                reason=(
                    f"rocm-smi at {exe} did not return valid JSON: {exc}. "
                    "Update the AMD driver, then retry."
                ),
            )

        # rocm-smi --showall --json keys cards as "card0", "card1", etc.
        # Defensive: only count entries that look like card indices we
        # recognize. We accept either "card0" or just "0" because the CLI
        # has shifted between major versions.
        indices: list[int] = []
        has_junction_per_idx: dict[int, bool] = {}
        for key, val in payload.items():
            idx = _maybe_card_index(key)
            if idx is None or not isinstance(val, dict):
                continue
            indices.append(idx)
            has_junction_per_idx[idx] = _looks_like_junction_capable(val)

        if not indices:
            return ProbeResult(
                available=False,
                reason=(
                    f"rocm-smi at {exe} returned no recognizable GPU entries. "
                    f"Output keys: {list(payload.keys())[:6]}…"
                ),
            )

        signals: list[str] = []
        for idx in indices:
            for tmpl in _AMD_SIGNALS_TEMPLATE:
                # Skip optional junction temp on cards that didn't
                # advertise it -- avoids surfacing a permanently NaN
                # signal in the dashboard.
                if "mem_junction_temp_c" in tmpl and not has_junction_per_idx.get(idx, False):
                    continue
                signals.append(tmpl.format(idx=idx))

        self._exe = exe
        self._gpu_indices = tuple(indices)
        self._signals = tuple(signals)
        self._health = HealthState.HEALTHY
        return ProbeResult(
            available=True,
            reason=(
                f"rocm-smi at {exe}; {len(indices)} GPU(s) detected; "
                f"junction-temp on: {sorted(i for i, ok in has_junction_per_idx.items() if ok)}"
            ),
            signals=self._signals,
            metadata={"exe": exe, "gpu_count": str(len(indices))},
        )

    def sample(self) -> dict[str, Sample]:
        # See module docstring: live sampling is intentionally unimplemented
        # until we can smoke-test against an actual AMD rig. We surface a
        # clear error instead of silently returning zeros so a user who
        # enables this collector before v0.3 gets a single FAILED probe-like
        # event rather than misleadingly clean charts.
        self._health = HealthState.FAILED
        raise NotImplementedError(
            "AmdCollector.sample() is not yet implemented. "
            "Track https://github.com/alonsorobots/at-field/issues for the "
            "v0.3 release that lights up live AMD sampling. "
            "In the meantime use LibreHardwareMonitor (already bundled) for "
            "AMD GPU temps -- it covers most consumer cards."
        )

    def health(self) -> HealthState:
        return self._health

    def shutdown(self) -> None:
        # No long-lived resources to release; the rocm-smi calls are
        # short-lived subprocesses.
        return


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _run_rocm_smi_json(exe: str, timeout_s: float) -> dict[str, Any]:
    """Invoke ``rocm-smi --showall --json`` and return the parsed payload.

    rocm-smi prints to stdout; on parse failure or non-zero exit we
    re-raise the underlying error so the probe surfaces something
    actionable.
    """
    out = subprocess.run(
        [exe, "--showall", "--json"],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    if out.returncode != 0:
        raise OSError(
            f"rocm-smi exited {out.returncode}: {out.stderr.strip() or '<no stderr>'}"
        )
    return json.loads(out.stdout or "{}")


def _maybe_card_index(key: str) -> int | None:
    """rocm-smi keys cards as "card0", "card1", or sometimes just "0".
    Returns the integer index, or None if the key isn't a card."""
    if key.startswith("card") and key[4:].isdigit():
        return int(key[4:])
    if key.isdigit():
        return int(key)
    return None


def _looks_like_junction_capable(card_payload: dict[str, Any]) -> bool:
    """Heuristically decide whether a card's payload includes a junction temp.

    rocm-smi has shifted the exact key name several times across versions
    (``Temperature (Sensor memory)``, ``temp_mem``, ``Memory Temperature
    (C)``…). We accept any key whose name -- when normalized -- contains
    both 'mem' or 'junction' AND a 'temp' / 'temperature' word.
    """
    for raw_key in card_payload:
        k = raw_key.lower()
        if "temp" not in k and "temperature" not in k:
            continue
        if "mem" in k or "junction" in k:
            return True
    return False
