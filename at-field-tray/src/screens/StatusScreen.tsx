import type { HealthSnapshot } from "../lib/api";

interface Props {
  health: HealthSnapshot | null;
  reachable: boolean;
}

export default function StatusScreen({ health, reachable }: Props) {
  if (!reachable || !health) {
    return (
      <div className="p-6">
        <div className="text-base font-semibold mb-2">Service unreachable</div>
        <div className="text-sm text-[var(--color-text-secondary)] leading-relaxed">
          The AT-Field watchdog isn't responding on http://127.0.0.1:8765/.
          Check that the Windows service <code className="font-mono">atfield-watchdog</code> is
          running, or run <code className="font-mono">atf run</code> in a terminal for foreground mode.
        </div>
      </div>
    );
  }

  const upHours = (health.uptime_s / 3600).toFixed(1);

  return (
    <div className="p-5 space-y-5 overflow-y-auto h-full">
      {/* Mode + counts row */}
      <section className="grid grid-cols-3 gap-3">
        <Stat label="Mode" value={health.mode === "observe-only" ? "Observe-only" : "Armed"} />
        <Stat label="Active rules" value={String(health.rules_active)} />
        <Stat label="Disabled rules" value={String(health.rules_disabled)} muted={health.rules_disabled === 0} />
      </section>

      <section className="grid grid-cols-3 gap-3">
        <Stat label="Ticks" value={health.tick_count.toLocaleString()} />
        <Stat label="Uptime" value={`${upHours}h`} />
        <Stat
          label="Last action"
          value={
            health.last_action
              ? `${health.last_action.kind} · ${ago(health.last_action.at)}`
              : "—"
          }
        />
      </section>

      {/* Collectors */}
      <section>
        <h2 className="text-xs font-semibold uppercase tracking-wider text-[var(--color-text-tertiary)] mb-2">
          Collectors
        </h2>
        <div className="space-y-2">
          {health.collectors.map((c) => (
            <div
              key={c.name}
              className="frosted rounded-lg px-4 py-3 border border-[var(--color-border)] flex items-start"
            >
              <span
                className="dot mt-1.5"
                data-status={c.health === "HEALTHY" ? "healthy" : c.health === "DEGRADED" ? "degraded" : "down"}
              />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <div className="text-sm font-medium">{c.name}</div>
                  <span className="text-[10px] text-[var(--color-text-tertiary)] font-mono uppercase">
                    {c.health}
                  </span>
                </div>
                <div className="text-xs text-[var(--color-text-secondary)] mt-0.5 leading-relaxed">
                  {c.reason}
                </div>
                {c.signals.length > 0 && (
                  <div className="text-[11px] text-[var(--color-text-tertiary)] mt-1">
                    {c.signals.length} signal{c.signals.length === 1 ? "" : "s"}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

function Stat({ label, value, muted }: { label: string; value: string; muted?: boolean }) {
  return (
    <div className="frosted rounded-lg px-4 py-3 border border-[var(--color-border)]">
      <div className="text-[10px] font-semibold uppercase tracking-wider text-[var(--color-text-tertiary)]">
        {label}
      </div>
      <div className={`text-lg font-semibold mt-0.5 ${muted ? "text-[var(--color-text-secondary)]" : ""}`}>
        {value}
      </div>
    </div>
  );
}

function ago(unixTs: number): string {
  const sec = Math.max(0, Date.now() / 1000 - unixTs);
  if (sec < 60) return `${Math.round(sec)}s ago`;
  if (sec < 3600) return `${Math.round(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.round(sec / 3600)}h ago`;
  return `${Math.round(sec / 86400)}d ago`;
}
