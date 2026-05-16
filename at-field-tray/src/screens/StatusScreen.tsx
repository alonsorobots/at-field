import type { HealthSnapshot } from "../lib/api";

interface Props {
  health: HealthSnapshot | null;
  reachable: boolean;
}

export default function StatusScreen({ health, reachable }: Props) {
  if (!reachable || !health) {
    return <ServiceUnreachable />;
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
        <h2 className="hud hud-dim text-xs font-semibold uppercase tracking-wider text-[var(--color-text-tertiary)] mb-2">
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
      <div className="hud hud-dim text-[10px] font-semibold uppercase tracking-wider text-[var(--color-text-tertiary)]">
        {label}
      </div>
      <div className={`text-lg font-semibold mt-0.5 ${muted ? "text-[var(--color-text-secondary)]" : ""}`}>
        {value}
      </div>
    </div>
  );
}

function ServiceUnreachable() {
  return (
    <div className="p-6 space-y-5 overflow-y-auto h-full">
      <div>
        <div className="flex items-center gap-2.5 mb-2">
          <span className="dot" data-status="down" />
          <div className="text-base font-semibold">AT-Field service is unreachable</div>
        </div>
        <div className="text-sm text-[var(--color-text-secondary)] leading-relaxed">
          The dashboard is running, but it can't reach the watchdog at{" "}
          <code className="font-mono text-[var(--color-text-primary)]">http://127.0.0.1:8765/</code>.
          That usually means the service isn't installed yet, or it's stopped.
        </div>
      </div>

      <section>
        <div className="hud hud-dim text-xs font-semibold uppercase tracking-wider text-[var(--color-text-tertiary)] mb-2">
          Option 1 — install as a Windows service (recommended)
        </div>
        <div className="frosted rounded-lg border border-[var(--color-border)] p-4 space-y-2">
          <div className="text-sm text-[var(--color-text-secondary)] leading-relaxed">
            Runs in the background, starts at boot, watches even when no user is logged in. Requires admin once.
          </div>
          <CopyBlock>{"pip install atfield\natf install"}</CopyBlock>
          <div className="text-[11px] text-[var(--color-text-tertiary)]">
            Run from an <span className="text-[var(--color-text-secondary)]">elevated PowerShell</span>.
          </div>
        </div>
      </section>

      <section>
        <div className="hud hud-dim text-xs font-semibold uppercase tracking-wider text-[var(--color-text-tertiary)] mb-2">
          Option 2 — try it in a terminal first
        </div>
        <div className="frosted rounded-lg border border-[var(--color-border)] p-4 space-y-2">
          <div className="text-sm text-[var(--color-text-secondary)] leading-relaxed">
            Foreground mode. Same engine, no service install. Quit with Ctrl-C.
          </div>
          <CopyBlock>{"atf run"}</CopyBlock>
        </div>
      </section>

      <section>
        <div className="hud hud-dim text-xs font-semibold uppercase tracking-wider text-[var(--color-text-tertiary)] mb-2">
          Already installed?
        </div>
        <div className="text-sm text-[var(--color-text-secondary)] leading-relaxed">
          Check the service status with <code className="font-mono text-[var(--color-text-primary)]">atf status</code>,
          or restart it from <code className="font-mono text-[var(--color-text-primary)]">services.msc</code>{" "}
          (look for <span className="text-[var(--color-text-primary)]">AT-Field Watchdog</span>). The dashboard
          reconnects automatically once the service comes back up.
        </div>
      </section>
    </div>
  );
}

function CopyBlock({ children }: { children: string }) {
  const handleCopy = () => {
    navigator.clipboard.writeText(children).catch(() => {});
  };
  return (
    <div className="relative">
      <pre className="bg-[var(--color-bg-secondary)] rounded-md px-3 py-2 text-xs font-mono text-[var(--color-text-primary)] overflow-x-auto">
        {children}
      </pre>
      <button
        onClick={handleCopy}
        className="absolute top-1.5 right-1.5 px-2 py-0.5 text-[10px] rounded
                   bg-[var(--color-surface-raised)] text-[var(--color-text-secondary)]
                   hover:bg-[var(--color-surface-hover)] hover:text-[var(--color-text-primary)] transition"
        title="Copy to clipboard"
      >
        Copy
      </button>
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
