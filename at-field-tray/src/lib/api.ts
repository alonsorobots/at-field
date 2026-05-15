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
  return jverb<T>("POST", path, body);
}

async function jpatch<T>(path: string, body?: unknown): Promise<T> {
  return jverb<T>("PATCH", path, body);
}

async function jverb<T>(method: string, path: string, body?: unknown): Promise<T> {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), REQ_TIMEOUT_MS);
  try {
    const resp = await fetch(`${BASE}${path}`, {
      method,
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
  /** Signal that triggered the rule (e.g. "gpu.0.mem_junction_temp_c").
      Optional for back-compat with older service builds. */
  signal?: string;
  /** Script extracted from the kill_root cmdline (e.g. "train.py"). Only
      populated for `kind === "kill"` and only when the launcher cmdline
      yielded an identifiable script. Used for the toast headline. */
  script?: string | null;
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

export interface SignalHistorySnapshot {
  signal: string;
  hours: number;
  /** Server's now() at slice time, for "X minutes ago" labelling. */
  now: number;
  unit: string;
  source: string;
  /** [unix_ts, value][] mixed-resolution: 1 Hz for last hour, 10 s for
      1-6h ago, 60 s for 6-24h ago. */
  samples: [number, number][];
  count: number;
}

export type RuleVerdict = "TRIGGER" | "BELOW" | "INSUFFICIENT";

export type RuleTier = "aggressive" | "normal" | "relaxed" | "custom";

export interface RuleTuning {
  min: number;
  max: number;
  aggressive_max: number;
  relaxed_min: number;
  step: number;
  unit: string;
  current_tier: RuleTier;
  presets: { aggressive: number; normal: number; relaxed: number };
}

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
  /** Slider metadata. Null for user-defined rules without canonical
      tier definitions (those just don't get a slider in the UI). */
  tuning: RuleTuning | null;
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
  signalHistory: (signal: string, hours: number = 1) =>
    jget<SignalHistorySnapshot>(
      `/signals/history?signal=${encodeURIComponent(signal)}&hours=${hours}`,
    ),
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
  patchRule: (baseRuleName: string, threshold: number) =>
    jpatch<{ rule: string; threshold: number; tier: RuleTier; reload_queued: boolean }>(
      `/rules/${encodeURIComponent(baseRuleName)}`,
      { threshold },
    ),
  applyProfile: (profile: "aggressive" | "normal" | "relaxed") =>
    jpost<{ profile: string; applied: Record<string, number>; reload_queued: boolean }>(
      "/profile",
      { profile },
    ),
};

// ─────────────────────────────────────────────────────────────────────
// Aggregate "tray status" derived from /health -- maps to the four-color
// tray icon. Centralized here so both the dashboard header and the
// (eventual) Rust tray icon use identical logic.
// ─────────────────────────────────────────────────────────────────────

export type TrayStatus = "loading" | "healthy" | "degraded" | "alerting" | "down";

export function deriveTrayStatus(
  h: HealthSnapshot | null,
  reachable: boolean,
  hasAttempted: boolean = true,
): TrayStatus {
  // Pre-first-poll state: don't slander the service as "down" before we've
  // even tried connecting. The dashboard's first poll fires on mount and
  // settles within ~1s; until then, render as "loading".
  if (!hasAttempted) return "loading";
  if (!reachable || h == null) return "down";
  // "Recent kill" beats everything else short of Down -- if we just
  // killed something the user wants to know.
  if (h.last_action != null && h.last_action.kind === "kill") {
    const ageS = Date.now() / 1000 - h.last_action.at;
    if (ageS < 5 * 60) return "alerting";
  }
  if (h.paused) return "degraded";

  // "Degraded" should mean: protection the user wanted is broken right now.
  // It should NOT mean: an optional sensor (e.g. LibreHardwareMonitor) was
  // never installed in the first place. That's a default state, not a
  // degradation -- nagging the user about it teaches them to ignore the
  // status light.
  //
  // So we only flag a collector as degrading the overall status when:
  //   1. It was successfully probed at startup (available: true), AND
  //   2. It's currently FAILED or DEGRADED (i.e. it WAS working, now isn't).
  //
  // A collector with available: false never worked -- the affected rules
  // were already pruned at startup with a clear reason in `rules_disabled`,
  // surfaced separately on the Status screen.
  const collectorBroken = h.collectors.some(
    (c) => c.available && (c.health === "FAILED" || c.health === "DEGRADED"),
  );
  if (collectorBroken) return "degraded";

  if (h.heartbeat_age_s != null && h.heartbeat_age_s > 30) return "degraded";
  return "healthy";
}
