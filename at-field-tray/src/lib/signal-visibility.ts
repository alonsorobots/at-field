/*
 * Per-signal visibility, persisted to localStorage.
 *
 * Hiding a signal is a *view* preference -- the watchdog keeps sampling and
 * acting on it; we just don't draw its tile. Hidden signals are never lost:
 * the Signals screen's "Manage" panel always lists them with a toggle to
 * bring them back. The preference survives reloads via the
 * `atfield.hidden_signals` key.
 *
 * Storage format is a JSON-encoded string array of wire names, e.g.
 *   ["system.swap_used_percent", "gpu.1.power_w"]
 * Anything not JSON-decodable is treated as "nothing hidden". We never throw
 * on bad localStorage data.
 */

const KEY = "atfield.hidden_signals";
const KEY_SEEN = "atfield.signals_seen";

export function loadHiddenSignals(): Set<string> {
  return loadStringSet(KEY);
}

export function saveHiddenSignals(hidden: Set<string>): void {
  saveStringSet(KEY, hidden);
}

// ─────────────────────────────────────────────────────────────────────
// Default visibility
// ─────────────────────────────────────────────────────────────────────

/**
 * Signals we hide on a user's first sight of them, because they're
 * forensic/power-user telemetry rather than glance-actionable for the
 * average operator. They stay fully sampled and rule-checked -- this only
 * keeps them off the default dashboard grid, and the Manage panel can
 * always bring them back.
 *
 *   - `*_volts`              PSU rail / Vcore voltages. No glance value;
 *                            logged for crash forensics.
 *   - `system.swap_used_percent`  Page file in isolation -- "RAM used" and
 *                            "Committed memory" already tell the story.
 */
export function shouldHideByDefault(signal: string): boolean {
  if (signal.endsWith("_volts")) return true;
  if (signal === "system.swap_used_percent") return true;
  return false;
}

/**
 * Reconcile the persisted hidden set against the live signals, seeding the
 * defaults exactly once per signal. "Seen" is tracked separately so that:
 *   - a default-hidden sensor that shows up later (collectors warm up at
 *     different rates) still gets hidden the first time it appears, and
 *   - a signal the user explicitly un-hid stays visible across restarts
 *     (we never re-seed something we've already shown them).
 *
 * Pure-ish: returns the next hidden set plus whether anything changed.
 * Callers persist on `changed`.
 */
export function reconcileDefaultVisibility(
  liveSignals: readonly string[],
  hidden: Set<string>,
): { hidden: Set<string>; changed: boolean } {
  const seen = loadStringSet(KEY_SEEN);
  let seenChanged = false;
  let hiddenChanged = false;
  const nextHidden = new Set(hidden);

  for (const sig of liveSignals) {
    if (seen.has(sig)) continue;
    seen.add(sig);
    seenChanged = true;
    if (shouldHideByDefault(sig) && !nextHidden.has(sig)) {
      nextHidden.add(sig);
      hiddenChanged = true;
    }
  }

  if (seenChanged) saveStringSet(KEY_SEEN, seen);
  return { hidden: hiddenChanged ? nextHidden : hidden, changed: hiddenChanged };
}

// ─────────────────────────────────────────────────────────────────────
// localStorage string-set plumbing
// ─────────────────────────────────────────────────────────────────────

function loadStringSet(key: string): Set<string> {
  if (typeof window === "undefined") return new Set();
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return new Set();
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return new Set();
    return new Set(parsed.filter((s): s is string => typeof s === "string"));
  } catch {
    return new Set();
  }
}

function saveStringSet(key: string, value: Set<string>): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, JSON.stringify([...value]));
  } catch {
    // Quota / privacy mode -- best-effort, swallow.
  }
}
