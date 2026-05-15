import { useId, useRef, useState } from "react";
import { rampForValue, rampGradientStops } from "../lib/format";

interface Point {
  /** unix seconds */
  ts: number;
  value: number;
}

interface Props {
  /**
   * Either a flat values array (legacy / minimal usage) or an array of
   * {ts, value} points. The latter unlocks the hover tooltip's
   * "X seconds ago" labelling.
   */
  values: number[] | Point[];
  width?: number;
  height?: number;
  threshold?: number | null;
  /**
   * Stroke override. By default the line is colored with a vertical
   * plasma gradient so each Y position maps to its own color (cool
   * purple for low values at the bottom of the chart, hot orange/yellow
   * for high values at the top). Set to a string to force a flat color.
   */
  color?: string;
  thresholdColor?: string;
  /** Optional per-point label formatter for the hover tooltip. */
  formatValue?: (v: number) => string;
  /**
   * Optional click handler. When provided, the SVG renders as a
   * cursor-pointer element so the user knows it's interactive (used by
   * SignalsScreen to drill into the per-signal detail view).
   */
  onClick?: () => void;
}

/**
 * SVG sparkline with optional hover tooltip and click-to-drill.
 *
 * Hand-rolled rather than using a chart library because each tile shows
 * ≤ 3600 points at 1 Hz refresh and we want zero JS overhead per redraw.
 * The path is normalized into a 0..1 coordinate space then scaled to the
 * SVG viewBox so re-renders never need to recompute pixel coords.
 *
 * Hover behavior: on mousemove we map the pointer's X to the nearest
 * sample index (constant-time given uniform xStep) and show a vertical
 * crosshair plus a value tooltip. We use SVG-native `<title>` as a
 * simple fallback for accessibility and let the React tooltip layer
 * handle the rich version.
 */
export default function Sparkline({
  values,
  width = 240,
  height = 36,
  threshold = null,
  color,
  thresholdColor = "var(--color-danger)",
  formatValue,
  onClick,
}: Props) {
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const gradientId = `sparkline-grad-${useId().replace(/:/g, "")}`;

  // Normalize input to Point[] internally.
  const points: Point[] =
    values.length === 0
      ? []
      : typeof values[0] === "number"
      ? (values as number[]).map((v, i) => ({ ts: i, value: v }))
      : (values as Point[]);

  if (points.length < 2) {
    return (
      <svg viewBox={`0 0 ${width} ${height}`} className="sparkline" preserveAspectRatio="none">
        <text
          x={width / 2}
          y={height / 2 + 4}
          textAnchor="middle"
          fontSize="10"
          fill="var(--color-text-tertiary)"
        >
          {points.length === 0 ? "no data" : "warming up…"}
        </text>
      </svg>
    );
  }

  // Domain spans from min to max of the values, but always includes the
  // threshold so the threshold line never falls outside the rendered area.
  let lo = Infinity;
  let hi = -Infinity;
  for (const p of points) {
    if (p.value < lo) lo = p.value;
    if (p.value > hi) hi = p.value;
  }
  if (threshold != null) {
    lo = Math.min(lo, threshold);
    hi = Math.max(hi, threshold);
  }
  if (hi - lo < 1e-6) {
    hi += 1;
    lo -= 1;
  }
  const range = hi - lo;
  const xStep = width / (points.length - 1);
  const yFor = (v: number) => height - ((v - lo) / range) * height;

  const polyPoints = points
    .map((p, i) => `${(i * xStep).toFixed(1)},${yFor(p.value).toFixed(1)}`)
    .join(" ");

  const thresholdY = threshold != null ? yFor(threshold) : null;

  const handleMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const svg = svgRef.current;
    if (!svg) return;
    const rect = svg.getBoundingClientRect();
    // Clamp pointer to viewBox space, then snap to nearest sample.
    const xVB = ((e.clientX - rect.left) / rect.width) * width;
    const idx = Math.round(xVB / xStep);
    setHoverIdx(Math.max(0, Math.min(points.length - 1, idx)));
  };

  const handleLeave = () => setHoverIdx(null);

  const hover = hoverIdx != null ? points[hoverIdx] : null;
  const hoverX = hoverIdx != null ? hoverIdx * xStep : null;
  const hoverY = hover != null ? yFor(hover.value) : null;

  // Use a vertical plasma gradient as the stroke unless the caller
  // forced a flat color. The gradient maps Y → plasma so high values
  // (top of chart) get hot colors and low values (bottom) get cool ones.
  const useGradient = color == null;
  const stops = useGradient ? rampGradientStops(lo, hi, threshold) : [];
  const strokeColor = useGradient ? `url(#${gradientId})` : color!;
  const dotColor = useGradient && hover != null
    ? rampForValue(hover.value, threshold)
    : (color ?? "var(--color-accent)");

  return (
    <div className="relative" style={{ width: "100%" }}>
      <svg
        ref={svgRef}
        viewBox={`0 0 ${width} ${height}`}
        className="sparkline"
        preserveAspectRatio="none"
        onMouseMove={handleMove}
        onMouseLeave={handleLeave}
        onClick={onClick}
        style={{ cursor: onClick ? "pointer" : "default" }}
      >
        {useGradient && (
          <defs>
            {/* userSpaceOnUse: gradient is anchored to the SVG viewBox (0..height)
                rather than the polyline's bounding box. The bbox version can
                quietly shrink to whatever Y range the data actually covers,
                which collapses the opacity range to invisibility when most
                samples sit at one end. Anchoring to the viewBox keeps the
                top of the chart at full opacity and the bottom at TROUGH
                opacity REGARDLESS of the line's extent. */}
            <linearGradient id={gradientId} gradientUnits="userSpaceOnUse"
              x1={0} y1={0} x2={0} y2={height}>
              {stops.map((s, i) => (
                <stop key={i} offset={s.offset} stopColor={s.color} stopOpacity={s.opacity} />
              ))}
            </linearGradient>
          </defs>
        )}
        {thresholdY != null && (
          <line
            x1={0}
            x2={width}
            y1={thresholdY}
            y2={thresholdY}
            stroke={thresholdColor}
            strokeWidth={1}
            strokeDasharray="3 3"
            opacity={0.6}
          />
        )}
        <polyline
          points={polyPoints}
          fill="none"
          stroke={strokeColor}
          strokeWidth={1.1}
          strokeLinejoin="round"
          strokeLinecap="round"
        />
        {hoverX != null && (
          <>
            <line
              x1={hoverX}
              x2={hoverX}
              y1={0}
              y2={height}
              stroke="var(--color-text-tertiary)"
              strokeWidth={0.5}
              opacity={0.6}
            />
            <circle
              cx={hoverX}
              cy={hoverY!}
              r={2.5}
              fill={dotColor}
              stroke="var(--color-bg)"
              strokeWidth={1}
            />
          </>
        )}
      </svg>
      {hover != null && (
        <SparklineTooltip
          x={hoverX! / width}
          value={hover.value}
          ts={hover.ts}
          threshold={threshold}
          formatValue={formatValue}
        />
      )}
    </div>
  );
}

function SparklineTooltip({
  x,
  value,
  ts,
  threshold,
  formatValue,
}: {
  x: number; // 0..1 fraction across the sparkline
  value: number;
  ts: number;
  threshold: number | null;
  formatValue?: (v: number) => string;
}) {
  // Anchor toward whichever side of the sparkline the cursor's NOT on so
  // the tooltip never extends off-screen.
  const onLeftHalf = x < 0.5;
  const fmt = formatValue ?? ((v) => v.toFixed(2));
  const ageS = ts > 1e9 ? Math.max(0, Date.now() / 1000 - ts) : null;
  const overThreshold = threshold != null && value > threshold;

  return (
    <div
      className="absolute -top-1 pointer-events-none text-[11px] leading-tight px-2 py-1 rounded
                 border border-[var(--color-border-strong)] bg-[var(--color-surface-raised)]
                 text-[var(--color-text-primary)] whitespace-nowrap shadow"
      style={{
        left: onLeftHalf ? `${x * 100}%` : undefined,
        right: onLeftHalf ? undefined : `${(1 - x) * 100}%`,
        transform: "translateY(-100%)",
        zIndex: 5,
      }}
    >
      <div className="font-semibold tabular-nums">{fmt(value)}</div>
      {threshold != null && (
        <div
          className="text-[10px]"
          style={{ color: overThreshold ? "var(--color-danger)" : "var(--color-text-tertiary)" }}
        >
          threshold {fmt(threshold)}
          {overThreshold && " ⚠ over"}
        </div>
      )}
      {ageS != null && (
        <div className="text-[10px] text-[var(--color-text-tertiary)]">
          {ageS < 60 ? `${ageS.toFixed(0)}s ago` : `${(ageS / 60).toFixed(1)}m ago`}
        </div>
      )}
    </div>
  );
}
