/*
 * Active Signals-tab category filter, persisted to localStorage.
 *
 * The Signals screen has an "All / Temp / GPU / CPU / Memory" tab bar above
 * the existing reorder/hide UI. The selection survives reloads via the
 * `atfield.signal_category` key. Unknown / malformed values fall back
 * to "all" -- never throws.
 *
 * "All" stays the default because anyone who hasn't explicitly narrowed
 * their view expects to see everything (it's the conservative choice
 * for a watchdog dashboard).
 */

import type { SignalCategory } from "./format";

/** "All" is the union of every other tab; we model it as its own value
    rather than `undefined` so the filter logic and tab UI can treat it
    uniformly. */
/** The tab strip also offers "temp", a cross-cutting view that is not a
    `SignalCategory` (see `isTemperatureSignal`) -- hence the union rather
    than reusing SignalCategory alone. */
export type ActiveCategory = "all" | "temp" | SignalCategory;

const KEY = "atfield.signal_category";
const VALID: readonly ActiveCategory[] = ["all", "temp", "gpu", "cpu", "memory", "other"];

export function loadActiveCategory(): ActiveCategory {
  if (typeof window === "undefined") return "all";
  try {
    const raw = window.localStorage.getItem(KEY);
    if (raw && (VALID as readonly string[]).includes(raw)) {
      return raw as ActiveCategory;
    }
  } catch {
    // Quota / privacy mode -- fall through to default.
  }
  return "all";
}

export function saveActiveCategory(c: ActiveCategory): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(KEY, c);
  } catch {
    // Best-effort: storage failures shouldn't break the UI.
  }
}
