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
import { formatValue as fmtUnit, isDefaultDisplaySignal, signalDisplayName } from "../lib/format";
import { getPollIntervalMs } from "../lib/preferences";
import { resolveOrder, saveSignalOrder } from "../lib/signal-order";

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
    resolveOrder(liveSignals, signalSortOrder),
  );

  // When the live signal set changes (collectors come/go, config reloads),
  // re-resolve the order so additions are appended and removals drop out.
  useEffect(() => {
    setOrder((prev) => {
      const liveSet = new Set(liveSignals);
      const surviving = prev.filter((s) => liveSet.has(s));
      const newcomers = liveSignals.filter((s) => !surviving.includes(s)).sort(signalSortOrder);
      const next = [...surviving, ...newcomers];
      // Avoid a render cycle if nothing changed.
      if (next.length === prev.length && next.every((s, i) => s === prev[i])) {
        return prev;
      }
      return next;
    });
  }, [liveSignals.join("|")]);

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
      <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
        <SortableContext items={order} strategy={rectSortingStrategy}>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
            {order.map((sig) => {
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
                />
              );
            })}
          </div>
        </SortableContext>
      </DndContext>
      <div className="mt-4 text-[10px] text-[var(--color-text-tertiary)] text-center">
        Drag any tile to reorder · click to drill in · order saved per machine
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
}: {
  signal: string;
  latest: SignalLatest;
  history: [number, number][];
  threshold: number | null;
  onSelect?: (signal: string) => void;
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
      className="frosted rounded-lg border border-[var(--color-border)]
                 hover:border-[var(--color-border-strong)] hover:bg-[var(--color-surface-hover)]
                 transition-colors flex"
    >
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
        <div className="flex items-baseline justify-between gap-2 mb-1">
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
// Helpers
// ─────────────────────────────────────────────────────────────────────

function useReactiveRefresh(gen: number | undefined, refresh: () => void) {
  useEffect(() => {
    if (gen == null || gen === 0) return;
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gen]);
}

/**
 * Default rank for new signals (only used when the user hasn't dragged
 * yet, or for tiles new since the last save). Most safety-critical first.
 */
function signalSortOrder(a: string, b: string): number {
  return signalRank(a) - signalRank(b) || a.localeCompare(b);
}
function signalRank(s: string): number {
  if (s.includes("temp_c")) return 0;
  if (s.includes("vram_used")) return 1;
  if (s.includes("util_percent")) return 2;
  if (s.includes("power_w")) return 3;
  if (s.startsWith("gpu.")) return 4;
  if (s.startsWith("system.")) return 5;
  return 9;
}
