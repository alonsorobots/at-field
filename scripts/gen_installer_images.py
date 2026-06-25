"""Generate the NSIS installer branding images from the AT-Field logo.

Tauri's NSIS bundler accepts three pieces of installer chrome:

  * ``installerIcon``  -- the .ico shown for setup.exe (we reuse icons/icon.ico)
  * ``headerImage``    -- 150x57 BMP, top strip of the inner wizard pages
  * ``sidebarImage``   -- 164x314 BMP, left panel of the Welcome / Finish pages

Without these NSIS falls back to its generic blue/grey artwork, which is why
the installed setup.exe looked unbranded. This script composites the orange
hexagon logo (at-field-tray/public/app-icon.png) onto backgrounds sized for
each slot and writes 24-bit BMPs (the format the NSIS MUI expects).

Run from the repo root with the project venv:

    .venv/Scripts/python.exe scripts/gen_installer_images.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent.parent
LOGO = REPO / "at-field-tray" / "public" / "app-icon.png"
OUT = REPO / "at-field-tray" / "src-tauri" / "installer"

# Tokyo-3 dark + the logo's orange.
DARK = (13, 17, 23)
ORANGE = (255, 102, 0)
GREY = (148, 158, 170)


def _load_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    candidates = [
        r"C:\Windows\Fonts\segoeuib.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
    ]
    for c in candidates:
        if Path(c).exists():
            return ImageFont.truetype(c, size)
    return ImageFont.load_default()


def _logo(height: int) -> Image.Image:
    img = Image.open(LOGO).convert("RGBA")
    w, h = img.size
    scale = height / h
    return img.resize((int(w * scale), height), Image.LANCZOS)


def make_header() -> None:
    # White header strip so it blends with the MUI page background; NSIS draws
    # its own page title text on the left, so we only place the logo at right.
    canvas = Image.new("RGB", (150, 57), (255, 255, 255))
    logo = _logo(46)
    x = 150 - logo.width - 6
    y = (57 - logo.height) // 2
    canvas.paste(logo, (x, y), logo)
    canvas.save(OUT / "header.bmp")


def make_sidebar() -> None:
    canvas = Image.new("RGB", (164, 314), DARK)
    draw = ImageDraw.Draw(canvas)

    logo = _logo(112)
    lx = (164 - logo.width) // 2
    canvas.paste(logo, (lx, 40), logo)

    wordmark = _load_font(22, bold=True)
    tag = _load_font(11, bold=False)

    def centered(text: str, font: ImageFont.ImageFont, y: int, fill) -> None:
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        draw.text(((164 - tw) // 2, y), text, font=font, fill=fill)

    centered("AT-FIELD", wordmark, 178, ORANGE)
    centered("hardware watchdog", tag, 208, GREY)

    canvas.save(OUT / "sidebar.bmp")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    make_header()
    make_sidebar()
    print(f"wrote {OUT / 'header.bmp'} (150x57) and {OUT / 'sidebar.bmp'} (164x314)")


if __name__ == "__main__":
    main()
