"""Generate placeholder icons for the AT-Field tray app.

Draws a hexagon containing an "AT" monogram in the AT-Field accent purple
on the dark theme background. Generates the full Tauri icon set:

  icons/
    32x32.png
    128x128.png
    128x128@2x.png
    icon.png
    icon.ico
    tray.png

These are PLACEHOLDERS suitable for the v0.2 first cut. A polished hex/AT
mark by an actual designer should replace them before v0.2.0 ships.

Usage (from at-field-tray/):
    python scripts/gen_icons.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT_DIR = Path(__file__).resolve().parent.parent / "src-tauri" / "icons"

ACCENT = (167, 139, 250, 255)        # #a78bfa
ACCENT_DARK = (139, 92, 246, 255)    # #8b5cf6
BG = (27, 20, 31, 255)               # #1b141f
SURFACE = (41, 36, 49, 255)          # #292431


def draw_icon(size: int, *, transparent_bg: bool = True) -> Image.Image:
    """Render the AT-Field mark at ``size``×``size`` pixels."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0) if transparent_bg else BG)
    draw = ImageDraw.Draw(img)

    cx, cy = size / 2, size / 2
    radius = size * 0.46
    # Hexagon with flat top -- compute six vertices.
    points = []
    for i in range(6):
        angle = math.pi / 3 * i + math.pi / 6  # rotate so top is flat
        x = cx + radius * math.cos(angle)
        y = cy + radius * math.sin(angle)
        points.append((x, y))

    # Outer hex stroke + fill: a slightly-darker surface base with a
    # crisp accent border so the icon reads against any tray background.
    draw.polygon(points, fill=SURFACE, outline=ACCENT, width=max(1, size // 32))

    # Inset hex glow.
    inset = []
    inset_radius = radius * 0.78
    for i in range(6):
        angle = math.pi / 3 * i + math.pi / 6
        x = cx + inset_radius * math.cos(angle)
        y = cy + inset_radius * math.sin(angle)
        inset.append((x, y))
    draw.polygon(inset, fill=ACCENT_DARK)

    # "AT" monogram, centered.
    label = "AT"
    font_size = int(size * 0.42)
    font = _load_font(font_size)
    bbox = draw.textbbox((0, 0), label, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = cx - tw / 2 - bbox[0]
    ty = cy - th / 2 - bbox[1]
    draw.text((tx, ty), label, fill=(240, 239, 242, 255), font=font)

    return img


def _load_font(size: int) -> ImageFont.ImageFont:
    """Pick a bold sans font: try Segoe UI Bold, then arial, then default."""
    for candidate in (
        r"C:\Windows\Fonts\segoeuib.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\arial.ttf",
    ):
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    sizes = {
        "32x32.png": 32,
        "128x128.png": 128,
        "128x128@2x.png": 256,
        "icon.png": 1024,
        "tray.png": 32,
    }

    for name, size in sizes.items():
        img = draw_icon(size)
        img.save(OUT_DIR / name, "PNG")
        print(f"  wrote {name} ({size}x{size})")

    # Multi-resolution .ico for Windows.
    ico_sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    base = draw_icon(256)
    base.save(OUT_DIR / "icon.ico", format="ICO", sizes=ico_sizes)
    print("  wrote icon.ico (16/32/48/64/128/256)")

    print(f"\ndone -> {OUT_DIR}")


if __name__ == "__main__":
    main()
