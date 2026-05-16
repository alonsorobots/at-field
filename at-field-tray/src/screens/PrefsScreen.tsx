/*
 * PrefsScreen -- standalone tab for user preferences.
 *
 * Lives in its own tab (rather than a section on Status) because:
 *  - Status is for ops triage (what is the watchdog DOING right now?);
 *    Prefs is for personalization. Mixing them muddies the mental model.
 *  - As preferences grow (theme, refresh rate, future autostart toggle),
 *    they deserve real estate that can scroll independently of health.
 *
 * Sections so far:
 *  - Theme picker (Eva color schemes -- the headline for this screen)
 *  - Refresh rate (moved from StatusScreen)
 *
 * Each section is self-contained and persists to localStorage on change.
 * No global "save" button -- changes apply immediately so the user gets
 * direct feedback (theme switch is instant; refresh-rate hint admits
 * that one applies on next reopen).
 */

import { useState } from "react";
import {
  POLL_INTERVAL_DEFAULT_MS,
  getPollIntervalMs,
  setPollIntervalMs,
} from "../lib/preferences";
import {
  THEMES,
  type ThemeId,
  getTheme,
  setTheme,
} from "../lib/theme";

export default function PrefsScreen() {
  return (
    <div className="p-5 space-y-5 overflow-y-auto h-full">
      <ThemePicker />
      <RefreshRateSection />
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// Theme picker
// ──────────────────────────────────────────────────────────────────────

function ThemePicker() {
  // Read from localStorage on first render. Subsequent changes flow
  // through setActive() which both stamps the document and updates state
  // so the active highlight follows the user.
  const [active, setActive] = useState<ThemeId>(() => getTheme());

  const choose = (id: ThemeId) => {
    setActive(id);
    setTheme(id); // applies to <html data-theme=...> immediately
  };

  return (
    <section>
      <h2 className="hud text-xs font-semibold uppercase tracking-wider text-[var(--color-text-tertiary)] mb-3">
        Color scheme
      </h2>
      <div className="grid grid-cols-2 gap-2">
        {THEMES.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => choose(t.id)}
            className="theme-tile"
            data-active={active === t.id}
            aria-label={`Apply ${t.label} theme`}
            aria-pressed={active === t.id}
          >
            <div className="flex items-center gap-3">
              <ThemeSwatch swatches={t.swatches} />
              <div className="min-w-0 flex-1 text-left">
                <div className="hud text-sm truncate">{t.label}</div>
              </div>
              {active === t.id && (
                <span
                  className="hud text-[10px] font-bold uppercase tracking-wider"
                  style={{ color: "var(--color-accent)" }}
                >
                  Active
                </span>
              )}
            </div>
          </button>
        ))}
      </div>
    </section>
  );
}

function ThemeSwatch({ swatches }: { swatches: [string, string, string] }) {
  // Three concentric chips so the user can see bg + accent + secondary
  // accent at a glance. Anchored colors so the picker tile reads honestly
  // even when its theme isn't active.
  const [bg, primary, secondary] = swatches;
  return (
    <div
      className="rounded-md w-14 h-10 flex-shrink-0 border border-[var(--color-border-strong)] overflow-hidden flex"
      style={{ background: bg }}
      aria-hidden
    >
      <div className="flex-1" style={{ background: bg }} />
      <div className="flex-1" style={{ background: primary }} />
      <div className="flex-1" style={{ background: secondary }} />
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// Refresh rate
// ──────────────────────────────────────────────────────────────────────

function RefreshRateSection() {
  const [intervalMs, setIntervalMs] = useState<number>(getPollIntervalMs());
  const [savedAt, setSavedAt] = useState<number | null>(null);

  const handleChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    const next = setPollIntervalMs(Number.parseInt(e.target.value, 10));
    setIntervalMs(next);
    setSavedAt(Date.now());
  };

  const choices: Array<{ ms: number; label: string }> = [
    { ms: 250, label: "0.25 s — snappy (more CPU)" },
    { ms: 500, label: "0.5 s" },
    { ms: 1000, label: "1 s — recommended (default)" },
    { ms: 2000, label: "2 s" },
    { ms: 5000, label: "5 s" },
    { ms: 10000, label: "10 s — battery saver" },
  ];

  return (
    <section>
      <h2 className="hud text-xs font-semibold uppercase tracking-wider text-[var(--color-text-tertiary)] mb-2">
        Dashboard refresh rate
      </h2>
      <div className="frosted rounded-lg border border-[var(--color-border)] p-4 flex items-start gap-3">
        <div className="flex-1">
          <p className="text-xs text-[var(--color-text-secondary)] leading-relaxed">
            How often the dashboard re-queries the watchdog. Faster
            updates make sparklines feel snappier; slower updates use
            less power. The watchdog itself runs at 1 Hz, so going below
            1 s mostly polls the same sample twice.
            {intervalMs !== POLL_INTERVAL_DEFAULT_MS && (
              <>
                {" "}
                <button
                  type="button"
                  className="underline text-[var(--color-accent)]"
                  onClick={() => {
                    setPollIntervalMs(POLL_INTERVAL_DEFAULT_MS);
                    setIntervalMs(POLL_INTERVAL_DEFAULT_MS);
                    setSavedAt(Date.now());
                  }}
                >
                  Reset to default
                </button>
              </>
            )}
          </p>
        </div>
        <div className="flex flex-col items-end gap-1">
          <select
            value={intervalMs}
            onChange={handleChange}
            className="atfield-input"
            aria-label="Dashboard refresh rate"
          >
            {choices.map((c) => (
              <option key={c.ms} value={c.ms}>
                {c.label}
              </option>
            ))}
          </select>
          {savedAt && (
            <span className="text-[10px] text-[var(--color-text-tertiary)]">
              Applies next time you open the dashboard
            </span>
          )}
        </div>
      </div>
    </section>
  );
}

