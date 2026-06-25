"""AT-Field — Windows GPU/VRAM/RAM watchdog for AI workloads.

Project goals (north stars; cite these when arguing about design choices):

1. **One-command install.** ``pip install atfield && atf install`` is the
   entire setup. No manual NSSM download, no manual sensor-daemon install.
2. **Works on most setups out of the box.** Capability is detected at
   startup; rules whose sensors aren't available are auto-disabled with a
   clear log message rather than failing the service.
3. **Zero config for the common case.** Shipped defaults protect a typical
   AI rig without the user opening ``config.toml``.

Public package surface is intentionally small at this stage; submodules are
imported on demand by the CLI (``atfield.cli``) and the service entrypoint
(``atfield.service``).
"""

from __future__ import annotations

__version__ = "0.4.2"

__all__ = ["__version__"]
