# AT-Field brand assets

Source-of-truth artwork for AT-Field. Anything in `at-field-tray/src-tauri/icons/`
and packaging artifacts is **derived** from these masters via
`at-field-tray/scripts/gen_icons.py` — don't edit the derived PNGs/ICOs by hand,
edit the master here and re-run the generator.

## Files

- **`logo_1024.png`** — 1024×1024 RGBA. Three concentric distressed-paint
  hexagons in the AT-Field accent orange. Inspired by the *Neon Genesis
  Evangelion* AT-Field motif (layered octahedral shielding rendered
  head-on). Use anywhere the full mark is appropriate: app icon, README
  header, installer artwork, social cards, etc.
- **`logo_1024_thick.png`** — alternate 1024×1024 master with thicker
  hexagon strokes. Used by `gen_icons.py` as the auto-resample fallback
  when no hand-painted size exists; the thicker strokes survive Lanczos
  downscale better than the default master.
- **`logo_16.png`, `logo_28.png`, `logo_32.png`, `logo_48.png`** — hand-
  painted small sizes. The generator prefers these over auto-resampled
  versions because per-pixel hand control beats Lanczos at small sizes.
  Drop a new `logo_NN.png` in this folder to add another hand-painted
  size (it must be exactly NN×NN px or the generator will refuse).

## Regenerating derived assets

From repo root:

```bash
.venv/Scripts/python.exe at-field-tray/scripts/gen_icons.py
```

That writes the full Tauri icon set (32/128/128@2x/icon.png, multi-resolution
icon.ico, and the small tray.png) into `at-field-tray/src-tauri/icons/`.
Then rebuild whatever consumes them:

- Tray app: `cd at-field-tray && npm run tauri build` (or `cargo build --release`)
- Service bundle: `python -m PyInstaller --noconfirm --clean packaging/pyinstaller/atfield.spec`

## Color references

Keep these in sync with `at-field-tray/src/styles/globals.css` so the painted
mark and the UI surface read as the same brand:

| Token         | Hex      | Used for                               |
| ------------- | -------- | -------------------------------------- |
| accent        | `#ff6a13`| Mark, status/tray icon, alert verdicts |
| bg            | `#1b141f`| App background                         |
| surface       | `#292431`| Cards / panels                         |
| text-primary  | `#f0eff2`| Headlines                              |
