"""PyInstaller entry point for the ``atf`` CLI binary.

Mirror of the ``atf`` / ``atfield`` console_scripts in pyproject.toml,
expressed as a real .py file for PyInstaller's benefit.
"""

from __future__ import annotations

import sys


def _force_utf8_console() -> None:
    """Make stdout/stderr UTF-8 inside the PyInstaller-frozen exe.

    Rich/Typer emits Unicode (ASCII box-drawing, ≤, →, etc.) when it
    detects a "rich"-capable terminal. PyInstaller-built consoles default
    to the legacy code page on Windows (cp1252), which crashes on the
    first non-ASCII char rendered. Forcing utf-8 here matches the
    behavior of the editable install when run in PowerShell or VS Code's
    integrated terminal.
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                # Best-effort -- if reconfigure fails (e.g. pipe redirect to a
                # binary stream), fall through and let the caller deal with it.
                pass


_force_utf8_console()

from atfield.cli import app  # noqa: E402  (must come after console fix)


if __name__ == "__main__":
    app()
