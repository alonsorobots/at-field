import type { RulesSnapshot } from "../lib/api";

interface Props {
  rules: RulesSnapshot | null;
}

/**
 * Compact rule table with current verdict, fraction-over-bar, threshold,
 * and cooldown countdown for each effective rule. Disabled rules render
 * below in muted style with the negotiation reason -- the operator
 * should never have to wonder why a rule "didn't fire".
 */
export default function RulesScreen({ rules }: Props) {
  if (!rules) {
    return <div className="p-6 text-sm text-[var(--color-text-secondary)]">Loading rules…</div>;
  }

  return (
    <div className="p-5 space-y-5 overflow-y-auto h-full">
      <section>
        <h2 className="text-xs font-semibold uppercase tracking-wider text-[var(--color-text-tertiary)] mb-2">
          Active rules
        </h2>
        {rules.effective.length === 0 ? (
          <div className="text-sm text-[var(--color-text-secondary)] italic">
            No rules are currently active. Check the Status tab for collector health.
          </div>
        ) : (
          <div className="space-y-2">
            {rules.effective.map((r) => (
              <div
                key={r.name}
                className="frosted rounded-lg px-4 py-3 border border-[var(--color-border)]"
              >
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <div className="text-sm font-medium truncate">{r.name}</div>
                    <div className="text-[11px] font-mono text-[var(--color-text-tertiary)] truncate">
                      {r.signal} &gt; {r.threshold} for {Math.round(r.window_s * r.min_fraction_over)}s of {r.window_s}s · {r.action}
                    </div>
                  </div>
                  <span className="verdict-pill" data-verdict={r.verdict}>
                    {r.verdict.toLowerCase()}
                  </span>
                </div>

                <div className="mt-2.5 flex items-center gap-3">
                  <FractionBar fraction={r.fraction_over} threshold={r.min_fraction_over} />
                  <div className="text-xs text-[var(--color-text-secondary)] tabular-nums whitespace-nowrap">
                    {(r.fraction_over * 100).toFixed(0)}% of window
                  </div>
                  <div className="text-[11px] text-[var(--color-text-tertiary)] whitespace-nowrap">
                    {r.latest_value != null && <>now {formatNum(r.latest_value)}</>}
                  </div>
                </div>

                {r.cooldown_remaining_s > 0.5 && (
                  <div className="text-[11px] text-[var(--color-warning)] mt-1.5">
                    cooldown {r.cooldown_remaining_s.toFixed(0)}s
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </section>

      {rules.disabled.length > 0 && (
        <section>
          <h2 className="text-xs font-semibold uppercase tracking-wider text-[var(--color-text-tertiary)] mb-2">
            Disabled rules
          </h2>
          <div className="space-y-2">
            {rules.disabled.map((d) => (
              <div
                key={`${d.rule}::${d.signal}`}
                className="rounded-lg px-4 py-3 border border-[var(--color-border)] bg-transparent"
              >
                <div className="text-sm font-medium text-[var(--color-text-secondary)]">{d.rule}</div>
                <div className="text-[11px] font-mono text-[var(--color-text-tertiary)] mt-0.5">{d.signal}</div>
                <div className="text-xs text-[var(--color-text-secondary)] mt-1">{d.reason}</div>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

function FractionBar({ fraction, threshold }: { fraction: number; threshold: number }) {
  const pct = Math.min(1, Math.max(0, fraction)) * 100;
  const thresholdPct = Math.min(1, Math.max(0, threshold)) * 100;
  return (
    <div className="relative flex-1 h-1.5 bg-[var(--color-surface-raised)] rounded-full overflow-hidden">
      <div
        className="absolute inset-y-0 left-0"
        style={{
          width: `${pct}%`,
          background: fraction >= threshold ? "var(--color-danger)" : "var(--color-accent)",
        }}
      />
      <div
        className="absolute inset-y-0 w-px bg-[var(--color-warning)]/70"
        style={{ left: `${thresholdPct}%` }}
        title={`threshold ${(threshold * 100).toFixed(0)}%`}
      />
    </div>
  );
}

function formatNum(v: number): string {
  if (Math.abs(v) >= 1e9) return `${(v / 1e9).toFixed(1)}G`;
  if (Math.abs(v) >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (Math.abs(v) >= 100) return v.toFixed(0);
  return v.toFixed(1);
}
