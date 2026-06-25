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

from atfield.lhm_config import LHM_CONFIG_FILENAME, REQUIRED_KEYS
from atfield.lhm_supervisor import (
    LhmSupervisor,
    LhmSupervisorConfig,
    ensure_url_reservation,
    find_lhm_executable,
    probe_lhm_http,
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

    def wait_for_spawn(self, timeout: float = 10.0) -> bool:
        # Generous ceiling: the event fires the instant spawn() is called,
        # so a large timeout costs nothing on passing runs -- it only gives
        # slow / contended CI runners slack. (A 2.0s ceiling flaked on a
        # Windows runner where the pre-spawn config write pushed past it.)
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
        # Tests use fake spawners; there's no real port to probe.
        # 0.0 disables the probe entirely.
        http_ready_timeout_s=0.0,
        # Tests don't ship a real LHM exe so the parent dir is a
        # tmp scratch space; the config writer would no-op there
        # but skipping it removes one source of cross-test noise.
        manage_config=False,
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
    def test_spawn_uses_configured_executable(self, tmp_path):
        """LHM 0.9.x doesn't accept CLI flags for the web server -- it
        reads them from LibreHardwareMonitor.config in its own dir.
        The supervisor therefore spawns LHM with no extra arguments by
        default; the bundled config file is what makes the web server
        come up on the port AT-Field expects."""
        spawner = _ScriptedSpawner()
        spawner.script.append(_FakeProc(exit_code=None, linger_until_terminated=True))
        cfg = _config(tmp_path)
        sup = LhmSupervisor(cfg, spawner=spawner)
        sup.start()
        try:
            assert spawner.wait_for_spawn(10.0)
            args = spawner.spawn_calls[0]
            assert args[0] == str(cfg.executable)
            # Without extra_args, no other CLI flags are passed -- the
            # config file owns everything.
            assert len(args) == 1
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
            http_ready_timeout_s=0.0,
            manage_config=False,
        )
        sup = LhmSupervisor(cfg, spawner=spawner)
        sup.start()
        try:
            assert spawner.wait_for_spawn(10.0)
            args = spawner.spawn_calls[0]
            # extra_args is appended verbatim; supervisor doesn't pass
            # the configured port as a CLI flag (LHM 0.9.x reads it from
            # LibreHardwareMonitor.config instead).
            assert args[-2:] == ["--config", "custom.xml"]
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
            http_ready_timeout_s=0.0,
            manage_config=False,
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


# ---------------------------------------------------------------------------
# Config management on spawn (post-v0.2 robustness fix)
# ---------------------------------------------------------------------------


class TestConfigManagement:
    """The supervisor re-asserts LHM's config on every spawn.

    This is the central robustness property after the v0.9.4 -> v0.9.6
    regression where LHM rewrote our pre-baked config and the HTTP
    server silently stopped coming up. Now: regardless of what's on
    disk before spawn, our required keys are present after spawn.
    """

    def test_ensures_config_before_spawn_when_enabled(self, tmp_path):
        spawner = _ScriptedSpawner()
        spawner.script.append(_FakeProc(exit_code=None, linger_until_terminated=True))
        cfg = LhmSupervisorConfig(
            executable=tmp_path / "FakeLHM.exe",
            port=8085,
            backoff_initial_s=0.05,
            http_ready_timeout_s=0.0,
            manage_config=True,
        )
        sup = LhmSupervisor(cfg, spawner=spawner)
        sup.start()
        try:
            assert spawner.wait_for_spawn(10.0)
            cfg_path = tmp_path / LHM_CONFIG_FILENAME
            assert cfg_path.exists(), (
                "supervisor should have ensured the LHM config file before spawn"
            )
            text = cfg_path.read_text(encoding="utf-8")
            for key in REQUIRED_KEYS:
                assert key in text, f"required LHM key {key!r} missing from config"
            assert 'value="8085"' in text  # port matches config
        finally:
            sup.stop()

    def test_overwrites_stale_config_keys_but_preserves_unrelated(self, tmp_path):
        # Pre-populate a config with our keys WRONG and an unrelated
        # user key. The supervisor should fix ours and leave theirs alone.
        cfg_path = tmp_path / LHM_CONFIG_FILENAME
        cfg_path.write_text(
            """<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <appSettings>
    <add key="runWebServerMenuItem" value="False" />
    <add key="webServerPortNumeric.Value" value="9999" />
    <add key="userFavoriteSensor" value="GPU Core Temp" />
  </appSettings>
</configuration>
""",
            encoding="utf-8",
        )
        spawner = _ScriptedSpawner()
        spawner.script.append(_FakeProc(exit_code=None, linger_until_terminated=True))
        cfg = LhmSupervisorConfig(
            executable=tmp_path / "FakeLHM.exe",
            port=8085,
            backoff_initial_s=0.05,
            http_ready_timeout_s=0.0,
            manage_config=True,
        )
        sup = LhmSupervisor(cfg, spawner=spawner)
        sup.start()
        try:
            assert spawner.wait_for_spawn(10.0)
            text = cfg_path.read_text(encoding="utf-8")
            assert 'key="runWebServerMenuItem" value="True"' in text
            assert 'key="webServerPortNumeric.Value" value="8085"' in text
            assert 'key="userFavoriteSensor" value="GPU Core Temp"' in text, (
                "supervisor must preserve unrelated user keys"
            )
        finally:
            sup.stop()


# ---------------------------------------------------------------------------
# Port probe
# ---------------------------------------------------------------------------


class TestProbeLhmHttp:
    def test_returns_true_when_port_accepts_connection(self):
        # Bind a real listening socket on an ephemeral port so the
        # probe's TCP connect succeeds.
        import socket as _sock

        srv = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            ok = probe_lhm_http(
                host="127.0.0.1",
                port=port,
                timeout_s=2.0,
                sleep=lambda _: None,
                poll_interval_s=0.01,
            )
            assert ok is True
        finally:
            srv.close()

    def test_returns_false_when_port_never_opens(self):
        # An ephemeral port that's NOT bound should never accept --
        # we expect timeout.
        ok = probe_lhm_http(
            host="127.0.0.1",
            port=1,  # privileged + unlikely to be bound
            timeout_s=0.2,
            sleep=lambda _: None,
            poll_interval_s=0.05,
        )
        assert ok is False

    def test_stop_event_short_circuits_wait(self):
        stop = threading.Event()
        stop.set()
        ok = probe_lhm_http(
            host="127.0.0.1",
            port=1,
            timeout_s=10.0,  # would otherwise wait 10s
            sleep=lambda _: None,
            stop_event=stop,
            poll_interval_s=0.05,
        )
        assert ok is False


class TestRestartOnNoHttp:
    """LHM tries StartHttpListener() exactly once and swallows failures,
    so 'process alive' != 'web server up'. The supervisor must kill and
    respawn when the HTTP probe never succeeds, up to a cap, then give
    up to avoid churning forever."""

    def _cfg(self, tmp_path, *, limit):
        return LhmSupervisorConfig(
            executable=tmp_path / "FakeLHM.exe",
            port=8085,
            backoff_initial_s=0.01,
            backoff_max_s=0.05,
            backoff_factor=2.0,
            shutdown_grace_s=0.1,
            http_ready_timeout_s=0.5,  # probe is stubbed; value just enables the path
            manage_config=False,
            http_failure_restart_limit=limit,
        )

    def test_respawns_then_gives_up_when_http_never_binds(self, tmp_path, monkeypatch):
        import atfield.lhm_supervisor as mod

        # Probe always reports "never came up", instantly.
        monkeypatch.setattr(mod, "probe_lhm_http", lambda **kw: False)

        spawner = _ScriptedSpawner()
        for _ in range(5):
            spawner.script.append(_FakeProc(exit_code=None, linger_until_terminated=True))

        sup = LhmSupervisor(self._cfg(tmp_path, limit=2), spawner=spawner)
        sup.start()
        try:
            # limit=2 -> failures 1 and 2 respawn, failure 3 gives up:
            # exactly 3 spawns, then no more.
            assert _wait_until(lambda: len(spawner.spawn_calls) >= 3, timeout=3.0)
            time.sleep(0.25)
            assert len(spawner.spawn_calls) == 3
        finally:
            sup.stop()

    def test_no_respawn_when_http_becomes_ready(self, tmp_path, monkeypatch):
        import atfield.lhm_supervisor as mod

        monkeypatch.setattr(mod, "probe_lhm_http", lambda **kw: True)

        spawner = _ScriptedSpawner()
        spawner.script.append(_FakeProc(exit_code=None, linger_until_terminated=True))
        spawner.script.append(_FakeProc(exit_code=None, linger_until_terminated=True))

        sup = LhmSupervisor(self._cfg(tmp_path, limit=2), spawner=spawner)
        sup.start()
        try:
            assert spawner.wait_for_spawn(10.0)
            time.sleep(0.25)
            assert len(spawner.spawn_calls) == 1  # healthy -> no respawn
            with sup.status_lock:
                assert sup._status.http_ready is True
        finally:
            sup.stop()


class TestEnsureUrlReservation:
    """The URL-ACL provisioning that lets LHM's HttpListener bind the
    wildcard prefix without being elevated. We stub ``subprocess.run`` so
    the suite never touches real http.sys state."""

    def _patch_run(self, monkeypatch, handler):
        import atfield.lhm_supervisor as mod

        monkeypatch.setattr(mod.sys, "platform", "win32", raising=False)
        monkeypatch.setattr(mod.subprocess, "run", handler)

    def test_skipped_off_windows(self, monkeypatch):
        import atfield.lhm_supervisor as mod

        monkeypatch.setattr(mod.sys, "platform", "linux", raising=False)
        assert ensure_url_reservation(8085) == "skipped:not-windows"

    def test_present_when_show_lists_url(self, monkeypatch):
        def handler(cmd, **kw):
            assert "show" in cmd
            return _completed(stdout="    Reserved URL : http://+:8085/\n")

        self._patch_run(monkeypatch, handler)
        assert ensure_url_reservation(8085) == "present"

    def test_added_when_absent_then_add_succeeds(self, monkeypatch):
        calls = []

        def handler(cmd, **kw):
            calls.append(cmd)
            if "show" in cmd:
                return _completed(stdout="", returncode=1)
            assert "add" in cmd and any(a.startswith("sddl=") for a in cmd)
            return _completed(returncode=0)

        self._patch_run(monkeypatch, handler)
        assert ensure_url_reservation(8085) == "added"
        assert any("add" in c for c in calls)

    def test_failed_when_add_returns_nonzero(self, monkeypatch):
        def handler(cmd, **kw):
            if "show" in cmd:
                return _completed(stdout="", returncode=1)
            return _completed(stdout="The requested operation requires elevation.", returncode=1)

        self._patch_run(monkeypatch, handler)
        result = ensure_url_reservation(8085)
        assert result.startswith("failed:")

    def test_never_raises_when_subprocess_explodes(self, monkeypatch):
        def handler(cmd, **kw):
            raise OSError("netsh not found")

        self._patch_run(monkeypatch, handler)
        # Must swallow and report, never propagate -- spawning LHM is more
        # important than the reservation.
        assert ensure_url_reservation(8085).startswith("failed:")


def _completed(*, stdout: str = "", stderr: str = "", returncode: int = 0):
    """Minimal stand-in for subprocess.CompletedProcess."""
    import subprocess

    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )
