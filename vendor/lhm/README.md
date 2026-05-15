# LibreHardwareMonitor (vendored)

AT-Field bundles [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
(LHM) so the watchdog has access to VRAM-junction temperatures on
consumer NVIDIA GPUs and CPU package temperatures -- neither of which
is reachable through NVML or basic Windows APIs alone.

## Why we bundle it

- **Friction** — asking users to install a second app before AT-Field
  works defeats the "install once, forget" goal.
- **Lifecycle** — LHM running for as long as AT-Field is running, no
  more, no less. Tying it to the watchdog service's process tree means
  it dies cleanly when AT-Field is uninstalled.
- **Visibility** — crashes and restart loops surface in AT-Field's
  audit log instead of being silent.

## License

LHM is licensed under the [Mozilla Public License, version 2.0](https://www.mozilla.org/en-US/MPL/2.0/).

The MPL permits redistribution provided we:

1. Vendor the binaries **unmodified** (we do).
2. Make the source available (it's on GitHub, linked above).
3. Include the license text alongside the binaries (the LHM zip ships
   a `License.txt` we leave in place).
4. Note our use of LHM in our own license / about screen (see the
   "About AT-Field" tray menu and `LICENSE-third-party.md`).

If you build AT-Field from source you must comply with the same terms.
The `scripts/fetch_lhm.ps1` script is the canonical, automated way to
populate this directory.

## How the binaries get here

This directory is intentionally empty in source control. Binaries are
fetched at build time:

```pwsh
pwsh scripts/fetch_lhm.ps1
```

…which downloads a pinned LHM release from GitHub and extracts the
contents into `dist/atfield/` (next to AT-Field's frozen
`atfield-service.exe`). The Tauri NSIS installer then bundles that
whole directory as a resource. At runtime,
`lhm_supervisor.find_lhm_executable()` finds LHM via
`Path(sys.executable).parent` and supervises it as a child process.

Local dev sees the same path: run `fetch_lhm.ps1` once after a
PyInstaller build to test the bundled-LHM flow end-to-end.
