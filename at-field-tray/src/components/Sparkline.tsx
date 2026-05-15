interface Props {
  values: number[];
  width?: number;
  height?: number;
  threshold?: number | null;
  color?: string;
  thresholdColor?: string;
}

/**
 * Tiny SVG sparkline. Hand-rolled rather than uPlot-of-the-week because
 * each rule shows ~30-60 points and we want zero JS overhead per redraw
 * at 1 Hz. uPlot is on the v0.3 wishlist if we ever ship a multi-hour
 * graph view.
 *
 * The path is normalized into a 0..1 coordinate space then scaled to the
 * SVG viewBox so re-renders never need to recompute pixel coords.
 */
export default function Sparkline({
  values,
  width = 240,
  height = 36,
  threshold = null,
  color = "var(--color-accent)",
  thresholdColor = "var(--color-warning)",
}: Props) {
  if (values.length < 2) {
    return (
      <svg viewBox={`0 0 ${width} ${height}`} className="sparkline" preserveAspectRatio="none">
        <text
          x={width / 2}
          y={height / 2 + 4}
          textAnchor="middle"
          fontSize="10"
          fill="var(--color-text-tertiary)"
        >
          {values.length === 0 ? "no data" : "warming up…"}
        </text>
      </svg>
    );
  }

  // Domain spans from min to max of the values, but always includes the
  // threshold so the threshold line never falls outside the rendered area.
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  if (threshold != null) {
    lo = Math.min(lo, threshold);
    hi = Math.max(hi, threshold);
  }
  if (hi - lo < 1e-6) {
    // Flat-line case: pad ±1 so the line doesn't sit on the bottom.
    hi += 1;
    lo -= 1;
  }
  const range = hi - lo;

  const xStep = width / (values.length - 1);
  const points = values
    .map((v, i) => {
      const x = i * xStep;
      const y = height - ((v - lo) / range) * height;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");

  const thresholdY =
    threshold != null ? height - ((threshold - lo) / range) * height : null;

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="sparkline" preserveAspectRatio="none">
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
        points={points}
        fill="none"
        stroke={color}
        strokeWidth={1.5}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}
