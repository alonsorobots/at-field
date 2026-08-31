"""Per-tick work must fit the tick budget, measured against a known workload.

WHY THIS FILE EXISTS. On 2026-08-30 a new per-tick process scan stretched the
service loop from 1.0 Hz to 0.22 Hz. Nothing failed, nothing logged, and
``/health`` went on reporting ``mode: armed, rules_active: 7``. But
``min_samples`` is sized from the CONFIGURED tick rate, so every kill rule
quietly became unsatisfiable: ``cpu-pkg-hot`` needed 21 samples from a 30 s
window that now held 7. A CPU ran at >=90 C for 719 of 766 samples in an hour,
peaking 95.62 C, and the 90 C rule never fired on any of three machines.

The whole failure is a PERFORMANCE regression with no functional symptom, so no
functional test could have caught it. These are the missing kind: a synthetic
job of known size, timed, with a budget that fails the build.

Two layers, because they catch different mistakes:

  * ``TestComponentBudgets`` times the individual operations the tick performs,
    against a synthetic process table of realistic size. Deterministic, no
    hardware dependency -- this is the layer that says "this operation is too
    expensive to do per tick" before it ever ships.
  * ``TestAchievableTickRate`` closes the loop that actually bit us: given the
    cadence a machine achieves, can the configured rules still fire at all?
    That is the question nobody was asking.
"""
import time

import pytest

from atfield.actuator import Actuator, ProcInfo, processes_over_rss_cap
from atfield.config import load_config_from_dict
from atfield.policy import PolicyEngine
from atfield.signals import Sample
from tests.test_actuator import FakeProvider, _FakeProc

# The service sleeps `tick_period - elapsed`, so any per-tick work that reaches
# the period IS the tick rate. Budget the work at a fraction of the period so
# there is headroom for collectors, evaluation and dispatch.
TICK_PERIOD_S = 1.0
PER_OP_BUDGET_S = 0.25

# Chronos had ~400 processes when the tick collapsed. Size the synthetic table
# to the machine that actually failed, not to a toy.
N_PROCESSES = 400


def _fake_processes(n=N_PROCESSES):
    return [
        ProcInfo(
            pid=1000 + i,
            ppid=4,
            name="python.exe" if i % 3 == 0 else "svchost.exe",
            cmdline=("C:\\python.exe", f"worker_{i}.py", "--shard", str(i)),
            rss_bytes=(i % 40) * (1024 ** 3) // 10,
            create_time=float(i),
        )
        for i in range(n)
    ]


def _provider(n=N_PROCESSES, hog_gb=0):
    """A process table that costs nothing to read, so the TEST measures OUR code."""
    procs = [
        _FakeProc(pid=1000 + i, ppid=4,
                  name="python.exe" if i % 3 == 0 else "svchost.exe",
                  rss=(i % 40) * (1024 ** 3) // 10)
        for i in range(n)
    ]
    if hog_gb:
        procs.append(_FakeProc(pid=9999, ppid=4, name="python.exe",
                               rss=hog_gb * (1024 ** 3)))
    return FakeProvider.from_procs(procs, own=4)


def _cfg(**kill):
    return load_config_from_dict({
        "general": {"tick_hz": 1},
        "kill": {"max_process_rss_gb": 32.0, **kill},
        "rules": [{
            "name": "cpu-pkg-hot",
            "signal": "system.cpu_package_temp_c",
            "threshold": 90.0,
            "window_s": 30,
            "min_fraction_over": 0.67,
            "action": "kill",
        }],
    })


def _median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2]


class TestComponentBudgets:
    def test_rss_cap_selection_is_cheap(self):
        procs = _fake_processes()
        cap = 32 * (1024 ** 3)
        ts = []
        for _ in range(20):
            t0 = time.perf_counter()
            processes_over_rss_cap(procs, cap)
            ts.append(time.perf_counter() - t0)
        assert _median(ts) < PER_OP_BUDGET_S

    def test_rss_cap_does_not_walk_the_process_table_every_tick(self):
        """THE REGRESSION, as a behavioural assertion.

        The scan itself is what cost 0.70 s median / 4.32 s worst on real
        psutil. Counting walks is the hardware-independent way to assert it:
        at 1 Hz over 10 s the cap must scan a handful of times, not ten.
        """
        prov = _provider()
        walks = {"n": 0}
        inner = prov.list_all

        def counting():
            walks["n"] += 1
            return inner()

        prov.list_all = counting        # type: ignore[method-assign]
        act = Actuator(_cfg(), provider=prov)
        t0 = 10 * 1_000_000_000
        for i in range(10):                      # 10 ticks == 10 simulated seconds
            act.enforce_rss_cap(now_ns=t0 + i * 1_000_000_000)
        assert walks["n"] <= 3, (
            f"the RSS cap walked the process table {walks['n']} times "
            f"in 10 ticks; on real psutil that is {walks['n'] * 0.70:.1f}s "
            f"of a {10 * TICK_PERIOD_S:.0f}s budget and is what disarmed every "
            f"kill rule on 2026-08-30"
        )

    def test_rss_cap_still_catches_an_offender(self):
        """Rate-limiting must not cost the cap its actual job."""
        act = Actuator(_cfg(), provider=_provider(n=20, hog_gb=67))
        assert act.enforce_rss_cap(now_ns=10 * 1_000_000_000)

    def test_policy_tick_is_cheap(self):
        eng = PolicyEngine(_cfg(), available_signals={"system.cpu_package_temp_c"})
        ts = []
        now = 10 * 1_000_000_000
        for _ in range(50):
            s = Sample(70.0, now, "test", "celsius")
            t0 = time.perf_counter()
            eng.tick({"system.cpu_package_temp_c": s}, now_ns=now)
            ts.append(time.perf_counter() - t0)
            now += 1_000_000_000
        assert _median(ts) < PER_OP_BUDGET_S


class TestAchievableTickRate:
    """Can the configured rules fire at the cadence the loop achieves?

    This is the check whose absence let a 90 C rule sit inert for an hour on
    three machines while every dashboard reported it armed.
    """

    @pytest.mark.parametrize("hz", [1.0, 0.5, 0.25, 0.1])
    def test_rules_remain_satisfiable_at_any_cadence(self, hz):
        eng = PolicyEngine(_cfg(), available_signals={"system.cpu_package_temp_c"})
        interval_ns = int(1_000_000_000 / hz)
        now = 10 * 1_000_000_000
        fired = []
        for _ in range(int(300 * hz) + 5):
            s = Sample(95.6, now, "test", "celsius")
            fired.extend(eng.tick({"system.cpu_package_temp_c": s}, now_ns=now))
            now += interval_ns
        assert fired, (
            f"at {hz} Hz a sustained 95.6 C never tripped a 90 C kill rule -- "
            f"the guard is inert, which is the 2026-08-30 failure"
        )
        assert eng.rules_unable_to_fire == ()
