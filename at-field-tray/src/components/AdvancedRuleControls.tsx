/*
 * AdvancedRuleControls -- editors for the rule fields that don't fit on a
 * single threshold slider: window_s, cooldown_s, action.
 *
 * Why a separate component instead of more inputs on RuleCard:
 *   - The threshold slider IS the primary control; 95% of users only ever
 *     touch that. Hiding window/cooldown/action behind an "Advanced" toggle
 *     keeps the default view glanceable and signals to power users where
 *     to look when they want more rope.
 *   - Each editor commits on blur (or button-click for the action select).
 *     We reuse the same debounce-then-PATCH pattern as the threshold
 *     slider so rapid keystrokes don't spam the atomic config rewrite.
 *   - Errors are rendered inline next to the field that caused them so
 *     the user sees which input the server rejected without us having to
 *     guess + highlight.
 */

import { useEffect, useRef, useState } from "react";
import type { EffectiveRuleView, RuleFieldUpdate } from "../lib/api";
import { api } from "../lib/api";

interface Props {
  rule: EffectiveRuleView;
  /** Default cooldown that applies when the rule's per-rule override is
      null (we surface this so the user knows what value they're falling
      back to). */
  defaultCooldownSeconds?: number;
  onPersisted?: () => void;
}

const ACTIONS: Array<EffectiveRuleView["action"]> = ["kill", "throttle", "log"];

const ACTION_LABEL: Record<EffectiveRuleView["action"], string> = {
  kill: "Kill the offending process",
  throttle: "Suspend (throttle) for a few seconds",
  log: "Log only (don't intervene)",
};

const APPLY_DEBOUNCE_MS = 350;

export default function AdvancedRuleControls({
  rule,
  defaultCooldownSeconds = 60,
  onPersisted,
}: Props) {
  return (
    <div className="space-y-3 mt-3 pt-3 border-t border-[var(--color-border)]">
      <FieldRow
        label="Sustained for"
        hint="How many seconds the signal must stay above threshold before triggering."
      >
        <SecondsField
          rule={rule}
          field="window_s"
          value={rule.window_s}
          min={1}
          max={600}
          unit="s"
          onPersisted={onPersisted}
        />
      </FieldRow>

      <FieldRow
        label="Cooldown"
        hint="After triggering, the rule sleeps this many seconds before being eligible to fire again."
      >
        <SecondsField
          rule={rule}
          field="cooldown_s"
          value={rule.cooldown_s ?? defaultCooldownSeconds}
          isDefault={rule.cooldown_s == null}
          min={0}
          max={3600}
          unit="s"
          defaultHint={`(default ${defaultCooldownSeconds}s)`}
          onPersisted={onPersisted}
        />
      </FieldRow>

      <FieldRow
        label="When triggered"
        hint="The action AT-Field takes when the rule fires. Kill is the default; throttle is gentler; log is observe-only."
      >
        <ActionSelect rule={rule} onPersisted={onPersisted} />
      </FieldRow>
    </div>
  );
}

function FieldRow({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="grid grid-cols-[120px_1fr] gap-3 items-start">
      <div>
        <div className="text-[11px] font-semibold text-[var(--color-text-secondary)]">
          {label}
        </div>
        {hint && (
          <div className="text-[10px] text-[var(--color-text-tertiary)] mt-0.5 leading-snug">
            {hint}
          </div>
        )}
      </div>
      <div>{children}</div>
    </div>
  );
}

interface SecondsFieldProps {
  rule: EffectiveRuleView;
  field: "window_s" | "cooldown_s";
  value: number;
  /** True when displaying the inherited default (rule's own value is null).
      We render in muted style + put a hint next to it. */
  isDefault?: boolean;
  defaultHint?: string;
  min: number;
  max: number;
  unit: string;
  onPersisted?: () => void;
}

function SecondsField({
  rule, field, value, isDefault, defaultHint, min, max, unit, onPersisted,
}: SecondsFieldProps) {
  const [draft, setDraft] = useState<string>(String(value));
  const [error, setError] = useState<string | null>(null);
  const [persisting, setPersisting] = useState(false);
  const debounceRef = useRef<number | null>(null);
  const lastServerRef = useRef<number>(value);

  // Reconcile to server-truth when an external change lands (preset
  // applied, parallel edit, etc.). Don't disturb a focused-and-being-typed
  // input -- only re-sync when the displayed value matches what we last
  // wrote (i.e. the user isn't mid-edit).
  useEffect(() => {
    if (value !== lastServerRef.current) {
      lastServerRef.current = value;
      setDraft(String(value));
    }
  }, [value]);

  const commit = (raw: string) => {
    if (debounceRef.current != null) window.clearTimeout(debounceRef.current);
    const parsed = Number(raw);
    if (!Number.isFinite(parsed) || !Number.isInteger(parsed)) {
      setError("must be a whole number of seconds");
      return;
    }
    if (parsed < min || parsed > max) {
      setError(`must be ${min}..${max}`);
      return;
    }
    setError(null);
    debounceRef.current = window.setTimeout(async () => {
      setPersisting(true);
      try {
        const update: RuleFieldUpdate = { [field]: parsed } as RuleFieldUpdate;
        await api.patchRuleFields(rule.base_rule, update);
        lastServerRef.current = parsed;
        onPersisted?.();
      } catch (e) {
        setError((e as Error).message);
        setDraft(String(lastServerRef.current));
      } finally {
        setPersisting(false);
      }
    }, APPLY_DEBOUNCE_MS);
  };

  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2">
        <input
          type="number"
          min={min}
          max={max}
          step={1}
          value={draft}
          onChange={(e) => {
            setDraft(e.target.value);
            commit(e.target.value);
          }}
          onBlur={(e) => commit(e.target.value)}
          aria-label={`${rule.base_rule} ${field}`}
          className={`atfield-input w-24 tabular-nums text-right ${
            isDefault ? "opacity-60" : ""
          }`}
        />
        <span className="text-[11px] text-[var(--color-text-tertiary)] tabular-nums">
          {unit}
        </span>
        {defaultHint && isDefault && (
          <span className="text-[10px] text-[var(--color-text-tertiary)] italic">
            {defaultHint}
          </span>
        )}
        {persisting && (
          <span className="text-[10px] text-[var(--color-text-tertiary)]">saving…</span>
        )}
      </div>
      {error && (
        <div className="text-[10px] text-[var(--color-danger)]">{error}</div>
      )}
    </div>
  );
}

function ActionSelect({
  rule, onPersisted,
}: {
  rule: EffectiveRuleView;
  onPersisted?: () => void;
}) {
  const [value, setValue] = useState<EffectiveRuleView["action"]>(rule.action);
  const [error, setError] = useState<string | null>(null);
  const [persisting, setPersisting] = useState(false);
  const lastServerRef = useRef<EffectiveRuleView["action"]>(rule.action);

  useEffect(() => {
    if (rule.action !== lastServerRef.current) {
      lastServerRef.current = rule.action;
      setValue(rule.action);
    }
  }, [rule.action]);

  const commit = async (next: EffectiveRuleView["action"]) => {
    setValue(next);
    setError(null);
    setPersisting(true);
    try {
      await api.patchRuleFields(rule.base_rule, { action: next });
      lastServerRef.current = next;
      onPersisted?.();
    } catch (e) {
      setError((e as Error).message);
      setValue(lastServerRef.current);
    } finally {
      setPersisting(false);
    }
  };

  return (
    <div className="space-y-1">
      <select
        value={value}
        onChange={(e) => commit(e.target.value as EffectiveRuleView["action"])}
        aria-label={`${rule.base_rule} action`}
        className="atfield-input w-full max-w-xs"
      >
        {ACTIONS.map((a) => (
          <option key={a} value={a}>
            {ACTION_LABEL[a]}
          </option>
        ))}
      </select>
      {persisting && (
        <div className="text-[10px] text-[var(--color-text-tertiary)]">saving…</div>
      )}
      {error && (
        <div className="text-[10px] text-[var(--color-danger)]">{error}</div>
      )}
    </div>
  );
}
