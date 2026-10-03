"""scripts/add_warning_rules.py puts one warning under each kill line a HOST config has.

Hosts carry hand-edited configs with their own kill lines (Chronos kills GPU core at
88 C and RAM at 92 %), so a warning copied from the defaults could sit ABOVE the kill
line and never fire before it. The helper derives each from the host's own rule, keeps
the file's text, and is idempotent.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from atfield.config import load_config  # noqa: E402

HOST = '''# hand-edited, keep me
[[rules]]
name              = "gpu-core-hot"
signal            = "gpu.*.core_temp_c"
threshold         = 88.0
window_s          = 30
min_fraction_over = 0.67
action            = "kill"

[[rules]]
name              = "ram-pressure"
signal            = "system.ram_used_percent"
threshold         = 92.0
window_s          = 60
min_fraction_over = 0.75
action            = "kill"
'''


def _run(p):
    env = {**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")}
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "add_warning_rules.py"), str(p)],
                          capture_output=True, text=True, env=env, check=True).stdout


def test_warnings_follow_the_hosts_own_kill_lines_and_keep_its_text(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(HOST, encoding="utf-8")
    _run(p)
    by = {r.name: r for r in load_config(p).rules}
    assert by["gpu-core-warm"].threshold == 84.0 and by["gpu-core-warm"].notify
    assert by["ram-high"].threshold == 87.0 and by["ram-high"].action == "log"
    assert by["ram-high"].window_s == 60 and by["ram-high"].cooldown_s == 600
    assert p.read_text(encoding="utf-8").startswith(HOST.rstrip("\n"))
    assert (tmp_path / "config.toml.pre-0.4.19").read_text(encoding="utf-8") == HOST


def test_a_second_run_adds_nothing(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(HOST, encoding="utf-8")
    _run(p)
    once = p.read_text(encoding="utf-8")
    assert "nothing to add" in _run(p)
    assert p.read_text(encoding="utf-8") == once
