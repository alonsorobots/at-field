import { useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import StatusHeader from "./components/StatusHeader";
import StatusScreen from "./screens/StatusScreen";
import SignalsScreen from "./screens/SignalsScreen";
import RulesScreen from "./screens/RulesScreen";
import EventsScreen from "./screens/EventsScreen";
import { api, deriveTrayStatus } from "./lib/api";
import { usePolling } from "./lib/hooks";

const TABS = [
  { id: "status", label: "Status" },
  { id: "signals", label: "Signals" },
  { id: "rules", label: "Rules" },
  { id: "events", label: "Events" },
] as const;
type TabId = (typeof TABS)[number]["id"];

const pageVariants = {
  initial: { opacity: 0, y: 6 },
  animate: { opacity: 1, y: 0 },
  exit: { opacity: 0, y: -6 },
};
const pageTransition = { duration: 0.18, ease: [0.4, 0, 0.2, 1] };

export default function App() {
  const [tab, setTab] = useState<TabId>("status");

  // Health is the spine: drives the header status dot + Status tab.
  // Rules is shared between Signals (for threshold lines) and Rules tab,
  // so we lift it here too. Each polled at 1 Hz; SignalsScreen polls
  // /signals on its own at 1 Hz to keep sparklines smooth.
  const healthQ = usePolling(api.health, 1000);
  const rulesQ = usePolling(api.rules, 1000);

  const trayStatus = deriveTrayStatus(healthQ.data, healthQ.reachable);

  return (
    <div className="flex flex-col h-screen w-screen bg-[var(--color-bg)] text-[var(--color-text-primary)]">
      <StatusHeader
        status={trayStatus}
        health={healthQ.data}
        onRefresh={() => {
          healthQ.refresh();
          rulesQ.refresh();
        }}
      />

      <div className="flex flex-1 min-h-0">
        {/* Left rail: tab nav */}
        <nav className="w-36 border-r border-[var(--color-border)] p-2 flex flex-col gap-1 flex-shrink-0">
          {TABS.map((t) => (
            <button
              key={t.id}
              className="tab-rail-button"
              data-active={tab === t.id}
              onClick={() => setTab(t.id)}
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

        {/* Right pane: active screen, with smooth transitions */}
        <main className="flex-1 min-w-0 min-h-0 relative overflow-hidden">
          <AnimatePresence mode="wait">
            <motion.div
              key={tab}
              className="absolute inset-0"
              variants={pageVariants}
              initial="initial"
              animate="animate"
              exit="exit"
              transition={pageTransition}
            >
              {tab === "status" && <StatusScreen health={healthQ.data} reachable={healthQ.reachable} />}
              {tab === "signals" && <SignalsScreen rules={rulesQ.data} />}
              {tab === "rules" && <RulesScreen rules={rulesQ.data} />}
              {tab === "events" && <EventsScreen />}
            </motion.div>
          </AnimatePresence>
        </main>
      </div>
    </div>
  );
}
