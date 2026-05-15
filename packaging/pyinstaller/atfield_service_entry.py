"""PyInstaller entry point for the watchdog Windows Service binary.

This is what NSSM invokes after the bundled installer registers the service.
Equivalent to the ``atfield-service`` console_script in pyproject.toml; we
just need a concrete .py file here because PyInstaller doesn't (yet) build
from setuptools entry-point names.
"""

from __future__ import annotations

import sys


def _force_utf8_console() -> None:
    """See atf_entry._force_utf8_console -- same fix, same reason. NSSM
    captures stdout/stderr from this binary into log files and chokes on
    the cp1252 default if any rich/Typer message is logged before the
    service swaps in its own RotatingFileHandler.
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_force_utf8_console()

from atfield.service import main  # noqa: E402  (must come after console fix)


if __name__ == "__main__":
    sys.exit(main())
