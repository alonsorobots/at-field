import { useEffect, useMemo, useState } from "react";
import {
  DndContext,
  PointerSensor,
  KeyboardSensor,
  useSensor,
  useSensors,
  closestCenter,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  rectSortingStrategy,
  arrayMove,
  useSortable,
  sortableKeyboardCoordinates,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import Sparkline from "../components/Sparkline";
import { api } from "../lib/api";
import type { RulesSnapshot, SignalLatest, SignalsSnapshot } from "../lib/api";
import { usePolling } from "../lib/hooks";
import {
  compareSignalPriority,
  formatValue as fmtUnit,
  isDefaultDisplaySignal,
  signalDisplayName,
} from "../lib/format";
import { getPollIntervalMs } from "../lib/preferences";
import { clearSignalOrder, resolveOrder, saveSignalOrder } from "../lib/signal-order";
import {
  loadHiddenSignals,
  reconcileDefaultVisibility,
  saveHiddenSignals,
} from "../lib/signal-visibility";

interface Props {
  rules: RulesSnapshot | null;
  /** Bumped by the global refresh button so the screen re-fetches. */
  refreshGen?: number;
  /** Called when the user clicks a sparkline. */
  onSelectSignal?: (signal: string) => void;
}

/**
 * Per-signal sparklines in a draggable 2-column grid. Drag-to-reorder
 * persists per browser via localStorage (`atfield.signal_order`); new
 * signals that show up after the user has saved an order get appended
 * in the default ranking. Each tile is also a click target that drills
 * into a per-signal detail view.
 *
 * Keyboard accessibility: focusable tiles can be picked up with Space
 * and moved with Arrow keys; Space again drops, Esc cancels. dnd-kit
 * handles this automatically once we register the KeyboardSensor.
 */
export default function SignalsScreen({ rules, refreshGen, onSelectSignal }: Props) {
  const { data, reachable, refresh } = usePolling<SignalsSnapshot>(
    () => api.signals(),
    getPollIntervalMs(),
  );
  useReactiveRefresh(refreshGen, refresh);

  const thresholdsBySignal = useMemo(() => {
    const m = new Map<string, number>();
    for (const r of rules?.effective ?? []) {
      const cur = m.get(r.signal);
      if (cur == null || r.threshold < cur) m.set(r.signal, r.threshold);
    }
    return m;
  }, [rules]);

  // Live signal list, sorted by user preference (with default-rank fallback
  // for newcomers). Maintained as React state so dnd-kit can mutate it.
  // We hide `*_bytes` signals -- their `*_percent` companion shows the same
  // intensity in a more glanceable unit, so two cards per resource (VRAM
  // bytes + VRAM percent, etc) is just visual clutter on the default view.
  const liveSignals = data
    ? Object.keys(data.latest).filter(isDefaultDisplaySignal)
    : [];
  const [order, setOrder] = useState<string[]>(() =>
    resolveOrder(liveSignals, compareSignalPriority),
  );

  // When the live signal set changes (collectors come/go, config reloads),
  // re-resolve the order so additions are appended and removals drop out.
  useEffect(() => {
    setOrder((prev) => {
      const liveSet = new Set(liveSignals);
      const surviving = prev.filter((s) => liveSet.has(s));
      const newcomers = liveSignals.filter((s) => !surviving.includes(s)).sort(compareSignalPriority);
      const next = [...surviving, ...newcomers];
      // Avoid a render cycle if nothing changed.
      if (next.length === prev.length && next.every((s, i) => s === prev[i])) {
        return prev;
      }
      return next;
    });
  }, [liveSignals.join("|")]);

  // Hidden signals are a view preference: the watchdog still samples and
  // acts on them, we just don't draw the tile. Persisted per-machine and
  // always reversible from the Manage panel below.
  const [hidden, setHidden] = useState<Set<string>>(() => loadHiddenSignals());
  const [manageOpen, setManageOpen] = useState(false);

  // Seed the default-hidden signals (voltages, page file) the first time
  // each one is observed. Reconciliation tracks "seen" separately so a
  // signal the user later un-hides stays visible across restarts.
  useEffect(() => {
    if (liveSignals.length === 0) return;
    setHidden((prev) => {
      const { hidden: next, changed } = reconcileDefaultVisibility(liveSignals, prev);
      if (changed) saveHiddenSignals(next);
      return changed ? next : prev;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [liveSignals.join("|")]);

  const persistHidden = (next: Set<string>) => {
    setHidden(next);
    saveHiddenSignals(next);
  };
  const toggleHidden = (sig: string) => {
    const next = new Set(hidden);
    if (next.has(sig)) next.delete(sig);
    else next.add(sig);
    persistHidden(next);
  };
  const showAll = () => persistHidden(new Set());
  const hideAll = () => persistHidden(new Set(order));

  // Forget the saved drag order and re-apply the default priority ranking.
  const resetOrder = () => {
    clearSignalOrder();
    setOrder([...liveSignals].sort(compareSignalPriority));
  };

  // What actually renders: ordered, minus hidden, minus anything no longer
  // reporting. `order` stays the full set so the Manage panel can reveal
  // hidden tiles and drag-reorder still has stable indices.
  const visibleOrder = order.filter((s) => !hidden.has(s) && data?.latest[s]);
  const hiddenCount = order.filter((s) => hidden.has(s)).length;

  const sensors = useSensors(
    useSensor(PointerSensor, {
      // Require the pointer to move 6 px before a drag starts so simple
      // clicks (drill-down) still go through to the sparkline.
      activationConstraint: { distance: 6 },
    }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const handleDragEnd = (e: DragEndEvent) => {
    const { active, over } = e;
    if (!over || active.id === over.id) return;
    const oldIdx = order.indexOf(String(active.id));
    const newIdx = order.indexOf(String(over.id));
    if (oldIdx < 0 || newIdx < 0) return;
    const next = arrayMove(order, oldIdx, newIdx);
    setOrder(next);
    saveSignalOrder(next);
  };

  if (!data || !reachable) {
    return <div className="p-6 text-sm text-[var(--color-text-secondary)]">Waiting for samples…</div>;
  }
  if (order.length === 0) {
    return <div className="p-6 text-sm text-[var(--color-text-secondary)]">No collectors are reporting yet.</div>;
  }

  return (
    <div className="p-4 overflow-y-auto h-full">
      <div className="flex items-center justify-between gap-2 mb-3">
        <div className="text-[11px] text-[var(--color-text-tertiary)] tabular-nums">
          {visibleOrder.length} shown
          {hiddenCount > 0 && <span> · {hiddenCount} hidden</span>}
        </div>
        <button
          type="button"
          onClick={() => setManageOpen((o) => !o)}
          aria-pressed={manageOpen}
          className={`flex items-center gap-1.5 px-2.5 py-1 rounded-md border text-xs transition-colors ${
            manageOpen
              ? "border-[var(--color-accent)] text-[var(--color-accent)] bg-[var(--color-surface-hover)]"
              : "border-[var(--color-border)] text-[var(--color-text-secondary)] hover:border-[var(--color-border-strong)] hover:text-[var(--color-text-primary)]"
          }`}
          title="Show or hide signals"
        >
          <SlidersIcon />
          {manageOpen ? "Done" : "Manage"}
        </button>
      </div>

      {manageOpen && (
        <div className="frosted rounded-lg border border-[var(--color-border)] mb-3 p-3">
          <div className="flex items-center justify-between gap-2 mb-1">
            <div className="text-[10px] font-semibold uppercase tracking-wider text-[var(--color-text-tertiary)]">
              Show / hide signals
            </div>
            <div className="flex items-center gap-3 text-[11px]">
              <button
                type="button"
                onClick={showAll}
                disabled={hiddenCount === 0}
                className="text-[var(--color-text-secondary)] hover:text-[var(--color-text-primary)] disabled:opacity-40 disabled:cursor-default"
              >
                Show all
              </button>
              <button
                type="button"
                onClick={hideAll}
                disabled={visibleOrder.length === 0}
                className="text-[var(--color-text-secondary)] hover:text-[var(--color-text-primary)] disabled:opacity-40 disabled:cursor-default"
              >
                Hide all
              </button>
              <button
                type="button"
                onClick={resetOrder}
                className="text-[var(--color-text-secondary)] hover:text-[var(--color-text-primary)]"
                title="Forget manual drag order and restore the default priority order"
              >
                Reset order
              </button>
            </div>
          </div>
          <p className="text-[10px] text-[var(--color-text-tertiary)] mb-2 leading-relaxed">
            Hidden signals are still sampled and rule-checked — this only changes what the dashboard draws.
          </p>
          <div className="space-y-0.5 max-h-64 overflow-y-auto">
            {order.map((sig) => {
              const isHidden = hidden.has(sig);
              return (
                <button
                  key={sig}
                  type="button"
                  onClick={() => toggleHidden(sig)}
                  aria-pressed={!isHidden}
                  className="w-full flex items-center gap-2.5 px-2 py-1.5 rounded text-left
                             hover:bg-[var(--color-surface-hover)] transition-colors"
                  title={isHidden ? `Show ${signalDisplayName(sig)}` : `Hide ${signalDisplayName(sig)}`}
                >
                  <span
                    className="flex-shrink-0"
                    style={{
                      color: isHidden
                        ? "var(--color-text-tertiary)"
                        : "var(--color-accent)",
                    }}
                  >
                    {isHidden ? <EyeOffIcon /> : <EyeIcon />}
                  </span>
                  <span className={`min-w-0 flex-1 ${isHidden ? "opacity-50" : ""}`}>
                    <span className="text-xs">{signalDisplayName(sig)}</span>
                    <span className="ml-2 font-mono text-[10px] text-[var(--color-text-tertiary)]">
                      {sig}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      )}

      {visibleOrder.length === 0 ? (
        <div className="frosted rounded-lg border border-[var(--color-border)] p-6 text-center">
          <div className="text-sm text-[var(--color-text-secondary)] mb-3">
            All signals are hidden.
          </div>
          <button
            type="button"
            onClick={showAll}
            className="px-3 py-1.5 rounded-md border border-[var(--color-border-strong)]
                       text-xs text-[var(--color-text-primary)] hover:bg-[var(--color-surface-hover)]"
          >
            Show all signals
          </button>
        </div>
      ) : (
        <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
          <SortableContext items={visibleOrder} strategy={rectSortingStrategy}>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
              {visibleOrder.map((sig) => {
                const latest = data.latest[sig];
                if (!latest) return null;
                return (
                  <SortableSignalTile
                    key={sig}
                    signal={sig}
                    latest={latest}
                    history={data.history[sig] ?? []}
                    threshold={thresholdsBySignal.get(sig) ?? null}
                    onSelect={onSelectSignal}
                    onHide={() => toggleHidden(sig)}
                  />
                );
              })}
            </div>
          </SortableContext>
        </DndContext>
      )}

      <div className="mt-4 text-[10px] text-[var(--color-text-tertiary)] text-center">
        Drag to reorder · click to drill in · hover a tile to hide · saved per machine
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Sortable tile
// ─────────────────────────────────────────────────────────────────────

function SortableSignalTile({
  signal,
  latest,
  history,
  threshold,
  onSelect,
  onHide,
}: {
  signal: string;
  latest: SignalLatest;
  history: [number, number][];
  threshold: number | null;
  onSelect?: (signal: string) => void;
  onHide?: () => void;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: signal,
  });

  const points = history.map(([ts, v]) => ({ ts, value: v }));
  const fmt = (v: number) => fmtUnit(v, latest.unit);
  const overTrigger = threshold != null && latest.value > threshold;

  // Left column has the drag-handle dots; the rest of the tile is the
  // click target so simple clicks still drill into the detail view.
  const style: React.CSSProperties = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.6 : 1,
    zIndex: isDragging ? 10 : "auto",
    cursor: isDragging ? "grabbing" : "default",
  };

  return (
    <div
      ref={setNodeRef}
      style={style}
      className="group relative frosted rounded-lg border border-[var(--color-border)]
                 hover:border-[var(--color-border-strong)] hover:bg-[var(--color-surface-hover)]
                 transition-colors flex"
    >
      {/* Quick-hide: appears on hover/focus, stays out of the way otherwise.
          The Manage panel is the canonical way to bring it back. */}
      {onHide && (
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onHide();
          }}
          className="absolute top-1 right-1 z-10 p-1 rounded
                     opacity-0 group-hover:opacity-100 focus-visible:opacity-100
                     transition-opacity text-[var(--color-text-tertiary)]
                     hover:text-[var(--color-text-primary)] hover:bg-[var(--color-surface-raised)]
                     focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-[var(--color-border-strong)]"
          title={`Hide ${signalDisplayName(signal)}`}
          aria-label={`Hide ${signalDisplayName(signal)}`}
        >
          <EyeOffIcon />
        </button>
      )}

      {/* Drag handle: just enough surface to grab without being intrusive. */}
      <div
        {...attributes}
        {...listeners}
        className="w-6 flex-shrink-0 flex items-center justify-center cursor-grab active:cursor-grabbing
                   text-[var(--color-text-tertiary)] hover:text-[var(--color-text-secondary)]
                   border-r border-[var(--color-border)] select-none"
        title="Drag to reorder"
        aria-label={`Drag handle for ${signalDisplayName(signal)}`}
      >
        <svg width="10" height="16" viewBox="0 0 10 16" fill="currentColor">
          <circle cx="2" cy="3" r="1" />
          <circle cx="8" cy="3" r="1" />
          <circle cx="2" cy="8" r="1" />
          <circle cx="8" cy="8" r="1" />
          <circle cx="2" cy="13" r="1" />
          <circle cx="8" cy="13" r="1" />
        </svg>
      </div>

      <button
        type="button"
        onClick={onSelect ? () => onSelect(signal) : undefined}
        className="flex-1 min-w-0 text-left px-3 py-2.5 cursor-pointer"
        title={`Open detail view for ${signal}`}
      >
        <div className="flex items-baseline justify-between gap-2 mb-1 pr-5">
          <div
            className="text-xs font-medium truncate"
            style={{ color: overTrigger ? "var(--color-danger)" : "inherit" }}
          >
            {signalDisplayName(signal)}
          </div>
          <div
            className="hud-glow text-sm font-semibold tabular-nums whitespace-nowrap"
            style={{
              color: overTrigger ? "var(--color-danger)" : "var(--color-detail)",
            }}
          >
            {fmt(latest.value)}
          </div>
        </div>
        <Sparkline
          values={points}
          threshold={threshold}
          formatValue={fmt}
          height={36}
        />
        <div className="mt-1 flex items-center justify-between text-[10px] text-[var(--color-text-tertiary)]">
          <span className="font-mono truncate">{signal}</span>
          {threshold != null && (
            <span className="whitespace-nowrap">trigger {fmt(threshold)}</span>
          )}
        </div>
      </button>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Icons
// ─────────────────────────────────────────────────────────────────────

function EyeIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
}

function EyeOffIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
      <line x1="1" y1="1" x2="23" y2="23" />
    </svg>
  );
}

function SlidersIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <line x1="4" y1="21" x2="4" y2="14" />
      <line x1="4" y1="10" x2="4" y2="3" />
      <line x1="12" y1="21" x2="12" y2="12" />
      <line x1="12" y1="8" x2="12" y2="3" />
      <line x1="20" y1="21" x2="20" y2="16" />
      <line x1="20" y1="12" x2="20" y2="3" />
      <line x1="1" y1="14" x2="7" y2="14" />
      <line x1="9" y1="8" x2="15" y2="8" />
      <line x1="17" y1="16" x2="23" y2="16" />
    </svg>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────

function useReactiveRefresh(gen: number | undefined, refresh: () => void) {
  useEffect(() => {
    if (gen == null || gen === 0) return;
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gen]);
}
