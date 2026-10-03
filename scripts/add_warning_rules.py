"""Append near-limit warning rules (0.4.19) to an existing config.toml, one under each
heat / memory kill rule it already has: 4 C under a temperature, 5 points under a
percentage, same signal and window, `action = "log"`, `notify = true`, cooldown 600 s.

The file is APPENDED to, never re-rendered: hosts carry hand-edited configs and their
own kill lines (Chronos kills GPU core at 88 C and RAM at 92 %), so each warning is
derived from that host's line. Idempotent: a warning whose name is already present is
skipped. The result is validated with the real loader before it is written.

    python scripts/add_warning_rules.py C:\\ProgramData\\ATField\\config.toml [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from atfield.config import load_config, load_config_from_dict  # noqa: E402

#: kill rule -> (warning name, margin under its threshold)
WARN_FOR = {
    "vram-junction-hot": ("vram-junction-warm", 4.0),
    "gpu-core-hot": ("gpu-core-warm", 4.0),
    "gpu-hotspot-hot": ("gpu-hotspot-warm", 4.0),
    "cpu-pkg-hot": ("cpu-pkg-warm", 4.0),
    "ram-pressure": ("ram-high", 5.0),
    "pagefile-pressure": ("pagefile-high", 5.0),
}


def warning_blocks(cfg) -> list[str]:
    have = {r.name for r in cfg.rules}
    out = []
    for r in cfg.rules:
        if r.action != "kill" or r.name not in WARN_FOR:
            continue
        name, margin = WARN_FOR[r.name]
        if name in have:
            continue
        out.append(
            "[[rules]]\n"
            f'name = "{name}"\n'
            f'signal = "{r.signal}"\n'
            f"threshold = {r.threshold - margin:.1f}\n"
            f"window_s = {r.window_s}\n"
            f"min_fraction_over = {r.min_fraction_over}\n"
            'action = "log"\n'
            "cooldown_s = 600\n"
            "notify = true\n"
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    p = Path(a.config)
    text = p.read_text(encoding="utf-8")
    blocks = warning_blocks(load_config(p))
    if not blocks:
        print("nothing to add")
        return 0
    new = text.rstrip("\n") + "\n\n# Near-limit warnings (AT-Field 0.4.19): one under each kill line.\n\n" \
        + "\n".join(blocks)
    import tomllib
    cfg = load_config_from_dict(tomllib.loads(new), source=str(p))   # refuse to write a bad file
    print(new[len(text.rstrip("\n")):])
    print(f"{len([r for r in cfg.rules if r.notify])} warning rule(s) after merge")
    if not a.dry_run:
        p.with_suffix(".toml.pre-0.4.19").write_text(text, encoding="utf-8")
        p.write_text(new, encoding="utf-8")
        print(f"written; backup at {p.with_suffix('.toml.pre-0.4.19')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
