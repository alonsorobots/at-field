# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: one onedir bundle, two console exes.

Output (in ``dist/atfield/``):

  atfield/
    atfield-service.exe   <- NSSM target
    atf.exe               <- CLI
    _internal/            <- shared Python runtime + deps (psutil, pynvml, etc.)

A single shared ``_internal`` keeps the bundle from doubling. Both exes are
built CONSOLE subsystem (so service stdout shows up in NSSM logs and the CLI
works in PowerShell). Build:

    pyinstaller --noconfirm --clean packaging/pyinstaller/atfield.spec

Tested with PyInstaller 6.20+ on Windows 11 / Python 3.10–3.14.
"""

from __future__ import annotations

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

REPO_ROOT = Path(os.getcwd()).resolve()
SRC = REPO_ROOT / "src"
ENTRY_DIR = REPO_ROOT / "packaging" / "pyinstaller"

# Hidden imports: PyInstaller's static analysis is good but not perfect for
# packages whose modules are imported lazily (CLI subcommands) or via plugin
# discovery (collectors).
HIDDEN = (
    collect_submodules("atfield")
    + [
        "psutil._psutil_windows",
        "psutil._pswindows",
        # pynvml ships as a single module; collect_submodules returns it.
        "pynvml",
        # Typer/Click/Rich completion modules pulled in via shutil at runtime.
        "rich.logging",
    ]
)

EXCLUDES = [
    # Test infra never needed in a service binary; trims the bundle.
    "pytest",
    "_pytest",
    "pluggy",
    "ruff",
    "mypy",
    # Notebook stack (none of which we depend on, but PyInstaller often
    # discovers them via transitive site-packages on dev machines).
    "IPython",
    "jupyter",
    "notebook",
    "tornado",
    # PIL is in the venv for the icon generator script; not a runtime dep.
    "PIL",
]

# ─────────────────────────────────────────────────────────────────────────────
# Analysis -- shared between both exes via MERGE
# ─────────────────────────────────────────────────────────────────────────────

# Datas: copy the install/uninstall scripts and config example next to the
# bundled exes. atf.exe (frozen) looks for ``scripts/install_service.ps1``
# beside ``sys.executable`` -- see _find_script() in cli.py.
SCRIPTS = REPO_ROOT / "scripts"
SHARED_DATAS = [
    (str(SCRIPTS / "install_service.ps1"),       "scripts"),
    (str(SCRIPTS / "uninstall_service.ps1"),     "scripts"),
    (str(SCRIPTS / "grant_service_control.ps1"), "scripts"),
    (str(SCRIPTS / "config.example.toml"),       "scripts"),
]

a_service = Analysis(
    [str(ENTRY_DIR / "atfield_service_entry.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=SHARED_DATAS,
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)

a_cli = Analysis(
    [str(ENTRY_DIR / "atf_entry.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=SHARED_DATAS,
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)

# MERGE deduplicates shared modules across both exes so we don't ship Python
# twice. The first arg of each tuple is the analysis; second is the binary's
# logical name; third is the on-disk filename.
MERGE(
    (a_service, "atfield-service", "atfield-service"),
    (a_cli, "atf", "atf"),
)

# ─────────────────────────────────────────────────────────────────────────────
# atfield-service.exe
# ─────────────────────────────────────────────────────────────────────────────

pyz_service = PYZ(a_service.pure)

exe_service = EXE(
    pyz_service,
    a_service.scripts,
    [],
    exclude_binaries=True,
    name="atfield-service",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                  # UPX hurts startup time on Windows; not worth it
    console=True,               # NSSM captures stdout/stderr
    disable_windowed_traceback=False,
    icon=str(REPO_ROOT / "at-field-tray" / "src-tauri" / "icons" / "icon.ico"),
)

# ─────────────────────────────────────────────────────────────────────────────
# atf.exe
# ─────────────────────────────────────────────────────────────────────────────

pyz_cli = PYZ(a_cli.pure)

exe_cli = EXE(
    pyz_cli,
    a_cli.scripts,
    [],
    exclude_binaries=True,
    name="atf",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    icon=str(REPO_ROOT / "at-field-tray" / "src-tauri" / "icons" / "icon.ico"),
)

# ─────────────────────────────────────────────────────────────────────────────
# Shared bundle
# ─────────────────────────────────────────────────────────────────────────────

coll = COLLECT(
    exe_service,
    a_service.binaries,
    a_service.datas,
    exe_cli,
    a_cli.binaries,
    a_cli.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="atfield",
)
