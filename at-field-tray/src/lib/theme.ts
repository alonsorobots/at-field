/*
 * Theme system -- selects which palette block in styles/globals.css is
 * active. Themes are CSS-only; the React tree never has to know which
 * theme is on. We just stamp `data-theme="<id>"` on <html>, and the
 * matching :root[data-theme="..."] rule in globals.css overrides every
 * --color-* CSS variable. Sparklines, sliders, status dots, and surfaces
 * all repaint on the next style flush.
 *
 * Persistence: localStorage. The choice survives restarts and applies
 * before React hydrates so we don't flash the default theme on cold
 * boot (see applyStoredThemeEarly() called from main.tsx).
 */

export type ThemeId =
  | "nerv"
  | "eva-01"
  | "eva-00"
  | "eva-02"
  | "eva-03"
  | "eva-04";

/** RGB triple. Used by the sparkline gradient builder; we keep it as
    a tuple-of-ints (not a hex string) because the gradient interpolates
    between adjacent stops via per-channel lerp. */
export type RGB = readonly [number, number, number];

export interface ThemeMeta {
  id: ThemeId;
  /** Human-readable name shown in the picker. */
  label: string;
  /** One-line flavor copy for the picker tile. */
  description: string;
  /** Three swatch hexes (bg, accent, secondary-accent) for the picker
      preview chip. Pulled by hand from the matching CSS block so the
      picker tile reads honestly even when the theme isn't active. */
  swatches: [string, string, string];
  /** 8-stop sparkline color ramp, sampled by rampColor() in format.ts.
      Index 0 = cool/recessive (data sitting near zero); index 6 (t=0.85)
      is the threshold anchor color (the "we're paying attention" hue);
      index 7 (t=1.0) is the over-threshold danger color. The math in
      rampT() assumes this 8-element shape -- changing the length means
      retuning the value→t mapping. */
  ramp: readonly [RGB, RGB, RGB, RGB, RGB, RGB, RGB, RGB];
}

export const THEMES: ThemeMeta[] = [
  {
    id: "nerv",
    label: "Nerv",
    description: "Calm watchdog. Muted dark purple, lavender accent.",
    swatches: ["#1b141f", "#a78bfa", "#fbbf24"],
    // Original brand ramp -- warm slate that recedes into the dark
    // purple bg, ramping up to brand orange at threshold and deep red
    // for sustained over-threshold values.
    ramp: [
      [58, 50, 64],
      [76, 64, 76],
      [102, 76, 76],
      [138, 88, 64],
      [184, 100, 48],
      [222, 116, 38],
      [255, 106, 19],
      [225, 32, 56],
    ],
  },
  {
    id: "eva-01",
    label: "EVA-01",
    description: "Test Type. Deep purple, bright green, shoulder orange.",
    swatches: ["#1a0f24", "#41bb42", "#e8790c"],
    // Cool purple base picks up the body color, ramps through green to
    // shoulder orange at threshold (the "warning" anchor that matches
    // EVA-01's color story); over-threshold lands on a bright red so it
    // still reads as urgent against the green-purple field.
    ramp: [
      [40, 30, 60],
      [60, 45, 80],
      [85, 60, 95],
      [85, 130, 80],
      [140, 150, 60],
      [200, 140, 40],
      [232, 121, 12],
      [232, 50, 38],
    ],
  },
  {
    id: "eva-00",
    label: "EVA-00",
    description: "Prototype. Deep yellow / orange the original Rei colors.",
    swatches: ["#1a1500", "#f6e201", "#f66e25"],
    // Olive base climbs through dim mustard up to the prototype yellow
    // at threshold, ending on the EVA-00 orange-red over-threshold.
    ramp: [
      [40, 38, 20],
      [70, 65, 30],
      [110, 100, 30],
      [160, 140, 30],
      [220, 195, 25],
      [240, 215, 18],
      [246, 226, 1],
      [246, 110, 37],
    ],
  },
  {
    id: "eva-02",
    label: "EVA-02",
    description: "Production Model. Asuka red with the visor yellow.",
    swatches: ["#1f0708", "#d93b48", "#f6e201"],
    // Asuka tradeoff: the iconic body color is red, but red also has to
    // mean "over threshold" semantically. Resolution: yellow visor as
    // the threshold anchor (it IS the warning color in the suit), red
    // takes over for sustained over-threshold (the body color firing
    // up). Cool maroon base recedes against the dark red bg.
    ramp: [
      [50, 30, 35],
      [80, 50, 55],
      [120, 70, 70],
      [160, 130, 60],
      [210, 175, 40],
      [240, 215, 30],
      [246, 226, 1],
      [217, 59, 72],
    ],
  },
  {
    id: "eva-03",
    label: "EVA-03",
    description: "Bardiel. Cool dark gray, cyan accent.",
    swatches: ["#15161a", "#4da8da", "#aaaab2"],
    // Cool slate-blue base climbs through cyan to the Bardiel cyan
    // accent at threshold; over-threshold falls back to the standard
    // warning red since EVA-03's palette has no native "danger" hue.
    ramp: [
      [30, 35, 45],
      [60, 70, 85],
      [85, 105, 120],
      [100, 130, 145],
      [90, 150, 180],
      [80, 165, 205],
      [77, 168, 218],
      [248, 113, 113],
    ],
  },
  {
    id: "eva-04",
    label: "EVA-04",
    description: "Lost. Desert tan and olive.",
    swatches: ["#1f1a10", "#c9b27a", "#80c060"],
    // Dark olive base ramps through warm tan up to the EVA-04 desert
    // tan at threshold; over-threshold lands on a burnt rust-orange
    // that fits the warm-earth palette.
    ramp: [
      [50, 40, 25],
      [80, 65, 40],
      [120, 90, 55],
      [160, 130, 80],
      [200, 170, 110],
      [220, 185, 130],
      [201, 178, 122],
      [217, 108, 74],
    ],
  },
];

const THEMES_BY_ID: Record<ThemeId, ThemeMeta> = THEMES.reduce(
  (acc, t) => {
    acc[t.id] = t;
    return acc;
  },
  {} as Record<ThemeId, ThemeMeta>,
);

/** Look up the active theme's sparkline color ramp.
 *
 * Reads the data-theme attribute off <html>, which setTheme() stamps.
 * Falls back to the default theme's ramp if the attribute is missing or
 * names a theme we don't know (forward-compat). Used by format.rampColor
 * on every gradient stop -- the read is just a DOM attribute lookup, no
 * getComputedStyle, so it's cheap enough to call dozens of times per
 * render.
 */
export function getActiveRamp(): readonly RGB[] {
  const id = document.documentElement.getAttribute("data-theme");
  if (id != null && id in THEMES_BY_ID) {
    return THEMES_BY_ID[id as ThemeId].ramp;
  }
  return THEMES_BY_ID[DEFAULT_THEME].ramp;
}

const STORAGE_KEY = "atfield.theme";
const DEFAULT_THEME: ThemeId = "nerv";

const VALID_IDS = new Set<string>(THEMES.map((t) => t.id));

function isThemeId(value: unknown): value is ThemeId {
  return typeof value === "string" && VALID_IDS.has(value);
}

/** Read the persisted theme id, falling back to the default if anything
    is unset, corrupted, or no longer recognized. */
export function getTheme(): ThemeId {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (isThemeId(raw)) return raw;
  } catch {
    // localStorage can throw in private browsing or sandboxed contexts;
    // fall through to default.
  }
  return DEFAULT_THEME;
}

/** Write the theme id and apply it immediately to the document root.
 *  Notifies any subscribers via {@link subscribeTheme} so React trees
 *  that depend on the active theme (sparklines whose stroke colors
 *  come from the JS ramp lookup, not CSS variables) can re-render. */
export function setTheme(id: ThemeId): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, id);
  } catch {
    // Same caveat as above; we still want the in-memory swap to happen.
  }
  applyTheme(id);
  for (const fn of _listeners) fn(id);
}

/** Stamp data-theme onto the document root so the matching CSS block
    takes effect. Safe to call before React mounts. */
export function applyTheme(id: ThemeId): void {
  document.documentElement.setAttribute("data-theme", id);
}

/** Apply the stored theme to <html> as early as possible. Call from
    main.tsx before ReactDOM.render so cold boot doesn't flash the
    default palette for a frame. */
export function applyStoredThemeEarly(): void {
  applyTheme(getTheme());
}

// ──────────────────────────────────────────────────────────────────────
// Subscription -- minimal listener registry for components that need to
// re-render on theme change. CSS variable updates already propagate
// automatically (they trigger style recalc in the engine), but SVG
// gradient stops are JS-computed via lib/format.rampColor which reads
// the active ramp once per render. Without this hook those components
// would only pick up the new colors on their next poll tick.
// ──────────────────────────────────────────────────────────────────────

type ThemeListener = (id: ThemeId) => void;
const _listeners = new Set<ThemeListener>();

/** Subscribe to theme changes. Returns an unsubscribe function. */
export function subscribeTheme(fn: ThemeListener): () => void {
  _listeners.add(fn);
  return () => {
    _listeners.delete(fn);
  };
}
