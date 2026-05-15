import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { AuditEvent } from "../lib/api";
import { extractScriptName } from "../lib/format";
import { usePolling } from "../lib/hooks";
import { getPollIntervalMs } from "../lib/preferences";

interface Props {
  /** Bumped by the global refresh button so the screen re-fetches. */
  refreshGen?: number;
}

const EVENT_COLORS: Record<string, string> = {
  startup: "var(--color-success)",
  shutdown: "var(--color-text-secondary)",
  action: "var(--color-warning)",
  kill_report: "var(--color-danger)",
  collector_health: "var(--color-warning)",
  pause: "var(--color-text-secondary)",
};

/**
 * Recent events stream from the audit log. Each row collapses interesting
 * detail (e.g. kill_report's process tree) behind a click-to-expand
 * disclosure -- the goal is "I can answer 'why was my job killed?' from
 * here without grep-ing events.jsonl by hand".
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

  return (
    <div className="overflow-y-auto h-full p-3 space-y-1.5">
      {events.map((e, idx) => (
        <EventRow
          key={`${e.ts}-${idx}`}
          event={e}
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

function EventRow({
  event,
  expanded,
  onToggle,
}: {
  event: AuditEvent;
  expanded: boolean;
  onToggle: () => void;
}) {
  const color = EVENT_COLORS[event.type] ?? "var(--color-text-tertiary)";
  const summary = summarize(event);
  const hasDetail = expanded && event.type !== "shutdown";

  return (
    <div
      className="rounded-md border border-[var(--color-border)] hover:border-[var(--color-border-strong)]
                 transition cursor-pointer"
      onClick={onToggle}
    >
      <div className="flex items-center gap-3 px-3 py-2 text-xs">
        <span
          className="inline-block w-1.5 h-1.5 rounded-full flex-shrink-0"
          style={{ background: color }}
        />
        <span className="font-mono text-[10px] text-[var(--color-text-tertiary)] w-16 flex-shrink-0">
          {formatTime(event.ts as number)}
        </span>
        <span className="font-medium text-[var(--color-text-primary)] w-28 flex-shrink-0">
          {event.type}
        </span>
        <span className="text-[var(--color-text-secondary)] truncate flex-1">{summary}</span>
      </div>
      {hasDetail && (
        <pre
          className="px-3 pb-2 pt-0 text-[10px] font-mono text-[var(--color-text-secondary)] whitespace-pre-wrap overflow-x-auto"
          onClick={(e) => e.stopPropagation()}
        >
          {JSON.stringify(event, null, 2)}
        </pre>
      )}
    </div>
  );
}

function summarize(e: AuditEvent): string {
  switch (e.type) {
    case "startup": {
      const sigs = (e.available_signals as string[] | undefined)?.length ?? 0;
      const dis = (e.disabled_rules as unknown[] | undefined)?.length ?? 0;
      return `service v${e.version} started — ${sigs} signals, ${dis} disabled rules`;
    }
    case "shutdown":
      return `service stopped (${e.reason})`;
    case "action":
      return `${e.kind} — rule ${e.rule}, signal ${e.signal} = ${e.latest_value} (over ${e.threshold}, ${(((e.fraction_over as number) ?? 0) * 100).toFixed(0)}% of window)`;
    case "kill_report": {
      const killed = (e.killed as Array<{ pid: number; name: string; cmdline?: string[] }> | undefined) ?? [];
      const root = e.kill_root as
        | { pid: number; name: string; script?: string | null; cmdline?: string[] }
        | null
        | undefined;
      if (e.skipped_reason) return `kill skipped: ${e.skipped_reason}`;
      // Prefer the server-provided `script` (canonical, computed at kill
      // time when cmdline is freshest). Fall back to client-side extraction
      // so events.jsonl entries written before the server helper landed
      // still get a friendly headline. The launcher exe + pid live in the
      // expanded JSON detail; keeping the headline focused on "what was
      // running" puts the answer to "why was my job killed?" up front.
      const script =
        (e.script as string | null | undefined) ??
        root?.script ??
        extractScriptName(root?.cmdline);
      const headline = script ?? root?.name ?? "process";
      const procCount = killed.length > 1 ? ` (${killed.length} processes)` : "";
      return `killed ${headline}${procCount}`;
    }
    case "collector_health":
      return `collector ${e.collector} → ${e.state}${e.reason ? ` (${e.reason})` : ""}`;
    case "pause":
      return e.until ? `paused until ${e.until}` : "unpaused";
    default:
      return JSON.stringify(e).slice(0, 120);
  }
}

function formatTime(unixTs: number): string {
  const d = new Date(unixTs * 1000);
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
