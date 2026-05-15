import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useRef, useState } from "react";
import type { HealthSnapshot } from "../lib/api";

interface Props {
  /** Latest /health snapshot. We watch `last_action` for new kill events. */
  health: HealthSnapshot | null;
}

/**
 * Top-of-window red banner that flashes whenever a new kill action lands.
 * 
 * Mirrors the Windows toast notification fired by the Rust side, but
 * provides the "RED text" emphasis the system toast can't. Auto-dismisses
 * after AUTO_DISMISS_MS; the user can click to dismiss earlier.
 *
 * Detection model: we keep a per-mount baseline of the last_action
 * timestamp we've ALREADY shown. On first /health observation we capture
 * the current timestamp (so we don't flash a banner for a kill that
 * happened before the dashboard was even opened). When `last_action.at`
 * advances past the baseline AND kind === "kill", we set the visible
 * payload and start the auto-dismiss timer.
 */
const AUTO_DISMISS_MS = 10_000;

interface KillEvent {
  at: number;
  script: string | null;
  rule: string | null;
  signal: string | null;
}

export default function KillToast({ health }: Props) {
  const baselineRef = useRef<number | null>(null);
  const [visible, setVisible] = useState<KillEvent | null>(null);
  const dismissTimer = useRef<number | null>(null);

  useEffect(() => {
    if (!health) return;
    const la = health.last_action;
    // First observation: baseline at the current last_action.at (or 0 if
    // none) so we don't replay history.
    if (baselineRef.current == null) {
      baselineRef.current = la?.at ?? 0;
      return;
    }
    if (la == null || la.kind !== "kill") return;
    if (la.at <= baselineRef.current) return;
    // New kill event! Update baseline so we don't fire again for the
    // same one, then show the banner.
    baselineRef.current = la.at;
    setVisible({
      at: la.at,
      script: (la as { script?: string | null }).script ?? null,
      rule: (la as { rule?: string | null }).rule ?? null,
      signal: (la as { signal?: string | null }).signal ?? null,
    });
  }, [health]);

  // Auto-dismiss
  useEffect(() => {
    if (visible == null) return;
    if (dismissTimer.current != null) window.clearTimeout(dismissTimer.current);
    dismissTimer.current = window.setTimeout(() => setVisible(null), AUTO_DISMISS_MS);
    return () => {
      if (dismissTimer.current != null) window.clearTimeout(dismissTimer.current);
    };
  }, [visible]);

  return (
    <AnimatePresence>
      {visible && (
        <motion.div
          key={visible.at}
          initial={{ opacity: 0, y: -8 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -8 }}
          transition={{ duration: 0.18 }}
          className="absolute top-2 left-1/2 -translate-x-1/2 z-50 cursor-pointer"
          onClick={() => setVisible(null)}
        >
          <div
            className="flex items-start gap-3 px-4 py-2.5 rounded-md shadow-lg max-w-xl"
            style={{
              // Brand-coral red bg with low alpha + strong red border so the
              // banner reads as "danger" against the dark UI without being
              // a screaming solid block.
              background: "rgba(248, 113, 113, 0.14)",
              border: "1px solid var(--color-danger)",
              backdropFilter: "blur(8px)",
            }}
          >
            <div
              className="w-2 h-2 rounded-full mt-1.5 flex-shrink-0"
              style={{ background: "var(--color-danger)" }}
            />
            <div className="min-w-0">
              <div
                className="text-sm font-semibold leading-tight"
                style={{ color: "var(--color-danger)" }}
              >
                AT-Field killed {visible.script ?? "a process"}
              </div>
              {(visible.rule || visible.signal) && (
                <div className="text-[11px] mt-0.5 text-[var(--color-text-secondary)] leading-snug">
                  {visible.rule && <span>Rule {visible.rule}</span>}
                  {visible.rule && visible.signal && <span> · </span>}
                  {visible.signal && <span className="font-mono">{visible.signal}</span>}
                </div>
              )}
            </div>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
