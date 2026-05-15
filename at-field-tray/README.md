# AT-Field tray app

User-mode tray icon + dashboard for the [AT-Field](../) watchdog service.

The watchdog itself is a Python Windows Service that runs as `LocalSystem`.
Windows Services can't render UI, so this is a **separate user-mode
process** that talks to the service over a localhost HTTP API
(`http://127.0.0.1:8765/`).

## What it does

- **Tray icon** that shows watchdog health at a glance: green (healthy),
  yellow (degraded / paused), red (recent kill action), gray (service down).
- **Right-click menu**: Show Dashboard, Pause for {30m, 1h, 4h, until reboot},
  Open `events.jsonl`, Open `watchdog.log`, About, Quit.
- **Dashboard window** with four tabs:
  - **Status** — service mode, collector cards with health, rule counts.
  - **Signals** — live sparklines per sensor with threshold lines.
  - **Rules** — current verdict, fraction-over bar, cooldown countdown.
  - **Events** — recent audit log entries with click-to-expand detail.

## Tech

- Rust + Tauri 2 (system webview, ~10 MB installer)
- React 19 + Vite 6 + Tailwind 4
- Framer Motion for tab transitions
- Aesthetic palette lifted from
  [VideoBricks](../../VideoBricks/src/styles/globals.css)

## Develop

Prerequisites: Node 18+, Rust 1.77+, plus
[Tauri's Windows prerequisites](https://tauri.app/start/prerequisites/)
(WebView2 runtime ships with Win10/11).

```bash
# From at-field-tray/
npm install
npm run tauri dev   # launches the Tauri shell with hot-reload
```

The frontend dev server runs on http://127.0.0.1:5174 (5173 is taken by
VideoBricks on the same dev box).

## Build a production installer

```bash
npm run tauri build
# Output: src-tauri/target/release/bundle/nsis/AT-Field_0.2.0_x64-setup.exe
```

The installer is currently per-user (`installMode: currentUser` in
`tauri.conf.json` — no admin required for the tray app itself). Wrapping
it with the Python watchdog service installer is tracked in the
[v0.2 plan doc](../docs/planning/v0.2-tray-app-plan.md) (Step 6 — single
bundled installer).

## Generate icons

The `src-tauri/icons/` set is generated programmatically by
`scripts/gen_icons.py` (uses Pillow). Re-run after editing
`gen_icons.py` if you tweak the AT monogram or palette.

```bash
python scripts/gen_icons.py
```
