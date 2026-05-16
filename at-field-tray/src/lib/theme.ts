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
  | "civvie"
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
    id: "civvie",
    label: "Civvie",
    swatches: ["#1b141f", "#a78bfa", "#fbbf24"],
    // The "civilian" theme -- the calm, no-frills look the dashboard
    // shipped with before the Magi-terminal aesthetic pass. Same warm
    // slate ramp that recedes into the dark purple bg, ramping up to
    // brand orange at threshold and deep red for sustained over-
    // threshold values. Pairs with the CSS override in globals.css
    // that swaps the HUD font for sans and strips the phosphor bloom
    // -- so picking Civvie genuinely reverts to the pre-bloom UI.
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
  // Each Eva theme's swatch tuple is [Base-surface, Accent, Other Detail]
  // and each ramp climbs from "blends with bg" → Accent at the threshold
  // anchor (index 6, t=0.85) → Other Detail at over-threshold (index 7,
  // t=1.0). Matches the design spec: Accent at 25% drives the sparkline
  // mid-to-high attention, Other Detail at 10% punctuates the moment a
  // value crosses the trigger.
  {
    id: "eva-00",
    label: "EVA-00",
    swatches: ["#1a2456", "#ffffff", "#1e8562"],
    // Base=Blue #345ac9, Accent=White, Other Detail=Green #1e8562.
    // Climbs from deep navy up to pure white (Rei's pale highlights)
    // then hard-cuts to the green eye over-threshold.
    ramp: [
      [20, 30, 70],
      [40, 60, 110],
      [70, 90, 150],
      [110, 130, 190],
      [160, 175, 220],
      [210, 220, 240],
      [255, 255, 255],
      [30, 133, 98],
    ],
  },
  {
    id: "eva-01",
    label: "EVA-01",
    swatches: ["#1a0f24", "#41bb42", "#e8790c"],
    // Base=Purple, Accent=Green #41bb42, Other Detail=Burnt Orange #e8790c.
    // Cool purple recess → green at threshold → shoulder orange over-
    // threshold (the classic EVA-01 alarm color).
    ramp: [
      [40, 30, 60],
      [60, 45, 80],
      [85, 60, 95],
      [85, 130, 80],
      [70, 175, 80],
      [60, 200, 70],
      [65, 187, 66],
      [232, 121, 12],
    ],
  },
  {
    id: "eva-02",
    label: "EVA-02",
    swatches: ["#2a0808", "#e45e15", "#8a2d75"],
    // Base=Bright Red #e41d18, Accent=Burnt Orange #e45e15,
    // Other Detail=Magenta #8a2d75. Dark red recess → burnt orange at
    // threshold → magenta over-threshold (the unit IS red, so magenta
    // carries the over-threshold beat without disappearing into the bg).
    ramp: [
      [50, 20, 20],
      [80, 30, 25],
      [120, 50, 30],
      [170, 70, 25],
      [210, 85, 20],
      [225, 90, 18],
      [228, 94, 21],
      [138, 45, 117],
    ],
  },
  {
    id: "eva-03",
    label: "EVA-03",
    swatches: ["#1a1e3f", "#ffffff", "#83365e"],
    // Base=Muted Purple #1a1e3f, Accent=White, Other Detail=Brighter
    // Purple #83365e. Recess through deeper purples up to white at
    // threshold, dropping to the brighter purple over-threshold so the
    // theme stays "all purple" even when alarms fire.
    ramp: [
      [35, 40, 70],
      [55, 60, 95],
      [85, 95, 135],
      [130, 140, 180],
      [180, 185, 215],
      [220, 220, 235],
      [255, 255, 255],
      [131, 54, 94],
    ],
  },
  {
    id: "eva-04",
    label: "EVA-04",
    swatches: ["#f4f4f6", "#4a4a4f", "#e8202d"],
    // LIGHT theme. Base=White, Accent=Gray #4a4a4f, Other Base=Black,
    // Other Detail=BRIGHT RED. On a white page, the ramp starts NEAR
    // the bg color (very light gray) and climbs DOWN in lightness to
    // dark gray at threshold, then bright red over-threshold. Same
    // semantic shape as the dark themes (low value = recede, high
    // value = pop) but inverted in lightness because the page itself
    // is light.
    ramp: [
      [220, 220, 222],
      [190, 190, 195],
      [160, 160, 165],
      [130, 130, 138],
      [100, 100, 108],
      [80, 80, 88],
      [74, 74, 79],
      [232, 32, 45],
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
const DEFAULT_THEME: ThemeId = "civvie";

const VALID_IDS = new Set<string>(THEMES.map((t) => t.id));

function isThemeId(value: unknown): value is ThemeId {
  return typeof value === "string" && VALID_IDS.has(value);
}

/** Read the persisted theme id, falling back to the default if anything
    is unset, corrupted, or no longer recognized.
 *
 * Migrates the pre-v0.3.1 "nerv" stored value to its new id ("civvie")
 * silently and rewrites the storage slot, so users who picked the
 * default theme before the rename don't get reset to default on first
 * launch after upgrade. */
export function getTheme(): ThemeId {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw === "nerv") {
      window.localStorage.setItem(STORAGE_KEY, "civvie");
      return "civvie";
    }
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
