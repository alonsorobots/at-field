"""pagefile-pressure must divide by the commit limit the host can REACH.

Measured 2026-09-24: DEMETER (125.6 GB RAM, SYSTEM-MANAGED page file, 8 GB
allocated, commit limit 133.6 GB) was killed -- 14 slots, host paused -- at 90 %
of today's limit, a limit Windows would have raised by growing the page file.
Chronos has a custom fixed 131,072 MB file: today's limit IS its ceiling.

Only the Windows reads are faked (GlobalMemoryStatusEx, the PagingFiles
registry value, the page file's size, the volume's free space); the collector's
real sample() path runs.
MUTATION: denominator back to today's limit -> test_system_managed... red.
MUTATION: drop the free-space bound -> test_system_managed_on_a_full_volume red.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
from collections import namedtuple

import pytest

from atfield.collectors import system as sysmod
from atfield.config import default_config

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows commit charge")

GiB = 1024 ** 3
THRESHOLD = next(r.threshold for r in default_config().rules if r.name == "pagefile-pressure")


class _FakeReg:
    HKEY_LOCAL_MACHINE = 0

    def __init__(self, paging):
        self.v = {"PagingFiles": list(paging), "ExistingPageFiles": ["\\??\\C:\\pagefile.sys"]}

    def OpenKey(self, *_a):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def QueryValueEx(self, _k, name):
        return self.v[name], 7


def _sample(monkeypatch, paging, *, ram_gb, limit_gb, committed_gb, file_gb, free_gb):
    def gmse(p):
        m = ctypes.cast(p, ctypes.POINTER(sysmod._MEMORYSTATUSEX)).contents
        m.ullTotalPhys = int(ram_gb * GiB)
        m.ullTotalPageFile = int(limit_gb * GiB)
        m.ullAvailPageFile = int((limit_gb - committed_gb) * GiB)
        return 1

    monkeypatch.setattr(ctypes.windll.kernel32, "GlobalMemoryStatusEx", gmse)
    monkeypatch.setitem(sys.modules, "winreg", _FakeReg(paging))
    monkeypatch.setattr(os.path, "getsize", lambda _p: file_gb * GiB)
    du = namedtuple("du", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda _d: du(0, 0, free_gb * GiB))
    c = sysmod.SystemCollector()
    s = c.sample()
    now = s.get("system.commit_percent_current_limit")
    return (s["system.commit_percent"].value, now and now.value,
            getattr(c, "commit_limit_basis", "<none>"))


DEMETER = dict(ram_gb=125.6, limit_gb=133.6, file_gb=8.0)


def test_system_managed_page_file_is_judged_against_what_it_can_grow_to(monkeypatch):
    pct, now_pct, basis = _sample(monkeypatch, ["?:\\pagefile.sys"], committed_gb=121.0,
                                  free_gb=500.0, **DEMETER)
    assert pct < THRESHOLD, (pct, basis)
    ceiling = 125.6 + 3 * 125.6                                    # RAM + max(3 x RAM, 4 GB)
    assert pct == pytest.approx(121.0 / ceiling * 100) and "system-managed" in basis
    assert now_pct == pytest.approx(121.0 / 133.6 * 100) and now_pct > THRESHOLD  # 90.6 %, kept


def test_system_managed_on_a_full_volume_can_only_grow_by_the_free_space(monkeypatch):
    # 2 GB free: ceiling = today's 133.6 + 2 = 135.6. (121 GB would be 89.2 % --
    # below 90 -- so 123 GB is committed here to sit over the line.)
    pct, _now, basis = _sample(monkeypatch, ["C:\\pagefile.sys 0 0"], committed_gb=123.0,
                                  free_gb=2.0, **DEMETER)
    assert pct == pytest.approx(123.0 / 135.6 * 100), basis
    assert pct > THRESHOLD


def test_custom_page_file_is_exactly_todays_formula(monkeypatch):
    # Chronos: c:\pagefile.sys 131072 131072 -- fixed, cannot grow.
    pct, now_pct, basis = _sample(monkeypatch, ["c:\\pagefile.sys 131072 131072"],
                                  ram_gb=126.0, limit_gb=254.0, committed_gb=231.0,
                                  file_gb=128.0, free_gb=100.0)
    assert pct == now_pct == pytest.approx(231.0 / 254.0 * 100)
    assert "custom" in basis


def test_unreadable_policy_falls_back_to_todays_limit(monkeypatch):
    def unreadable(*_a):
        raise OSError("simulated: access denied")

    monkeypatch.setattr(_FakeReg, "QueryValueEx", unreadable)
    pct, now_pct, basis = _sample(monkeypatch, ["?:\\pagefile.sys"], committed_gb=121.0,
                                  free_gb=500.0, **DEMETER)
    assert pct == now_pct and "current_limit" in basis
