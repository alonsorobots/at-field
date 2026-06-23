import { useEffect, useId, useMemo, useState } from "react";
import { motion } from "framer-motion";
import { api } from "../lib/api";
import type { RulesSnapshot, SignalHistorySnapshot } from "../lib/api";
import { usePolling } from "../lib/hooks";
import {
  formatBytes,
  formatTimeOfDay,
  formatValue as fmtUnit,
  rampForValue,
  rampGradientStops,
  signalDisplayName,
} from "../lib/format";

interface Props {
  signal: string;
  rules: RulesSnapshot | null;
  onBack: () => void;
  refreshGen?: number;
}

const RANGE_OPTIONS: Array<{ id: string; label: string; hours: number; pollMs: number }> = [
  { id: "1h", label: "1 hour", hours: 1, pollMs: 1000 },
  { id: "6h", label: "6 hours", hours: 6, pollMs: 5000 },
  { id: "24h", label: "24 hours", hours: 24, pollMs: 30000 },
];

/**
 * Per-signal drill-down. Polls /signals/history for the selected window
 * (1h / 6h / 24h) and renders a full-pane time-series chart with a
 * threshold reference line, hover tooltip, and summary stats.
 *
 * Mixed-resolution semantics (server-side): the most recent hour is
 * 1 Hz raw samples; 1-6h ago is 10-second means; 6-24h ago is 60-second
 * means. Each averaged tier represents the MEAN of its window, so a
 * 30-second spike from 4 hours ago shows up dampened (averaged into a
 * 10-s sample) but never disappears.
 */
export default function SignalDetailScreen({ signal, rules, onBack, refreshGen }: Props) {
  const [rangeIdx, setRangeIdx] = useState(0);
  const range = RANGE_OPTIONS[rangeIdx];

  const { data, reachable, refresh } = usePolling<SignalHistorySnapshot>(
    () => api.signalHistory(signal, range.hours),
    range.pollMs,
  );

  useEffect(() => {
    if (refreshGen != null && refreshGen > 0) refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshGen]);

  // Refetch immediately when range changes so the chart doesn't lag a
  // poll cycle behind the click.
  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rangeIdx]);

  // Threshold for this signal -- pick the lowest-threshold rule (it's
  // the one that fires first).
  const threshold = useMemo(() => {
    let lowest: number | null = null;
    for (const r of rules?.effective ?? []) {
      if (r.signal === signal && (lowest == null || r.threshold < lowest)) {
        lowest = r.threshold;
      }
    }
    return lowest;
  }, [rules, signal]);

  const stats = useMemo(() => {
    if (!data || data.samples.length === 0) return null;
    let lo = Infinity;
    let hi = -Infinity;
    let sum = 0;
    let timeOver = 0;
    let prev: [number, number] | null = null;
    for (const [ts, v] of data.samples) {
      if (v < lo) lo = v;
      if (v > hi) hi = v;
      sum += v;
      if (threshold != null && prev) {
        const [pTs, pV] = prev;
        const dt = ts - pTs;
        if (pV > threshold) timeOver += dt;
      }
      prev = [ts, v];
    }
    return {
      min: lo,
      max: hi,
      mean: sum / data.samples.length,
      latest: data.samples[data.samples.length - 1][1],
      timeOverS: timeOver,
      sampleCount: data.samples.length,
      windowS: range.hours * 3600,
    };
  }, [data, threshold, range.hours]);

  const fmt = (v: number) => (data ? fmtUnit(v, data.unit) : v.toFixed(2));

  return (
    <div className="flex flex-col h-full">
      {/* Header bar with Back, signal name, range picker */}
      <div className="px-4 py-3 border-b border-[var(--color-border)] flex items-center gap-3">
        <button
          onClick={onBack}
          className="px-2 py-1 rounded text-xs border border-[var(--color-border-strong)]
                     bg-[var(--color-surface-raised)] hover:bg-[var(--color-surface-hover)] transition"
          title="Back to signal grid (Esc)"
        >
          ← Back
        </button>
        <div className="flex-1 min-w-0">
          <div className="text-sm font-semibold truncate">{signalDisplayName(signal)}</div>
          <div className="text-[10px] font-mono text-[var(--color-text-tertiary)] truncate">
            {signal}
            {data?.source && <> · source: {data.source}</>}
          </div>
        </div>
        <div className="flex gap-1">
          {RANGE_OPTIONS.map((r, i) => (
            <button
              key={r.id}
              onClick={() => setRangeIdx(i)}
              className="px-2.5 py-1 text-xs rounded border transition"
              style={{
                borderColor:
                  i === rangeIdx ? "var(--color-accent)" : "var(--color-border-strong)",
                background:
                  i === rangeIdx
                    ? "var(--color-surface-hover)"
                    : "var(--color-surface-raised)",
                color: i === rangeIdx ? "var(--color-text-primary)" : "var(--color-text-secondary)",
              }}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>

      {/* Stats strip */}
      {stats && (
        <div className="px-4 py-2 border-b border-[var(--color-border)] flex flex-wrap gap-x-6 gap-y-1 text-xs">
          <Stat label="Now" value={fmt(stats.latest)} live highlight={threshold != null && stats.latest > threshold} />
          <Stat label="Min" value={fmt(stats.min)} />
          <Stat label="Mean" value={fmt(stats.mean)} />
          <Stat label="Max" value={fmt(stats.max)} highlight={threshold != null && stats.max > threshold} />
          {threshold != null && (
            <>
              <Stat label="Trigger" value={fmt(threshold)} muted />
              <Stat
                label="Time over trigger"
                value={formatDuration(stats.timeOverS)}
                highlight={stats.timeOverS > 0}
              />
            </>
          )}
          <Stat label="Samples" value={`${stats.sampleCount}`} muted />
        </div>
      )}

      {/* Main chart pane */}
      <div className="flex-1 min-h-0 p-4">
        {!reachable && (
          <div className="text-sm text-[var(--color-text-secondary)]">Service unreachable.</div>
        )}
        {reachable && !data && (
          <div className="text-sm text-[var(--color-text-secondary)]">Loading…</div>
        )}
        {data && data.samples.length === 0 && (
          <div className="text-sm text-[var(--color-text-secondary)]">
            No samples in the last {range.label} for this signal.
          </div>
        )}
        {data && data.samples.length > 0 && (
          <DetailChart data={data} threshold={threshold} fmt={fmt} />
        )}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Chart: SVG, hand-rolled, with hover crosshair + axis labels
// ─────────────────────────────────────────────────────────────────────

function DetailChart({
  data,
  threshold,
  fmt,
}: {
  data: SignalHistorySnapshot;
  threshold: number | null;
  fmt: (v: number) => string;
}) {
  const uid = useId().replace(/:/g, "");
  const gradientId = `detail-chart-grad-${uid}`;
  const bloomId = `detail-chart-bloom-${uid}`;
  const width = 1000;
  const height = 360;
  const padL = 56;
  const padR = 16;
  const padT = 12;
  const padB = 28;
  const innerW = width - padL - padR;
  const innerH = height - padT - padB;

  const samples = data.samples;
  const tsLo = samples[0][0];
  const tsHi = samples[samples.length - 1][0];
  const tRange = Math.max(1, tsHi - tsLo);

  let lo = Infinity;
  let hi = -Infinity;
  for (const [, v] of samples) {
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  if (threshold != null) {
    lo = Math.min(lo, threshold);
    hi = Math.max(hi, threshold);
  }
  if (hi - lo < 1e-6) {
    hi += 1;
    lo -= 1;
  }
  // Pad y-range slightly so the line never touches the top/bottom border.
  const yPad = (hi - lo) * 0.08;
  lo -= yPad;
  hi += yPad;
  const yRange = hi - lo;

  const xFor = (ts: number) => padL + ((ts - tsLo) / tRange) * innerW;
  const yFor = (v: number) => padT + (1 - (v - lo) / yRange) * innerH;

  const polyPoints = samples
    .map(([ts, v]) => `${xFor(ts).toFixed(1)},${yFor(v).toFixed(1)}`)
    .join(" ");

  const thresholdY = threshold != null ? yFor(threshold) : null;

  // Y-axis ticks: 4 evenly spaced.
  const yTicks = [0, 0.33, 0.66, 1].map((t) => lo + t * yRange);
  // X-axis ticks: ~5 evenly spaced across the visible range.
  const xTicks = Array.from({ length: 5 }, (_, i) => tsLo + (i / 4) * tRange);

  // Hover state
  const [hover, setHover] = useState<{ idx: number; clientX: number } | null>(null);
  const handleMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const svg = e.currentTarget;
    const rect = svg.getBoundingClientRect();
    const xRatio = (e.clientX - rect.left) / rect.width;
    const xVB = xRatio * width;
    if (xVB < padL || xVB > width - padR) {
      setHover(null);
      return;
    }
    const ts = tsLo + ((xVB - padL) / innerW) * tRange;
    // Binary search for nearest sample.
    let l = 0;
    let r = samples.length - 1;
    while (l < r) {
      const mid = (l + r) >> 1;
      if (samples[mid][0] < ts) l = mid + 1;
      else r = mid;
    }
    const cands = [Math.max(0, l - 1), l, Math.min(samples.length - 1, l + 1)];
    let best = cands[0];
    let bestDt = Math.abs(samples[best][0] - ts);
    for (const c of cands) {
      const dt = Math.abs(samples[c][0] - ts);
      if (dt < bestDt) {
        best = c;
        bestDt = dt;
      }
    }
    setHover({ idx: best, clientX: e.clientX });
  };

  const hoverSample = hover != null ? samples[hover.idx] : null;
  // The line is colored with a vertical plasma gradient: hot colors at
  // the top of the chart (high values), cool at the bottom. So a spike
  // up to the threshold reads as orange even after the curve drops back.
  const stops = rampGradientStops(lo, hi, threshold);
  const stroke = `url(#${gradientId})`;
  // Color the hover dot to match the curve underneath -- ramp is value-
  // anchored, so the dot color is independent of the visible Y range.
  const hoverDotColor = hoverSample
    ? rampForValue(hoverSample[1], threshold)
    : "var(--color-accent)";

  return (
    <div className="relative h-full w-full">
      <motion.svg
        viewBox={`0 0 ${width} ${height}`}
        className="w-full h-full"
        preserveAspectRatio="none"
        onMouseMove={handleMove}
        onMouseLeave={() => setHover(null)}
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.2 }}
      >
        {/* Brand-ramp gradient stroke. Color is anchored to threshold
            (cool below, brand orange at, red above); opacity fades the
            curve toward the bg at the trough of its current dynamic
            range so peaks pop and quiet stretches recede. Anchored to
            the SVG viewBox via userSpaceOnUse so the opacity range is
            stable regardless of where data is clustered. */}
        <defs>
          <linearGradient id={gradientId} gradientUnits="userSpaceOnUse"
            x1={0} y1={0} x2={0} y2={height}>
            {stops.map((s, i) => (
              <stop key={i} offset={s.offset} stopColor={s.color} stopOpacity={s.opacity} />
            ))}
          </linearGradient>
          {/* CRT phosphor bloom (Magi terminal aesthetic). Slightly
              larger stdDeviation than the tile sparkline because this
              chart has more vertical real estate and a thicker stroke. */}
          <filter id={bloomId} x="-10%" y="-10%" width="120%" height="120%">
            <feGaussianBlur in="SourceGraphic" stdDeviation="2.2" result="blur" />
            <feMerge>
              <feMergeNode in="blur" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>

        {/* Y-axis grid + labels */}
        {yTicks.map((v, i) => {
          const y = yFor(v);
          return (
            <g key={`y${i}`}>
              <line
                x1={padL}
                x2={width - padR}
                y1={y}
                y2={y}
                stroke="var(--color-border)"
                strokeWidth={0.5}
                strokeDasharray={i === 0 || i === yTicks.length - 1 ? "" : "2 4"}
              />
              <text
                x={padL - 6}
                y={y + 3}
                textAnchor="end"
                fontSize="10"
                fill="var(--color-text-tertiary)"
              >
                {fmt(v)}
              </text>
            </g>
          );
        })}

        {/* X-axis labels */}
        {xTicks.map((ts, i) => (
          <text
            key={`x${i}`}
            x={xFor(ts)}
            y={height - padB + 14}
            textAnchor={i === 0 ? "start" : i === xTicks.length - 1 ? "end" : "middle"}
            fontSize="10"
            fill="var(--color-text-tertiary)"
          >
            {formatTimeOfDay(ts)}
          </text>
        ))}

        {/* Threshold line. Red because crossing it is the action that the
            user cares about; making it warning-yellow undersells the
            severity. */}
        {thresholdY != null && (
          <>
            <line
              x1={padL}
              x2={width - padR}
              y1={thresholdY}
              y2={thresholdY}
              stroke="var(--color-danger)"
              strokeWidth={1}
              strokeDasharray="4 4"
              opacity={0.75}
            />
            <text
              x={width - padR - 4}
              y={thresholdY - 5}
              textAnchor="end"
              fontFamily="var(--font-display)"
              fontSize={12}
              fontWeight={700}
              letterSpacing="0.08em"
              fill="var(--color-danger)"
              style={{
                filter:
                  "drop-shadow(0 0 2px var(--color-danger)) drop-shadow(0 0 6px color-mix(in srgb, var(--color-danger) 50%, transparent))",
              }}
            >
              TRIGGER {fmt(threshold!)}
            </text>
          </>
        )}

        {/* Data line */}
        <polyline
          points={polyPoints}
          fill="none"
          stroke={stroke}
          strokeWidth={1.25}
          strokeLinejoin="round"
          strokeLinecap="round"
          filter={`url(#${bloomId})`}
        />

        {/* Hover crosshair */}
        {hover != null && hoverSample && (
          <>
            <line
              x1={xFor(hoverSample[0])}
              x2={xFor(hoverSample[0])}
              y1={padT}
              y2={height - padB}
              stroke="var(--color-text-tertiary)"
              strokeWidth={0.5}
              opacity={0.6}
            />
            <circle
              cx={xFor(hoverSample[0])}
              cy={yFor(hoverSample[1])}
              r={3.5}
              fill={hoverDotColor}
              stroke="var(--color-bg)"
              strokeWidth={1.5}
            />
          </>
        )}
      </motion.svg>
      {hover != null && hoverSample && (
        <DetailHoverTooltip
          ts={hoverSample[0]}
          value={hoverSample[1]}
          threshold={threshold}
          fmt={fmt}
          xFrac={
            (xFor(hoverSample[0]) - padL) / innerW > 0.5
              ? null
              : (xFor(hoverSample[0]) - padL) / innerW
          }
          xFracRight={
            (xFor(hoverSample[0]) - padL) / innerW > 0.5
              ? 1 - (xFor(hoverSample[0]) - padL) / innerW
              : null
          }
        />
      )}
    </div>
  );
}

function DetailHoverTooltip({
  ts,
  value,
  threshold,
  fmt,
  xFrac,
  xFracRight,
}: {
  ts: number;
  value: number;
  threshold: number | null;
  fmt: (v: number) => string;
  xFrac: number | null;
  xFracRight: number | null;
}) {
  const overThreshold = threshold != null && value > threshold;
  const ageS = Math.max(0, Date.now() / 1000 - ts);
  const ageLabel = ageS < 60
    ? `${ageS.toFixed(0)}s ago`
    : ageS < 3600
    ? `${(ageS / 60).toFixed(1)}m ago`
    : `${(ageS / 3600).toFixed(1)}h ago`;
  return (
    <div
      className="absolute pointer-events-none text-xs px-3 py-1.5 rounded
                 border border-[var(--color-border-strong)] bg-[var(--color-surface-raised)]
                 shadow z-10"
      style={{
        // x offset is in the inner chart space; we approximate by using
        // CSS percentages relative to the parent <div> which is 100%.
        left: xFrac != null ? `calc(${(xFrac * 100).toFixed(1)}% + 56px)` : undefined,
        right: xFracRight != null ? `calc(${(xFracRight * 100).toFixed(1)}% + 16px)` : undefined,
        top: 16,
      }}
    >
      <div className="font-semibold tabular-nums" style={{ color: overThreshold ? "var(--color-danger)" : "inherit" }}>
        {fmt(value)}
        {overThreshold && " ⚠"}
      </div>
      <div className="text-[10px] text-[var(--color-text-tertiary)]">{formatTimeOfDay(ts)}</div>
      <div className="text-[10px] text-[var(--color-text-tertiary)]">{ageLabel}</div>
      {threshold != null && (
        <div className="text-[10px] text-[var(--color-text-tertiary)]">trigger {fmt(threshold)}</div>
      )}
    </div>
  );
}

function Stat({
  label,
  value,
  highlight,
  muted,
  live,
}: {
  label: string;
  value: string;
  highlight?: boolean;
  muted?: boolean;
  /** The live "Now" value -- painted in the theme's data color so it
      reads as the signature 3rd color, matching the Signals tiles. */
  live?: boolean;
}) {
  const color = highlight
    ? "var(--color-danger)"
    : muted
    ? "var(--color-text-tertiary)"
    : live
    ? "var(--color-detail)"
    : "var(--color-text-primary)";
  return (
    <div>
      <div className="hud hud-dim text-[10px] uppercase tracking-wider text-[var(--color-text-tertiary)]">
        {label}
      </div>
      <div
        className={`text-sm tabular-nums font-medium${highlight || (!muted && !highlight) ? " hud-glow" : ""}`}
        style={{ color }}
      >
        {value}
      </div>
    </div>
  );
}

function formatDuration(s: number): string {
  if (s <= 0) return "0s";
  if (s < 60) return `${s.toFixed(0)}s`;
  if (s < 3600) return `${(s / 60).toFixed(1)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}

// Touch the import so eslint doesn't complain about formatBytes being unused
// (kept exported from format.ts for other screens).
void formatBytes;
