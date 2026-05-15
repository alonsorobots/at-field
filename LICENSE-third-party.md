# Third-party software bundled with AT-Field

AT-Field is MIT-licensed. The redistributable bundles
(`AT-Field_*-setup.exe`, `atfield-*-windows-x64.zip`) include the
following third-party components, each under its own license. None of
the components are modified.

## LibreHardwareMonitor (LHM)

- **License:** [Mozilla Public License, version 2.0](https://www.mozilla.org/en-US/MPL/2.0/)
- **Source:** https://github.com/LibreHardwareMonitor/LibreHardwareMonitor
- **Why bundled:** AT-Field uses LHM's HTTP server to read VRAM
  junction temperatures on consumer NVIDIA GPUs and CPU package
  temperatures, neither of which is reachable through NVML or
  Windows-native APIs alone.
- **How vendored:** `scripts/fetch_lhm.ps1` downloads a pinned LHM
  release archive from GitHub at build time and extracts it into
  `dist/atfield/` next to AT-Field's frozen binaries. The full LHM
  payload (including its `License.txt`) ships unmodified inside the
  installed product at `<InstallDir>\resources\atfield\`. See
  `vendor/lhm/README.md` for the full rationale.

## NSSM (Non-Sucking Service Manager)

- **License:** Public domain
- **Source:** https://nssm.cc/
- **Why bundled:** NSSM is the de-facto Windows service wrapper. Our
  installer downloads `nssm.exe` (2.24, win64) into the AT-Field
  state directory the first time the service is registered, and
  invokes it to register `atfield-service.exe` as a Windows service.
- **How vendored:** Downloaded on demand by `scripts/install_service.ps1`,
  not bundled inside the .exe (the file is small and the upstream URL
  has been stable since 2014).

## Python runtime + dependencies

The PyInstaller-frozen `atfield-service.exe` and `atf.exe` embed:

- **CPython 3.12** — [PSF License v2](https://docs.python.org/3/license.html).
- **psutil** — BSD-3-Clause.
- **nvidia-ml-py** — BSD-3-Clause.
- **typer / click / rich** — MIT.
- **requests** — Apache 2.0.

Their license texts ship inside `_internal/` alongside the runtime.

## Tauri tray app dependencies

The Tauri tray binary (`at-field-tray.exe`) is built against:

- **Tauri 2** — MIT or Apache 2.0 (dual).
- **WebView2 Runtime** — Microsoft Software License (preinstalled on
  Windows 11; Tauri ships its own copy on older Windows 10 if needed).
- **React 19, Vite 6, Tailwind CSS 4, Framer Motion** — MIT.

A full SBOM for each release is attached to the GitHub release page
once we wire up Syft / SPDX tooling (TBD).
