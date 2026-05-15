"""Prometheus exposition-format renderer for AT-Field signals + rules.

Why this exists
---------------
Power users running their AI rigs in a homelab often already have a
Prometheus + Grafana stack scraping every other piece of hardware. AT-Field
sits on the most performance-critical signals on the box -- VRAM junction
temps, GPU usage, RAM pressure -- and exposing those in Prometheus format
costs us essentially nothing while letting them light up exactly the
dashboards they already trust.

We hand-roll the text format here rather than pulling in
``prometheus_client``. The exposition format is six lines of spec
(https://prometheus.io/docs/instrumenting/exposition_formats/) and the
official client library would add ~500 KB to our PyInstaller bundle for
features we don't use (push gateway, multi-process mode, custom
collectors). A 50-line renderer is the right call.

What we expose
--------------
* ``atfield_signal{signal="<name>",unit="<unit>",source="<src>"}`` —
  the latest value of every collector signal currently in scope.
* ``atfield_signal_age_seconds{signal="<name>"}`` — wall-clock age of
  that sample so a Grafana dashboard can grey out stale lines.
* ``atfield_rule_threshold{rule="<name>"}`` — what the rule is configured
  to fire at right now (changes when the user moves a slider).
* ``atfield_rule_fraction_over{rule="<name>"}`` — fraction of the
  evaluation window currently above threshold (0.0-1.0).
* ``atfield_rule_triggers_total{rule="<name>"}`` — counter, increments
  on every TRIGGER verdict the engine has issued since startup.
* ``atfield_rule_cooldown_remaining_seconds{rule="<name>"}``.
* ``atfield_uptime_seconds`` — service uptime gauge.
* ``atfield_paused`` — 1 if the watchdog is in pause mode, else 0.

Output is plain ``text/plain; version=0.0.4`` so curl or a browser can
read it as-is. The HTTP layer (:mod:`atfield.http_api`) calls
:func:`render_metrics` from a `/metrics` route.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

__all__ = ["PROMETHEUS_CONTENT_TYPE", "render_metrics"]


# Per Prometheus exposition spec, this exact value MUST be the
# Content-Type so scrapers parse the body as text format v0.0.4.
PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def render_metrics(
    *,
    health: dict[str, Any],
    signals: dict[str, Any],
    rules: dict[str, Any],
) -> bytes:
    """Render the full /metrics body from API snapshots.

    Inputs are exactly the dicts that ``ServiceState.snapshot_*`` produce,
    so the HTTP layer can build a metrics response without owning Prometheus
    formatting concerns. The function is pure and stateless -- safe to
    call from any thread that already has a coherent snapshot.

    Returns UTF-8 bytes, the body the HTTP layer wfile.write()s.
    """
    lines: list[str] = []
    now = _now_from_health(health)

    # ─── Service-level gauges ────────────────────────────────────────────
    _emit_help(lines, "atfield_uptime_seconds", "How long the watchdog has been running.")
    _emit_type(lines, "atfield_uptime_seconds", "gauge")
    lines.append(f"atfield_uptime_seconds {_fmt_float(health.get('uptime_s', 0.0))}")

    _emit_help(lines, "atfield_paused", "1 if the watchdog is currently paused, else 0.")
    _emit_type(lines, "atfield_paused", "gauge")
    lines.append(f"atfield_paused {1 if health.get('paused') else 0}")

    _emit_help(lines, "atfield_collector_healthy",
               "1 per collector if probe was OK and current health is HEALTHY, else 0.")
    _emit_type(lines, "atfield_collector_healthy", "gauge")
    for c in health.get("collectors", []):
        name = _q(c.get("name", "?"))
        ok = 1 if c.get("available") and c.get("health") == "HEALTHY" else 0
        lines.append(f'atfield_collector_healthy{{collector="{name}"}} {ok}')

    # ─── Live signals ────────────────────────────────────────────────────
    latest: dict[str, Any] = signals.get("latest", {})
    _emit_help(lines, "atfield_signal", "Latest value reported for this signal.")
    _emit_type(lines, "atfield_signal", "gauge")
    for sig_name, payload in sorted(latest.items()):
        if not isinstance(payload, dict):
            continue
        value = payload.get("value")
        if not isinstance(value, (int, float)):
            continue
        labels = _signal_labels(sig_name, payload)
        lines.append(f"atfield_signal{{{labels}}} {_fmt_float(float(value))}")

    _emit_help(lines, "atfield_signal_age_seconds",
               "Wall-clock age of the latest sample for this signal.")
    _emit_type(lines, "atfield_signal_age_seconds", "gauge")
    for sig_name, payload in sorted(latest.items()):
        if not isinstance(payload, dict):
            continue
        ts = payload.get("ts")
        if not isinstance(ts, (int, float)):
            continue
        age = max(0.0, now - float(ts)) if now > 0 else 0.0
        lines.append(
            f'atfield_signal_age_seconds{{signal="{_q(sig_name)}"}} {_fmt_float(age)}'
        )

    # ─── Rules ──────────────────────────────────────────────────────────
    effective: list[dict[str, Any]] = rules.get("effective", []) or []
    if effective:
        _emit_help(lines, "atfield_rule_threshold",
                   "Configured threshold the rule fires at.")
        _emit_type(lines, "atfield_rule_threshold", "gauge")
        for r in effective:
            lines.append(_rule_metric("atfield_rule_threshold", r, "threshold"))

        _emit_help(lines, "atfield_rule_fraction_over",
                   "Fraction of the evaluation window currently above threshold (0..1).")
        _emit_type(lines, "atfield_rule_fraction_over", "gauge")
        for r in effective:
            lines.append(_rule_metric("atfield_rule_fraction_over", r, "fraction_over"))

        _emit_help(lines, "atfield_rule_triggers_total",
                   "Cumulative count of TRIGGER verdicts since service start.")
        _emit_type(lines, "atfield_rule_triggers_total", "counter")
        for r in effective:
            lines.append(_rule_metric("atfield_rule_triggers_total", r, "triggers"))

        _emit_help(lines, "atfield_rule_cooldown_remaining_seconds",
                   "Seconds remaining before this rule can fire again.")
        _emit_type(lines, "atfield_rule_cooldown_remaining_seconds", "gauge")
        for r in effective:
            lines.append(
                _rule_metric(
                    "atfield_rule_cooldown_remaining_seconds", r, "cooldown_remaining_s",
                )
            )

    # Trailing newline -- per the spec, scraping is line-based and a missing
    # final EOL is technically a malformed exposition.
    body = "\n".join(lines) + "\n"
    return body.encode("utf-8")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _now_from_health(health: dict[str, Any]) -> float:
    """Recover an effective `now` so we can compute signal age without
    the caller passing a separate timestamp. We use ``last_tick_at``
    (which the service updates each loop) plus heartbeat_age, falling
    back to 0 if neither is set yet."""
    last = health.get("last_tick_at")
    age = health.get("heartbeat_age_s")
    if isinstance(last, (int, float)) and isinstance(age, (int, float)):
        return float(last) + float(age)
    if isinstance(last, (int, float)):
        return float(last)
    return 0.0


def _signal_labels(name: str, payload: dict[str, Any]) -> str:
    parts = [f'signal="{_q(name)}"']
    unit = payload.get("unit")
    if isinstance(unit, str) and unit:
        parts.append(f'unit="{_q(unit)}"')
    src = payload.get("source")
    if isinstance(src, str) and src:
        parts.append(f'source="{_q(src)}"')
    return ",".join(parts)


def _rule_metric(metric_name: str, rule: dict[str, Any], field: str) -> str:
    name = rule.get("name", "?")
    base = rule.get("base_rule", "?")
    sig = rule.get("signal", "?")
    val = rule.get(field)
    if not isinstance(val, (int, float)):
        # Skip emitting partial/malformed lines; Prometheus chokes on them.
        return ""
    labels = (
        f'rule="{_q(str(name))}",'
        f'base_rule="{_q(str(base))}",'
        f'signal="{_q(str(sig))}"'
    )
    return f"{metric_name}{{{labels}}} {_fmt_float(float(val))}"


def _emit_help(lines: list[str], name: str, text: str) -> None:
    """Emit a HELP comment. Multi-line HELP isn't allowed; we condense
    to a single line so any long descriptions get truncated by Prometheus."""
    text = text.replace("\n", " ").strip()
    lines.append(f"# HELP {name} {text}")


def _emit_type(lines: list[str], name: str, ttype: str) -> None:
    if ttype not in ("counter", "gauge", "histogram", "summary", "untyped"):
        ttype = "untyped"
    lines.append(f"# TYPE {name} {ttype}")


def _q(value: str) -> str:
    """Escape a label value per Prometheus exposition rules: backslash,
    quote, and newline. We never embed newlines or quotes, but signal
    sources can contain backslashes (e.g. Windows paths in metadata)."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt_float(v: float) -> str:
    """Render a float in a form Prometheus parses safely. Integers come out
    as integers, floats with a fixed precision that's more than enough
    for sensor readings without dragging in repr's gnarly tail."""
    if v == int(v):
        return f"{int(v)}"
    return f"{v:.6g}"


# Touchable for tests that want to peek at how a single rule line renders.
def _rule_lines(rules: Iterable[dict[str, Any]], metric: str, field: str) -> list[str]:
    out: list[str] = []
    for r in rules:
        line = _rule_metric(metric, r, field)
        if line:
            out.append(line)
    return out
