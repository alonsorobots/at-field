# Packaging AT-Field

There are three artifacts AT-Field ships, each built independently:

| Artifact                              | Built by              | Used by                             |
| ------------------------------------- | --------------------- | ----------------------------------- |
| `atfield-X.Y.Z-py3-none-any.whl`      | `python -m build`     | Power users (`pip install atfield`) |
| `dist/atfield/` (onedir, two console exes) | PyInstaller spec | Bundled into the NSIS installer     |
| `AT-Field_X.Y.Z_x64-setup.exe`        | Tauri/NSIS            | The end user. Just double-click.    |

This doc covers the **PyInstaller bundle** and how it's assembled into the
end-user installer. The Tauri app itself (frontend / Rust dev workflow) is
documented in `at-field-tray/README.md`; end-user install/verification is in
[`docs/install.md`](install.md).

## What the PyInstaller bundle contains

```
dist/atfield/
  atfield-service.exe   <- NSSM target; runs as LocalSystem
  atf.exe               <- CLI for the user (status, pause, doctor, etc.)
  atfield-sensors.exe   <- headless LHM sensor helper (built by build_helper.ps1)
  LibreHardwareMonitor.exe + *.dll  <- vendored by fetch_lhm.ps1
  _internal/            <- shared Python runtime + dependencies
    scripts/            <- install/uninstall/grant PowerShell + config example
```

The install scripts are PyInstaller `datas`, so in the onedir layout they
land under `_internal/scripts/` (not a top-level `scripts/`). The tray's
`service_installer.rs` and `install_service.ps1` both account for this.

Both exes share `_internal/` (deduplicated via PyInstaller's `MERGE`), so the
total bundle is ~36 MB rather than ~70 MB it would be if each was built
standalone. Both are *console* subsystem so NSSM captures stdout/stderr and
the CLI prints normally in PowerShell.

## Building locally

```pwsh
# One-time setup
pip install -e .[build]

# Build (cleans previous output)
pyinstaller --noconfirm --clean packaging/pyinstaller/atfield.spec
```

Build time on a recent dev machine is ~30 s. Output lands in
`dist/atfield/`.

## Smoke-testing the bundle

After a build:

```pwsh
# CLI works
.\dist\atfield\atf.exe --help
.\dist\atfield\atf.exe doctor

# Service binary boots, binds :8765, answers /health
.\dist\atfield\atfield-service.exe --state-dir C:\temp\atf_smoke
# in another shell
curl http://127.0.0.1:8765/health
```

If `atf doctor` reports the NVML and system collectors as `OK`, the bundle
contains the right native deps. The LHM check will be `UNAVAILABLE` unless
LibreHardwareMonitor is also running -- that's expected.

## Building the full installer locally

The PyInstaller bundle above is only the watchdog half. To produce the
end-user `AT-Field_X.Y.Z_x64-setup.exe`, stage the bundle (PyInstaller + LHM +
sensor helper) and run the Tauri/NSIS build:

```pwsh
# 1. Frozen Python bundle (see "Building locally" above)
pyinstaller --noconfirm --clean packaging/pyinstaller/atfield.spec

# 2. Vendor LibreHardwareMonitor into dist/atfield  (needs internet)
pwsh scripts/fetch_lhm.ps1

# 3. Compile the headless sensor helper -> dist/atfield/atfield-sensors.exe
pwsh scripts/build_helper.ps1

# 4. If AT-Field is ALREADY installed on THIS machine, stop the service first
#    (see the file-lock gotcha below):
Stop-Service ATFieldWatchdog        # elevated

# 5. Build the installer (frontend + Rust + NSIS)
cd at-field-tray && npm ci && npm run tauri build

# 6. Restart the watchdog if you stopped it in step 4
Start-Service ATFieldWatchdog
```

Output:
`at-field-tray/src-tauri/target/release/bundle/nsis/AT-Field_<ver>_x64-setup.exe`.

> **File-lock gotcha (local builds only).** A running watchdog holds open
> handles to `atfield-sensors.exe` / `LibreHardwareMonitorLib.dll`. Tauri's
> resource copy silently *skips* locked files, producing an installer with
> broken sensors. Always stop the service before a local installer build
> (step 4). CI is immune -- the runner has no service installed, which is why
> the GitHub Release artifact is the one to distribute.

To sanity-check a built installer without a clean VM, extract it and confirm
the sensor files staged:

```pwsh
& "C:\Program Files\7-Zip\7z.exe" x AT-Field_<ver>_x64-setup.exe "atfield/*" -oC:\temp\atf_check
# expect: atf.exe, atfield-service.exe, atfield-sensors.exe,
#         LibreHardwareMonitor.exe, LibreHardwareMonitorLib.dll,
#         _internal\scripts\install_service.ps1
```

## Hidden imports

PyInstaller's static analysis catches most of `atfield`, but a few modules
are imported lazily and need to be listed in `HIDDEN` in `atfield.spec`:

- `psutil._psutil_windows`, `psutil._pswindows` -- psutil's Windows backends
- `pynvml` -- single-module package
- `rich.logging` -- pulled in transitively by Typer's error formatter

If you add a new optional dependency that's imported lazily (e.g. an
ADLX/ROCm collector), add its top-level module to `HIDDEN` and rebuild.

## Excludes

We strip pytest, ruff, mypy, IPython/Jupyter, and Pillow from the bundle.
Pillow is in the dev venv only because `scripts/gen_icons.py` uses it; it's
not a runtime dependency.

## CI

`.github/workflows/release.yml` builds this bundle on every `v*` tag push
(and via `workflow_dispatch`). The `standalone` job runs, in order:

1. `pyinstaller --noconfirm --clean packaging/pyinstaller/atfield.spec`
2. `scripts/fetch_lhm.ps1` — vendors LibreHardwareMonitor + its DLLs
3. `scripts/build_helper.ps1` — compiles `atfield-sensors.exe` (in-box csc)

It then zips `dist/atfield/*` as the `standalone-bundle` artifact
(`atfield-<version>-windows-x64.zip`). The `tray-installer` job downloads
that zip, re-stages it at `dist/atfield/`, and runs the Tauri/NSIS build —
so the single installer ships both the tray app and a complete, sensor-ready
watchdog. End-user install/verification lives in [`docs/install.md`](install.md).

## Publishing to PyPI (Trusted Publishing)

`pip install atfield` is served from PyPI. The `publish-pypi` job in
`release.yml` uploads the wheel + sdist using **Trusted Publishing** — PyPI
verifies GitHub's OIDC identity, so there are **no API tokens or repo secrets**
to manage. One-time setup on the maintainer's PyPI account:

1. Create/sign in to a PyPI account and enable 2FA (mandatory).
2. Go to **Account → Publishing → Add a new pending publisher** with:
   - **PyPI Project Name:** `atfield`
   - **Owner:** `alonsorobots`
   - **Repository name:** `at-field`
   - **Workflow name:** `release.yml`
   - **Environment name:** `pypi`
3. (Optional) In the GitHub repo, create an Environment named `pypi`
   (Settings → Environments) for an extra approval gate.

The next non-prerelease `v*` tag then publishes automatically and claims the
`atfield` name. Until this is configured, the `publish-pypi` job fails but does
**not** block the GitHub Release (installer + wheel still ship there).

## NSIS installer artwork

The branded setup chrome (`installerIcon`, `headerImage`, `sidebarImage` in
`tauri.conf.json`) comes from `at-field-tray/src-tauri/installer/`. Regenerate
the BMPs from the logo with:

```pwsh
.venv/Scripts/python.exe scripts/gen_installer_images.py
```

NSIS requires 24-bit BMPs at exactly 150×57 (header) and 164×314 (sidebar);
the script enforces those sizes.

## Known caveats

- **Icon path** in the spec assumes the Tauri tray icon exists at
  `at-field-tray/src-tauri/icons/icon.ico`. If you run PyInstaller before
  the tray scaffold is bootstrapped, change or remove the `icon=` line.
- The bundle is **not signed**. The NSIS installer wraps it with the same
  unsigned-binary friction; signing is on the v1.0 milestone, not v0.2.
- **Antivirus false positives** are possible with PyInstaller bundles. We
  haven't seen any with Defender on a freshly-built artifact, but mileage
  may vary on AV products that flag uncommon entry-point patterns.
