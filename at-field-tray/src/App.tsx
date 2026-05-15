import { useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import KillToast from "./components/KillToast";
import StatusHeader from "./components/StatusHeader";
import StatusScreen from "./screens/StatusScreen";
import SignalsScreen from "./screens/SignalsScreen";
import SignalDetailScreen from "./screens/SignalDetailScreen";
import RulesScreen from "./screens/RulesScreen";
import EventsScreen from "./screens/EventsScreen";
import SetupScreen from "./screens/SetupScreen";
import PrefsScreen from "./screens/PrefsScreen";
import { api, deriveTrayStatus } from "./lib/api";
import { usePolling, useTheme } from "./lib/hooks";
import { getPollIntervalMs } from "./lib/preferences";

const TABS = [
  { id: "signals", label: "Signals" },
  { id: "rules", label: "Rules" },
  { id: "events", label: "Events" },
  { id: "status", label: "Status" },
  { id: "prefs", label: "Prefs" },
] as const;
type TabId = (typeof TABS)[number]["id"];

const pageVariants = {
  initial: { opacity: 0, y: 6 },
  animate: { opacity: 1, y: 0 },
  exit: { opacity: 0, y: -6 },
};
const pageTransition = { duration: 0.18, ease: [0.4, 0, 0.2, 1] };

export default function App() {
  // Default to "signals" -- the live data view is what people open the
  // dashboard FOR. Status is for first-run / debugging and is a click away.
  const [tab, setTab] = useState<TabId>("signals");

  // When non-null, SignalsScreen swaps for a per-signal detail view that
  // pulls multi-resolution history. Clearing it returns to the grid.
  const [selectedSignal, setSelectedSignal] = useState<string | null>(null);

  // Bumped by the refresh button. Every screen that polls subscribes via
  // a useEffect that calls its hook's `refresh()` on every change. This is
  // simpler than threading a context through and avoids re-creating the
  // polling hooks on each refresh.
  const [refreshGen, setRefreshGen] = useState(0);
  const triggerRefresh = () => setRefreshGen((g) => g + 1);

  // Poll cadence is user-tweakable in the Status screen; we read it
  // once on mount. Changing it requires reopening the dashboard, which
  // is fine for a setting that's tuned approximately once and then
  // forgotten.
  const [pollIntervalMs] = useState<number>(() => getPollIntervalMs());
  const healthQ = usePolling(api.health, pollIntervalMs);
  const rulesQ = usePolling(api.rules, pollIntervalMs);

  // Subscribe at the App root so a theme switch from PrefsScreen
  // re-renders the whole tree (including sparklines, whose stroke
  // colors come from the JS ramp lookup, not CSS variables).
  useTheme();

  const trayStatus = deriveTrayStatus(healthQ.data, healthQ.reachable, healthQ.hasAttempted);

  // Show the first-run setup screen ONLY after we've made at least one
  // attempt and confirmed the service is unreachable. Without the
  // hasAttempted gate we'd flash the SetupScreen for ~1s on every cold
  // boot, which is jarring when the service is just starting up.
  const showSetup = healthQ.hasAttempted && !healthQ.reachable;

  return (
    <div className="flex flex-col h-screen w-screen bg-[var(--color-bg)] text-[var(--color-text-primary)] relative">
      <KillToast health={healthQ.data} />
      <StatusHeader
        status={trayStatus}
        health={healthQ.data}
        onRefresh={() => {
          healthQ.refresh();
          rulesQ.refresh();
          triggerRefresh();
        }}
      />

      <div className="flex flex-1 min-h-0">
        <nav className="w-36 border-r border-[var(--color-border)] p-2 flex flex-col gap-1 flex-shrink-0">
          {TABS.map((t) => (
            <button
              key={t.id}
              className="tab-rail-button"
              data-active={tab === t.id}
              onClick={() => {
                setTab(t.id);
                setSelectedSignal(null);
              }}
            >
              {t.label}
            </button>
          ))}
          <div className="flex-1" />
          <div className="text-[10px] text-[var(--color-text-tertiary)] px-3 py-2 leading-relaxed">
            v{healthQ.data?.version ?? "?"}
            <br />
            <span className="text-[var(--color-text-secondary)]">localhost:8765</span>
          </div>
        </nav>

        <main className="flex-1 min-w-0 min-h-0 relative overflow-hidden">
          <AnimatePresence mode="wait">
            <motion.div
              key={selectedSignal ?? tab}
              className="absolute inset-0"
              variants={pageVariants}
              initial="initial"
              animate="animate"
              exit="exit"
              transition={pageTransition}
            >
              {showSetup && (
                <SetupScreen
                  onInstalled={() => {
                    healthQ.refresh();
                    rulesQ.refresh();
                    triggerRefresh();
                  }}
                />
              )}
              {!showSetup && tab === "signals" && selectedSignal == null && (
                <SignalsScreen
                  rules={rulesQ.data}
                  refreshGen={refreshGen}
                  onSelectSignal={setSelectedSignal}
                />
              )}
              {!showSetup && tab === "signals" && selectedSignal != null && (
                <SignalDetailScreen
                  signal={selectedSignal}
                  rules={rulesQ.data}
                  onBack={() => setSelectedSignal(null)}
                  refreshGen={refreshGen}
                />
              )}
              {!showSetup && tab === "rules" && (
                <RulesScreen rules={rulesQ.data} onMutated={rulesQ.refresh} />
              )}
              {!showSetup && tab === "events" && <EventsScreen refreshGen={refreshGen} />}
              {!showSetup && tab === "status" && (
                <StatusScreen health={healthQ.data} reachable={healthQ.reachable} />
              )}
              {!showSetup && tab === "prefs" && <PrefsScreen />}
            </motion.div>
          </AnimatePresence>
        </main>
      </div>
    </div>
  );
}
