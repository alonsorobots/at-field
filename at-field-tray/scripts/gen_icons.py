"""Generate the Tauri / PyInstaller icon set from the brand masters.

Source-of-truth artwork lives in ``brand/``. Two tiers:

  HAND-PAINTED (preferred)
    brand/logo_16.png, logo_28.png, logo_32.png, logo_48.png, ...
    Used verbatim at their native resolution -- no resize, no binarize.
    Drop a new ``logo_NN.png`` in brand/ to add another hand-painted size.
    Hand-painted icons always beat auto-resampled ones at small sizes;
    every shipped Windows app does this.

  AUTO-RESAMPLED (fallback)
    brand/logo_1024_thick.png  -- thick three-hex master (1024×1024).
                        Used at any size where no hand-painted version
                        exists. Trim → Lanczos resize → alpha-binarize.
                        (Falls back to brand/logo_1024.png if the thick
                        variant isn't on disk -- both are accepted.)

Outputs (in ``at-field-tray/src-tauri/icons/``):

    32x32.png          taskbar small / NSIS small      <- HAND if 32 exists
    128x128.png        taskbar / dock at 1×            <- AUTO from logo2
    128x128@2x.png     taskbar / dock at 2× (256 px)   <- AUTO from logo2
    icon.png           1024×1024 brand master          <- logo2 verbatim
    icon.ico           multi-resolution Windows icon   <- per-size: HAND
                                                          where available,
                                                          AUTO elsewhere
    tray.png           32×32 systray-specific          <- HAND if 32 exists

Re-run whenever ``brand/*`` changes:

    .venv/Scripts/python.exe at-field-tray/scripts/gen_icons.py
"""

from __future__ import annotations

import re
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BRAND = REPO_ROOT / "brand"
OUT_DIR = REPO_ROOT / "at-field-tray" / "src-tauri" / "icons"


def _resolve_thick_master() -> Path:
    """Find the auto-fallback master regardless of which name the artist used.

    The brand directory has gone through a couple of renames -- ``logo.png``
    became ``logo2.png`` became ``logo_1024.png`` / ``logo_1024_thick.png``.
    Rather than break every time the artist tweaks a filename, we accept
    the canonical names in priority order and use whichever exists.
    """
    candidates = [
        BRAND / "logo_1024_thick.png",
        BRAND / "logo_1024.png",
        BRAND / "logo2.png",
        BRAND / "logo.png",
    ]
    for c in candidates:
        if c.is_file():
            return c
    raise FileNotFoundError(
        f"no auto-fallback master in {BRAND}; expected one of "
        + ", ".join(c.name for c in candidates)
    )


THICK_MASTER = _resolve_thick_master()

_HAND_PATTERN = re.compile(r"^logo_(\d+)\.png$", re.IGNORECASE)


def _discover_hand_painted() -> dict[int, Path]:
    """Scan ``brand/`` for ``logo_NN.png`` files and validate dimensions.

    Each hand-painted file must be square and exactly NN×NN pixels (the
    point of hand-painting is to control every pixel; if the file isn't
    NN×NN the user almost certainly made a mistake we should surface).
    """
    found: dict[int, Path] = {}
    for path in sorted(BRAND.glob("logo_*.png")):
        m = _HAND_PATTERN.match(path.name)
        if not m:
            continue
        size = int(m.group(1))
        with Image.open(path) as im:
            if im.size != (size, size):
                raise ValueError(
                    f"{path.name} declares size {size} but is {im.size}. "
                    "Hand-painted icons must be square at the declared size."
                )
        found[size] = path
    return found


def _load(path: Path) -> Image.Image:
    if not path.exists():
        raise FileNotFoundError(
            f"brand master not found at {path}. See brand/README.md."
        )
    return Image.open(path).convert("RGBA")


def _trim_alpha(img: Image.Image, padding_frac: float = 0.04) -> Image.Image:
    """Crop transparent margins from an RGBA image, then re-pad uniformly.

    Keeps the mark visually centered after the crop. The small re-pad
    avoids edge-clipping when the result is later resized with Lanczos.
    """
    bbox = img.split()[-1].getbbox()
    if bbox is None:
        return img
    cropped = img.crop(bbox)
    side = max(cropped.size)
    pad = int(side * padding_frac)
    canvas = Image.new("RGBA", (side + 2 * pad, side + 2 * pad), (0, 0, 0, 0))
    offset = (
        (canvas.size[0] - cropped.size[0]) // 2,
        (canvas.size[1] - cropped.size[1]) // 2,
    )
    canvas.paste(cropped, offset, cropped)
    return canvas


def _resize(img: Image.Image, size: int) -> Image.Image:
    return img.resize((size, size), Image.Resampling.LANCZOS)


def _binarize_alpha(img: Image.Image, threshold: int = 96) -> Image.Image:
    """Snap the alpha channel to fully opaque or fully transparent.

    Why: the brand logo has a distressed/painted edge with lots of
    partially-transparent pixels. After a big Lanczos downscale (1024 →
    32) those partial-alpha pixels dominate the silhouette, and they
    visually wash out / darken when composited against a dark Windows
    taskbar (alpha 0.4 orange + black = muddy brown). Binarizing the
    alpha keeps the painted-edge SHAPE intact while restoring the punchy
    saturation of the source orange. We only do this at small sizes; the
    full-resolution master keeps its texture untouched.
    """
    r, g, b, a = img.split()
    a = a.point(lambda v: 255 if v >= threshold else 0)
    return Image.merge("RGBA", (r, g, b, a))


def _render_auto(source: Image.Image, size: int) -> Image.Image:
    """Trim → Lanczos resize → alpha-binarize. The fallback path when no
    hand-painted icon exists at ``size``."""
    trimmed = _trim_alpha(source, padding_frac=0.04)
    out = _resize(trimmed, size) if trimmed.size != (size, size) else trimmed.copy()
    return _binarize_alpha(out)


def _render_at(
    size: int,
    *,
    hand: dict[int, Path],
    auto_master: Image.Image,
) -> tuple[Image.Image, str]:
    """Render the icon at ``size`` px. Returns (image, source-tag)."""
    if size in hand:
        return _load(hand[size]), f"HAND ({hand[size].name})"
    return _render_auto(auto_master, size), f"auto ({THICK_MASTER.name})"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    auto_master = _load(THICK_MASTER)
    hand = _discover_hand_painted()

    if hand:
        sizes_str = ", ".join(f"{s}px" for s in sorted(hand))
        print(f"hand-painted icons found: {sizes_str}")
    else:
        print(f"no hand-painted icons found; everything resampled from {THICK_MASTER.name}")
    print()

    work = {
        "32x32.png":      32,
        "128x128.png":    128,
        "128x128@2x.png": 256,
        "tray.png":       32,
    }

    for name, size in work.items():
        out, tag = _render_at(size, hand=hand, auto_master=auto_master)
        out.save(OUT_DIR / name, "PNG")
        print(f"  wrote {name} ({size}x{size}) [{tag}]")

    # icon.png is the in-app brand artwork (used by README header, social
    # cards, About dialog). Always use the full-detail thick master at
    # native 1024 -- no resize, no binarize.
    auto_master.save(OUT_DIR / "icon.png", "PNG")
    print(f"  wrote icon.png (1024x1024) [{THICK_MASTER.name} verbatim]")

    # Multi-resolution .ico for Windows. Per-size we pick the hand-painted
    # version if available, otherwise auto-resample from logo2. Includes
    # the standard sizes (16/24/32/48/64/128/256) plus any non-standard
    # hand-painted size like 28 (which Win11 uses on certain DPI configs).
    standard = {16, 24, 32, 48, 64, 128, 256}
    ico_targets = sorted(standard | set(hand))
    layers = []
    for size in ico_targets:
        img, tag = _render_at(size, hand=hand, auto_master=auto_master)
        layers.append(img)
    # Pillow's ICO writer: pass the largest as base, the rest via
    # append_images. Each one keeps its native size in the resulting .ico.
    base = layers[-1]
    base.save(
        OUT_DIR / "icon.ico",
        format="ICO",
        sizes=[(s, s) for s in ico_targets],
        append_images=layers[:-1],
    )
    print(f"  wrote icon.ico ({'/'.join(map(str, ico_targets))})")

    print(f"\ndone -> {OUT_DIR}")
    print(f"auto fallback master: {THICK_MASTER.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
