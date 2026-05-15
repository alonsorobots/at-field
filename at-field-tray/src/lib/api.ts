/*
 * Thin client over the watchdog service's localhost HTTP API.
 *
 * Endpoint contract is defined in src/atfield/http_api.py (Python side).
 * Types here mirror the JSON the service returns; if you add a field on
 * one side, add it on the other.
 *
 * All calls use plain fetch with a short timeout via AbortController; the
 * service binds 127.0.0.1:8765 and is on the same machine, so latency is
 * sub-ms when up. A network error means "service is down" -- callers
 * surface that to the user as the gray tray dot.
 */

const DEFAULT_BASE = "http://127.0.0.1:8765";

const BASE = (typeof window !== "undefined" && (window as any).__ATFIELD_API__) || DEFAULT_BASE;

const REQ_TIMEOUT_MS = 2000;

async function jget<T>(path: string): Promise<T> {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), REQ_TIMEOUT_MS);
  try {
    const resp = await fetch(`${BASE}${path}`, { signal: ctrl.signal });
    if (!resp.ok) throw new Error(`HTTP ${resp.status} ${resp.statusText}`);
    return (await resp.json()) as T;
  } finally {
    clearTimeout(t);
  }
}

async function jpost<T>(path: string, body?: unknown): Promise<T> {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), REQ_TIMEOUT_MS);
  try {
    const resp = await fetch(`${BASE}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
      signal: ctrl.signal,
    });
    if (!resp.ok) {
      const txt = await resp.text();
      throw new Error(`HTTP ${resp.status}: ${txt}`);
    }
    return (await resp.json()) as T;
  } finally {
    clearTimeout(t);
  }
}

// ─────────────────────────────────────────────────────────────────────
// Response types -- mirror src/atfield/http_api.py snapshots
// ─────────────────────────────────────────────────────────────────────

export type Mode = "armed" | "observe-only";

export interface CollectorView {
  name: string;
  available: boolean;
  reason: string;
  health: "HEALTHY" | "DEGRADED" | "FAILED" | "UNPROBED";
  signals: string[];
}

export interface LastAction {
  at: number;       // unix seconds
  kind: "log" | "throttle" | "kill";
  rule: string;
}

export interface HealthSnapshot {
  version: string;
  mode: Mode;
  paused: boolean;
  paused_until: number | null;
  started_at: number;
  uptime_s: number;
  tick_count: number;
  last_tick_at: number | null;
  heartbeat_age_s: number | null;
  collectors: CollectorView[];
  rules_active: number;
  rules_disabled: number;
  last_action: LastAction | null;
}

export interface SignalLatest {
  value: number;
  ts: number;       // unix seconds when the tick saw it
  source: string;
  unit: string;
}

export interface SignalsSnapshot {
  latest: Record<string, SignalLatest>;
  /** [unix_ts, value][] per signal, oldest first. */
  history: Record<string, [number, number][]>;
}

export type RuleVerdict = "TRIGGER" | "BELOW" | "INSUFFICIENT";

export interface EffectiveRuleView {
  name: string;
  base_rule: string;
  signal: string;
  threshold: number;
  window_s: number;
  min_fraction_over: number;
  action: "log" | "throttle" | "kill";
  min_samples: number;
  verdict: RuleVerdict;
  fraction_over: number;
  latest_value: number | null;
  triggers: number;
  cooldown_remaining_s: number;
}

export interface DisabledRuleView {
  rule: string;
  signal: string;
  reason: string;
}

export interface RulesSnapshot {
  effective: EffectiveRuleView[];
  disabled: DisabledRuleView[];
}

export interface AuditEvent {
  type: string;
  ts: number;
  ts_iso: string;
  // Other fields are event-type-specific. Keep this typed as `any`-ish
  // so the events screen can render any event shape.
  [k: string]: unknown;
}

export interface EventsSnapshot {
  events: AuditEvent[];
  count: number;
}

// ─────────────────────────────────────────────────────────────────────
// Public API surface
// ─────────────────────────────────────────────────────────────────────

export const api = {
  health: () => jget<HealthSnapshot>("/health"),
  signals: (since?: number) =>
    jget<SignalsSnapshot>(since != null ? `/signals?since=${since}` : "/signals"),
  rules: () => jget<RulesSnapshot>("/rules"),
  events: (params?: { since?: number; limit?: number }) => {
    const qs = new URLSearchParams();
    if (params?.since != null) qs.set("since", String(params.since));
    if (params?.limit != null) qs.set("limit", String(params.limit));
    const tail = qs.toString();
    return jget<EventsSnapshot>(tail ? `/events?${tail}` : "/events");
  },
  pause: (durationSeconds?: number) =>
    jpost<{ paused: boolean; until: string }>("/pause", { duration_s: durationSeconds }),
  unpause: () => jpost<{ paused: boolean; cleared: boolean }>("/unpause"),
  reload: () => jpost<{ reload_queued: boolean }>("/reload"),
};

// ─────────────────────────────────────────────────────────────────────
// Aggregate "tray status" derived from /health -- maps to the four-color
// tray icon. Centralized here so both the dashboard header and the
// (eventual) Rust tray icon use identical logic.
// ─────────────────────────────────────────────────────────────────────

export type TrayStatus = "healthy" | "degraded" | "alerting" | "down";

export function deriveTrayStatus(
  h: HealthSnapshot | null,
  reachable: boolean,
): TrayStatus {
  if (!reachable || h == null) return "down";
  // "Recent kill" beats everything else short of Down -- if we just
  // killed something the user wants to know.
  if (h.last_action != null && h.last_action.kind === "kill") {
    const ageS = Date.now() / 1000 - h.last_action.at;
    if (ageS < 5 * 60) return "alerting";
  }
  if (h.paused) return "degraded";
  if (h.collectors.some((c) => c.health === "FAILED" || c.health === "DEGRADED")) {
    return "degraded";
  }
  if (h.heartbeat_age_s != null && h.heartbeat_age_s > 30) return "degraded";
  return "healthy";
}
