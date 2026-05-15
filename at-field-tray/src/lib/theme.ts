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
}

export const THEMES: ThemeMeta[] = [
  {
    id: "nerv",
    label: "Nerv",
    description: "Calm watchdog. Muted dark purple, lavender accent.",
    swatches: ["#1b141f", "#a78bfa", "#fbbf24"],
  },
  {
    id: "eva-01",
    label: "EVA-01",
    description: "Test Type. Deep purple, bright green, shoulder orange.",
    swatches: ["#1a0f24", "#41bb42", "#e8790c"],
  },
  {
    id: "eva-00",
    label: "EVA-00",
    description: "Prototype. Deep yellow / orange the original Rei colors.",
    swatches: ["#1a1500", "#f6e201", "#f66e25"],
  },
  {
    id: "eva-02",
    label: "EVA-02",
    description: "Production Model. Asuka red with the visor yellow.",
    swatches: ["#1f0708", "#d93b48", "#f6e201"],
  },
  {
    id: "eva-03",
    label: "EVA-03",
    description: "Bardiel. Cool dark gray, cyan accent.",
    swatches: ["#15161a", "#4da8da", "#aaaab2"],
  },
  {
    id: "eva-04",
    label: "EVA-04",
    description: "Lost. Desert tan and olive.",
    swatches: ["#1f1a10", "#c9b27a", "#80c060"],
  },
];

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

/** Write the theme id and apply it immediately to the document root. */
export function setTheme(id: ThemeId): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, id);
  } catch {
    // Same caveat as above; we still want the in-memory swap to happen.
  }
  applyTheme(id);
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
