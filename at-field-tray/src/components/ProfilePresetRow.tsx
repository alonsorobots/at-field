/*
 * ProfilePresetRow -- single-row picker that nudges every rule's
 * threshold to its Aggressive / Normal / Relaxed canonical value.
 *
 * The "currently active" preset is inferred from the Rules snapshot:
 * if every known rule's threshold matches the same preset's value, that
 * preset is highlighted. Otherwise we render "Custom" -- the user has
 * tuned at least one slider away from the canon, and we don't lie about
 * which preset they're "on".
 *
 * Clicking a preset POSTs /profile, then calls onApplied so the parent
 * can refetch /rules. The server triggers an engine reload as part of
 * the same request; thresholds typically reflect the new values within
 * one /rules poll (1s).
 */

import { useState } from "react";
import type { EffectiveRuleView } from "../lib/api";
import { api } from "../lib/api";

type Profile = "aggressive" | "normal" | "relaxed";

interface Props {
  rules: EffectiveRuleView[];
  onApplied?: () => void;
}

const ORDER: Profile[] = ["aggressive", "normal", "relaxed"];

const LABEL: Record<Profile, string> = {
  aggressive: "Aggressive",
  normal: "Normal",
  relaxed: "Relaxed",
};

const HINT: Record<Profile, string> = {
  aggressive: "Lower thresholds across all rules. Fires earlier; fewer false negatives.",
  normal: "Balanced defaults from the conservative profile. Recommended starting point.",
  relaxed: "Higher thresholds across all rules. More headroom; only fires on clear distress.",
};

export default function ProfilePresetRow({ rules, onApplied }: Props) {
  const [pending, setPending] = useState<Profile | null>(null);
  const [error, setError] = useState<string | null>(null);

  const activeProfile = inferActiveProfile(rules);

  const apply = async (profile: Profile) => {
    setPending(profile);
    setError(null);
    try {
      await api.applyProfile(profile);
      onApplied?.();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setPending(null);
    }
  };

  return (
    <div className="frosted rounded-lg px-4 py-3">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="text-sm font-semibold">Profile</div>
          <div className="text-[11px] text-[var(--color-text-tertiary)] mt-0.5">
            Quick presets across all rules. Use the per-rule sliders below for fine control.
          </div>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          {ORDER.map((p) => (
            <button
              key={p}
              type="button"
              className="preset-pill"
              data-active={activeProfile === p}
              disabled={pending !== null}
              onClick={() => apply(p)}
              title={HINT[p]}
            >
              {LABEL[p]}
            </button>
          ))}
          <span
            className="preset-pill"
            data-active={activeProfile === "custom"}
            style={{ pointerEvents: "none" }}
            title="Any per-rule slider that diverges from the presets puts you here."
          >
            Custom
          </span>
        </div>
      </div>
      {pending && (
        <div className="text-[11px] text-[var(--color-text-tertiary)] mt-2">
          applying {LABEL[pending]}…
        </div>
      )}
      {error && (
        <div className="text-[11px] text-[var(--color-danger)] mt-2">
          {error}
        </div>
      )}
    </div>
  );
}

/** Walk the rules; if every (known) rule's threshold matches the same
 *  preset's value, return that preset. Otherwise "custom" -- meaning
 *  the user has tuned at least one slider away from the canon. */
function inferActiveProfile(
  rules: EffectiveRuleView[],
): Profile | "custom" {
  const tunable = rules.filter((r) => r.tuning != null);
  if (tunable.length === 0) return "custom";
  for (const profile of ORDER) {
    const matches = tunable.every((r) => {
      const preset = r.tuning!.presets[profile];
      // Allow tiny float jitter (e.g. 85.0 vs 85.00000001 from re-reads).
      return Math.abs(r.threshold - preset) < 0.01;
    });
    if (matches) return profile;
  }
  return "custom";
}
