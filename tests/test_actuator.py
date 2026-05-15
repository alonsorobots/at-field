"""Tests for :mod:`atfield.actuator`.

Built around a :class:`FakeProvider` that simulates a process tree without
spawning real processes -- killing real processes from a test suite is a
recipe for tears. The tree topology in each test mirrors a real ML
training scenario (jupyter -> ipykernel -> python; torchrun -> python ;
accelerate -> deepspeed -> python -> python workers; etc).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from atfield.actuator import (
    Actuator,
    ProcInfo,
    find_kill_root,
    script_name_from_cmdline,
)
from atfield.config import default_config
from atfield.policy import Action


@dataclass
class _FakeProc:
    pid: int
    ppid: int
    name: str
    cmdline: tuple[str, ...] = ()
    rss: int = 0
    alive: bool = True


@dataclass
class FakeProvider:
    """Test double for :class:`atfield.actuator.ProcessProvider`.

    Build a tree by passing a list of ``(pid, ppid, name)`` tuples; the
    provider exposes the same interface as PsutilProvider but does no
    real I/O. Calls to ``terminate`` and ``kill`` are recorded so tests
    can assert on send-order.
    """

    procs: dict[int, _FakeProc] = field(default_factory=dict)
    own: int = 99999
    terminated: list[int] = field(default_factory=list)
    killed: list[int] = field(default_factory=list)
    suspended: list[int] = field(default_factory=list)
    resumed: list[int] = field(default_factory=list)

    @classmethod
    def from_tree(cls, edges: list[tuple[int, int, str, int]], own: int = 99999) -> FakeProvider:
        """edges = [(pid, ppid, name, rss_bytes), ...]"""
        d = {pid: _FakeProc(pid=pid, ppid=ppid, name=name, rss=rss) for pid, ppid, name, rss in edges}
        return cls(procs=d, own=own)

    def own_pid(self) -> int:
        return self.own

    def _to_info(self, p: _FakeProc) -> ProcInfo:
        return ProcInfo(pid=p.pid, ppid=p.ppid, name=p.name, cmdline=p.cmdline, rss_bytes=p.rss)

    def list_all(self) -> list[ProcInfo]:
        return [self._to_info(p) for p in self.procs.values() if p.alive]

    def get(self, pid: int):
        p = self.procs.get(pid)
        if p is None or not p.alive:
            return None
        return self._to_info(p)

    def parent(self, pid: int):
        p = self.procs.get(pid)
        if p is None or not p.alive:
            return None
        parent = self.procs.get(p.ppid)
        if parent is None:
            return None
        return self._to_info(parent)

    def descendants(self, pid: int) -> list[ProcInfo]:
        # BFS over alive children
        out: list[ProcInfo] = []
        stack = [pid]
        seen = {pid}
        while stack:
            cur = stack.pop()
            for p in self.procs.values():
                if p.ppid == cur and p.alive and p.pid not in seen:
                    seen.add(p.pid)
                    out.append(self._to_info(p))
                    stack.append(p.pid)
        return out

    def terminate(self, pid: int) -> None:
        self.terminated.append(pid)
        if pid in self.procs:
            # Simulate well-behaved process: terminate -> alive=False after grace
            # (We don't decrement here; tests opt-in via _flip_dead.)
            pass

    def kill(self, pid: int) -> None:
        self.killed.append(pid)
        if pid in self.procs:
            self.procs[pid].alive = False

    def is_alive(self, pid: int) -> bool:
        p = self.procs.get(pid)
        return bool(p and p.alive)

    def suspend(self, pid: int) -> bool:
        p = self.procs.get(pid)
        if p is None or not p.alive:
            return False
        self.suspended.append(pid)
        return True

    def resume(self, pid: int) -> bool:
        p = self.procs.get(pid)
        if p is None or not p.alive:
            return False
        self.resumed.append(pid)
        return True

    def _flip_dead_after_terminate(self) -> None:
        for pid in self.terminated:
            if pid in self.procs:
                self.procs[pid].alive = False


# ---------------------------------------------------------------------------
# find_kill_root
# ---------------------------------------------------------------------------


class TestFindKillRoot:
    def test_walks_up_through_python_to_torchrun(self):
        # explorer -> torchrun -> python -> python (worker)
        provider = FakeProvider.from_tree(
            [
                (10, 0, "explorer.exe", 0),
                (20, 10, "torchrun", 0),
                (30, 20, "python.exe", 0),
                (40, 30, "python.exe", 0),  # worker
            ]
        )
        root = find_kill_root(
            40,
            provider=provider,
            killable_names=frozenset(["python.exe"]),
            launcher_names=frozenset(["torchrun"]),
        )
        assert root is not None
        assert root.pid == 20  # torchrun is the topmost keeper
        assert root.name == "torchrun"

    def test_walks_through_jupyter_chain(self):
        # services -> jupyter -> ipykernel_launcher -> python (cell)
        provider = FakeProvider.from_tree(
            [
                (5, 0, "services.exe", 0),
                (10, 5, "jupyter", 0),
                (20, 10, "ipykernel_launcher", 0),
                (30, 20, "python.exe", 0),
            ]
        )
        root = find_kill_root(
            30,
            provider=provider,
            killable_names=frozenset(["python.exe"]),
            launcher_names=frozenset(["jupyter", "ipykernel_launcher"]),
        )
        assert root is not None
        assert root.pid == 10
        assert root.name == "jupyter"

    def test_stops_at_topmost_keeper(self):
        # python -> python -> python (no launcher)
        provider = FakeProvider.from_tree(
            [
                (5, 0, "explorer.exe", 0),
                (10, 5, "python.exe", 0),
                (20, 10, "python.exe", 0),
                (30, 20, "python.exe", 0),
            ]
        )
        root = find_kill_root(
            30,
            provider=provider,
            killable_names=frozenset(["python.exe"]),
            launcher_names=frozenset(),
        )
        assert root is not None
        assert root.pid == 10  # topmost python whose parent is explorer

    def test_offender_not_in_keepers_returns_none(self):
        # Refuse to walk up an arbitrary non-python process
        provider = FakeProvider.from_tree(
            [
                (5, 0, "explorer.exe", 0),
                (10, 5, "chrome.exe", 0),
            ]
        )
        root = find_kill_root(
            10,
            provider=provider,
            killable_names=frozenset(["python.exe"]),
            launcher_names=frozenset(),
        )
        assert root is None, "non-python offender must not result in a kill root"

    def test_missing_pid_returns_none(self):
        provider = FakeProvider.from_tree([(5, 0, "explorer.exe", 0)])
        assert (
            find_kill_root(
                999, provider=provider,
                killable_names=frozenset(["python.exe"]),
                launcher_names=frozenset(),
            )
            is None
        )

    def test_case_insensitive_name_matching(self):
        provider = FakeProvider.from_tree(
            [
                (5, 0, "explorer.exe", 0),
                (10, 5, "PYTHON.EXE", 0),  # uppercase
                (20, 10, "python.exe", 0),
            ]
        )
        root = find_kill_root(
            20,
            provider=provider,
            killable_names=frozenset(["python.exe"]),
            launcher_names=frozenset(),
        )
        assert root is not None
        assert root.pid == 10


# ---------------------------------------------------------------------------
# Actuator.execute
# ---------------------------------------------------------------------------


def _action(kind: str = "kill") -> Action:
    return Action(
        kind=kind,
        rule_name="r",
        base_rule_name="r",
        signal="system.ram_used_percent",
        threshold=80,
        fraction_over=1.0,
        samples_considered=10,
        latest_value=95.0,
        triggered_at_ns=0,
        cooldown_seconds=60,
    )


class TestActuator:
    def _build(self, edges, *, own=99999):
        provider = FakeProvider.from_tree(edges, own=own)
        return Actuator(default_config(), provider=provider, sleep=lambda _s: None), provider

    def test_log_action_does_not_kill(self):
        actuator, provider = self._build([(10, 0, "python.exe", 1)])
        report = actuator.execute(_action(kind="log"))
        assert report.killed == ()
        assert report.skipped_reason is not None
        assert provider.terminated == []
        assert provider.killed == []

    def test_kill_action_with_no_eligible_offender_skipped(self):
        # No python procs anywhere
        actuator, provider = self._build([(10, 0, "explorer.exe", 1)])
        report = actuator.execute(_action(kind="kill"))
        assert report.kill_root is None
        assert "no eligible offender" in (report.skipped_reason or "")
        assert provider.killed == []

    def test_kill_action_terminates_then_kills_survivors(self):
        # explorer -> torchrun -> python -> python; trigger kill on PID 40
        actuator, provider = self._build(
            [
                (10, 0, "explorer.exe", 0),
                (20, 10, "torchrun", 1000),
                (30, 20, "python.exe", 5_000_000_000),
                (40, 30, "python.exe", 100),
            ]
        )
        report = actuator.execute(_action(kind="kill"), candidate_pids=[40, 30])
        assert report.kill_root is not None
        assert report.kill_root.pid == 20
        # Should have terminated all three (root + 2 descendants)
        assert sorted(provider.terminated) == [20, 30, 40]
        # All survived terminate (FakeProvider doesn't auto-die), so kill() runs
        assert sorted(provider.killed) == [20, 30, 40]

    def test_self_protection_filters_own_pid(self):
        # python -> python where one is the watchdog
        own_pid = 99
        actuator, provider = self._build(
            [
                (10, 0, "explorer.exe", 0),
                (20, 10, "python.exe", 100),
                (99, 20, "python.exe", 100),  # this is "us"
            ],
            own=own_pid,
        )
        report = actuator.execute(_action(kind="kill"), candidate_pids=[20])
        # Own PID must not be in killed list
        assert own_pid not in provider.killed
        assert own_pid not in provider.terminated

    def test_never_kill_names_filter(self):
        actuator, provider = self._build(
            [
                (10, 0, "services.exe", 0),
                (20, 10, "python.exe", 100),
                (30, 20, "explorer.exe", 100),  # in never-kill-names
            ]
        )
        report = actuator.execute(_action(kind="kill"), candidate_pids=[20])
        # explorer must not be killed
        assert 30 not in provider.killed

    def test_picks_highest_rss_offender_from_candidates(self):
        actuator, _provider = self._build(
            [
                (10, 0, "explorer.exe", 0),
                (20, 10, "python.exe", 100),         # small
                (30, 10, "python.exe", 1_000_000),   # bigger
                (40, 10, "chrome.exe", 999_999_999), # excluded by name
            ]
        )
        report = actuator.execute(_action(kind="kill"), candidate_pids=[20, 30, 40])
        assert report.offender_pid == 30

    def test_aggressive_mode_skips_grace_window(self):
        cfg = default_config()
        # Build with aggressive mode
        from dataclasses import replace
        cfg2 = replace(cfg, kill=replace(cfg.kill, mode="aggressive"))
        provider = FakeProvider.from_tree(
            [
                (10, 0, "explorer.exe", 0),
                (20, 10, "python.exe", 100),
            ]
        )
        sleep_durations: list[float] = []
        actuator = Actuator(cfg2, provider=provider, sleep=lambda s: sleep_durations.append(s))
        actuator.execute(_action(kind="kill"), candidate_pids=[20])
        # Aggressive: no grace_seconds sleep, only the brief drain
        assert all(s < 1.0 for s in sleep_durations), f"unexpected long sleep in aggressive mode: {sleep_durations}"
        assert provider.killed == [20]

    def test_kill_report_succeeded_property(self):
        actuator, _provider = self._build([(10, 0, "explorer.exe", 0), (20, 10, "python.exe", 100)])
        report = actuator.execute(_action(kind="kill"), candidate_pids=[20])
        assert report.succeeded is True

    def test_kill_report_failed_when_process_survives(self):
        # Make a python proc that survives kill() (simulated unkillable)
        provider = FakeProvider.from_tree([(10, 0, "explorer.exe", 0), (20, 10, "python.exe", 100)])
        # Override kill to not actually decrement alive
        orig_kill = provider.kill
        def stubborn_kill(pid):
            provider.killed.append(pid)
            # don't flip alive
        provider.kill = stubborn_kill  # type: ignore[method-assign]
        actuator = Actuator(default_config(), provider=provider, sleep=lambda _: None)
        report = actuator.execute(_action(kind="kill"), candidate_pids=[20])
        assert any(k.survived for k in report.killed)
        assert not report.succeeded


class TestScriptNameFromCmdline:
    """Heuristic that extracts the human-recognizable script behind a launcher.

    These cases mirror real cmdlines we've seen in the wild; if you change
    the heuristic, please add the case here BEFORE touching the helper.
    """

    def test_empty_cmdline_returns_none(self):
        assert script_name_from_cmdline(()) is None
        assert script_name_from_cmdline(None) is None

    def test_bare_interpreter_returns_none(self):
        assert script_name_from_cmdline(("python.exe",)) is None

    def test_simple_python_script(self):
        cmd = ("python.exe", "train.py", "--lr", "1e-4")
        assert script_name_from_cmdline(cmd) == "train.py"

    def test_python_script_with_path(self):
        cmd = ("python.exe", "scripts/train.py", "--lr", "1e-4")
        assert script_name_from_cmdline(cmd) == "train.py"

    def test_python_script_with_windows_path(self):
        cmd = ("python.exe", "C:\\projects\\foo\\train.py")
        assert script_name_from_cmdline(cmd) == "train.py"

    def test_python_module_mode(self):
        cmd = ("python.exe", "-m", "torch.distributed.run", "--nproc-per-node=2")
        assert script_name_from_cmdline(cmd) == "torch.distributed.run"

    def test_python_unbuffered_flag_then_script(self):
        cmd = ("python.exe", "-u", "scripts/train.py")
        assert script_name_from_cmdline(cmd) == "train.py"

    def test_python_combined_short_flags_then_script(self):
        # `-uOO` is a bundle of -u, -O, -O
        cmd = ("python.exe", "-uOO", "train.py")
        assert script_name_from_cmdline(cmd) == "train.py"

    def test_python_inline_code_returns_marker(self):
        cmd = ("python.exe", "-c", "import torch; torch.cuda.empty_cache()")
        assert script_name_from_cmdline(cmd) == "<inline -c>"

    def test_python_W_flag_takes_value(self):
        # -W default::DeprecationWarning train.py
        cmd = ("python.exe", "-W", "default::DeprecationWarning", "train.py")
        assert script_name_from_cmdline(cmd) == "train.py"

    def test_python_X_flag_takes_value(self):
        cmd = ("python.exe", "-X", "dev", "train.py")
        assert script_name_from_cmdline(cmd) == "train.py"

    def test_python_long_flag_skipped(self):
        cmd = ("python.exe", "--check-hash-based-pycs", "always", "train.py")
        # We treat --foo as a flag that doesn't consume the next arg, so
        # `always` becomes the "script". This is wrong in theory but right
        # often enough in practice -- known tradeoff documented in the
        # helper's docstring.
        result = script_name_from_cmdline(cmd)
        # Either "always" or "train.py" is acceptable; document whichever
        # the current heuristic returns so we notice if it changes.
        assert result == "always"

    def test_powershell_file_flag(self):
        cmd = ("powershell.exe", "-NoProfile", "-File", "C:\\foo\\bar.ps1")
        assert script_name_from_cmdline(cmd) == "bar.ps1"

    def test_powershell_command_returns_marker(self):
        cmd = ("powershell.exe", "-Command", "Get-Process python")
        assert script_name_from_cmdline(cmd) == "<inline -Command>"

    def test_cmd_exe_slash_c(self):
        cmd = ("cmd.exe", "/c", "run.bat")
        assert script_name_from_cmdline(cmd) == "run.bat"

    def test_node_script(self):
        # We don't special-case node, but the "first non-flag positional"
        # heuristic catches it.
        cmd = ("node.exe", "server.mjs")
        assert script_name_from_cmdline(cmd) == "server.mjs"

    def test_cross_separator_basename(self):
        cmd = ("python.exe", "C:/Users/me/projects/train.py")
        assert script_name_from_cmdline(cmd) == "train.py"

    def test_double_dash_terminator(self):
        cmd = ("python.exe", "--", "weirdly-named-script")
        assert script_name_from_cmdline(cmd) == "weirdly-named-script"


# ---------------------------------------------------------------------------
# Throttle action (suspend/resume)
# ---------------------------------------------------------------------------


def _throttle_action(rule_name: str = "test", signal: str = "test.signal") -> Action:
    return Action(
        kind="throttle",
        rule_name=rule_name,
        base_rule_name=rule_name,
        signal=signal,
        threshold=0.0,
        fraction_over=1.0,
        samples_considered=10,
        latest_value=99.0,
        triggered_at_ns=0,
        cooldown_seconds=10,
    )


class TestThrottleAction:
    def test_throttle_suspends_root_and_descendants(self, monkeypatch):
        from dataclasses import replace as dc_replace
        provider = FakeProvider.from_tree([
            (1, 0, "explorer.exe", 0),
            (100, 1, "torchrun", 1_000_000),
            (200, 100, "python.exe", 50_000_000),
        ])
        cfg = default_config()
        # Tiny duration so the test doesn't hang.
        cfg = dc_replace(cfg, kill=dc_replace(cfg.kill, throttle_duration_seconds=1))
        actuator = Actuator(cfg, provider=provider)
        try:
            report = actuator.execute(_throttle_action(), candidate_pids=[200])
            assert report.kill_root is not None
            # Kill root walks up to torchrun (the launcher).
            assert report.kill_root.pid == 100
            # Both root and descendant got suspended.
            assert set(provider.suspended) == {100, 200}
            # No actual kills.
            assert provider.killed == []
            assert provider.terminated == []
        finally:
            actuator.shutdown()

    def test_throttle_records_succeeded_methods(self, monkeypatch):
        from dataclasses import replace as dc_replace
        provider = FakeProvider.from_tree([
            (1, 0, "explorer.exe", 0),
            (100, 1, "python.exe", 50_000_000),
        ])
        cfg = default_config()
        cfg = dc_replace(cfg, kill=dc_replace(cfg.kill, throttle_duration_seconds=1))
        actuator = Actuator(cfg, provider=provider)
        try:
            report = actuator.execute(_throttle_action(), candidate_pids=[100])
            assert all(k.method == "suspend" for k in report.killed)
            # By design throttled procs are still alive (survived=True).
            assert all(k.survived for k in report.killed)
        finally:
            actuator.shutdown()

    def test_shutdown_resumes_active_throttles(self):
        from dataclasses import replace as dc_replace
        provider = FakeProvider.from_tree([
            (100, 0, "python.exe", 50_000_000),
        ])
        cfg = default_config()
        # Long duration so the timer won't fire during the test.
        cfg = dc_replace(cfg, kill=dc_replace(cfg.kill, throttle_duration_seconds=600))
        actuator = Actuator(cfg, provider=provider)
        actuator.execute(_throttle_action(), candidate_pids=[100])
        assert 100 in provider.suspended
        assert 100 not in provider.resumed

        actuator.shutdown()
        assert 100 in provider.resumed

    def test_throttle_with_no_eligible_offender_is_noop(self):
        provider = FakeProvider.from_tree([
            (1, 0, "explorer.exe", 0),
        ])
        actuator = Actuator(default_config(), provider=provider)
        try:
            report = actuator.execute(_throttle_action(), candidate_pids=[1])
            assert report.kill_root is None
            assert "no eligible offender" in (report.skipped_reason or "")
            assert provider.suspended == []
        finally:
            actuator.shutdown()

    def test_overlapping_throttles_keep_latest_timer(self):
        """Two throttles for the same PID -- the second should cancel the
        first's timer (longest stay wins) rather than scheduling double resumes."""
        from dataclasses import replace as dc_replace
        provider = FakeProvider.from_tree([
            (100, 0, "python.exe", 50_000_000),
        ])
        cfg = default_config()
        cfg = dc_replace(cfg, kill=dc_replace(cfg.kill, throttle_duration_seconds=600))
        actuator = Actuator(cfg, provider=provider)
        actuator.execute(_throttle_action("first"), candidate_pids=[100])
        actuator.execute(_throttle_action("second"), candidate_pids=[100])
        try:
            # Both calls suspended (idempotent at the provider level).
            assert provider.suspended.count(100) == 2
            # Only one entry tracked -- prior timer cancelled.
            assert len(actuator._active_throttles) == 1
        finally:
            actuator.shutdown()
