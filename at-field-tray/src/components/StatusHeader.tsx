import { motion } from "framer-motion";
import type { HealthSnapshot, TrayStatus } from "../lib/api";
import { api } from "../lib/api";

const STATUS_LABEL: Record<TrayStatus, string> = {
  healthy: "Healthy",
  degraded: "Degraded",
  alerting: "Alerting",
  down: "Service down",
};

interface Props {
  status: TrayStatus;
  health: HealthSnapshot | null;
  onRefresh: () => void;
}

/**
 * Top-of-window header. Always visible regardless of which tab is active.
 *
 * Left: status dot + mode chip + version. Right: pause/unpause button +
 * refresh. Pause/unpause talks to the service over the localhost API and
 * triggers an immediate `onRefresh` so the next health snapshot reflects
 * the change without waiting for the next poll tick.
 */
export default function StatusHeader({ status, health, onRefresh }: Props) {
  const handlePauseToggle = async () => {
    if (!health) return;
    try {
      if (health.paused) {
        await api.unpause();
      } else {
        // Default to 1h pause; the dropdown variant lives in the tray menu.
        await api.pause(60 * 60);
      }
      onRefresh();
    } catch (e) {
      // Surfacing this isn't critical for now; the next /health poll will
      // reflect the actual state regardless. Log for the dev console.
      console.error("pause toggle failed", e);
    }
  };

  return (
    <header className="frosted flex items-center justify-between gap-4 px-5 py-3 border-b border-[var(--color-border)]">
      <div className="flex items-center min-w-0">
        <motion.span
          key={status}
          className="dot"
          data-status={status}
          initial={{ scale: 0.7, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          transition={{ duration: 0.15 }}
        />
        <div className="flex flex-col min-w-0">
          <div className="text-sm font-semibold truncate">
            AT-Field <span className="text-[var(--color-text-secondary)] font-normal">{STATUS_LABEL[status]}</span>
          </div>
          <div className="text-[11px] text-[var(--color-text-tertiary)] truncate">
            {health ? (
              <>
                {health.mode === "observe-only" ? "OBSERVE-ONLY" : "armed"} · v{health.version} ·{" "}
                {health.tick_count} ticks
                {health.heartbeat_age_s != null && ` · last ${health.heartbeat_age_s.toFixed(1)}s ago`}
              </>
            ) : (
              "connecting…"
            )}
          </div>
        </div>
      </div>
      <div className="flex items-center gap-2">
        <button
          onClick={handlePauseToggle}
          disabled={!health}
          className="px-3 py-1.5 rounded-md text-xs font-medium border border-[var(--color-border-strong)]
                     bg-[var(--color-surface-raised)] hover:bg-[var(--color-surface-hover)]
                     disabled:opacity-50 disabled:cursor-not-allowed transition"
          title={health?.paused ? "Resume the watchdog" : "Pause the watchdog for 1 hour"}
        >
          {health?.paused ? "Unpause" : "Pause 1h"}
        </button>
        <button
          onClick={onRefresh}
          className="px-3 py-1.5 rounded-md text-xs font-medium border border-[var(--color-border-strong)]
                     bg-[var(--color-surface-raised)] hover:bg-[var(--color-surface-hover)] transition"
          title="Refresh now"
        >
          Refresh
        </button>
      </div>
    </header>
  );
}
