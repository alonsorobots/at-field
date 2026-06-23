"""LibreHardwareMonitor configuration writer.

Why this exists
---------------
LHM persists user preferences in ``LibreHardwareMonitor.config`` -- a
.NET ``appSettings`` XML file sitting next to its ``.exe``. We need
three of those settings to be a specific value for AT-Field to work:

* ``runWebServerMenuItem = True``   -- otherwise LHM doesn't expose
  ``http://127.0.0.1:<port>/data.json`` and the collector can't read it.
* ``webServerPortNumeric.Value = <port>`` -- the port the collector
  reads. Defaults to 8085 to match :mod:`atfield.collectors.lhm`.
* ``startMinMenuItem = True`` + ``minimizeToTrayMenuItem = True`` --
  so LHM doesn't pop its main window on every service start.

Plus one we'd really like:

* ``checkUpdatesAtStartMenuItem = False`` -- bundled LHM should not
  attempt to update itself to a different binary than the version the
  AT-Field supervisor knows about.

Why we don't ship a pre-baked file
----------------------------------
We tried that in v0.2. It broke between LHM 0.9.4 and 0.9.6: LHM 0.9.6
rewrites the file from its in-memory settings on first boot and
overwrites the pre-baked one before AT-Field gets to talk to its HTTP
server. The brittle path is "ship XML on disk and pray LHM doesn't
touch it". The robust path is "own the config, write it deterministically
before every spawn, and merge cleanly into whatever the user / a prior
LHM run wrote".

What this module does
---------------------
:func:`ensure_lhm_config` accepts the directory where LHM lives and:

* Reads any existing ``LibreHardwareMonitor.config``, preserving every
  ``<add key=... value=.../>`` setting that isn't one of ours.
* Prunes LHM's runaway per-sensor graph-history blobs (see
  ``_MAX_PRESERVED_VALUE_LEN``) so the file can't grow into the megabytes
  and stall LHM's HTTP server on startup.
* Overwrites only the keys AT-Field requires (above) with our values.
* Writes atomically (temp file + ``os.replace``) so a power loss
  mid-write can't leave LHM with a half-written XML file.
* Creates a minimal valid file if none exists yet.

Result: regardless of what LHM, the user, or a previous AT-Field
version left in that file, after we call ``ensure_lhm_config`` the
supervisor can trust that LHM, when launched, will come up with the
HTTP server on the right port and the window minimized.

Schema notes
------------
The .NET ``appSettings`` schema is::

    <configuration>
      <appSettings>
        <add key="..." value="..." />
      </appSettings>
    </configuration>

That has been stable since .NET Framework 2.0 (2005). We use
``xml.etree.ElementTree`` rather than a TOML library because the
file is XML and the stdlib already speaks XML.
"""

from __future__ import annotations

import logging
import os
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Final

__all__ = [
    "LHM_CONFIG_FILENAME",
    "REQUIRED_KEYS",
    "ensure_lhm_config",
]

_log = logging.getLogger("atfield.lhm_config")

LHM_CONFIG_FILENAME: Final = "LibreHardwareMonitor.config"

# LHM persists each sensor's *plotted graph history* straight into
# appSettings -- one key per sensor like
# ``/gpu-nvidia/0/temperature/0/values`` whose value is a serialized
# time-series that grows without bound on a long-lived instance. Observed
# in the wild: a single 24 MB config after ~2 weeks of continuous
# supervised running, at which point LHM's HTTP server stopped coming up
# within the supervisor's readiness window (the watchdog then sat in
# `process_up_no_http` and silently disabled the LHM-backed rules).
#
# None of these are settings AT-Field (or a headless LHM) needs, and every
# *real* setting LHM writes is short (booleans, ports, window geometry), so
# we drop any preserved value longer than this cap. Our own required keys
# are applied afterwards and are never affected.
_MAX_PRESERVED_VALUE_LEN: Final = 512


def _required_keys(port: int) -> dict[str, str]:
    """Return the key→value map AT-Field enforces in LHM's config.

    Kept as a function (not a constant) so the port is interpolated at
    call time -- the supervisor's port is configurable via
    :class:`LhmSupervisorConfig.port`.
    """
    return {
        "runWebServerMenuItem": "True",
        "webServerPortNumeric.Value": str(port),
        "startMinMenuItem": "True",
        "minimizeToTrayMenuItem": "True",
        "checkUpdatesAtStartMenuItem": "False",
    }


# Snapshot of the keys we manage. Tests / callers that want to know
# "is this an AT-Field-controlled key?" can use this without having to
# call _required_keys(0).
REQUIRED_KEYS: Final = frozenset(_required_keys(0).keys())


def ensure_lhm_config(lhm_dir: Path, *, port: int = 8085) -> Path:
    """Idempotently patch the LHM config in ``lhm_dir`` so AT-Field can
    talk to it.

    Parameters
    ----------
    lhm_dir :
        Directory holding ``LibreHardwareMonitor.exe``. The config file
        lives next to the binary as ``LibreHardwareMonitor.config``.
    port :
        HTTP port to enforce in ``webServerPortNumeric.Value``. Defaults
        to 8085 to match :mod:`atfield.collectors.lhm`.

    Returns
    -------
    Path
        The full path to the (now-correct) config file. Useful for
        callers that want to log it.

    Notes
    -----
    Safe to call repeatedly. Safe to call concurrently with LHM running
    -- LHM only re-reads the file on startup (and when the user opens
    Options inside the GUI), so a write while LHM is alive simply means
    the change takes effect on the next spawn.

    On corrupt XML in the existing file we *discard the existing file*
    rather than fail -- if LHM crashed mid-write and left garbage,
    fighting it is pointless and the user wants the watchdog to work.
    The discarded keys are logged at WARNING so we don't silently nuke
    user customization.
    """
    lhm_dir = Path(lhm_dir)
    lhm_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = lhm_dir / LHM_CONFIG_FILENAME

    required = _required_keys(port)

    # Load any existing config to preserve unrelated settings.
    existing: dict[str, str] = {}
    if cfg_path.exists():
        try:
            existing = _read_app_settings(cfg_path)
        except ET.ParseError as exc:
            _log.warning(
                "lhm config at %s is corrupt (%s); rewriting from scratch",
                cfg_path, exc,
            )
            existing = {}

    # Drop LHM's runaway per-sensor graph history before merging so the
    # written config stays small and LHM's web server comes up promptly.
    oversized = {k: v for k, v in existing.items() if len(v) > _MAX_PRESERVED_VALUE_LEN}
    if oversized:
        pruned_bytes = sum(len(v) for v in oversized.values())
        _log.warning(
            "pruning %d oversized LHM config value(s) (~%.1f MB of graph history) from %s",
            len(oversized), pruned_bytes / 1e6, cfg_path,
        )
        for k in oversized:
            existing.pop(k, None)

    merged = dict(existing)
    merged.update(required)  # our keys win

    _write_app_settings_atomic(cfg_path, merged)
    _log.debug("ensured lhm config at %s (%d keys)", cfg_path, len(merged))
    return cfg_path


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------


def _read_app_settings(path: Path) -> dict[str, str]:
    """Parse ``LibreHardwareMonitor.config`` and return the appSettings
    keys as a plain dict.

    Any ``<add>`` without both ``key`` and ``value`` attributes is
    silently skipped -- we won't pretend to understand malformed entries
    and we won't crash on them either.
    """
    tree = ET.parse(path)
    root = tree.getroot()
    out: dict[str, str] = {}
    for app_settings in root.iter("appSettings"):
        for add in app_settings.findall("add"):
            key = add.get("key")
            value = add.get("value")
            if key is not None and value is not None:
                out[key] = value
    return out


def _write_app_settings_atomic(path: Path, settings: dict[str, str]) -> None:
    """Write a complete ``LibreHardwareMonitor.config`` atomically.

    Atomic write semantics:
    1. Build the new content in a temp file in the same directory.
    2. ``os.replace`` swaps it into place (atomic on Windows + POSIX).
    3. A power loss mid-write leaves either the old file intact or the
       new file fully written -- never a half-written XML document.

    Same-directory placement matters because ``os.replace`` requires
    source and destination to be on the same filesystem to be atomic.
    """
    # Build the XML document. We construct manually rather than via
    # ElementTree's serializer so the output is deterministic and
    # diff-friendly across runs (no namespace prefix shuffling, stable
    # key ordering).
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        "<!--",
        "  AT-Field-managed LibreHardwareMonitor settings.",
        "",
        "  This file is rewritten by atfield.lhm_config.ensure_lhm_config()",
        "  on every service start. Manual edits to keys AT-Field manages",
        "  (runWebServerMenuItem, webServerPortNumeric.Value,",
        "  startMinMenuItem, minimizeToTrayMenuItem,",
        "  checkUpdatesAtStartMenuItem) will be reverted on the next",
        "  watchdog restart. Other keys are preserved untouched.",
        "-->",
        "<configuration>",
        "  <appSettings>",
    ]
    for key in sorted(settings.keys()):
        # XML attribute escaping for the value -- ET.tostring would do
        # this but at the cost of the deterministic layout above.
        value = (
            settings[key]
            .replace("&", "&amp;")
            .replace('"', "&quot;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        lines.append(f'    <add key="{key}" value="{value}" />')
    lines.append("  </appSettings>")
    lines.append("</configuration>")
    lines.append("")  # trailing newline
    payload = "\n".join(lines).encode("utf-8")

    # Same-directory temp file = atomic replace works on Windows.
    fd, tmp_name = tempfile.mkstemp(
        prefix=".lhmcfg.", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        os.replace(tmp_path, path)
    except Exception:
        # Best-effort cleanup of the temp file on failure; don't
        # mask the original error.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
