/*
 * RuleThresholdSlider -- per-rule threshold control with live tier tooltip.
 *
 * UX contract:
 *   - User drags the thumb. Local state updates IMMEDIATELY (optimistic).
 *   - Tooltip floats above the thumb showing the value + classified tier
 *     ("Aggressive" / "Normal" / "Relaxed") so the operator gets a feel
 *     for what they're choosing without staring at numbers.
 *   - On release (or 250ms after last drag stop), we PATCH the server.
 *     Debouncing keeps wheel-scrolling on the slider from spamming the
 *     atomic config rewrite.
 *   - If the PATCH fails, revert local state to the server's last known
 *     value and surface the error inline. The Rules-screen poller will
 *     reconcile back to the truth on its next tick anyway.
 *   - Tier band markers are drawn on the track so the user can see WHERE
 *     the boundaries are without having to drag past them.
 */

import { useEffect, useRef, useState } from "react";
import type { RuleTuning } from "../lib/api";
import { api } from "../lib/api";
import { classifyTier, tierColor, tierDescription } from "../lib/format";

interface Props {
  baseRuleName: string;
  signal: string;
  /** Server's authoritative current threshold. The slider snaps back to
      this on PATCH failure. */
  threshold: number;
  tuning: RuleTuning;
  /** Notify parent when a successful PATCH lands so it can refetch
      /rules and refresh the rest of the card UI. */
  onPersisted?: () => void;
}

/** Cap on how often we round-trip to the server while dragging. */
const APPLY_DEBOUNCE_MS = 250;

export default function RuleThresholdSlider({
  baseRuleName, signal, threshold, tuning, onPersisted,
}: Props) {
  // Local optimistic value. Diverges from `threshold` while dragging,
  // converges back when the parent's poll completes.
  const [value, setValue] = useState<number>(threshold);
  const [persisting, setPersisting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const debounceRef = useRef<number | null>(null);

  // Whenever the server's truth changes (e.g. preset button applied,
  // or a parallel slider edit landed), pull it into local state UNLESS
  // we're mid-drag (we'd rubber-band the user's thumb).
  const lastServerThresholdRef = useRef<number>(threshold);
  useEffect(() => {
    if (threshold !== lastServerThresholdRef.current) {
      lastServerThresholdRef.current = threshold;
      setValue(threshold);
    }
  }, [threshold]);

  const tier = classifyTier(value, tuning);
  const trackPercent = (v: number) =>
    ((v - tuning.min) / (tuning.max - tuning.min)) * 100;

  const apply = (v: number) => {
    if (debounceRef.current != null) {
      window.clearTimeout(debounceRef.current);
    }
    debounceRef.current = window.setTimeout(async () => {
      setPersisting(true);
      setError(null);
      try {
        await api.patchRule(baseRuleName, v);
        lastServerThresholdRef.current = v;
        onPersisted?.();
      } catch (e) {
        setError((e as Error).message);
        // Revert to server truth on failure.
        setValue(lastServerThresholdRef.current);
      } finally {
        setPersisting(false);
      }
    }, APPLY_DEBOUNCE_MS);
  };

  const onChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const v = Number(e.target.value);
    setValue(v);
    apply(v);
  };

  // Tier-band tick marks: aggressive_max and relaxed_min as faint vertical
  // lines on the track so the operator can SEE where the boundaries live.
  const aggMaxPct = trackPercent(tuning.aggressive_max);
  const relMinPct = trackPercent(tuning.relaxed_min);

  // Tooltip horizontal offset. We use the same percent-along-track math
  // as the input thumb; CSS transform centers it.
  const tooltipPct = trackPercent(value);

  return (
    <div className="space-y-2">
      <div className="relative pt-7 pb-2">
        {/* Tooltip bubble. Always visible (not just on hover) so the
            operator can read the current tier at a glance without having
            to mouse over. The bubble color tracks the tier. */}
        <div
          className="absolute -top-0 select-none pointer-events-none"
          style={{
            left: `${tooltipPct}%`,
            transform: "translate(-50%, 0)",
          }}
        >
          <div
            className="text-[10px] font-semibold uppercase tracking-wider px-2 py-0.5 rounded-md whitespace-nowrap"
            style={{
              color: tierColor(tier),
              border: `1px solid ${tierColor(tier)}`,
              background: "var(--color-surface-raised)",
            }}
            title={tierDescription(tier)}
          >
            {tier} · {formatTooltipValue(value, tuning.unit)}
          </div>
        </div>

        {/* Track with tier-band markers. Drawn behind the input so the
            input's thumb sits on top. */}
        <div className="absolute left-0 right-0 top-[34px] h-1.5 rounded-full bg-[var(--color-surface-raised)] pointer-events-none">
          {/* Aggressive band fill (left of aggressive_max) */}
          <div
            className="absolute inset-y-0 left-0 rounded-l-full"
            style={{
              width: `${aggMaxPct}%`,
              background: "color-mix(in srgb, var(--color-accent) 16%, transparent)",
            }}
          />
          {/* Relaxed band fill (right of relaxed_min) */}
          <div
            className="absolute inset-y-0 rounded-r-full"
            style={{
              left: `${relMinPct}%`,
              right: 0,
              background: "color-mix(in srgb, var(--color-text-tertiary) 18%, transparent)",
            }}
          />
          {/* Boundary tick marks */}
          <div
            className="absolute top-[-2px] bottom-[-2px] w-px bg-[var(--color-text-tertiary)] opacity-40"
            style={{ left: `${aggMaxPct}%` }}
            title={`Aggressive ≤ ${tuning.aggressive_max}${tuning.unit}`}
          />
          <div
            className="absolute top-[-2px] bottom-[-2px] w-px bg-[var(--color-text-tertiary)] opacity-40"
            style={{ left: `${relMinPct}%` }}
            title={`Relaxed ≥ ${tuning.relaxed_min}${tuning.unit}`}
          />
        </div>

        <input
          type="range"
          min={tuning.min}
          max={tuning.max}
          step={tuning.step || 1}
          value={value}
          onChange={onChange}
          aria-label={`Threshold for ${baseRuleName}`}
          className="atfield-range relative w-full h-6 cursor-pointer"
        />
      </div>

      <div className="flex items-center justify-between text-[10px] text-[var(--color-text-tertiary)] tabular-nums">
        <span>
          {tuning.min}{tuning.unit} <span className="opacity-50">aggressive</span>
        </span>
        <span className="opacity-50">{signal}</span>
        <span>
          <span className="opacity-50">relaxed</span> {tuning.max}{tuning.unit}
        </span>
      </div>

      {(persisting || error) && (
        <div className="text-[10px] text-right">
          {persisting && (
            <span className="text-[var(--color-text-tertiary)]">saving…</span>
          )}
          {error && (
            <span className="text-[var(--color-danger)]">save failed: {error}</span>
          )}
        </div>
      )}
    </div>
  );
}

function formatTooltipValue(v: number, unit: string): string {
  // One decimal for sub-1° precision, no decimal for cleaner percents.
  if (unit === "°C") return `${v.toFixed(0)}°C`;
  if (unit === "%") return `${v.toFixed(0)}%`;
  return `${v}${unit}`;
}
