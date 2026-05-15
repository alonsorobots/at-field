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
    "ConfigWriteError",
    "materialize_default_config",
    "update_rule_threshold",
]


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

    Workflow:

    1. If ``config_path`` doesn't exist, materialize a default config first
       so the user gets a hand-editable file they can keep tweaking.
    2. Locate the ``[[rules]]`` block whose ``name = "<rule_name>"`` matches.
    3. Replace the first ``threshold = X`` line within that block.
    4. Write to a tempfile in the same directory + atomic rename
       (``os.replace`` is atomic on Windows for same-volume targets).

    Raises ``ConfigWriteError`` if the rule isn't found, the threshold
    line is missing, or the file can't be written.
    """
    config_path = Path(config_path)
    if not config_path.exists():
        materialize_default_config(config_path)

    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigWriteError(f"cannot read {config_path}: {exc}") from exc

    new_text = _replace_rule_threshold(text, rule_name, new_threshold)
    if new_text is None:
        raise ConfigWriteError(
            f"rule {rule_name!r} not found in {config_path} "
            f"(or its [[rules]] block has no `threshold = ...` line)"
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


def _replace_rule_threshold(text: str, rule_name: str, new_threshold: float) -> str | None:
    """Return ``text`` with the matching rule's threshold updated, or None
    if the target rule + its threshold line couldn't be located."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    in_rule_block = False
    in_target = False
    found_threshold = False
    pending_block_start = -1  # so we can scan backwards for `name` if it
                              # comes AFTER `threshold` (rare but legal)

    for line in lines:
        stripped = line.strip()

        # New `[[rules]]` header -- close any prior block and start fresh.
        if stripped.startswith("[[rules]]"):
            in_rule_block = True
            in_target = False
            pending_block_start = len(out)
            out.append(line)
            continue
        # Any other section header closes the current `[[rules]]` block.
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
                continue
            if in_target and not found_threshold:
                m_th = _THRESHOLD_LINE.match(line)
                if m_th:
                    rendered = _render_float(new_threshold)
                    rebuilt = (
                        f"{m_th.group('prefix')}{rendered}{m_th.group('trailing')}"
                    )
                    # Preserve trailing newline if the original had one.
                    if line.endswith("\r\n"):
                        rebuilt += "\r\n"
                    elif line.endswith("\n"):
                        rebuilt += "\n"
                    out.append(rebuilt)
                    found_threshold = True
                    continue

        out.append(line)

    if not found_threshold:
        # Couldn't find the rule (or the rule had no threshold line we
        # could match). Caller turns this into a 4xx.
        return None
    _ = pending_block_start  # silence linter; reserved for future name-after-threshold
    return "".join(out)


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
