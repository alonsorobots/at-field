"""Tests for :mod:`atfield.lhm_supervisor`.

We use a fake :class:`ProcessSpawner` so the suite never actually
launches LibreHardwareMonitor (the real binary may not exist on CI).
The fake lets us script a sequence of "exit codes" the would-be LHM
emits so we can drive the restart-with-backoff state machine.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from atfield.lhm_supervisor import (
    LhmSupervisor,
    LhmSupervisorConfig,
    find_lhm_executable,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeProc:
    """Scripted ProcessHandle. ``script`` is a queue of "lifetimes":
    each entry is either an int (exit code emitted after a brief delay)
    or the special string "linger" (process stays alive until terminated)."""

    def __init__(self, *, exit_code: int | None, linger_until_terminated: bool):
        self._exit_code = exit_code
        self._linger = linger_until_terminated
        self._terminated = False
        self.pid = 12345
        self._spawned_at = time.monotonic()
        # When _linger=False, simulate a quick crash: poll() returns
        # the exit code immediately. When True, poll() returns None
        # until terminate() flips it.

    def poll(self) -> int | None:
        if self._linger and not self._terminated:
            return None
        return self._exit_code

    def terminate(self) -> None:
        self._terminated = True

    def kill(self) -> None:
        self._terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return self._exit_code or 0


class _ScriptedSpawner:
    """Spawner whose returned procs are pre-scripted.

    Use ``.script.append(...)`` to enqueue procs; each spawn() pops the
    front. ``spawn_calls`` records the args we were called with for
    assertions on the supervisor's CLI assembly.
    """

    def __init__(self):
        self.script: list[_FakeProc] = []
        self.spawn_calls: list[list[str]] = []
        self._spawn_event = threading.Event()

    def spawn(self, args):
        self.spawn_calls.append(list(args))
        self._spawn_event.set()
        if not self.script:
            raise OSError("no fake proc enqueued -- test script exhausted")
        return self.script.pop(0)

    def wait_for_spawn(self, timeout: float = 2.0) -> bool:
        ok = self._spawn_event.wait(timeout)
        self._spawn_event.clear()
        return ok


class _RaisingSpawner:
    """Always raises OSError -- simulates 'binary missing'."""

    def __init__(self, message="LHM.exe not found"):
        self._message = message
        self.spawn_calls = 0

    def spawn(self, args):
        self.spawn_calls += 1
        raise OSError(self._message)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(tmp_path: Path) -> LhmSupervisorConfig:
    return LhmSupervisorConfig(
        executable=tmp_path / "FakeLHM.exe",
        port=8085,
        backoff_initial_s=0.05,
        backoff_max_s=0.2,
        backoff_factor=2.0,
        shutdown_grace_s=0.2,
    )


def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# Spawner contract
# ---------------------------------------------------------------------------


class TestSpawnedArgs:
    def test_spawn_uses_configured_executable_and_port(self, tmp_path):
        spawner = _ScriptedSpawner()
        spawner.script.append(_FakeProc(exit_code=None, linger_until_terminated=True))
        cfg = _config(tmp_path)
        sup = LhmSupervisor(cfg, spawner=spawner)
        sup.start()
        try:
            assert spawner.wait_for_spawn(2.0)
            assert spawner.spawn_calls[0][0] == str(cfg.executable)
            assert "--server" in spawner.spawn_calls[0]
            assert "--port" in spawner.spawn_calls[0]
            assert "8085" in spawner.spawn_calls[0]
        finally:
            sup.stop()

    def test_extra_args_are_appended(self, tmp_path):
        spawner = _ScriptedSpawner()
        spawner.script.append(_FakeProc(exit_code=None, linger_until_terminated=True))
        cfg = LhmSupervisorConfig(
            executable=tmp_path / "FakeLHM.exe",
            port=9090,
            extra_args=("--config", "custom.xml"),
            backoff_initial_s=0.05,
        )
        sup = LhmSupervisor(cfg, spawner=spawner)
        sup.start()
        try:
            assert spawner.wait_for_spawn(2.0)
            args = spawner.spawn_calls[0]
            assert args[-2:] == ["--config", "custom.xml"]
            assert "9090" in args
        finally:
            sup.stop()


# ---------------------------------------------------------------------------
# Status reporting
# ---------------------------------------------------------------------------


class TestStatus:
    def test_running_status_after_spawn(self, tmp_path):
        spawner = _ScriptedSpawner()
        spawner.script.append(_FakeProc(exit_code=None, linger_until_terminated=True))
        sup = LhmSupervisor(_config(tmp_path), spawner=spawner)
        sup.start()
        try:
            assert _wait_until(lambda: sup.snapshot_status().running)
            s = sup.snapshot_status()
            assert s.pid == 12345
            assert s.last_error is None
            assert s.stopping is False
        finally:
            sup.stop()

    def test_stop_marks_status_stopping(self, tmp_path):
        spawner = _ScriptedSpawner()
        spawner.script.append(_FakeProc(exit_code=None, linger_until_terminated=True))
        sup = LhmSupervisor(_config(tmp_path), spawner=spawner)
        sup.start()
        _wait_until(lambda: sup.snapshot_status().running)
        sup.stop()
        s = sup.snapshot_status()
        assert s.stopping is True
        assert s.running is False


# ---------------------------------------------------------------------------
# Restart on unexpected exit
# ---------------------------------------------------------------------------


class TestRestartOnExit:
    def test_unexpected_exit_triggers_respawn(self, tmp_path):
        spawner = _ScriptedSpawner()
        # First proc dies immediately; second lingers.
        spawner.script.append(_FakeProc(exit_code=1, linger_until_terminated=False))
        spawner.script.append(_FakeProc(exit_code=None, linger_until_terminated=True))
        sup = LhmSupervisor(_config(tmp_path), spawner=spawner)
        sup.start()
        try:
            # Wait for both spawns
            assert _wait_until(lambda: len(spawner.spawn_calls) >= 2, timeout=3.0)
            s = sup.snapshot_status()
            assert s.last_exit_code == 1 or s.running  # restart in flight
        finally:
            sup.stop()

    def test_clean_stop_does_not_respawn(self, tmp_path):
        spawner = _ScriptedSpawner()
        spawner.script.append(_FakeProc(exit_code=None, linger_until_terminated=True))
        sup = LhmSupervisor(_config(tmp_path), spawner=spawner)
        sup.start()
        _wait_until(lambda: sup.snapshot_status().running)
        sup.stop()
        # Give the loop time to do anything wrong.
        time.sleep(0.2)
        assert len(spawner.spawn_calls) == 1, (
            "supervisor should not have respawned after stop()"
        )


# ---------------------------------------------------------------------------
# Spawn failure (binary missing)
# ---------------------------------------------------------------------------


class TestSpawnFailure:
    def test_oserror_is_recorded_and_retried(self, tmp_path):
        spawner = _RaisingSpawner("FakeLHM.exe not found")
        cfg = LhmSupervisorConfig(
            executable=tmp_path / "FakeLHM.exe",
            backoff_initial_s=0.05,
            backoff_max_s=0.1,
        )
        sup = LhmSupervisor(cfg, spawner=spawner)
        sup.start()
        try:
            assert _wait_until(lambda: spawner.spawn_calls >= 2, timeout=2.0)
            s = sup.snapshot_status()
            assert s.running is False
            assert s.last_error is not None
            assert "not found" in s.last_error
        finally:
            sup.stop()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_double_start_only_spawns_once(self, tmp_path):
        spawner = _ScriptedSpawner()
        spawner.script.append(_FakeProc(exit_code=None, linger_until_terminated=True))
        sup = LhmSupervisor(_config(tmp_path), spawner=spawner)
        sup.start()
        sup.start()  # second start should be a no-op
        try:
            assert _wait_until(lambda: sup.snapshot_status().running)
            time.sleep(0.1)
            assert len(spawner.spawn_calls) == 1
        finally:
            sup.stop()

    def test_double_stop_is_safe(self, tmp_path):
        spawner = _ScriptedSpawner()
        spawner.script.append(_FakeProc(exit_code=None, linger_until_terminated=True))
        sup = LhmSupervisor(_config(tmp_path), spawner=spawner)
        sup.start()
        _wait_until(lambda: sup.snapshot_status().running)
        sup.stop()
        sup.stop()  # second stop must not raise


# ---------------------------------------------------------------------------
# Type validation
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_rejects_non_config_argument(self):
        with pytest.raises(TypeError):
            LhmSupervisor({"executable": "x"})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# find_lhm_executable
# ---------------------------------------------------------------------------


class TestFindLhmExecutable:
    def test_env_override_takes_precedence(self, tmp_path, monkeypatch):
        binary = tmp_path / "Override.exe"
        binary.write_bytes(b"")
        monkeypatch.setenv("ATFIELD_LHM_EXE", str(binary))
        # Even with a bundled root, env should win.
        bundled = tmp_path / "bundled"
        bundled.mkdir()
        (bundled / "LibreHardwareMonitor.exe").write_bytes(b"")
        result = find_lhm_executable(bundled_root=bundled)
        assert result == binary

    def test_bundled_root_is_searched(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ATFIELD_LHM_EXE", raising=False)
        bundled = tmp_path / "bundled"
        bundled.mkdir()
        binary = bundled / "LibreHardwareMonitor.exe"
        binary.write_bytes(b"")
        # Steer past the program files fallback by pointing it nowhere.
        monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "no-such-pfdir"))
        result = find_lhm_executable(bundled_root=bundled)
        assert result == binary

    def test_returns_none_when_nothing_exists(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ATFIELD_LHM_EXE", raising=False)
        monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "missing"))
        result = find_lhm_executable(bundled_root=tmp_path / "missing")
        assert result is None

    def test_extra_search_paths_are_consulted(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ATFIELD_LHM_EXE", raising=False)
        monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "missing"))
        extras = tmp_path / "extras"
        extras.mkdir()
        (extras / "LibreHardwareMonitor.exe").write_bytes(b"")
        result = find_lhm_executable(extra_search_paths=(extras,))
        assert result == extras / "LibreHardwareMonitor.exe"
