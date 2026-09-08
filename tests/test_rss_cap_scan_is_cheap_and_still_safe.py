"""Making the RSS-cap scan fast must not make it blind.

MEASURED on Chronos, elevated (how the service actually runs), cold process
list, best of 3, n=466:

    name+ppid            21.6 ms
    memory_info().rss    10.9 ms
    create_time()         0.9 ms
    cmdline()          2059.9 ms     <- 98.5% of the walk
    ALL               2091.4 ms
    without cmdline      32.7 ms     <- 64x faster

The live consequence: `SLOW TICK: 4367.0ms against a 1000.0ms budget --
slowest phase rss_cap at 4364.6ms`, 6,557 times, holding Chronos at 0.36 Hz
against a configured 1 Hz and leaving its thermal rules at 2.4x margin --
on the machine that already ran an hour at Tjmax when this same component
starved the loop to 0.22 Hz.

Privilege level INVERTS this. Unelevated, cmdline() costs 12.6 ms and
memory_info() costs 1098 ms; a fix designed from an ordinary shell would have
removed exactly the wrong call and made the walk 23x worse. That is why these
numbers are quoted with their context.

WHAT MAKES THIS DANGEROUS TO OPTIMISE. `cmdline` is not decoration: it is a
SAFETY FILTER. `_is_killable()` applies `never_kill_cmdline_patterns` (added in
4e2ba76 so a protected launcher bounds the kill-root walk-up), and the RSS cap
kills a process *and every descendant*. Dropping cmdline from the scan to make
it fast would silently un-protect everything that filter exists for.

So the scan goes lazy, not blind: cheap fields for the whole list, then cmdline
fetched only for the handful actually over the cap, and the never-kill filter
applied there. Same decisions, ~64x less work.

These tests pin the SAFETY half. The speed half is proved on the host, by
`SLOW TICK` going quiet -- a unit test runs unelevated and would measure the
inverted numbers above.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dataclasses import replace  # noqa: E402

from atfield.actuator import Actuator, ProcInfo  # noqa: E402
from atfield.config import default_config  # noqa: E402

CAP_GB = 4.0
OVER = int(5 * 1024 ** 3)      # over a 4 GB cap
UNDER = int(1 * 1024 ** 3)


# Reuse the repo's own provider double rather than re-implementing the
# ProcessProvider protocol -- it already tracks terminate/kill ordering and
# stays in step with the protocol as that grows.
sys.path.insert(0, str(Path(__file__).parent))
from test_actuator import FakeProvider  # noqa: E402


class _LazyProvider(FakeProvider):
    """FakeProvider that models a CHEAP scan: `list_all()` returns entries with
    an EMPTY cmdline, and `get()` is the only way to obtain one.

    That mirrors what the real change does -- the sweep skips the 2,060 ms of
    cmdline reads -- and lets the test see exactly which processes were paid
    for, via `cmdline_fetched`.
    """

    def __init__(self, procs: list[ProcInfo]):
        super().__init__()
        self._full = {p.pid: p for p in procs}
        self._procs = dict(self._full)
        self.cmdline_fetched: list[int] = []

    def list_all(self) -> list[ProcInfo]:
        return [ProcInfo(pid=p.pid, ppid=p.ppid, name=p.name, cmdline=(),
                         rss_bytes=p.rss_bytes) for p in self._full.values()]

    def list_all_lean(self) -> list[ProcInfo]:
        return self.list_all()

    def get(self, pid: int):
        # Count only OUR synthetic processes. _protected_pids() also calls
        # get() for real system pids discovered from client manifests, and
        # attributing those to the RSS scan would make this assertion
        # measure the wrong thing entirely.
        if pid in self._full:
            self.cmdline_fetched.append(pid)
        return self._full.get(pid)

    def descendants(self, pid):
        return []


def _cfg(never_cmdline=()):
    """killable_names / never_kill_cmdline_patterns live on TargetingConfig,
    not KillConfig; max_process_rss_gb is the one on KillConfig."""
    base = default_config()
    return replace(
        base,
        targeting=replace(base.targeting,
                          killable_names=("python.exe",),
                          never_kill_cmdline_patterns=tuple(never_cmdline)),
        kill=replace(base.kill, max_process_rss_gb=CAP_GB),
    )


def _actuator(procs, *, never_cmdline=()):
    prov = _LazyProvider(procs)
    return Actuator(_cfg(never_cmdline), provider=prov, sleep=lambda _s: None), prov


def test_a_process_over_the_cap_is_still_killed():
    """The cap must keep working -- speed is not the only thing being tested."""
    act, prov = _actuator([
        ProcInfo(pid=100, ppid=1, name="python.exe",
                 cmdline=("python.exe", "train.py"), rss_bytes=OVER),
    ])
    reports = act.enforce_rss_cap()
    assert reports, "a process over the cap was not killed"
    assert 100 in prov.killed


def test_a_NEVER_KILL_process_over_the_cap_is_STILL_SPARED():
    """THE SAFETY GATE.

    `never_kill_cmdline_patterns` is matched against the cmdline. Under a lazy
    scan the cmdline is absent during the sweep, so a naive optimisation drops
    this filter entirely and kills a process it promised never to touch --
    along with every descendant.
    """
    act, prov = _actuator(
        [ProcInfo(pid=200, ppid=1, name="python.exe",
                  cmdline=("python.exe", "-m", "kiroshi.coordinator"),
                  rss_bytes=OVER)],
        # fnmatch against the JOINED cmdline, so the glob needs wildcards --
        # a bare substring never matches, which would have made this safety
        # assertion pass vacuously once the code was "fixed".
        never_cmdline=("*kiroshi.coordinator*",),
    )
    reports = act.enforce_rss_cap()
    assert prov.killed == [], (
        "killed a process matching never_kill_cmdline_patterns -- the lazy "
        "scan lost the safety filter")
    assert not reports


def test_cmdline_is_fetched_ONLY_for_processes_over_the_cap():
    """The whole point of the change: 466 processes must not cost 466 cmdline
    reads. Only the offenders do."""
    procs = [ProcInfo(pid=i, ppid=1, name="python.exe",
                      cmdline=("python.exe", f"p{i}.py"), rss_bytes=UNDER)
             for i in range(1, 40)]
    procs.append(ProcInfo(pid=999, ppid=1, name="python.exe",
                          cmdline=("python.exe", "hog.py"), rss_bytes=OVER))
    act, prov = _actuator(procs)
    act.enforce_rss_cap()
    assert 999 in prov.cmdline_fetched
    under_cap = [p for p in prov.cmdline_fetched if p != 999]
    assert not under_cap, (
        f"paid for cmdline on {len(under_cap)} processes under the cap; "
        "that is the 2,060 ms this change exists to remove")


def test_nothing_over_the_cap_means_no_cmdline_reads_at_all():
    """The common case, every tick, on a healthy machine: zero offenders,
    zero cmdline cost."""
    act, prov = _actuator([
        ProcInfo(pid=i, ppid=1, name="python.exe",
                 cmdline=("python.exe", f"p{i}.py"), rss_bytes=UNDER)
        for i in range(1, 40)
    ])
    assert act.enforce_rss_cap() == []
    assert prov.cmdline_fetched == []


def test_a_process_that_exits_between_scan_and_fetch_is_skipped():
    """The race the lazy fetch introduces: a process can be over the cap in the
    sweep and gone by the time its cmdline is read. That must be a no-op, not
    a crash inside the tick loop that also handles thermal emergencies."""
    class _Vanishing(_LazyProvider):
        def get(self, pid):
            self.cmdline_fetched.append(pid)
            return None      # gone

    prov = _Vanishing([ProcInfo(pid=300, ppid=1, name="python.exe",
                                cmdline=("python.exe", "x.py"), rss_bytes=OVER)])
    act = Actuator(_cfg(), provider=prov, sleep=lambda _s: None)
    assert act.enforce_rss_cap() == []
    assert prov.killed == []


def test_the_healthy_path_does_NO_full_scan_at_all():
    """The property that actually recovers the tick budget.

    `_protected_pids()` calls the FULL `list_all()` -- with cmdline -- whenever
    `never_kill_cmdline_patterns` is configured, and the candidate sweep used
    to call it a second time. Chronos has those patterns set and paid BOTH:
    measured `SLOW TICK: 4367.0ms against a 1000.0ms budget -- rss_cap
    4364.6ms`. DEMETER and Aurora have none configured, paid one walk, and ran
    at 0.78 Hz against Chronos's 0.36 Hz.

    With nothing over the cap -- which is every run in 1,812 recorded events
    across three hosts -- neither expensive call may happen.
    """
    class _Counting(_LazyProvider):
        def __init__(self, procs):
            super().__init__(procs)
            self.full_scans = 0
            self.lean_scans = 0

        def list_all(self):
            self.full_scans += 1
            return super().list_all()

        def list_all_lean(self):
            self.lean_scans += 1
            return super().list_all()

    prov = _Counting([
        ProcInfo(pid=i, ppid=1, name="python.exe",
                 cmdline=("python.exe", f"p{i}.py"), rss_bytes=UNDER)
        for i in range(1, 40)
    ])
    act = Actuator(_cfg(never_cmdline=("*kiroshi.coordinator*",)),
                   provider=prov, sleep=lambda _s: None)

    assert act.enforce_rss_cap() == []
    assert prov.lean_scans == 1, "the cheap sweep must still run"
    assert prov.full_scans == 0, (
        f"paid for {prov.full_scans} full cmdline walk(s) with nothing over "
        "the cap -- that is the 2,060 ms per walk this change exists to remove")
    assert prov.cmdline_fetched == []


def test_an_offender_still_pays_for_the_full_facts():
    """The refusing direction: cheap must not mean careless. Once something IS
    over the cap, the protected-pid set and the cmdline are consulted before
    anything is killed."""
    class _Counting(_LazyProvider):
        def __init__(self, procs):
            super().__init__(procs)
            self.full_scans = 0

        def list_all(self):
            self.full_scans += 1
            return super().list_all()

        def list_all_lean(self):
            return super().list_all()

    prov = _Counting([ProcInfo(pid=500, ppid=1, name="python.exe",
                               cmdline=("python.exe", "hog.py"), rss_bytes=OVER)])
    act = Actuator(_cfg(never_cmdline=("*kiroshi.coordinator*",)),
                   provider=prov, sleep=lambda _s: None)
    reports = act.enforce_rss_cap()
    assert reports and 500 in prov.killed
    assert prov.full_scans >= 1, "killed without consulting the protected set"
    assert 500 in prov.cmdline_fetched, "killed without reading its cmdline"
