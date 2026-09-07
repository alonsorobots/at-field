# Wedged NVML session — Chronos, captured 2026-09-06 23:57 local

Live `/signals`, `/health`, `/headroom`, `/headroom/detail` from AT-Field
**0.4.10** (the 09-02 build), 4.2 days into a wedged NVML session, taken
immediately before the curing restart.

| | |
|---|---|
| Service started | 2026-09-02 19:04:50, 25 s after boot |
| NVML session opened against | driver **610.88** (probe string in `health.json`) |
| Driver actually installed | **616.56** — Windows UserPnp event 20003 re-attached `nvlddmkm` at 19:14:55 / 19:15:04 / 19:15:38 |
| `SIGNAL LOST` logged | 19:15:13, both `gpu.0.core_temp_c` and `gpu.1.core_temp_c` |
| Starvation retracted | 2026-09-03 12:46:48 — `gpu.0.core_temp_c` "recovered" after 63,106 s because the constant `0.0` began arriving |

**Raising (6 signals frozen 4.2 days):** `gpu.1.core_temp_c`,
`gpu.1.util_percent`, `gpu.1.power_w`, `gpu.1.vram_used_bytes`,
`gpu.1.vram_used_percent`, `gpu.0.vram_used_bytes`, `gpu.0.vram_used_percent`.

**SUCCESS-with-garbage (fresh timestamps, impossible values):**
`gpu.0.core_temp_c = 0.0` (2393/2393 identical samples over 24 h) and
`gpu.0.power_w = 0.041`. Ground truth at capture time was 26 °C / 37.9 W.

Only `mem_junction_temp_c` on both cards stayed real — those come from LHM,
not NVML.

Pinned by `tests/test_wedged_nvml_fixtures.py`. Do not re-capture without
re-reading that file: three specific shapes make this evidence, and a
careless re-capture loses them.
