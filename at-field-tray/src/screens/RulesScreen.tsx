import { useState } from "react";
import type { EffectiveRuleView, RulesSnapshot } from "../lib/api";
import { humanizeRule, humanizeRuleStatus, signalDisplayName } from "../lib/format";
import RuleThresholdSlider from "../components/RuleThresholdSlider";
import ProfilePresetRow from "../components/ProfilePresetRow";
import AdvancedRuleControls from "../components/AdvancedRuleControls";

interface Props {
  rules: RulesSnapshot | null;
  /** Called after a slider PATCH or profile preset application persists.
      Lets the parent refetch /rules so the UI converges to server truth. */
  onMutated?: () => void;
}

const RULE_TITLE: Record<string, string> = {
  "gpu-core-hot": "GPU running hot",
  "vram-junction-hot": "VRAM running hot",
  "ram-pressure": "System RAM pressure",
  "pagefile-pressure": "Pagefile pressure",
  "cpu-pkg-hot": "CPU running hot",
};

const RULE_DESCRIPTION: Record<string, string> = {
  "gpu-core-hot":
    "Watches the GPU core temperature for sustained heat that can throttle or damage the card. Triggers a kill of the offending process tree.",
  "vram-junction-hot":
    "Watches the VRAM junction temperature -- the canary that fires before the GPU core does on memory-bound jobs.",
  "ram-pressure":
    "Watches system RAM utilization. Triggers when memory stays pinned high enough that the OOM killer or paging would damage performance.",
  "pagefile-pressure":
    "Watches the Windows commit charge (pagefile + RAM). Sustained high commit means you're about to hit the memory wall hard.",
  "cpu-pkg-hot":
    "Watches the CPU temperature for sustained heat that indicates a runaway thread or cooling problem.",
};

/**
 * Per-rule cards with human descriptions, current verdict, and progress
 * bar. Disabled rules are listed below in a muted style with the reason
 * the operator should never have to guess "why didn't this fire?".
 */
export default function RulesScreen({ rules, onMutated }: Props) {
  if (!rules) {
    return <div className="p-6 text-sm text-[var(--color-text-secondary)]">Loading rules…</div>;
  }

  return (
    <div className="p-5 space-y-5 overflow-y-auto h-full">
      {rules.effective.length > 0 && (
        <ProfilePresetRow rules={rules.effective} onApplied={onMutated} />
      )}

      <section>
        <h2 className="text-xs font-semibold uppercase tracking-wider text-[var(--color-text-tertiary)] mb-2">
          Active rules
        </h2>
        {rules.effective.length === 0 ? (
          <div className="text-sm text-[var(--color-text-secondary)] italic">
            No rules are currently active. Check the Status tab for collector health.
          </div>
        ) : (
          <div className="space-y-2">
            {rules.effective.map((r) => (
              <RuleCard key={r.name} r={r} onMutated={onMutated} />
            ))}
          </div>
        )}
      </section>

      {rules.disabled.length > 0 && (
        <section>
          <h2 className="text-xs font-semibold uppercase tracking-wider text-[var(--color-text-tertiary)] mb-2">
            Disabled rules
          </h2>
          <p className="text-[11px] text-[var(--color-text-tertiary)] mb-2">
            These rules can't run on this machine -- usually because the sensor they need isn't
            installed (e.g. no LibreHardwareMonitor for CPU temperatures).
          </p>
          <div className="space-y-2">
            {rules.disabled.map((d) => {
              const title = RULE_TITLE[d.rule] ?? d.rule;
              return (
                <div
                  key={`${d.rule}::${d.signal}`}
                  className="rounded-lg px-4 py-3 border border-[var(--color-border)] bg-transparent"
                >
                  <div className="text-sm font-medium text-[var(--color-text-secondary)]">
                    {title}
                  </div>
                  <div className="text-[11px] text-[var(--color-text-tertiary)] mt-0.5">
                    Needed signal: {signalDisplayName(d.signal)} (<span className="font-mono">{d.signal}</span>)
                  </div>
                  <div className="text-xs text-[var(--color-text-secondary)] mt-1">{d.reason}</div>
                </div>
              );
            })}
          </div>
        </section>
      )}
    </div>
  );
}

function RuleCard({ r, onMutated }: { r: EffectiveRuleView; onMutated?: () => void }) {
  const title = RULE_TITLE[r.base_rule] ?? r.name;
  const desc = RULE_DESCRIPTION[r.base_rule];
  const triggering = r.verdict === "TRIGGER";
  // Advanced controls are collapsed by default; the threshold slider IS
  // the primary UI. Power users click to reveal window/cooldown/action
  // editors. Per-card local state keeps each card's expansion independent.
  const [showAdvanced, setShowAdvanced] = useState(false);
  return (
    <div
      className="frosted rounded-lg px-4 py-3 border"
      style={{
        borderColor: triggering ? "var(--color-danger)" : "var(--color-border)",
      }}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-sm font-semibold truncate">{title}</div>
          <div className="text-[11px] text-[var(--color-text-secondary)] mt-0.5">
            {humanizeRule(r)}
          </div>
        </div>
        <span className="verdict-pill" data-verdict={r.verdict}>
          {VERDICT_LABEL[r.verdict]}
        </span>
      </div>

      {desc && (
        <div className="text-[11px] text-[var(--color-text-tertiary)] mt-2 leading-relaxed">
          {desc}
        </div>
      )}

      {r.tuning && (
        <div className="mt-3">
          <RuleThresholdSlider
            baseRuleName={r.base_rule}
            signal={r.signal}
            threshold={r.threshold}
            tuning={r.tuning}
            onPersisted={onMutated}
          />
        </div>
      )}

      <div className="mt-3 flex items-center gap-3">
        <FractionBar fraction={r.fraction_over} threshold={r.min_fraction_over} />
        <div className="text-[11px] text-[var(--color-text-secondary)] tabular-nums whitespace-nowrap min-w-[80px]">
          {r.latest_value != null && <>now {formatNum(r.latest_value)}</>}
        </div>
      </div>

      <div className="mt-1.5 flex items-center justify-between text-[11px]">
        <span className="text-[var(--color-text-tertiary)]">{humanizeRuleStatus(r)}</span>
        <div className="flex items-center gap-3">
          {r.cooldown_remaining_s > 0.5 && (
            <span className="text-[var(--color-warning)]">
              cooldown {r.cooldown_remaining_s.toFixed(0)}s
            </span>
          )}
          {r.triggers > 0 && r.cooldown_remaining_s <= 0.5 && (
            <span className="text-[var(--color-text-tertiary)]">
              {r.triggers} trigger{r.triggers === 1 ? "" : "s"} so far
            </span>
          )}
          <button
            type="button"
            onClick={() => setShowAdvanced((v) => !v)}
            className="text-[var(--color-text-secondary)] hover:text-[var(--color-text-primary)] transition-colors"
            aria-expanded={showAdvanced}
            aria-controls={`advanced-${r.name}`}
          >
            {showAdvanced ? "Hide advanced" : "Advanced…"}
          </button>
        </div>
      </div>

      {showAdvanced && (
        <div id={`advanced-${r.name}`}>
          <AdvancedRuleControls rule={r} onPersisted={onMutated} />
        </div>
      )}
    </div>
  );
}

const VERDICT_LABEL: Record<EffectiveRuleView["verdict"], string> = {
  TRIGGER: "triggering",
  BELOW: "ok",
  INSUFFICIENT: "warming up",
};

function FractionBar({ fraction, threshold }: { fraction: number; threshold: number }) {
  const pct = Math.min(1, Math.max(0, fraction)) * 100;
  const thresholdPct = Math.min(1, Math.max(0, threshold)) * 100;
  return (
    <div className="relative flex-1 h-1.5 bg-[var(--color-surface-raised)] rounded-full overflow-hidden">
      <div
        className="absolute inset-y-0 left-0"
        style={{
          width: `${pct}%`,
          background: fraction >= threshold ? "var(--color-danger)" : "var(--color-accent)",
        }}
      />
      <div
        className="absolute inset-y-0 w-px bg-[var(--color-warning)]/70"
        style={{ left: `${thresholdPct}%` }}
        title={`triggers at ${(threshold * 100).toFixed(0)}% of window`}
      />
    </div>
  );
}

function formatNum(v: number): string {
  if (Math.abs(v) >= 1e9) return `${(v / 1e9).toFixed(1)}G`;
  if (Math.abs(v) >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (Math.abs(v) >= 100) return v.toFixed(0);
  return v.toFixed(1);
}
