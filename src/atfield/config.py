"""AT-Field configuration: TOML schema, conservative defaults, validation.

This module owns the on-disk config format (`config.toml`) and turns it into a
frozen, fully-validated :class:`AtFieldConfig` tree that the rest of the
service can rely on without further defensive checks.

Design notes
------------
* **No third-party validation library.** The schema is small and fully
  describable with stdlib + ``dataclasses``. Keeping pydantic out of the
  service hot path keeps the dependency surface (and cold-start time on a
  Windows Service) minimal.
* **Strict parsing, soft fallback.** This module *raises* on malformed input;
  the service layer (see :pyfile:`PLANNING.md` §5.4) is responsible for
  catching :class:`ConfigError` and switching to observe-only mode. Splitting
  those responsibilities keeps config logic decoupled from runtime policy.
* **Signal globs are not expanded here.** A signal like ``gpu.*.core_temp_c``
  is validated for shape only. Per-GPU expansion happens in
  ``atfield.policy`` once the NVML collector has enumerated devices.
* **Defaults match the "Conservative" profile** locked in by PLANNING.md §3
  and elaborated in §8. Editing those defaults is a planning-level decision,
  not an implementation one — do not silently widen them.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final, Literal

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.10 only
    import tomli as tomllib  # type: ignore[no-redef]

__all__ = [
    "DEFAULT_CONFIG_FILENAME",
    "ApiConfig",
    "AtFieldConfig",
    "ConfigError",
    "GeneralConfig",
    "KillConfig",
    "RuleConfig",
    "TargetingConfig",
    "default_config",
    "default_state_dir",
    "load_config",
    "load_config_from_dict",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ConfigError(ValueError):
    """Raised on any malformed or out-of-range config input.

    The service entrypoint catches this and falls back to observe-only mode
    (PLANNING.md §5.4); other callers (CLI, tests) should let it propagate.
    """


# ---------------------------------------------------------------------------
# Constants & defaults (locked in by PLANNING.md §3 / §8)
# ---------------------------------------------------------------------------


DEFAULT_CONFIG_FILENAME: Final = "config.toml"

# Killable / launcher / never-kill defaults are duplicated verbatim from
# PLANNING.md §8 so this file is self-contained for documentation purposes.
_DEFAULT_KILLABLE_NAMES: Final[tuple[str, ...]] = (
    "python.exe",
    "pythonw.exe",
    "python3.exe",
)
_DEFAULT_LAUNCHER_NAMES: Final[tuple[str, ...]] = (
    "torchrun",
    "accelerate",
    "deepspeed",
    "mpiexec",
    "ray",
    "ray-worker",
    "jupyter",
    "ipykernel_launcher",
)
_DEFAULT_NEVER_KILL_NAMES: Final[tuple[str, ...]] = (
    "explorer.exe",
    "services.exe",
    "code.exe",
    "windbg.exe",
    "atfield-service.exe",
    "atf.exe",
)

_VALID_LOG_LEVELS: Final = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_VALID_KILL_MODES: Final = frozenset({"graceful", "aggressive"})
_VALID_ACTIONS: Final = frozenset({"log", "throttle", "kill"})

# Signal grammar:
#   <scope>.<metric>                     e.g. system.ram_used_percent
#   <scope>.<id>.<metric>                e.g. gpu.0.core_temp_c, gpu.*.core_temp_c
# scope/metric are lower-snake; id is digits or "*".
_SIGNAL_PATTERN: Final = re.compile(
    r"^[a-z][a-z0-9_]*"          # scope
    r"(?:\.(?:\*|\d+))?"          # optional id (digits or *)
    r"(?:\.[a-z][a-z0-9_]*)+$"    # one or more dotted metric segments
)


def default_state_dir() -> Path:
    """Return ``%PROGRAMDATA%\\ATField`` (Windows) with a sane cross-platform fallback.

    The fallback exists so unit tests on non-Windows CI can construct a
    default config without env shenanigans; the production target is always
    Windows (see PLANNING.md §3).
    """
    program_data = os.environ.get("PROGRAMDATA")
    if program_data:
        return Path(program_data) / "ATField"
    return Path(r"C:\ProgramData\ATField")


# ---------------------------------------------------------------------------
# Schema dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GeneralConfig:
    tick_hz: int = 1
    log_level: str = "INFO"
    state_dir: Path = field(default_factory=default_state_dir)


@dataclass(frozen=True, slots=True)
class TargetingConfig:
    killable_names: tuple[str, ...] = _DEFAULT_KILLABLE_NAMES
    launcher_names: tuple[str, ...] = _DEFAULT_LAUNCHER_NAMES
    never_kill_names: tuple[str, ...] = _DEFAULT_NEVER_KILL_NAMES


@dataclass(frozen=True, slots=True)
class KillConfig:
    mode: Literal["graceful", "aggressive"] = "graceful"
    grace_seconds: int = 5
    post_kill_cooldown_seconds: int = 60
    # Default duration (seconds) for the ``throttle`` action: the
    # actuator suspends the offending process tree for this long, then
    # resumes it. Long enough that a thermal spike has time to dissipate
    # (~15-30s), short enough that the workload doesn't notice it as
    # anything but a brief stall. Per-rule overrides can be added later.
    throttle_duration_seconds: int = 30


@dataclass(frozen=True, slots=True)
class ApiConfig:
    """Localhost HTTP API the tray app reads.

    Defaults bind to ``127.0.0.1`` only; remote access requires explicit
    operator opt-in. Flipping ``enabled = false`` disables the listener
    entirely (CLI-only deployments).
    """

    enabled: bool = True
    bind: str = "127.0.0.1"
    port: int = 8765


@dataclass(frozen=True, slots=True)
class RuleConfig:
    name: str
    signal: str
    threshold: float
    window_s: int
    min_fraction_over: float
    action: Literal["log", "throttle", "kill"]
    # Per-rule override; resolved against KillConfig.post_kill_cooldown_seconds
    # via :meth:`AtFieldConfig.cooldown_for`.
    cooldown_s: int | None = None


@dataclass(frozen=True, slots=True)
class AtFieldConfig:
    general: GeneralConfig
    targeting: TargetingConfig
    kill: KillConfig
    api: ApiConfig
    rules: tuple[RuleConfig, ...]

    def cooldown_for(self, rule: RuleConfig) -> int:
        """Effective post-action cooldown for ``rule`` in seconds."""
        return rule.cooldown_s if rule.cooldown_s is not None else self.kill.post_kill_cooldown_seconds


# ---------------------------------------------------------------------------
# Conservative-profile defaults (PLANNING.md §3 & §8)
# ---------------------------------------------------------------------------


def _default_rules() -> tuple[RuleConfig, ...]:
    return (
        RuleConfig(
            name="vram-junction-hot",
            signal="gpu.*.mem_junction_temp_c",
            threshold=90.0,
            window_s=20,
            min_fraction_over=0.67,
            action="kill",
        ),
        RuleConfig(
            name="gpu-core-hot",
            signal="gpu.*.core_temp_c",
            threshold=83.0,
            window_s=30,
            min_fraction_over=0.67,
            action="kill",
        ),
        RuleConfig(
            name="ram-pressure",
            signal="system.ram_used_percent",
            threshold=85.0,
            window_s=60,
            min_fraction_over=0.75,
            action="kill",
        ),
        RuleConfig(
            name="pagefile-pressure",
            signal="system.commit_percent",
            threshold=90.0,
            window_s=60,
            min_fraction_over=0.75,
            action="kill",
        ),
        RuleConfig(
            name="cpu-pkg-hot",
            signal="system.cpu_package_temp_c",
            threshold=90.0,
            window_s=30,
            min_fraction_over=0.67,
            action="kill",
        ),
    )


def default_config() -> AtFieldConfig:
    """Return the locked-in Conservative profile from PLANNING.md §3 / §8."""
    return AtFieldConfig(
        general=GeneralConfig(),
        targeting=TargetingConfig(),
        kill=KillConfig(),
        api=ApiConfig(),
        rules=_default_rules(),
    )


# ---------------------------------------------------------------------------
# Loading & validation
# ---------------------------------------------------------------------------


def load_config(path: str | os.PathLike[str] | None) -> AtFieldConfig:
    """Load and validate config from a TOML file.

    Behavior
    --------
    * ``path is None`` → return :func:`default_config`.
    * ``path`` does not exist → return :func:`default_config` (first-run case;
      the installer will write a starter file from ``scripts/config.example.toml``,
      but a missing file should not crash the service).
    * File exists → parse and validate; raises :class:`ConfigError` on any problem.
    """
    if path is None:
        return default_config()

    p = Path(path)
    if not p.exists():
        return default_config()

    try:
        with p.open("rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{p}: invalid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{p}: cannot read config: {exc}") from exc

    return load_config_from_dict(raw, source=str(p))


def load_config_from_dict(data: dict[str, Any], *, source: str = "<dict>") -> AtFieldConfig:
    """Validate a parsed TOML dict (or test fixture) into an :class:`AtFieldConfig`.

    Unknown top-level sections are rejected to catch typos like ``[targetting]``
    or ``[Rules]`` early — silent ignores would let a misconfigured production
    service drift from the operator's intent.
    """
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: expected a TOML table at the root")

    allowed_top = {"general", "targeting", "kill", "api", "rules"}
    unknown = set(data.keys()) - allowed_top
    if unknown:
        raise ConfigError(
            f"{source}: unknown top-level section(s): {sorted(unknown)}; "
            f"expected one of {sorted(allowed_top)}"
        )

    base = default_config()
    general = _parse_general(data.get("general"), base.general, source)
    targeting = _parse_targeting(data.get("targeting"), base.targeting, source)
    kill = _parse_kill(data.get("kill"), base.kill, source)
    api = _parse_api(data.get("api"), base.api, source)
    rules = _parse_rules(data.get("rules"), base.rules, source)

    cfg = AtFieldConfig(general=general, targeting=targeting, kill=kill, api=api, rules=rules)
    _cross_validate(cfg, source)
    return cfg


# ---------------------------------------------------------------------------
# Per-section parsers
# ---------------------------------------------------------------------------


def _require_table(value: Any, where: str, source: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{source}: [{where}] must be a TOML table, got {type(value).__name__}")
    return value


def _check_unknown_keys(table: dict[str, Any], allowed: set[str], where: str, source: str) -> None:
    unknown = set(table.keys()) - allowed
    if unknown:
        raise ConfigError(
            f"{source}: [{where}] has unknown key(s): {sorted(unknown)}; "
            f"expected subset of {sorted(allowed)}"
        )


def _as_int(value: Any, where: str, source: str, *, minimum: int | None = None) -> int:
    # Reject bools explicitly: bool is a subclass of int in Python and TOML
    # has a real boolean type; silently accepting it would mask config typos.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{source}: {where} must be an integer, got {type(value).__name__}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{source}: {where} must be >= {minimum}, got {value}")
    return value


def _as_number(value: Any, where: str, source: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{source}: {where} must be a number, got {type(value).__name__}")
    return float(value)


def _as_str(value: Any, where: str, source: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{source}: {where} must be a non-empty string")
    return value


def _as_str_list(value: Any, where: str, source: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
        raise ConfigError(f"{source}: {where} must be a list of non-empty strings")
    return tuple(value)


def _parse_general(raw: Any, base: GeneralConfig, source: str) -> GeneralConfig:
    if raw is None:
        return base
    table = _require_table(raw, "general", source)
    _check_unknown_keys(table, {"tick_hz", "log_level", "state_dir"}, "general", source)

    out = base
    if "tick_hz" in table:
        out = replace(out, tick_hz=_as_int(table["tick_hz"], "general.tick_hz", source, minimum=1))
    if "log_level" in table:
        level = _as_str(table["log_level"], "general.log_level", source).upper()
        if level not in _VALID_LOG_LEVELS:
            raise ConfigError(
                f"{source}: general.log_level must be one of {sorted(_VALID_LOG_LEVELS)}, got {level!r}"
            )
        out = replace(out, log_level=level)
    if "state_dir" in table:
        out = replace(out, state_dir=Path(_as_str(table["state_dir"], "general.state_dir", source)))
    return out


def _parse_targeting(raw: Any, base: TargetingConfig, source: str) -> TargetingConfig:
    if raw is None:
        return base
    table = _require_table(raw, "targeting", source)
    _check_unknown_keys(
        table,
        {"killable_names", "launcher_names", "never_kill_names"},
        "targeting",
        source,
    )

    out = base
    if "killable_names" in table:
        out = replace(out, killable_names=_as_str_list(table["killable_names"], "targeting.killable_names", source))
    if "launcher_names" in table:
        out = replace(out, launcher_names=_as_str_list(table["launcher_names"], "targeting.launcher_names", source))
    if "never_kill_names" in table:
        out = replace(out, never_kill_names=_as_str_list(table["never_kill_names"], "targeting.never_kill_names", source))

    if not out.killable_names:
        raise ConfigError(f"{source}: targeting.killable_names must contain at least one name")
    return out


def _parse_kill(raw: Any, base: KillConfig, source: str) -> KillConfig:
    if raw is None:
        return base
    table = _require_table(raw, "kill", source)
    _check_unknown_keys(
        table,
        {"mode", "grace_seconds", "post_kill_cooldown_seconds", "throttle_duration_seconds"},
        "kill",
        source,
    )

    out = base
    if "mode" in table:
        mode = _as_str(table["mode"], "kill.mode", source)
        if mode not in _VALID_KILL_MODES:
            raise ConfigError(
                f"{source}: kill.mode must be one of {sorted(_VALID_KILL_MODES)}, got {mode!r}"
            )
        out = replace(out, mode=mode)  # type: ignore[arg-type]
    if "grace_seconds" in table:
        out = replace(out, grace_seconds=_as_int(table["grace_seconds"], "kill.grace_seconds", source, minimum=0))
    if "post_kill_cooldown_seconds" in table:
        out = replace(
            out,
            post_kill_cooldown_seconds=_as_int(
                table["post_kill_cooldown_seconds"],
                "kill.post_kill_cooldown_seconds",
                source,
                minimum=0,
            ),
        )
    if "throttle_duration_seconds" in table:
        out = replace(
            out,
            throttle_duration_seconds=_as_int(
                table["throttle_duration_seconds"],
                "kill.throttle_duration_seconds",
                source,
                minimum=1,
            ),
        )
    return out


def _parse_api(raw: Any, base: ApiConfig, source: str) -> ApiConfig:
    if raw is None:
        return base
    table = _require_table(raw, "api", source)
    _check_unknown_keys(table, {"enabled", "bind", "port"}, "api", source)

    out = base
    if "enabled" in table:
        if not isinstance(table["enabled"], bool):
            raise ConfigError(f"{source}: api.enabled must be a boolean")
        out = replace(out, enabled=table["enabled"])
    if "bind" in table:
        out = replace(out, bind=_as_str(table["bind"], "api.bind", source))
    if "port" in table:
        port = _as_int(table["port"], "api.port", source, minimum=1)
        if port > 65535:
            raise ConfigError(f"{source}: api.port must be 1..65535, got {port}")
        out = replace(out, port=port)
    return out


def _parse_rules(raw: Any, base_rules: tuple[RuleConfig, ...], source: str) -> tuple[RuleConfig, ...]:
    if raw is None:
        # No [[rules]] table at all → fall back to defaults so a stripped-down
        # config (e.g. one that only customizes [kill]) still has protection.
        return base_rules
    if not isinstance(raw, list) or not all(isinstance(r, dict) for r in raw):
        raise ConfigError(f"{source}: [[rules]] must be an array of tables")
    if not raw:
        raise ConfigError(
            f"{source}: [[rules]] is present but empty; remove the section to use defaults, "
            f"or add at least one rule"
        )

    parsed: list[RuleConfig] = []
    seen_names: set[str] = set()
    allowed_keys = {
        "name",
        "signal",
        "threshold",
        "window_s",
        "min_fraction_over",
        "action",
        "cooldown_s",
    }
    required_keys = {"name", "signal", "threshold", "window_s", "min_fraction_over", "action"}

    for idx, entry in enumerate(raw):
        where = f"rules[{idx}]"
        _check_unknown_keys(entry, allowed_keys, where, source)
        missing = required_keys - entry.keys()
        if missing:
            raise ConfigError(f"{source}: {where} missing required key(s): {sorted(missing)}")

        name = _as_str(entry["name"], f"{where}.name", source)
        if name in seen_names:
            raise ConfigError(f"{source}: duplicate rule name {name!r}")
        seen_names.add(name)

        signal = _as_str(entry["signal"], f"{where}.signal", source)
        if not _SIGNAL_PATTERN.match(signal):
            raise ConfigError(
                f"{source}: {where}.signal {signal!r} is not a valid signal path "
                f"(expected e.g. 'system.ram_used_percent' or 'gpu.*.core_temp_c')"
            )

        threshold = _as_number(entry["threshold"], f"{where}.threshold", source)
        window_s = _as_int(entry["window_s"], f"{where}.window_s", source, minimum=1)
        min_fraction = _as_number(entry["min_fraction_over"], f"{where}.min_fraction_over", source)
        if not 0.0 < min_fraction <= 1.0:
            raise ConfigError(
                f"{source}: {where}.min_fraction_over must be in (0, 1], got {min_fraction}"
            )

        action = _as_str(entry["action"], f"{where}.action", source)
        if action not in _VALID_ACTIONS:
            raise ConfigError(
                f"{source}: {where}.action must be one of {sorted(_VALID_ACTIONS)}, got {action!r}"
            )

        cooldown_s: int | None = None
        if "cooldown_s" in entry:
            cooldown_s = _as_int(entry["cooldown_s"], f"{where}.cooldown_s", source, minimum=0)

        parsed.append(
            RuleConfig(
                name=name,
                signal=signal,
                threshold=threshold,
                window_s=window_s,
                min_fraction_over=min_fraction,
                action=action,  # type: ignore[arg-type]
                cooldown_s=cooldown_s,
            )
        )

    return tuple(parsed)


def _cross_validate(cfg: AtFieldConfig, source: str) -> None:
    """Validate invariants that span sections."""
    overlap = set(cfg.targeting.killable_names) & set(cfg.targeting.never_kill_names)
    if overlap:
        raise ConfigError(
            f"{source}: targeting.killable_names and targeting.never_kill_names overlap on "
            f"{sorted(overlap)} — refusing to load an ambiguous policy"
        )
