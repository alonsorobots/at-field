import { useMemo } from "react";
import Sparkline from "../components/Sparkline";
import { api } from "../lib/api";
import type { RulesSnapshot, SignalsSnapshot } from "../lib/api";
import { usePolling } from "../lib/hooks";

interface Props {
  rules: RulesSnapshot | null;
}

/**
 * Per-signal sparklines, grouped by scope (gpu / system / cpu) and
 * ordered with the most-volatile signals on top. Pulls /signals every
 * second; the right-side numeric is the latest value formatted by unit.
 *
 * Threshold lines are drawn for any signal that has at least one rule
 * targeting it -- visual confirmation that "your VRAM temp is approaching
 * 90 °C" without making the user open the Rules tab.
 */
export default function SignalsScreen({ rules }: Props) {
  const { data, reachable } = usePolling<SignalsSnapshot>(() => api.signals(), 1000);

  // Map signal -> threshold for the dashed reference line. If multiple
  // rules target one signal (rare; conservative defaults don't), the
  // lowest threshold wins (it's the one that fires first).
  const thresholdsBySignal = useMemo(() => {
    const m = new Map<string, number>();
    for (const r of rules?.effective ?? []) {
      const cur = m.get(r.signal);
      if (cur == null || r.threshold < cur) m.set(r.signal, r.threshold);
    }
    return m;
  }, [rules]);

  if (!data || !reachable) {
    return <div className="p-6 text-sm text-[var(--color-text-secondary)]">Waiting for samples…</div>;
  }

  const signals = Object.keys(data.latest).sort();
  if (signals.length === 0) {
    return <div className="p-6 text-sm text-[var(--color-text-secondary)]">No collectors are reporting yet.</div>;
  }

  return (
    <div className="p-5 space-y-2 overflow-y-auto h-full">
      {signals.map((sig) => {
        const latest = data.latest[sig];
        const history = data.history[sig] ?? [];
        const values = history.map(([, v]) => v);
        const threshold = thresholdsBySignal.get(sig) ?? null;
        return (
          <div
            key={sig}
            className="frosted rounded-lg px-4 py-3 border border-[var(--color-border)]"
          >
            <div className="flex items-center justify-between mb-1">
              <div className="text-xs font-mono text-[var(--color-text-secondary)]">{sig}</div>
              <div className="text-sm font-semibold tabular-nums">{formatValue(latest.value, latest.unit)}</div>
            </div>
            <Sparkline values={values} threshold={threshold} height={32} />
            {threshold != null && (
              <div className="text-[10px] text-[var(--color-text-tertiary)] mt-1">
                threshold {formatValue(threshold, latest.unit)}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function formatValue(v: number, unit: string): string {
  switch (unit) {
    case "celsius":
      return `${v.toFixed(0)}°C`;
    case "percent":
      return `${v.toFixed(1)}%`;
    case "watts":
      return `${v.toFixed(0)} W`;
    case "bytes":
      return formatBytes(v);
    default:
      return v.toString();
  }
}

function formatBytes(v: number): string {
  if (v >= 1024 ** 3) return `${(v / 1024 ** 3).toFixed(2)} GB`;
  if (v >= 1024 ** 2) return `${(v / 1024 ** 2).toFixed(0)} MB`;
  if (v >= 1024) return `${(v / 1024).toFixed(0)} KB`;
  return `${v} B`;
}
