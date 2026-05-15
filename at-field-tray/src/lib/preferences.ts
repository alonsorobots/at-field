/*
 * User-tweakable UI preferences.
 *
 * Persisted in localStorage so they survive across tray restarts. Values
 * are namespaced under "atfield." to coexist with anything else the
 * Tauri webview might ever store.
 *
 * Each preference exposes:
 *   - a getter that always returns a value (default if missing)
 *   - a setter that writes through to localStorage
 *   - a validator that clamps the input to a sane range
 *
 * Why not a context provider? These prefs are read once at component
 * mount (poll interval) or on user action (changes from a Settings
 * panel). A simple module is enough; a useSyncExternalStore subscriber
 * would be overkill for two scalars.
 */

const NS = "atfield.";
const KEY_POLL_INTERVAL_MS = `${NS}pollIntervalMs`;

export const POLL_INTERVAL_MIN_MS = 250;
export const POLL_INTERVAL_MAX_MS = 30_000;
export const POLL_INTERVAL_DEFAULT_MS = 1000;

/**
 * The dashboard polls /health, /rules, /signals at this rate. Lower is
 * snappier but burns more CPU on the watchdog process; higher saves
 * battery but the live charts feel laggy. 1 s is the recommended
 * default -- it's exactly the watchdog's tick rate, so faster polls
 * just return the same sample twice.
 */
export function getPollIntervalMs(): number {
  if (typeof window === "undefined") return POLL_INTERVAL_DEFAULT_MS;
  try {
    const raw = window.localStorage.getItem(KEY_POLL_INTERVAL_MS);
    if (raw == null) return POLL_INTERVAL_DEFAULT_MS;
    const parsed = Number.parseInt(raw, 10);
    if (!Number.isFinite(parsed)) return POLL_INTERVAL_DEFAULT_MS;
    return clampPollIntervalMs(parsed);
  } catch {
    return POLL_INTERVAL_DEFAULT_MS;
  }
}

export function setPollIntervalMs(value: number): number {
  const clamped = clampPollIntervalMs(value);
  if (typeof window !== "undefined") {
    try {
      window.localStorage.setItem(KEY_POLL_INTERVAL_MS, String(clamped));
    } catch {
      // localStorage can fail in odd webview configurations; the
      // session-scoped value still applies, just won't persist.
    }
  }
  return clamped;
}

function clampPollIntervalMs(v: number): number {
  if (!Number.isFinite(v)) return POLL_INTERVAL_DEFAULT_MS;
  return Math.max(POLL_INTERVAL_MIN_MS, Math.min(POLL_INTERVAL_MAX_MS, Math.floor(v)));
}
