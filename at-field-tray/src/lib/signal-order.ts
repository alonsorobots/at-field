/*
 * Signal-tile ordering, persisted to localStorage.
 *
 * The user can drag-reorder the signals grid; the order survives reloads
 * via a simple `atfield.signal_order` key. Signals not yet in the saved
 * order (e.g. brand-new on the next tick after a config reload) get
 * appended in the default rank order so the list never blanks out.
 *
 * The storage format is a JSON-encoded string array, e.g.
 *   ["gpu.0.core_temp_c", "gpu.1.core_temp_c", "system.ram_used_percent", ...]
 * Anything not JSON-decodable is treated as "no saved order" and we fall
 * back to defaults. We never throw on bad localStorage data.
 */

const KEY = "atfield.signal_order";

export function loadSignalOrder(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((s): s is string => typeof s === "string");
  } catch {
    return [];
  }
}

export function saveSignalOrder(order: string[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(KEY, JSON.stringify(order));
  } catch {
    // Quota / privacy mode -- best-effort, swallow.
  }
}

/**
 * Resolve a final display order given the live signals and any saved
 * preference. Saved signals come first (in their saved order, but
 * filtered to ones still present); brand-new signals are appended in
 * their default `defaultOrder` ranking.
 */
export function resolveOrder(liveSignals: string[], defaultOrder: (a: string, b: string) => number): string[] {
  const saved = loadSignalOrder();
  const liveSet = new Set(liveSignals);
  const ordered: string[] = [];
  const seen = new Set<string>();
  for (const s of saved) {
    if (liveSet.has(s) && !seen.has(s)) {
      ordered.push(s);
      seen.add(s);
    }
  }
  const newcomers = liveSignals.filter((s) => !seen.has(s)).sort(defaultOrder);
  return [...ordered, ...newcomers];
}
