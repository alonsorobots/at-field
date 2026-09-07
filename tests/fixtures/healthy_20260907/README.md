# Healthy control — Chronos, captured 2026-09-07 00:49 local

The same four endpoints from the same host and the same AT-Field **0.4.10**
binary, ~1 minute after the elevated `Restart-Service ATFieldWatchdog` that
cured the wedge documented in `../wedged_nvml_20260906/`.

The restart alone moved the nvml collector `DEGRADED → HEALTHY`, its probe
string `610.88 → 616.56`, and `rules_starved 1 → 0`. All twelve GPU signals
are live at ~1.4 s age and match `nvidia-smi` (gpu.0 26 °C / 37.8 W,
gpu.1 31 °C / 9.5 W). `gpu-core-hot[gpu.1.core_temp_c]` is back in
`/headroom` at 0.6477 after being absent from the endpoint entirely.

This is the **control**: a liveness gate that condemns anything here is
over-firing. It is also still the pre-`4a7e799` binary, so no signal carries
`thermal_band_c` — see the last test in `tests/test_wedged_nvml_fixtures.py`.
