"""This machine's CPU model and its thermal limit, for the package-temperature warning.

2026-10-27: a cpu-pkg-warm notice at 88 C made an agent throttle hard, though the user's
real limit is the AVERAGE package temperature (Chronos's 9950X3D: <= 92 C, Tjmax 95) and
momentary spikes above it are fine. The notice now names the CPU, its Tjmax and the
average target -- read on the machine the reading came from, never another host's.

Target = Tjmax - 3 C (the user's 92 C for a 95 C part), never above AT-Field's own kill
line. A model missing from ``TJMAX_C`` has no target, and the message says so.
"""
from __future__ import annotations

import functools
from dataclasses import dataclass

#: Model substring -> Tjmax (C), from the vendor spec (AMD "Max. Operating Temperature",
#: Intel "T junction"). Add a host's CPU here when it joins.
TJMAX_C: dict[str, float] = {
    "Ryzen 9 9950X3D": 95.0,       # AMD spec; Chronos, Demeter
    "i7-9700K": 100.0,             # Intel ARK T junction; Aurora
}
TARGET_UNDER_TJMAX_C = 3.0


@dataclass(frozen=True, slots=True)
class CpuLimits:
    model: str                     # as the OS names it, trimmed ("?" if unreadable)
    tjmax_c: float | None


def limits_for(model: str) -> CpuLimits:
    tj = next((t for k, t in TJMAX_C.items() if k.lower() in model.lower()), None)
    return CpuLimits(short_name(model), tj)


def short_name(model: str) -> str:
    """'AMD Ryzen 9 9950X3D 16-Core Processor' -> 'AMD Ryzen 9 9950X3D';
    'Intel(R) Core(TM) i7-9700K CPU @ 3.60GHz' -> 'Intel Core i7-9700K'."""
    s = model.replace("(R)", "").replace("(TM)", "").split(" CPU @")[0]
    words = [w for w in s.split() if not w.endswith("-Core") and w != "Processor"]
    return " ".join(words) or "?"


def target_c(lim: CpuLimits, kill_line: float | None) -> float | None:
    if lim.tjmax_c is None:
        return None
    t = lim.tjmax_c - TARGET_UNDER_TJMAX_C
    return min(t, kill_line) if kill_line is not None else t


@functools.lru_cache(maxsize=1)
def this_cpu() -> CpuLimits:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            model = str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
    except Exception:  # noqa: BLE001 -- non-Windows / unreadable: say unknown
        model = "?"
    return limits_for(model)
