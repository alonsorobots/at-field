"""Atomic, comment-preserving updates to ``config.toml``.

The dashboard slider sends ``PATCH /rules/<name>`` with a new threshold;
this module persists the change to the on-disk config without disturbing
anything else (other rules, comments, formatting, the user's hand-tuned
``[targeting]`` lists, etc.).

Why we hand-roll a tiny TOML mutator instead of pulling tomlkit:
  * The mutation surface is small (one numeric value inside a known
    `[[rules]]` block).
  * Adding a TOML round-trip dep just to flip a number is a poor
    cost/benefit -- the ~50 KB of tomlkit would dominate our cold-start.
  * Regex-against-known-structure stays robust because the producer
    (``materialize_default_config``) controls layout when the config
    doesn't already exist; user-edited files keep working because the
    line we match (``threshold = <number>``) is documented as the only
    thing the slider touches.

Failure-mode contract: every public function raises ``ConfigWriteError``
on any I/O or shape problem. The HTTP layer turns that into a 4xx/5xx
without modifying the file.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Iterable
from pathlib import Path

from atfield.config import (
    AtFieldConfig,
    ConfigError,
    default_config,
    load_config,
)

__all__ = [
    "MUTABLE_RULE_FIELDS",
    "ConfigWriteError",
    "materialize_default_config",
    "update_rule_field",
    "update_rule_threshold",
]

# Whitelist of rule fields the dashboard is allowed to mutate. Each maps to
# (a Python type for validation, a render function that produces the TOML
# literal). The API layer cross-checks PATCH payloads against this map so
# we don't accidentally let the UI rewrite something that requires a
# service restart (e.g. the `name` field).
MUTABLE_RULE_FIELDS: dict[str, type] = {
    "threshold": float,
    "window_s": int,
    "cooldown_s": int,
    "action": str,
    "min_fraction_over": float,
}



class ConfigWriteError(RuntimeError):
    """Raised on any failure to atomically rewrite the config file."""


# Matches `threshold = 85.0` (with arbitrary float, optional surrounding
# whitespace, and an optional trailing comment). We capture the prefix and
# trailing parts so we can preserve formatting + comments.
_THRESHOLD_LINE = re.compile(
    r"^(?P<prefix>\s*threshold\s*=\s*)"
    r"(?P<value>-?\d+(?:\.\d+)?)"
    r"(?P<trailing>.*?)$"
)
# Matches `name = "rule-name"` (single or double quoted).
_NAME_LINE = re.compile(r'^\s*name\s*=\s*["\'](?P<name>[^"\']+)["\']')


def update_rule_threshold(
    config_path: Path,
    rule_name: str,
    new_threshold: float,
) -> None:
    """Atomically set ``rule_name``'s threshold to ``new_threshold``.

    Thin wrapper over :func:`update_rule_field` kept for back-compat with
    the dashboard's PHASE 1 patch endpoint and external callers (e.g.
    tests). Equivalent to ``update_rule_field(path, name, "threshold", v)``.
    """
    update_rule_field(config_path, rule_name, "threshold", new_threshold)


def update_rule_field(
    config_path: Path,
    rule_name: str,
    field: str,
    new_value: object,
) -> None:
    """Atomically set ``rule_name``'s ``field`` to ``new_value``.

    Workflow:

    1. Reject any field not in :data:`MUTABLE_RULE_FIELDS`.
    2. If ``config_path`` doesn't exist, materialize a default config first
       so the user gets a hand-editable file they can keep tweaking.
    3. Locate the ``[[rules]]`` block whose ``name = "<rule_name>"`` matches.
    4. Replace the first ``<field> = X`` line within that block. If the
       line doesn't exist (e.g. ``cooldown_s`` is omitted from the
       starter config when None), inject one immediately after the rule's
       ``action = "..."`` line so the file stays well-formed.
    5. Write atomically.

    Raises ``ConfigWriteError`` on validation, lookup, or I/O failures.
    """
    if field not in MUTABLE_RULE_FIELDS:
        raise ConfigWriteError(
            f"field {field!r} is not in the set of dashboard-tunable rule "
            f"fields ({sorted(MUTABLE_RULE_FIELDS)})"
        )
    expected_type = MUTABLE_RULE_FIELDS[field]
    if not isinstance(new_value, expected_type) and not (
        expected_type is float and isinstance(new_value, int)
    ):
        raise ConfigWriteError(
            f"value for {field!r} must be {expected_type.__name__}, "
            f"got {type(new_value).__name__}"
        )

    config_path = Path(config_path)
    if not config_path.exists():
        materialize_default_config(config_path)

    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigWriteError(f"cannot read {config_path}: {exc}") from exc

    rendered = _render_field_value(field, new_value)
    new_text = _replace_or_inject_rule_field(text, rule_name, field, rendered)
    if new_text is None:
        raise ConfigWriteError(
            f"rule {rule_name!r} not found in {config_path}"
        )

    _atomic_write(config_path, new_text)


def materialize_default_config(config_path: Path) -> None:
    """Write the locked-in default config to ``config_path``.

    Used when the slider patches a rule on a service that's running off
    in-memory defaults (no on-disk config). The file written here is
    intentionally simple and self-documenting; it's a starting point for
    further hand-editing.
    """
    config_path = Path(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    cfg = default_config()
    _atomic_write(config_path, _render_default_config(cfg))


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _replace_or_inject_rule_field(
    text: str,
    rule_name: str,
    field: str,
    rendered_value: str,
) -> str | None:
    """Return ``text`` with the matching rule's ``field`` set to
    ``rendered_value`` (already a TOML literal). Returns None if the rule
    block can't be found.

    If the rule block contains a matching ``field = ...`` line, we
    rewrite it in place (preserving any trailing comment + newline style).
    If the line doesn't exist (common for optional fields like
    ``cooldown_s`` that the default renderer omits when None), we inject
    a new line at the end of the rule block.
    """
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    in_rule_block = False
    in_target = False
    found_field = False
    target_block_end_idx = -1  # index in `out` of last appended line of the
                               # target block, for end-of-block injection.

    field_line_re = re.compile(
        rf"^(?P<prefix>\s*{re.escape(field)}\s*=\s*)"
        rf"(?P<value>(?:'[^']*'|\"[^\"]*\"|-?\d+(?:\.\d+)?|true|false))"
        rf"(?P<trailing>.*?)$"
    )

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("[[rules]]"):
            # Closing the previous target block: if we never saw the field
            # line, we'll need to inject. We do that AFTER walking the
            # whole text so we don't disturb iteration here.
            in_rule_block = True
            in_target = False
            out.append(line)
            continue
        if stripped.startswith("[") and not stripped.startswith("[["):
            in_rule_block = False
            in_target = False
            out.append(line)
            continue

        if in_rule_block:
            m_name = _NAME_LINE.match(line)
            if m_name and m_name.group("name") == rule_name:
                in_target = True
                out.append(line)
                target_block_end_idx = len(out) - 1
                continue
            if in_target:
                # Track the last non-blank line of the target block so we
                # know where to inject if needed.
                if stripped:
                    target_block_end_idx = len(out)
                if not found_field:
                    m_field = field_line_re.match(line)
                    if m_field:
                        rebuilt = (
                            f"{m_field.group('prefix')}{rendered_value}"
                            f"{m_field.group('trailing')}"
                        )
                        if line.endswith("\r\n"):
                            rebuilt += "\r\n"
                        elif line.endswith("\n"):
                            rebuilt += "\n"
                        out.append(rebuilt)
                        found_field = True
                        target_block_end_idx = len(out) - 1
                        continue

        out.append(line)

    if target_block_end_idx == -1:
        return None  # rule wasn't found

    if not found_field:
        # Inject a new field line right after the last non-blank line of
        # the target block. We choose the existing line ending convention
        # so we don't mix CRLF and LF.
        anchor = out[target_block_end_idx]
        eol = "\r\n" if anchor.endswith("\r\n") else "\n"
        injected = f"{field} = {rendered_value}{eol}"
        out.insert(target_block_end_idx + 1, injected)

    return "".join(out)


def _render_field_value(field: str, value: object) -> str:
    """Render ``value`` in the TOML literal form appropriate for ``field``.

    Strings are quoted; floats keep at least one decimal; ints stay int.
    Action gets a tighter validation: only the actuator-supported kinds
    survive (kill / throttle / log).
    """
    if field in ("threshold", "min_fraction_over"):
        return _render_float(float(value))  # type: ignore[arg-type]
    if field in ("window_s", "cooldown_s"):
        return str(int(value))  # type: ignore[arg-type]
    if field == "action":
        if not isinstance(value, str):
            raise ConfigWriteError("action must be a string")
        kind = value.strip()
        if kind not in {"kill", "throttle", "log"}:
            raise ConfigWriteError(
                f"action must be one of kill / throttle / log, got {kind!r}"
            )
        return f'"{kind}"'
    raise ConfigWriteError(f"unrenderable field {field!r}")


def _render_float(v: float) -> str:
    """Render a threshold for TOML.

    We keep one decimal place even for round numbers (`85` -> `85.0`)
    because the schema's `_as_number` parser tolerates both, and consistent
    formatting reads better in a hand-edited file. Also avoids losing
    precision on legitimate fractional thresholds.
    """
    if float(v).is_integer():
        return f"{int(v)}.0"
    # Round to 2 decimals to keep config diffs tidy.
    return f"{round(v, 2)}"


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically.

    On Windows, ``os.replace`` is atomic for same-volume same-directory
    targets. We write to a sibling tempfile and replace, avoiding any
    window where a reader could observe an empty / half-written file.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except (OSError, AttributeError):
                # fsync may be unavailable on some Windows filesystems;
                # the os.replace below still publishes a complete file.
                pass
        os.replace(tmp_path, path)
        tmp_path = None  # mark as moved
    except OSError as exc:
        raise ConfigWriteError(f"cannot write {path}: {exc}") from exc
    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _render_default_config(cfg: AtFieldConfig) -> str:
    """Render a fresh `config.toml` from the in-memory default config.

    Conservative output: section order matches the dataclass declaration,
    each rule lives in its own `[[rules]]` block, and every line we'd
    care to mutate later (the `threshold = ...` in particular) sits on
    its own line so the mutator's regex can find it.
    """
    lines: list[str] = [
        "# AT-Field configuration. See docs/configuration.md for the full schema.",
        "# This file was auto-generated from defaults; edit freely.",
        "",
        "[general]",
        f"tick_hz = {cfg.general.tick_hz}",
        f'log_level = "{cfg.general.log_level}"',
        "",
        "[targeting]",
        f"killable_names = {_render_str_list(cfg.targeting.killable_names)}",
        f"launcher_names = {_render_str_list(cfg.targeting.launcher_names)}",
        f"never_kill_names = {_render_str_list(cfg.targeting.never_kill_names)}",
        "",
        "[kill]",
        f'mode = "{cfg.kill.mode}"',
        f"grace_seconds = {cfg.kill.grace_seconds}",
        f"post_kill_cooldown_seconds = {cfg.kill.post_kill_cooldown_seconds}",
        "",
        "[api]",
        f"enabled = {str(cfg.api.enabled).lower()}",
        f'bind = "{cfg.api.bind}"',
        f"port = {cfg.api.port}",
        "",
    ]
    for r in cfg.rules:
        lines.extend([
            "[[rules]]",
            f'name = "{r.name}"',
            f'signal = "{r.signal}"',
            f"threshold = {_render_float(r.threshold)}",
            f"window_s = {r.window_s}",
            f"min_fraction_over = {r.min_fraction_over}",
            f'action = "{r.action}"',
        ])
        if r.cooldown_s is not None:
            lines.append(f"cooldown_s = {r.cooldown_s}")
        lines.append("")
    return "\n".join(lines)


def _render_str_list(items: Iterable[str]) -> str:
    inner = ", ".join(f'"{x}"' for x in items)
    return f"[{inner}]"


# Round-trip self-check used by tests: load the file we just wrote and
# verify it parses to a valid config. Public so tests can import.
def verify_roundtrip(path: Path) -> AtFieldConfig:
    """Load ``path`` via the canonical loader; raises ``ConfigError`` on
    any parse / validation failure. Useful as a defensive smoke test
    after a write."""
    try:
        return load_config(path)
    except ConfigError:
        raise
