# Installing AT-Field

How to get AT-Field running on a Windows machine and confirm it works.

**Short version: download the installer, double-click it, accept the one UAC
prompt. Done.** The installer registers the watchdog service for you — there's
no separate in-app step. Everything below is just verification and
troubleshooting.

> Building the installer from source (CI, local Tauri/NSIS build, the
> file-lock gotcha) is a maintainer topic and lives in
> [`docs/packaging.md`](packaging.md).

---

## 1. Download

You need one file:

```
AT-Field_<version>_x64-setup.exe      e.g. AT-Field_0.4.0_x64-setup.exe
```

Get it from the **[latest GitHub Release](https://github.com/alonsorobots/at-field/releases/latest)**
(the `tray-installer` asset). It bundles *everything* — the tray app, the
watchdog service, the Python runtime, and the LibreHardwareMonitor sensor
stack — so CPU / VRAM / PSU temps work out of the box with no extra downloads.

> Prefer Python? `pip install atfield` + `atf install` is the power-user path,
> documented in the top-level [`README.md`](../README.md).

---

## 2. Install (one UAC prompt)

1. Double-click `AT-Field_<version>_x64-setup.exe`.
2. **SmartScreen** may warn ("Windows protected your PC") because the binary
   isn't code-signed yet — click **More info → Run anyway**. (Signing is on the
   v1.0 roadmap.)
3. **Accept the UAC prompt.** This is a per-machine install: it needs admin
   once, installs to `C:\Program Files\AT-Field`, and — in its post-install
   step — registers the watchdog for you. That single consent covers the whole
   setup.
4. The tray app launches with a *"AT-Field is watching."* toast. Windows 11
   hides new tray icons in the `^` overflow — click the chevron and drag the
   AT-Field icon out if you want it always visible.

By the time setup closes, the watchdog is already running and auto-starts at
every boot.

> The dashboard's **Setup / Status** screen has **Install / Uninstall
> watchdog** buttons. These are a *repair / fallback* path (each prompts for
> UAC) — handy if the service ever needs re-registering. You don't need them
> for a normal install.

---

## 3. Verify it works

From the **dashboard** (easiest) — click the tray icon:

- **Status** page → collectors should read **HEALTHY**:
  - `system` (CPU / RAM),
  - `nvml` (NVIDIA GPU — only present on machines with an NVIDIA card),
  - `lhm` (LibreHardwareMonitor — CPU package temp, VRAM temp, PSU rails).
- **Signals** page → live sparklines update every couple of seconds.

From a terminal (optional), using the bundled CLI at
`C:\Program Files\AT-Field\resources\atfield\atf.exe` (or `atf` if pip-installed):

```pwsh
atf status     # heartbeat + working signal map
atf inputs     # one-shot collector probe + a sample of every signal
Get-Service ATFieldWatchdog
curl http://127.0.0.1:8765/health
```

`/health` should report this release's `version`, `"mode": "armed"`, and every
collector `"available": true`.

> On a machine **without** an NVIDIA GPU, `nvml` will be `UNAVAILABLE` — that's
> correct, not a bug. `system` and `lhm` still work.

---

## 4. Updating / reinstalling

Re-running the installer over an existing install is safe — its post-install
hook re-registers the watchdog cleanly (the script is idempotent: it stops,
removes, and re-creates the service). You can also refresh the watchdog without
reinstalling by clicking **Install watchdog** on the Status screen.

If you want routine `Restart-Service ATFieldWatchdog` without a UAC prompt each
time, run the bundled helper once (elevated):

```pwsh
powershell -ExecutionPolicy Bypass -File "C:\Program Files\AT-Field\resources\atfield\_internal\scripts\grant_service_control.ps1"
```

---

## 5. Uninstalling

- **App + watchdog together**: *Settings → Apps → AT-Field → Uninstall* (or the
  Start-menu uninstaller). The pre-uninstall hook removes the `ATFieldWatchdog`
  service and its NSSM wrapper before deleting files — one step, no leftovers.
- **Watchdog only** (e.g. to re-register): the tray's **Uninstall watchdog**
  button (UAC).
- State under `%ProgramData%\ATField` (config, logs, `events.jsonl`) is left in
  place either way; delete it manually for a truly clean slate.

---

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| SmartScreen blocks the installer | Unsigned binary. *More info → Run anyway*. |
| Tray icon missing after install | Windows 11 overflow. Click the taskbar `^` and drag it out. |
| `lhm` collector `FAILED` / `UNAVAILABLE` | Click **Install watchdog** on the Status screen to re-register; confirm `atfield-sensors.exe` + `LibreHardwareMonitor.exe` are in `...\resources\atfield\`. |
| `nvml` `UNAVAILABLE` | Expected on machines without an NVIDIA GPU. |
| Service won't start | Check `%ProgramData%\ATField\service.stderr.log`. |
| "Install watchdog" greyed out / "missing bundled watchdog" | The installer didn't stage the watchdog (a locked-file local build — see [packaging.md](packaging.md)). Use the GitHub Release artifact. |
