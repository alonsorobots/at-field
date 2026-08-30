"""Tests for :mod:`atfield.actuator`.

Built around a :class:`FakeProvider` that simulates a process tree without
spawning real processes -- killing real processes from a test suite is a
recipe for tears. The tree topology in each test mirrors a real ML
training scenario (jupyter -> ipykernel -> python; torchrun -> python ;
accelerate -> deepspeed -> python -> python workers; etc).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from atfield.actuator import (
    processes_over_rss_cap,
    Actuator,
    ProcInfo,
    find_kill_root,
    offender_axis,
    script_name_from_cmdline,
)
from atfield.client_registry import discover_protected
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
    cpu: float = 0.0
    create_time: float = 0.0


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
    cpu_sampled: list[tuple[int, ...]] = field(default_factory=list)

    @classmethod
    def from_tree(cls, edges: list[tuple[int, int, str, int]], own: int = 99999) -> FakeProvider:
        """edges = [(pid, ppid, name, rss_bytes), ...]"""
        d = {pid: _FakeProc(pid=pid, ppid=ppid, name=name, rss=rss) for pid, ppid, name, rss in edges}
        return cls(procs=d, own=own)

    @classmethod
    def from_procs(cls, procs: list[_FakeProc], own: int = 99999) -> FakeProvider:
        """Build from fully-specified procs (cpu, cmdline, create_time)."""
        return cls(procs={p.pid: p for p in procs}, own=own)

    def own_pid(self) -> int:
        return self.own

    def _to_info(self, p: _FakeProc) -> ProcInfo:
        return ProcInfo(
            pid=p.pid,
            ppid=p.ppid,
            name=p.name,
            cmdline=p.cmdline,
            rss_bytes=p.rss,
            create_time=p.create_time,
        )

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

    def sample_cpu_percent(self, pids, interval: float) -> dict[int, float]:
        self.cpu_sampled.append(tuple(pids))
        return {
            pid: self.procs[pid].cpu
            for pid in pids
            if pid in self.procs and self.procs[pid].alive
        }

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


def _action(kind: str = "kill", signal: str = "system.ram_used_percent") -> Action:
    return Action(
        kind=kind,
        rule_name="r",
        base_rule_name="r",
        signal=signal,
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


# ---------------------------------------------------------------------------
# Offender axis + supervisor protection
#
# Regression cover for a mis-targeted kill on the dev rig (2026-08-05): a CPU
# thermal rule killed a job coordinator that was not causing the heat. Two
# independent defects combined, and each is pinned separately below.
# ---------------------------------------------------------------------------


def _cfg(tmp_path, **targeting):
    """Config rooted at a temp state_dir so manifest lookups stay hermetic."""
    cfg = default_config()
    return replace(
        cfg,
        general=replace(cfg.general, state_dir=Path(tmp_path)),
        targeting=replace(cfg.targeting, **targeting),
    )


def _write_manifest(root, filename: str, **fields) -> None:
    d = Path(root) / "clients" / "kiroshi"
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_text(json.dumps(fields), encoding="utf-8")


class TestOffenderAxis:
    def test_cpu_signals_rank_by_cpu(self):
        assert offender_axis("system.cpu_package_temp_c") == "cpu"
        assert offender_axis("system.cpu_used_percent") == "cpu"

    def test_other_signals_rank_by_memory(self):
        for sig in (
            "system.ram_used_percent",
            "system.commit_percent",
            "system.hard_fault_rate",
        ):
            assert offender_axis(sig) == "memory"


class TestCpuAxisOffenderSelection:
    """A CPU rule must target the busiest process, not the fattest.

    The burn loop that caused the thermal event held almost no memory, so RSS
    ranking never saw it and picked the largest idle service instead.
    """

    def _rig(self, tmp_path):
        procs = [
            _FakeProc(pid=10, ppid=0, name="explorer.exe"),
            # Fat, but idle.
            _FakeProc(
                pid=20, ppid=10, name="python.exe", rss=4_000_000_000, cpu=0.4,
                cmdline=("python.exe", "-m", "service"),
            ),
            # Tiny, but pinning a core: the actual source of the heat.
            _FakeProc(
                pid=30, ppid=10, name="python.exe", rss=12_000_000, cpu=99.0,
                cmdline=("python.exe", "burn.py"),
            ),
        ]
        provider = FakeProvider.from_procs(procs)
        actuator = Actuator(_cfg(tmp_path), provider=provider, sleep=lambda _s: None)
        return actuator, provider

    def test_cpu_rule_targets_the_busy_process(self, tmp_path):
        actuator, provider = self._rig(tmp_path)
        report = actuator.execute(_action("kill", signal="system.cpu_package_temp_c"))
        assert report.offender_pid == 30
        touched = set(provider.terminated) | set(provider.killed)
        assert 20 not in touched, "the idle service must not be collateral"

    def test_memory_rule_still_targets_the_fat_process(self, tmp_path):
        actuator, _provider = self._rig(tmp_path)
        report = actuator.execute(_action("kill", signal="system.ram_used_percent"))
        assert report.offender_pid == 20

    def test_cpu_rule_measures_rather_than_guesses(self, tmp_path):
        actuator, provider = self._rig(tmp_path)
        actuator.execute(_action("kill", signal="system.cpu_package_temp_c"))
        assert provider.cpu_sampled, "cpu axis must sample real busy-ness"


class TestFindKillRootProtection:
    def _tree(self):
        # supervisor(python) -> job root(python) -> worker(python)
        return FakeProvider.from_tree(
            [
                (10, 0, "explorer.exe", 0),
                (20, 10, "python.exe", 0),
                (30, 20, "python.exe", 0),
                (40, 30, "python.exe", 0),
            ]
        )

    def test_stops_below_protected_supervisor(self):
        root = find_kill_root(
            40,
            provider=self._tree(),
            killable_names=frozenset(["python.exe"]),
            launcher_names=frozenset(),
            protected_pids=frozenset({20}),
        )
        assert root is not None
        assert root.pid == 30, "target the job subtree, not the supervisor"

    def test_unprotected_walk_up_still_reaches_the_top(self):
        # Pins the existing dispatcher behaviour: with no protection signal the
        # climb goes all the way up, which is what took a whole service down
        # when its supervisor was just another python.exe.
        root = find_kill_root(
            40,
            provider=self._tree(),
            killable_names=frozenset(["python.exe"]),
            launcher_names=frozenset(),
        )
        assert root is not None
        assert root.pid == 20

    def test_protected_offender_yields_no_root(self):
        assert (
            find_kill_root(
                20,
                provider=self._tree(),
                killable_names=frozenset(["python.exe"]),
                launcher_names=frozenset(),
                protected_pids=frozenset({20}),
            )
            is None
        )


class TestNeverKillCmdlinePatterns:
    """Name matching cannot express "this python.exe but not that one"."""

    def _rig(self, tmp_path, patterns):
        procs = [
            _FakeProc(pid=10, ppid=0, name="explorer.exe"),
            _FakeProc(
                pid=20, ppid=10, name="pythonw.exe", rss=900_000_000, cpu=90.0,
                cmdline=("pythonw.exe", "-m", "kiroshi", "tray"),
            ),
            _FakeProc(
                pid=30, ppid=10, name="python.exe", rss=10_000, cpu=50.0,
                cmdline=("python.exe", "burn.py"),
            ),
        ]
        provider = FakeProvider.from_procs(procs)
        cfg = _cfg(tmp_path, never_kill_cmdline_patterns=patterns)
        return Actuator(cfg, provider=provider, sleep=lambda _s: None), provider

    def test_without_pattern_the_tray_is_selected(self, tmp_path):
        actuator, _provider = self._rig(tmp_path, ())
        report = actuator.execute(_action("kill", signal="system.cpu_package_temp_c"))
        assert report.offender_pid == 20

    def test_pattern_spares_it(self, tmp_path):
        actuator, provider = self._rig(tmp_path, ("*-m kiroshi tray*",))
        report = actuator.execute(_action("kill", signal="system.cpu_package_temp_c"))
        assert report.offender_pid == 30
        touched = set(provider.terminated) | set(provider.killed)
        assert 20 not in touched

    def test_pattern_also_bounds_the_kill_root_walk_up(self, tmp_path):
        """A pattern-protected supervisor must stop the climb, not merely survive it.

        Applying the patterns only when filtering the final target list leaves
        the supervisor alive but still lets the walk-up return it as the kill
        root, so every sibling job underneath dies to shed the load of one.
        """
        procs = [
            _FakeProc(pid=10, ppid=0, name="explorer.exe"),
            _FakeProc(
                pid=20, ppid=10, name="python.exe", rss=500_000_000, cpu=2.0,
                cmdline=("python.exe", "-m", "kiroshi", "mcp", "--fixer", "auto"),
            ),
            _FakeProc(
                pid=30, ppid=20, name="python.exe", rss=10_000, cpu=95.0,
                cmdline=("python.exe", "job_a.py"),
            ),
            _FakeProc(
                pid=40, ppid=20, name="python.exe", rss=10_000, cpu=1.0,
                cmdline=("python.exe", "job_b.py"),
            ),
        ]
        provider = FakeProvider.from_procs(procs)
        cfg = _cfg(tmp_path, never_kill_cmdline_patterns=("*-m kiroshi mcp*",))
        actuator = Actuator(cfg, provider=provider, sleep=lambda _s: None)

        report = actuator.execute(_action("kill", signal="system.cpu_package_temp_c"))

        assert report.offender_pid == 30
        assert report.kill_root is not None
        assert report.kill_root.pid == 30
        touched = set(provider.terminated) | set(provider.killed)
        assert 20 not in touched, "supervisor was killed"
        assert 40 not in touched, "sibling job was collateral damage"


class TestClientRegistry:
    def _live(self, pid=20, create_time=1000.0):
        return FakeProvider.from_procs(
            [_FakeProc(pid=pid, ppid=0, name="python.exe", create_time=create_time)]
        )

    def test_configured_role_is_protected(self, tmp_path):
        _write_manifest(
            tmp_path, "coordinator-20.json",
            pid=20, role="coordinator", name="kiroshi", started_at=1000.0,
        )
        found = discover_protected(
            tmp_path, provider=self._live(), protected_roles=["coordinator"]
        )
        assert set(found) == {20}

    def test_unconfigured_role_is_not_protected(self, tmp_path):
        _write_manifest(
            tmp_path, "coordinator-20.json",
            pid=20, role="coordinator", name="kiroshi", started_at=1000.0,
        )
        found = discover_protected(
            tmp_path, provider=self._live(), protected_roles=["fixer"]
        )
        assert found == {}

    def test_manifest_may_opt_itself_in(self, tmp_path):
        # No operator configuration at all: the service declares itself.
        _write_manifest(
            tmp_path, "thing-20.json",
            pid=20, role="anything", name="kiroshi",
            started_at=1000.0, atfield_never_kill=True,
        )
        found = discover_protected(tmp_path, provider=self._live(), protected_roles=[])
        assert set(found) == {20}

    def test_manifest_outliving_its_process_is_ignored(self, tmp_path):
        _write_manifest(
            tmp_path, "coordinator-20.json",
            pid=20, role="coordinator", name="kiroshi", started_at=1000.0,
        )
        found = discover_protected(
            tmp_path, provider=FakeProvider.from_procs([]),
            protected_roles=["coordinator"],
        )
        assert found == {}, "stale manifests must not protect anything"

    def test_recycled_pid_is_not_protected(self, tmp_path):
        # Same PID, but the live process started long after the manifest.
        _write_manifest(
            tmp_path, "coordinator-20.json",
            pid=20, role="coordinator", name="kiroshi", started_at=1000.0,
        )
        found = discover_protected(
            tmp_path,
            provider=self._live(create_time=9_000_000.0),
            protected_roles=["coordinator"],
        )
        assert found == {}

    def test_malformed_manifest_is_skipped(self, tmp_path):
        d = Path(tmp_path) / "clients" / "kiroshi"
        d.mkdir(parents=True, exist_ok=True)
        (d / "junk.json").write_text("{not json", encoding="utf-8")
        found = discover_protected(
            tmp_path, provider=self._live(), protected_roles=["coordinator"]
        )
        assert found == {}

    def test_missing_state_dir_is_harmless(self, tmp_path):
        found = discover_protected(
            Path(tmp_path) / "nope", provider=self._live(), protected_roles=["x"]
        )
        assert found == {}


class TestSupervisorSurvivesThermalEvent:
    """End-to-end cover for the 2026-08-05 mis-targeted kill.

    A CPU thermal rule fired while a job subtree was hot under a registered
    coordinator. The coordinator must survive; only the job dies.
    """

    def _rig(self, tmp_path):
        procs = [
            _FakeProc(pid=10, ppid=0, name="explorer.exe"),
            _FakeProc(
                pid=20, ppid=10, name="python.exe", rss=3_000_000_000, cpu=0.2,
                create_time=1000.0,
                cmdline=("python.exe", "kiroshi", "coordinator"),
            ),
            _FakeProc(
                pid=30, ppid=20, name="python.exe", rss=50_000_000, cpu=97.0,
                cmdline=("python.exe", "job.py"),
            ),
            _FakeProc(
                pid=40, ppid=30, name="python.exe", rss=20_000_000, cpu=95.0,
                cmdline=("python.exe", "job.py"),
            ),
        ]
        provider = FakeProvider.from_procs(procs)
        _write_manifest(
            tmp_path, "coordinator-20.json",
            pid=20, role="coordinator", name="kiroshi", started_at=1000.0,
        )
        cfg = _cfg(tmp_path, protected_client_roles=("coordinator",))
        return Actuator(cfg, provider=provider, sleep=lambda _s: None), provider

    def test_kills_the_job_not_the_coordinator(self, tmp_path):
        actuator, provider = self._rig(tmp_path)
        report = actuator.execute(_action("kill", signal="system.cpu_package_temp_c"))
        assert report.kill_root is not None
        assert report.kill_root.pid == 30
        touched = set(provider.terminated) | set(provider.killed)
        assert 20 not in touched, "the coordinator must survive"
        assert {30, 40} <= touched, "the whole job subtree should go"

    def test_protection_holds_for_memory_rules_too(self, tmp_path):
        actuator, provider = self._rig(tmp_path)
        # The coordinator is the fattest process, so RSS ranking would pick it.
        report = actuator.execute(_action("kill", signal="system.ram_used_percent"))
        assert report.offender_pid == 30
        assert 20 not in set(provider.terminated) | set(provider.killed)

    def test_throttle_also_spares_the_coordinator(self, tmp_path):
        actuator, provider = self._rig(tmp_path)
        try:
            actuator.execute(_action("throttle", signal="system.cpu_package_temp_c"))
            assert 20 not in provider.suspended
            assert 30 in provider.suspended
        finally:
            actuator.shutdown()


# ---------------------------------------------------------------------------
# Per-process RSS cap and owner escalation (2026-08-30 incident)
# ---------------------------------------------------------------------------


GB = 1024 ** 3


class TestProcessesOverRssCap:
    """The pure selector: is ONE process unreasonable, regardless of the machine."""

    def test_disabled_cap_selects_nothing(self):
        procs = [ProcInfo(pid=1, ppid=0, name="python.exe", cmdline=(), rss_bytes=99 * GB)]
        assert processes_over_rss_cap(procs, 0) == []

    def test_selects_only_over_cap_largest_first(self):
        procs = [
            ProcInfo(pid=1, ppid=0, name="python.exe", cmdline=(), rss_bytes=2 * GB),
            ProcInfo(pid=2, ppid=0, name="python.exe", cmdline=(), rss_bytes=67 * GB),
            ProcInfo(pid=3, ppid=0, name="python.exe", cmdline=(), rss_bytes=9 * GB),
        ]
        got = processes_over_rss_cap(procs, 8 * GB)
        assert [p.pid for p in got] == [2, 3], "largest first, under-cap excluded"

    def test_boundary_is_strict(self):
        """Exactly at the cap is not over it -- a cap you cannot sit on is a trap."""
        procs = [ProcInfo(pid=1, ppid=0, name="python.exe", cmdline=(), rss_bytes=8 * GB)]
        assert processes_over_rss_cap(procs, 8 * GB) == []


class TestRssCapEnforcement:
    def _actuator(self, edges, cap_gb):
        provider = FakeProvider.from_tree(edges)
        cfg = default_config()
        cfg = replace(cfg, kill=replace(cfg.kill, max_process_rss_gb=cap_gb))
        return Actuator(cfg, provider=provider, sleep=lambda _s: None), provider

    def test_off_by_default(self):
        cfg = default_config()
        assert cfg.kill.max_process_rss_gb == 0.0, "a cap must be opt-in per machine"

    def test_kills_the_hog_and_leaves_siblings(self):
        """One failed unit of work, not a dead job -- the whole point."""
        actuator, provider = self._actuator(
            [
                (10, 0, "python.exe", 1 * GB),     # runner
                (11, 10, "python.exe", 67 * GB),   # the hog
                (12, 10, "python.exe", 2 * GB),    # innocent sibling
            ],
            cap_gb=8.0,
        )
        reports = actuator.enforce_rss_cap()
        assert len(reports) == 1
        assert reports[0].offender_pid == 11
        assert 11 in provider.killed
        assert 12 not in provider.killed, "sibling worker must survive"
        assert 10 not in provider.killed, "must NOT walk up to the runner"

    def test_no_grace_window_for_over_cap(self):
        """Over-cap is already pathological; do not wait politely while it grows."""
        actuator, provider = self._actuator(
            [(10, 0, "python.exe", 67 * GB)], cap_gb=8.0
        )
        actuator.enforce_rss_cap()
        assert provider.killed == [10]
        assert provider.terminated == [], "should go straight to kill"


class TestOwnerEscalation:
    """Killing a respawnable child of a protected owner achieves nothing."""

    def _setup(self, tmp_path, *, can_stop=True, never_kill=False):
        import json as _json

        state = tmp_path / "state"
        clients = state / "clients" / "kiroshi"
        clients.mkdir(parents=True)
        manifest = {
            "pid": 10,
            "role": "runner",
            "name": "kiroshi",
            "started_at": 0.0,
        }
        if can_stop:
            manifest["control"] = {"graceful_stop": "drop a '<role>-<pid>.stop' file"}
        if never_kill:
            manifest["atfield_never_kill"] = True
        (clients / "runner-10.json").write_text(_json.dumps(manifest), encoding="utf-8")

        # runner(10) owns worker(11); worker is what a RAM rule will pick.
        provider = FakeProvider.from_tree(
            [(10, 0, "python.exe", 1 * GB), (11, 10, "python.exe", 60 * GB)]
        )
        for p in provider.procs.values():
            p.create_time = 0.0
        cfg = default_config()
        cfg = replace(
            cfg,
            general=replace(cfg.general, state_dir=state),
            targeting=replace(cfg.targeting, protected_client_roles=("runner",)),
        )
        return Actuator(cfg, provider=provider, sleep=lambda _s: None), provider, clients

    @staticmethod
    def _respawn(provider, ppid=10):
        """A self-healing pool replaces the worker we just killed.

        Without this the fake tree has no offender left after one kill and the
        test would prove nothing -- respawning IS the behaviour that made the
        real incident unwinnable.
        """
        new_pid = max(provider.procs) + 1
        proc = _FakeProc(pid=new_pid, ppid=ppid, name="python.exe", rss=60 * GB)
        proc.create_time = 0.0
        provider.procs[new_pid] = proc
        return new_pid

    def test_first_kill_spares_the_owner(self, tmp_path):
        actuator, provider, clients = self._setup(tmp_path)
        actuator.execute(_action())
        assert 11 in provider.killed or 11 in provider.terminated
        assert 10 not in provider.killed, "owner is protected on the first pass"
        assert not list(clients.glob("*.stop")), "too early to ask it to stop"

    def test_second_futile_kill_asks_the_owner_to_stop(self, tmp_path):
        actuator, provider, clients = self._setup(tmp_path)
        actuator.execute(_action())
        self._respawn(provider)
        actuator.execute(_action())
        stops = list(clients.glob("runner-10.stop"))
        assert stops, "must use the control channel the manifest advertised"
        assert 10 not in provider.killed, "ask before shooting"

    def test_third_futile_kill_takes_the_owner_down(self, tmp_path):
        actuator, provider, clients = self._setup(tmp_path)
        for _ in range(3):
            actuator.execute(_action())
            self._respawn(provider)
        assert 10 in provider.killed, (
            "a supervisor that keeps respawning through kills IS the problem"
        )

    def test_explicit_never_kill_is_never_overridden(self, tmp_path):
        actuator, provider, clients = self._setup(tmp_path, never_kill=True)
        for _ in range(4):
            actuator.execute(_action())
            self._respawn(provider)
        assert 10 not in provider.killed, (
            "atfield_never_kill is the client's hard refusal, not a preference"
        )

    def test_owner_without_stop_channel_still_escalates(self, tmp_path):
        """No control channel is a reason to escalate sooner, not to give up."""
        actuator, provider, clients = self._setup(tmp_path, can_stop=False)
        for _ in range(3):
            actuator.execute(_action())
            self._respawn(provider)
        assert not list(clients.glob("*.stop"))
        assert 10 in provider.killed


class TestHardCeiling:
    def test_above_ceiling_skips_the_grace_window(self):
        """At 97% a 5-second courtesy window is most of the time remaining."""
        provider = FakeProvider.from_tree([(10, 0, "python.exe", 1 * GB)])
        cfg = default_config()
        cfg = replace(cfg, kill=replace(cfg.kill, hard_ceiling_percent=96.0))
        actuator = Actuator(cfg, provider=provider, sleep=lambda _s: None)
        actuator.execute(_action())          # latest_value 95.0 -> below ceiling
        assert provider.terminated == [10], "below the ceiling, still graceful"

        provider2 = FakeProvider.from_tree([(20, 0, "python.exe", 1 * GB)])
        actuator2 = Actuator(cfg, provider=provider2, sleep=lambda _s: None)
        hot = replace(_action(), latest_value=97.3)
        actuator2.execute(hot)
        assert provider2.killed == [20]
        assert provider2.terminated == [], "above the ceiling, no grace"
