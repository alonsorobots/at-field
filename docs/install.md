# Installing AT-Field on another computer

This guide is for getting AT-Field running on a **fresh Windows machine**
(e.g. host-c or host-b) the way a casual user would, and for verifying it
actually works end-to-end.

If you just want the short version: **download the installer, double-click
it, then click "Install watchdog" once in the app.** Everything else below
is verification and troubleshooting.

---

## 1. What to install

There is one file a normal user needs:

```
AT-Field_<version>_x64-setup.exe      e.g. AT-Field_0.3.0_x64-setup.exe
```

It bundles *everything*: the tray app, the watchdog service binaries, the
Python runtime, and the LibreHardwareMonitor sensor stack (so CPU / VRAM /
PSU temps work out of the box — no separate downloads).

Where to get it:

- **GitHub Release** — the recommended source. Each `v*` tag publishes the
  installer as a release asset (`tray-installer`).
- **A local build** — see [§6](#6-producing-a-fresh-installer). The latest
  locally-built copy in this repo lives at
  `dist/AT-Field_<version>_x64-setup.exe`.

> `.exe` vs `pip`: the installer is the low-friction path for people who
> just want to try it. `pip install atfield` + `atf install` is the
> power-user path and is documented in the top-level `README.md`.

---

## 2. Install on a clean machine

1. Copy `AT-Field_<version>_x64-setup.exe` to the target machine.
2. Double-click it.
3. **SmartScreen** will likely warn ("Windows protected your PC") because
   the binary isn't code-signed yet. Click **More info → Run anyway**.
   This is expected for unsigned installers and is on the v1.0 to-do list.
4. The installer is a *current-user* install — it does **not** need admin
   and drops the app under `%LOCALAPPDATA%`. It finishes in a few seconds.
5. The tray app launches. On first launch you'll get a toast: *"AT-Field is
   watching."* Windows 11 hides new tray icons in the `^` overflow — click
   the chevron in the taskbar and drag the AT-Field icon out if you want it
   always visible.

### Register the watchdog (one UAC prompt)

The tray app runs as you (no admin). The **watchdog service** needs to be
registered as a Windows service, which requires admin **once**:

1. Open the dashboard (click the tray icon).
2. Go to the **Setup / Status** screen. If the service isn't installed yet
   it offers an **Install watchdog** button.
3. Click it and **accept the UAC prompt**. This runs the bundled
   `install_service.ps1` elevated, which:
   - downloads NSSM (the service wrapper) into `%ProgramData%\ATField`,
   - registers the `ATFieldWatchdog` service (auto-start, LocalSystem),
   - auto-detects the bundled `LibreHardwareMonitor.exe` + `atfield-sensors.exe`
     and bakes their paths into the service environment, and
   - starts the service.

That's the only elevation in the whole flow. After this, the watchdog
auto-starts at every boot and the tray reconnects automatically.

---

## 3. Verify it works

From the **dashboard** (easiest):

- **Status** page → the three collectors should read **HEALTHY**:
  - `system` (CPU/RAM via psutil),
  - `nvml` (NVIDIA GPU — only on machines with an NVIDIA card),
  - `lhm` (LibreHardwareMonitor — CPU package temp, VRAM temp, PSU rails).
- **Signals** page → live sparklines update every couple of seconds.

If `lhm` is HEALTHY, the sensor helper shipped and was detected correctly —
that's the main thing this build was hardened for.

From a terminal (power users), using the bundled CLI staged next to the app
(`...\resources\atfield\atf.exe`) or `atf` if pip-installed:

```pwsh
atf status     # heartbeat + working signal map
atf inputs     # one-shot collector probe + a sample of every signal
Get-Service ATFieldWatchdog
curl http://127.0.0.1:8765/health
```

`/health` should report `"version": "<this release>"`, `"mode": "armed"`,
and every collector with `"available": true`.

> On a machine **without** an NVIDIA GPU the `nvml` collector will be
> `UNAVAILABLE` — that's correct, not a bug. `system` and `lhm` still work.

---

## 4. "Simulating" a clean-machine experience

You don't need to wipe a machine to sanity-check the new-user path:

- **A machine that has never had AT-Field** (host-c / host-b) is the real
  test. Run §2–§3 there.
- **On a dev machine**, the closest simulation without disrupting your setup
  is to extract the installer and inspect the staged layout:

  ```pwsh
  & "C:\Program Files\7-Zip\7z.exe" x AT-Field_0.3.0_x64-setup.exe "atfield/*" -oC:\temp\atf_check
  ```

  Confirm these exist (the files that make sensors work):
  `atfield\atf.exe`, `atfield\atfield-service.exe`, `atfield\atfield-sensors.exe`,
  `atfield\LibreHardwareMonitor.exe`, `atfield\LibreHardwareMonitorLib.dll`, and
  `atfield\_internal\scripts\install_service.ps1`.

  The tray finds the install script under `_internal\scripts\` (and a flat
  `scripts\` if present), and `install_service.ps1` locates LHM + the helper
  whether they sit next to the script or one directory up — so both Tauri
  staging layouts work.

---

## 5. Updating / reinstalling

Re-running the installer over an existing install is safe. To refresh the
watchdog itself after an update, click **Install watchdog** again (it stops
and re-registers the service cleanly — the script is idempotent).

If you want routine `Restart-Service ATFieldWatchdog` without a UAC prompt
each time, run the bundled helper once (elevated):

```pwsh
powershell -ExecutionPolicy Bypass -File "<install>\resources\atfield\_internal\scripts\grant_service_control.ps1"
```

---

## 6. Producing a fresh installer

### Preferred: CI (clean, reproducible)

Push a `v*` tag, or run the **Release** workflow via *workflow_dispatch*.
`.github/workflows/release.yml` builds the wheel, the PyInstaller bundle
(`pyinstaller` → `fetch_lhm.ps1` → `build_helper.ps1`), and the Tauri NSIS
installer on clean `windows-latest` runners, then attaches them to the
release. Grab the `tray-installer` artifact.

### Local build (on a Windows dev box)

```pwsh
# 1. Frozen Python bundle (atf.exe, atfield-service.exe, _internal/)
pyinstaller --noconfirm --clean packaging/pyinstaller/atfield.spec

# 2. Vendor LibreHardwareMonitor into dist/atfield  (needs internet)
pwsh scripts/fetch_lhm.ps1

# 3. Compile the headless sensor helper -> dist/atfield/atfield-sensors.exe
pwsh scripts/build_helper.ps1

# 4. If AT-Field is ALREADY installed on THIS machine, stop the service
#    first -- its running helper locks files Tauri needs to copy:
Stop-Service ATFieldWatchdog        # elevated, or after grant_service_control.ps1

# 5. Build the installer (frontend + Rust + NSIS)
cd at-field-tray
npm ci
npm run tauri build

# 6. Restart the watchdog if you stopped it in step 4
Start-Service ATFieldWatchdog
```

Output:
`at-field-tray\src-tauri\target\release\bundle\nsis\AT-Field_<ver>_x64-setup.exe`

> **Gotcha (local builds only):** the watchdog's running sensor helper
> holds an open handle to `atfield-sensors.exe` / `LibreHardwareMonitorLib.dll`.
> Tauri's directory copy silently *skips* locked files, producing an
> installer with broken sensors. Always stop the service before a local
> installer build (step 4). CI is immune because the runner has no service
> installed.

---

## 7. Uninstalling

- **App**: Windows *Settings → Apps → AT-Field → Uninstall* (or the Start-menu
  uninstaller).
- **Watchdog service**: use the tray's **Uninstall watchdog** (UAC), or run
  `<install>\resources\atfield\_internal\scripts\uninstall_service.ps1`
  elevated. This removes the `ATFieldWatchdog` service and NSSM wrapper.
  State under `%ProgramData%\ATField` (config, logs, `events.jsonl`) is left
  in place; delete it manually if you want a truly clean slate.

---

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| SmartScreen blocks the installer | Unsigned binary. *More info → Run anyway*. |
| Tray icon missing after install | Windows 11 overflow. Click the taskbar `^` and drag it out. |
| `lhm` collector `FAILED`/`UNAVAILABLE` | The install ran before LHM was detected, or sensors are off. Click **Install watchdog** again; confirm `atfield-sensors.exe` + `LibreHardwareMonitor.exe` are in `...\resources\atfield\`. |
| `nvml` `UNAVAILABLE` | Expected on machines without an NVIDIA GPU. |
| Service won't start | Check `%ProgramData%\ATField\service.stderr.log`. |
| "Install watchdog" greyed out / "missing bundled watchdog" | The installer didn't stage the watchdog (a locked-file local build — see §6 gotcha). Rebuild with the service stopped, or use the CI artifact. |
