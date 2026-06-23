import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { AuditEvent } from "../lib/api";
import { extractScriptName, formatTimeAgo, formatTimeOfDay, signalDisplayName } from "../lib/format";
import { usePolling } from "../lib/hooks";
import { getPollIntervalMs } from "../lib/preferences";

interface Props {
  /** Bumped by the global refresh button so the screen re-fetches. */
  refreshGen?: number;
}

/**
 * Recent events stream from the audit log, designed for triage.
 *
 * The scan order is: severity first (a kill jumps out in red), then
 * "what + which process" (the bold headline), then "when" (relative time
 * up front, wall-clock under it). Everything else is demoted to a muted
 * info row so the eye skips it. Click any row to expand the raw JSON --
 * the goal is "answer 'did something get killed, when, and what?' at a
 * glance, drill in only when you want the gory detail".
 */
export default function EventsScreen({ refreshGen }: Props) {
  // Events refresh at half the dashboard's general poll rate (audit lines
  // append on the order of seconds, not milliseconds, so polling them as
  // fast as live signals is wasteful). Floor at 1 s so users who picked
  // 250 ms for sparkline freshness don't hammer this endpoint too hard.
  const baseMs = getPollIntervalMs();
  const eventsPollMs = Math.max(1000, baseMs * 2);
  const { data, reachable, refresh } = usePolling(() => api.events({ limit: 200 }), eventsPollMs);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  useEffect(() => {
    if (refreshGen != null && refreshGen > 0) refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshGen]);

  if (!reachable || !data) {
    return <div className="p-6 text-sm text-[var(--color-text-secondary)]">Loading events…</div>;
  }
  if (data.events.length === 0) {
    return (
      <div className="p-6 text-sm text-[var(--color-text-secondary)]">
        No events yet. The audit log starts populating as soon as the service ticks.
      </div>
    );
  }

  // Newest first (the file is append-only so we reverse the tail).
  const events = [...data.events].reverse();
  const now = Date.now() / 1000;

  return (
    <div className="overflow-y-auto h-full p-3 space-y-1">
      {events.map((e, idx) => (
        <EventRow
          key={`${e.ts}-${idx}`}
          event={e}
          now={now}
          expanded={expanded.has(idx)}
          onToggle={() =>
            setExpanded((cur) => {
              const next = new Set(cur);
              if (next.has(idx)) next.delete(idx);
              else next.add(idx);
              return next;
            })
          }
        />
      ))}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Severity triage
// ─────────────────────────────────────────────────────────────────────

type Severity = "critical" | "warning" | "info";

const SEVERITY_COLOR: Record<Severity, string> = {
  critical: "var(--color-danger)",
  warning: "var(--color-warning)",
  info: "var(--color-text-tertiary)",
};

interface Triage {
  severity: Severity;
  /** Short fixed-width category tag shown at the left of every row. */
  tag: string;
  /** Bold one-liner: what happened + (for kills) which process. */
  headline: string;
  /** Muted second line: the "why" / supporting detail. Empty = no line. */
  detail: string;
}

function EventRow({
  event,
  now,
  expanded,
  onToggle,
}: {
  event: AuditEvent;
  now: number;
  expanded: boolean;
  onToggle: () => void;
}) {
  const { severity, tag, headline, detail } = triage(event);
  const color = SEVERITY_COLOR[severity];
  const ts = event.ts as number;

  return (
    <div
      className="rounded-md border border-[var(--color-border)] hover:border-[var(--color-border-strong)]
                 transition cursor-pointer overflow-hidden"
      style={{
        borderLeft: `3px solid ${color}`,
        // Critical events get a faint danger wash so a kill is unmistakable
        // while scrolling; everything else stays on the plain surface.
        background:
          severity === "critical"
            ? "color-mix(in srgb, var(--color-danger) 8%, transparent)"
            : undefined,
      }}
      onClick={onToggle}
    >
      <div className="flex items-start gap-3 px-3 py-2">
        <span
          className="font-mono text-[10px] font-semibold uppercase tracking-wider w-12 flex-shrink-0 pt-0.5"
          style={{ color }}
        >
          {tag}
        </span>
        <div className="flex-1 min-w-0">
          <div
            className={`text-xs truncate ${severity === "critical" ? "font-semibold" : "font-medium"}`}
            style={{ color: severity === "critical" ? color : "var(--color-text-primary)" }}
          >
            {headline}
          </div>
          {detail && (
            <div className="text-[11px] text-[var(--color-text-secondary)] truncate mt-0.5 leading-relaxed">
              {detail}
            </div>
          )}
        </div>
        <div className="text-right flex-shrink-0 leading-tight">
          <div className="text-[11px] text-[var(--color-text-secondary)]">{formatTimeAgo(ts, now)}</div>
          <div className="text-[10px] text-[var(--color-text-tertiary)] font-mono">{formatTimeOfDay(ts)}</div>
        </div>
      </div>
      {expanded && (
        <pre
          className="px-3 pb-2 pt-1 text-[10px] font-mono text-[var(--color-text-secondary)] whitespace-pre-wrap overflow-x-auto border-t border-[var(--color-border)] mt-1"
          onClick={(e) => e.stopPropagation()}
        >
          {JSON.stringify(event, null, 2)}
        </pre>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Event → triage mapping
// ─────────────────────────────────────────────────────────────────────

function triage(e: AuditEvent): Triage {
  switch (e.type) {
    case "kill_report":
      return triageKill(e);
    case "action":
      return triageAction(e);
    case "collector_health": {
      const state = String(e.state ?? "");
      const healthy = state === "HEALTHY";
      return {
        severity: healthy ? "info" : "warning",
        tag: healthy ? "INFO" : "WARN",
        headline: `Collector ${e.collector} → ${state.toLowerCase() || "changed"}`,
        detail: e.reason ? String(e.reason) : "",
      };
    }
    case "startup": {
      const sigs = (e.available_signals as string[] | undefined)?.length ?? 0;
      const dis = (e.disabled_rules as unknown[] | undefined)?.length ?? 0;
      return {
        severity: "info",
        tag: "UP",
        headline: `Service started${e.version ? ` (v${e.version})` : ""}`,
        detail: `${sigs} signals active · ${dis} rule${dis === 1 ? "" : "s"} disabled`,
      };
    }
    case "shutdown":
      return {
        severity: "info",
        tag: "DOWN",
        headline: "Service stopped",
        detail: e.reason ? String(e.reason) : "",
      };
    case "pause":
      return e.until
        ? { severity: "info", tag: "PAUSE", headline: `Paused until ${e.until}`, detail: "kill actions suspended" }
        : { severity: "info", tag: "RESUME", headline: "Resumed", detail: "kill actions re-armed" };
    default:
      return { severity: "info", tag: "INFO", headline: e.type, detail: JSON.stringify(e).slice(0, 120) };
  }
}

function triageKill(e: AuditEvent): Triage {
  const killed = (e.killed as Array<{ pid: number; name: string; cmdline?: string[] }> | undefined) ?? [];
  const root = e.kill_root as
    | { pid: number; name: string; script?: string | null; cmdline?: string[] }
    | null
    | undefined;

  if (e.skipped_reason) {
    return {
      severity: "warning",
      tag: "KILL?",
      headline: "Kill skipped",
      detail: String(e.skipped_reason),
    };
  }

  // Prefer the server-provided `script` (canonical, computed at kill time
  // when cmdline is freshest); fall back to client extraction so old log
  // lines still get a friendly headline.
  const script =
    (e.script as string | null | undefined) ?? root?.script ?? extractScriptName(root?.cmdline);
  const what = script ?? root?.name ?? "process";
  const extra = killed.length > 1 ? ` +${killed.length - 1} more` : "";

  const detailBits: string[] = [];
  if (root) detailBits.push(`${root.name} · pid ${root.pid}`);
  detailBits.push(`${killed.length} process${killed.length === 1 ? "" : "es"} terminated`);

  return {
    severity: "critical",
    tag: "KILL",
    headline: `Killed ${what}${extra}`,
    detail: detailBits.join(" · "),
  };
}

function triageAction(e: AuditEvent): Triage {
  const kind = String(e.kind ?? "action");
  const kindVerb: Record<string, string> = {
    kill: "kill triggered",
    throttle: "throttled",
    log: "logged",
  };
  const sigLabel = e.signal ? signalDisplayName(String(e.signal)) : (e.rule ? String(e.rule) : "rule");
  const value = e.latest_value as number | undefined;
  const threshold = e.threshold as number | undefined;
  const pct = ((e.fraction_over as number | undefined) ?? 0) * 100;

  const detailBits: string[] = [];
  if (value != null && threshold != null) {
    detailBits.push(`${fmtSignalValue(String(e.signal ?? ""), value)} ≥ ${fmtSignalValue(String(e.signal ?? ""), threshold)}`);
  }
  detailBits.push(`${pct.toFixed(0)}% of window over`);
  if (e.rule) detailBits.push(`rule ${e.rule}`);

  return {
    // A "kill" action verdict is the moment a rule decided to pull the
    // trigger -- it precedes the kill_report and is worth flagging hot.
    severity: kind === "kill" ? "critical" : "warning",
    tag: kind === "kill" ? "TRIP" : "ALERT",
    headline: `${sigLabel} — ${kindVerb[kind] ?? kind}`,
    detail: detailBits.join(" · "),
  };
}

/** Infer a unit from the signal-name suffix and format the value. The
    audit `action` event carries raw numbers without a unit, mirroring
    format.ts's formatThresholdValue (which isn't exported). */
function fmtSignalValue(signal: string, v: number): string {
  if (/_c$/.test(signal)) return `${v.toFixed(0)}°C`;
  if (/_percent$/.test(signal)) return `${v.toFixed(0)}%`;
  if (/_w$/.test(signal)) return `${v.toFixed(0)} W`;
  if (/_volts$/.test(signal)) return `${v.toFixed(2)} V`;
  return v.toFixed(0);
}
