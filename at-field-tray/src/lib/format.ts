/*
 * Display formatting utilities used by every screen.
 *
 * All "the API says X, the UI should say Y" translations live here so
 * the screens stay declarative and the rules for naming things stay in
 * one place. Anything you'd want to tweak after a friend tries the app
 * lives in this file.
 */

import type { EffectiveRuleView } from "./api";

// ─────────────────────────────────────────────────────────────────────
// Script name extraction (kill report headline)
// ─────────────────────────────────────────────────────────────────────

const PY_VALUELESS_SHORT_FLAGS = new Set("BOdEhiIqRsSuvVx".split(""));
const PY_VALUE_TAKING_SHORT_FLAGS = new Set(["-W", "-X", "-c"]);

function basename(p: string): string {
  if (!p) return p;
  const idx = Math.max(p.lastIndexOf("\\"), p.lastIndexOf("/"));
  return idx >= 0 ? p.slice(idx + 1) : p;
}

/**
 * Best-effort "what was the user actually running?" extraction from a
 * process cmdline. Mirrors `script_name_from_cmdline` in the Python
 * actuator -- kept in sync so old events.jsonl entries (written before
 * the server-side helper landed) still get a friendly headline.
 *
 * Examples:
 *   ['python.exe', 'train.py', '--lr', '1e-4']         → 'train.py'
 *   ['python.exe', '-m', 'torch.distributed.run', ...] → 'torch.distributed.run'
 *   ['python.exe', '-u', 'scripts/train.py']           → 'train.py'
 *   ['powershell.exe', '-File', 'C:\\foo\\bar.ps1']    → 'bar.ps1'
 *   ['cmd.exe', '/c', 'run.bat']                       → 'run.bat'
 *   ['python.exe', '-c', 'import torch; ...']          → '<inline -c>'
 *   ['python.exe']                                     → null
 */
export function extractScriptName(cmdline: string[] | undefined | null): string | null {
  if (!cmdline || cmdline.length < 2) return null;
  const argv = cmdline.slice(1);

  let i = 0;
  while (i < argv.length) {
    const arg = argv[i];

    if (arg === "-c") return "<inline -c>";
    if (arg === "-Command") return "<inline -Command>";

    if (arg === "-m") return i + 1 < argv.length ? argv[i + 1] : null;

    const lc = arg.toLowerCase();
    if ((lc === "-file" || lc === "-f") && i + 1 < argv.length) {
      return basename(argv[i + 1]);
    }
    if ((arg === "/c" || arg === "/k" || arg === "/C" || arg === "/K") && i + 1 < argv.length) {
      return basename(argv[i + 1]);
    }
    if (arg === "--") {
      return i + 1 < argv.length ? basename(argv[i + 1]) : null;
    }
    if (arg.startsWith("--")) {
      i += 1;
      continue;
    }
    if (arg.startsWith("-") && arg.length >= 2) {
      if (PY_VALUE_TAKING_SHORT_FLAGS.has(arg)) {
        i += 2;
        continue;
      }
      const stripped = arg.replace(/^-+/, "");
      if (stripped && stripped.split("").every((c) => PY_VALUELESS_SHORT_FLAGS.has(c))) {
        i += 1;
        continue;
      }
      i += 1;
      continue;
    }
    return basename(arg);
  }
  return null;
}

// ─────────────────────────────────────────────────────────────────────
// Signal display name
// ─────────────────────────────────────────────────────────────────────

/**
 * Convert a wire signal name to a human label.
 *
 * Wire names are dotted, lowercase, machine-friendly (e.g.
 * `gpu.0.core_temp_c`); display names are Title Case with the unit in
 * parens (e.g. "GPU 0 Core Temp (°C)"). Unknown signals fall back to a
 * best-effort prettifier so we never show literal `foo.bar.baz` to the
 * user — but please add a real entry here when you wire up a new signal.
 */
export function signalDisplayName(wire: string): string {
  const explicit = SIGNAL_NAMES[wire];
  if (explicit) return explicit;

  // Pattern-match common shapes: gpu.<n>.<metric> and system.<metric>.
  const gpuMatch = wire.match(/^gpu\.(\d+)\.(.+)$/);
  if (gpuMatch) {
    const idx = gpuMatch[1];
    const metric = gpuMatch[2];
    return `GPU ${idx} ${metricLabel(metric)}`;
  }
  const sysMatch = wire.match(/^system\.(.+)$/);
  if (sysMatch) {
    return `System ${metricLabel(sysMatch[1])}`;
  }
  return prettify(wire);
}

const SIGNAL_NAMES: Record<string, string> = {
  // Curated overrides where the auto-generator would produce something
  // awkward. Keep alphabetized.
  //
  // Naming convention:
  //   * "RAM used"          — physical memory (what Task Manager calls
  //                            "Memory" / "In use"). The most intuitive label.
  //   * "Committed memory"  — RAM + page file backed virtual commit. This
  //                            is what actually triggers OOM-class failures
  //                            on Windows; the OS calls this "Commit Charge"
  //                            and Task Manager labels it "Committed".
  //   * "Page file used"    — page file alone. Rarely the metric you want
  //                            in isolation; included for power users.
  "gpu.processes": "GPU process count",
  "system.commit_percent": "Committed memory (%)",
  "system.ram_used_percent": "RAM used (%)",
  "system.swap_used_percent": "Page file used (%)",
  "system.cpu_package_temp_c": "CPU temp (°C)",
};

/** Signal-display filter: returns false for signals we don't want surfaced
    in the default UI. Currently hides every `*_bytes` signal because the
    `*_percent` companion already conveys the same intensity in a more
    intuitive unit. The bytes signals still exist on the wire (history,
    /signals API, audit) so power-user tooling can read them; this only
    keeps the dashboard from showing two cards per resource. */
export function isDefaultDisplaySignal(wire: string): boolean {
  if (wire.endsWith("_bytes")) return false;
  return true;
}

function metricLabel(metric: string): string {
  // gpu.0.core_temp_c -> "Core temp (°C)"
  // gpu.0.power_w -> "Power (W)"
  // gpu.0.vram_used_percent -> "VRAM used (%)"
  // gpu.0.vram_used_bytes -> "VRAM used"
  // gpu.0.util_percent -> "Utilization (%)"
  // gpu.0.mem_junction_temp_c -> "VRAM junction temp (°C)"
  if (metric === "core_temp_c") return "Core temp (°C)";
  if (metric === "mem_junction_temp_c") return "VRAM junction temp (°C)";
  if (metric === "power_w") return "Power (W)";
  if (metric === "util_percent") return "Utilization (%)";
  if (metric === "vram_used_percent") return "VRAM used (%)";
  if (metric === "vram_used_bytes") return "VRAM used";

  // Trailing _c / _w / _percent / _bytes get peeled off into a unit suffix.
  const m = metric.match(/^(.+?)_(c|w|percent|bytes|count)$/);
  if (m) {
    const [, base, suffix] = m;
    const unit = { c: "(°C)", w: "(W)", percent: "(%)", bytes: "(B)", count: "" }[suffix];
    return `${prettify(base)}${unit ? " " + unit : ""}`;
  }
  return prettify(metric);
}

function prettify(s: string): string {
  return s
    .replace(/[._]/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase())
    .trim();
}

// ─────────────────────────────────────────────────────────────────────
// Value formatting
// ─────────────────────────────────────────────────────────────────────

export function formatValue(v: number, unit: string): string {
  switch (unit) {
    case "celsius":
      return `${v.toFixed(0)}°C`;
    case "percent":
      return `${v.toFixed(1)}%`;
    case "watts":
      return `${v.toFixed(0)} W`;
    case "bytes":
      return formatBytes(v);
    case "count":
      return v.toFixed(0);
    default:
      return v.toString();
  }
}

export function formatBytes(v: number): string {
  if (v >= 1024 ** 3) return `${(v / 1024 ** 3).toFixed(2)} GB`;
  if (v >= 1024 ** 2) return `${(v / 1024 ** 2).toFixed(0)} MB`;
  if (v >= 1024) return `${(v / 1024).toFixed(0)} KB`;
  return `${v} B`;
}

export function formatTimeAgo(unixSeconds: number, nowSeconds: number = Date.now() / 1000): string {
  const ageS = Math.max(0, nowSeconds - unixSeconds);
  if (ageS < 60) return `${ageS.toFixed(0)}s ago`;
  if (ageS < 3600) return `${(ageS / 60).toFixed(0)}m ago`;
  if (ageS < 86400) return `${(ageS / 3600).toFixed(1)}h ago`;
  return `${(ageS / 86400).toFixed(1)}d ago`;
}

export function formatTimeOfDay(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

// ─────────────────────────────────────────────────────────────────────
// Brand color ramp
// ─────────────────────────────────────────────────────────────────────
//
// Designed to be brand-coherent rather than perceptually uniform:
//
//   t = 0.00   deep warm slate, ~recedes into the dark bg
//   t ↑        builds through dim taupe → faded amber
//   t = 0.85   brand orange (#ff6a13). Anchored to the threshold so
//              "the line is the brand color" means "you're at the trigger"
//   t = 0.95   vermillion (just over)
//   t = 1.00   deep red (way over -- something's wrong)
//
// We don't use a ScientificViz palette like plasma/viridis because:
//   * Their middle hues (pink, magenta, teal, green) compete with our
//     brand orange and turn the dashboard into a candy bowl.
//   * Their "danger" end (yellow) reads as "happy" on dark UI, not "hot".
//   * Brand color recognition matters: if the line turns orange, the user
//     should immediately associate "AT-Field is paying attention here".
//
// The ramp itself is theme-owned -- defined per Eva color scheme in
// lib/theme.ts. getActiveRamp() looks up the active scheme's ramp by
// reading data-theme on <html>, which setTheme() stamps. This commit
// replaced the previous module-level RAMP_BRAND constant; the Nerv
// theme's ramp is byte-identical to RAMP_BRAND, so existing renders
// don't visually change unless the user picks a different theme.

import { getActiveRamp } from "./theme";

/**
 * Sample the active theme's ramp at t ∈ [0, 1] and return a CSS rgb() color.
 *
 * Anchor points (consistent across all themes by construction):
 *   t=0.00  → cool/recedes (theme's bg-adjacent tint)
 *   t=0.85  → "warning" anchor (theme's threshold-marker hue)
 *   t=1.00  → "danger" anchor (theme's over-threshold hue)
 *
 * The math here doesn't care which theme is active; it just walks the
 * 8-stop array. Switching themes via setTheme() makes every subsequent
 * call to rampColor() pick up the new ramp on the next React render.
 */
export function rampColor(t: number): string {
  const ramp = getActiveRamp();
  const clamped = Math.min(1, Math.max(0, t));
  const scaled = clamped * (ramp.length - 1);
  const lo = Math.floor(scaled);
  const hi = Math.min(ramp.length - 1, lo + 1);
  const frac = scaled - lo;
  const a = ramp[lo];
  const b = ramp[hi];
  const r = Math.round(a[0] + (b[0] - a[0]) * frac);
  const g = Math.round(a[1] + (b[1] - a[1]) * frac);
  const blue = Math.round(a[2] + (b[2] - a[2]) * frac);
  return `rgb(${r}, ${g}, ${blue})`;
}

/** Back-compat alias for the prior `plasmaColor` name -- keep callers
    working until we've finished sweeping all references. */
export const plasmaColor = rampColor;

/**
 * Map a single value to its ramp color, anchored at the threshold.
 *
 *   value = 0                    →  rampColor(0.0)   (cool / recessive)
 *   value = threshold * 0.5      →  rampColor(0.42)
 *   value = threshold            →  rampColor(0.85)  (BRAND orange)
 *   value = threshold * 1.30     →  rampColor(1.0)   (deep red)
 *
 * When no threshold is known we fall back to a "neutral informational"
 * tone (brand orange-ish) since we have no danger reference.
 */
export function rampForValue(value: number, threshold: number | null): string {
  return rampColor(rampT(value, threshold));
}

/** Back-compat alias. */
export const plasmaForValue = rampForValue;

/** The actual value→t function. Exposed for callers that want to drive
    the gradient stops (per-Y interpolation in chart components). */
export function rampT(value: number, threshold: number | null): number {
  if (threshold == null) return 0.71; // informational, brand-warm
  if (threshold <= 0) return 0.85;
  if (value <= 0) return 0.0;
  if (value >= threshold) {
    // Beyond the threshold: ramps from 0.85 (at threshold) to 1.0 (at
    // 1.3× threshold, i.e. 30 % over). Past that we clamp to 1.0.
    const u = (value - threshold) / (threshold * 0.30);
    return Math.min(1.0, 0.85 + u * 0.15);
  }
  // Below threshold: linear from 0 (at value=0) to 0.85 (at value=threshold).
  return (value / threshold) * 0.85;
}

// ─────────────────────────────────────────────────────────────────────
// Plasma gradient builder — for SVG <linearGradient> stops along a chart
// ─────────────────────────────────────────────────────────────────────

export interface GradientStop {
  /** "0%" through "100%". 0% is the TOP of the chart (highest value);
      100% is the bottom (lowest value). This matches SVG's default y1/y2
      convention where y1=0% is the top of the bounding box. */
  offset: string;
  color: string;
  /** SVG `stop-opacity` ∈ [0, 1]. Used to fade the bottom of each curve
      toward the background, encoding "intensity within this curve's
      current dynamic range" -- a flat-quiet curve is mostly translucent,
      a spike pops fully opaque at its tip. Independent of the
      threshold-anchored color encoding. */
  opacity: number;
}

/**
 * Build SVG gradient stops for the brand-ramp coloring of a line chart.
 *
 * Critically, color is mapped from value→t via {@link rampT}, NOT from
 * chart-Y position. That means a sparkline showing 24-26 % RAM (well
 * below the 85 % trigger) is mostly cool no matter how zoomed in. A
 * sparkline whose curve crosses the threshold gets brand orange RIGHT
 * at the crossing line. A spike well above threshold gets red at the
 * spike's tip even while the rest of the curve is cool.
 *
 * Conventions:
 *   - Top of chart (`offset: "0%"`) = highest value in visible range.
 *   - Bottom (`offset: "100%"`) = lowest value.
 *   - We sample N evenly along the chart-Y axis and compute color from
 *     the corresponding value.
 */
/** Opacity at the bottom of each curve's visible range (the "trough" or
    "quiet" end). The top of the range is always 1.0.
    
    Tuned empirically: 0.30 is the lowest we can go while still keeping
    the line clearly readable against the #1b141f background; below that
    the trough end starts disappearing into the bg. Linear interpolation
    looked too subtle (50%→100% on a thin line just reads as "the line
    is there, slightly dimmer"), so we use a quadratic curve via
    OPACITY_GAMMA: the TOP half of each curve stays near-opaque (peaks
    pop crisply), but the bottom 60% fades aggressively so it visibly
    recedes. */
const TROUGH_OPACITY = 0.30;
const OPACITY_GAMMA = 1.8;

export function rampGradientStops(
  lo: number,
  hi: number,
  threshold: number | null,
): GradientStop[] {
  if (hi - lo < 1e-6) {
    // No dynamic range -> no intensity signal -> use the upper opacity
    // bound so a perfectly-flat curve still reads cleanly.
    const c = rampForValue((lo + hi) / 2, threshold);
    return [
      { offset: "0%", color: c, opacity: 1 },
      { offset: "100%", color: c, opacity: 1 },
    ];
  }
  const N = 24;
  const stops: GradientStop[] = [];
  for (let i = 0; i <= N; i++) {
    const t = i / N;
    const offsetPct = t * 100;
    // SVG gradient: i=0 is the top (highest value in visible range), i=N
    // is the bottom (lowest). We map BOTH the color (via threshold-anchored
    // ramp) AND the opacity (via this-curve dynamic-range non-linear ramp).
    const value = hi - (hi - lo) * t;
    // Quadratic-ish fade: at t=0 (top) opacity=1, at t=1 (bottom)
    // opacity=TROUGH_OPACITY, with most of the fade happening in the
    // lower 60 % of the chart. A pure-linear ramp made the effect feel
    // invisible on curves whose data spends most time near one end.
    const fade = Math.pow(t, OPACITY_GAMMA);
    stops.push({
      offset: `${offsetPct.toFixed(1)}%`,
      color: rampColor(rampT(value, threshold)),
      opacity: 1.0 - (1.0 - TROUGH_OPACITY) * fade,
    });
  }
  return stops;
}

/** Back-compat alias. */
export const plasmaGradientStops = rampGradientStops;

// ─────────────────────────────────────────────────────────────────────
// Rule humanizer
// ─────────────────────────────────────────────────────────────────────

const ACTION_LABEL: Record<EffectiveRuleView["action"], string> = {
  log: "log only",
  throttle: "throttle",
  kill: "kill the offending process tree",
};

/**
 * Render a rule's gist as a single human sentence.
 *
 * Example:
 *   "GPU 0 Core temp ≥ 83°C for 20 of last 30s → kill the offending
 *    process tree"
 */
export function humanizeRule(r: EffectiveRuleView): string {
  const sig = signalDisplayName(r.signal);
  const thr = formatThresholdValue(r.signal, r.threshold);
  const sustain = Math.round(r.window_s * r.min_fraction_over);
  return `${sig} ≥ ${thr} for ${sustain}s of last ${r.window_s}s → ${ACTION_LABEL[r.action]}`;
}

/**
 * One-liner you can put under the rule title showing what it's CURRENTLY
 * seeing relative to its threshold.
 */
export function humanizeRuleStatus(r: EffectiveRuleView): string {
  if (r.verdict === "INSUFFICIENT") {
    return `not enough samples yet (need ${r.min_samples})`;
  }
  if (r.verdict === "TRIGGER") {
    return `triggering: ${(r.fraction_over * 100).toFixed(0)}% of window over threshold`;
  }
  if (r.fraction_over === 0) {
    return "fully below threshold";
  }
  return `${(r.fraction_over * 100).toFixed(0)}% of window over threshold (need ${(r.min_fraction_over * 100).toFixed(0)}%)`;
}

function formatThresholdValue(signal: string, threshold: number): string {
  // Infer unit from signal name suffix; the API doesn't carry it on rules.
  if (/_c$/.test(signal)) return `${threshold.toFixed(0)}°C`;
  if (/_percent$/.test(signal)) return `${threshold.toFixed(0)}%`;
  if (/_w$/.test(signal)) return `${threshold.toFixed(0)} W`;
  if (/_bytes$/.test(signal)) return formatBytes(threshold);
  return threshold.toString();
}

/**
 * Classify a threshold against tier bands. Mirrors the server's
 * `rule_profiles.classify` so the slider can show its tooltip without
 * waiting for the PATCH round-trip.
 *
 * - threshold ≤ aggressive_max → "aggressive" (kill earlier; protective)
 * - threshold ≥ relaxed_min    → "relaxed"    (more headroom; permissive)
 * - otherwise                  → "normal"
 */
export type ProfileTier = "aggressive" | "normal" | "relaxed";

export function classifyTier(
  threshold: number,
  bands: { aggressive_max: number; relaxed_min: number },
): ProfileTier {
  if (threshold <= bands.aggressive_max) return "aggressive";
  if (threshold >= bands.relaxed_min) return "relaxed";
  return "normal";
}

const TIER_COLOR: Record<ProfileTier, string> = {
  aggressive: "var(--color-accent)",
  normal: "var(--color-text-secondary)",
  relaxed: "var(--color-text-tertiary)",
};

export function tierColor(tier: ProfileTier): string {
  return TIER_COLOR[tier];
}

/**
 * One-line plain-English description of what a tier means. Used for the
 * slider tooltip and the profile-preset button hover hints.
 */
export function tierDescription(tier: ProfileTier): string {
  switch (tier) {
    case "aggressive":
      return "fires early; protective at the cost of false positives";
    case "normal":
      return "balanced default; matches PLANNING.md §3 conservative profile";
    case "relaxed":
      return "extra headroom; only fires on clear hardware distress";
  }
}
