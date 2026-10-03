"""A rule can WARN before the kill line (0.4.19): `notify = true` on a log/throttle rule.

2026-10-03, Chronos: AT-Field killed four processes for CPU heat (cpu-pkg-hot, 90 C)
and the first anyone heard was the kill pop-up. The engine already supported `log`
and `throttle` rules, but nothing marked one as a WARNING (observe-only mode turns
every kill into `log`, so "log" alone cannot mean "pop up"). This pins the config half:
  * notify parses, defaults to False, and must be a real boolean;
  * notify on a kill rule is refused (a kill always notifies -- ambiguity is a bug);
  * the default renderer writes it, and a dashboard edit of another field keeps it.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from atfield.config import ConfigError, load_config, load_config_from_dict  # noqa: E402
from atfield.config_writer import _render_default_config, update_rule_field  # noqa: E402

KILL = {"name": "cpu-pkg-hot", "signal": "system.cpu_package_temp_c", "threshold": 90.0,
        "window_s": 30, "min_fraction_over": 0.67, "action": "kill"}
WARN = {"name": "cpu-pkg-warm", "signal": "system.cpu_package_temp_c", "threshold": 86.0,
        "window_s": 30, "min_fraction_over": 0.67, "action": "log", "notify": True, "cooldown_s": 600}


def _cfg(*rules):
    return load_config_from_dict({"rules": [dict(r) for r in rules]})


def test_notify_parses_and_defaults_to_false():
    cfg = _cfg(KILL, WARN)
    by = {r.name: r for r in cfg.rules}
    assert by["cpu-pkg-warm"].notify is True
    assert by["cpu-pkg-hot"].notify is False


def test_notify_on_a_kill_rule_is_refused():
    with pytest.raises(ConfigError, match="kill rule always notifies"):
        _cfg({**KILL, "notify": True})


def test_notify_must_be_a_boolean():
    with pytest.raises(ConfigError, match="true or false"):
        _cfg({**WARN, "notify": "yes"})


def test_the_renderer_writes_notify_and_it_round_trips(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(_render_default_config(_cfg(KILL, WARN)), encoding="utf-8")
    by = {r.name: r for r in load_config(p).rules}
    assert by["cpu-pkg-warm"].notify is True and by["cpu-pkg-hot"].notify is False


def test_a_dashboard_threshold_edit_keeps_notify(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(_render_default_config(_cfg(KILL, WARN)), encoding="utf-8")
    update_rule_field(p, "cpu-pkg-warm", "threshold", 85.0)
    by = {r.name: r for r in load_config(p).rules}
    assert by["cpu-pkg-warm"].threshold == 85.0
    assert by["cpu-pkg-warm"].notify is True, "editing the threshold dropped the warning flag"
