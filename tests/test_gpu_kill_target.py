"""A GPU rule kills only a process with MEASURED GPU use, or nobody (0.4.23, issue #2).

2026-10-18 12:45 on Chronos: `gpu.1.mem_junction_temp_c` (an LHM signal) crossed
vram-junction-hot. Its index cannot be mapped to an NVML device, so the service passed no
candidates and the actuator fell back to the "memory" axis -- and killed the fattest-RSS
python on the host, an idle viewer, while the heater (both cards at 99%) kept running
until its owner stopped it 50 s later. Pinned through run_service with the REAL Actuator
over tests/test_actuator.FakeProvider; the NVML collector is a fake whose
`process_utilization()` returns {nvml_idx: {pid: mean SM %}}.
"""
import json
import logging

import atfield.service as svc
from atfield import agent_inbox as ai
from atfield import reporter
from atfield.actuator import Actuator
from atfield.collectors import HealthState, ProbeResult
from atfield.signals import Sample, monotonic_ns
from tests.test_actuator import FakeProvider, _FakeProc

LHM_SIG, NVML_SIG = "gpu.1.mem_junction_temp_c", "gpu.1.core_temp_c"
GB = 1 << 30
SA = {"CLAUDE_CODE_MESSAGING_SOCKET": r"\\.\pipe\LOCAL\cc-msg-a",
      "CLAUDE_CODE_MESSAGING_TOKEN": "tok-A", "CLAUDE_CODE_SESSION_ID": "aaaaaaaa-1111"}
SB = {"CLAUDE_CODE_MESSAGING_SOCKET": r"\\.\pipe\LOCAL\cc-msg-b",
      "CLAUDE_CODE_MESSAGING_TOKEN": "tok-B", "CLAUDE_CODE_SESSION_ID": "bbbbbbbb-2222"}
VIEWER, HEATER = 41144, 500


def procs(heater_name="python.exe"):
    """The idle viewer is FAT (smplx + torch); the heater is lean. Different sessions."""
    return [_FakeProc(10, 0, "explorer.exe"),
            _FakeProc(VIEWER, 10, "python.exe", rss=5 * GB, cpu=1.0, env=SB),
            _FakeProc(HEATER, 10, heater_name, rss=100 << 20, cpu=100.0, env=SA),
            _FakeProc(600, 10, "python.exe", rss=200 << 20, cpu=3.0, env=SA)]


def rule(name, signal, threshold, action, **kw):
    lines = ["[[rules]]", f'name = "{name}"', f'signal = "{signal}"', f"threshold = {threshold}",
             "window_s = 1", "min_fraction_over = 0.5", f'action = "{action}"']
    return "\n".join(lines + [f"{k} = {json.dumps(v)}" for k, v in kw.items()])


def run(tmp_path, monkeypatch, signal, use, *, procs_=None, action="kill", notify=False):
    """One run_service pass with `signal` at 92 C. `use` is what the fake NVML collector's
    process_utilization() returns (None = not measurable). Returns (provider, posts,
    kill_report payloads, caplog-free warnings list)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    state = tmp_path / "state"
    provider = FakeProvider.from_procs(procs_ or procs())

    class Lhm:
        name = "lhm"

        def probe(self):
            return ProbeResult(available=True, reason="fake", signals=(LHM_SIG,))

        def health(self):
            return HealthState.HEALTHY

        def sample(self):
            return {LHM_SIG: Sample(92.0, monotonic_ns(), self.name, "celsius")}

    class Nvml:
        name = "nvml"

        def probe(self):
            return ProbeResult(available=True, reason="fake", signals=(NVML_SIG,))

        def health(self):
            return HealthState.HEALTHY

        def sample(self):
            return {NVML_SIG: Sample(92.0 if signal == NVML_SIG else 40.0, monotonic_ns(),
                                     self.name, "celsius")}

        def process_utilization(self, window_s=10.0):
            return use

    cs = [Lhm(), Nvml()]
    posts, sent = [], []
    monkeypatch.setattr(svc, "_probe_all_collectors",
                        lambda audit: (cs, {c.name: c.probe() for c in cs}))
    monkeypatch.setattr(svc, "Actuator", lambda cfg: Actuator(cfg, provider=provider,
                                                              sleep=lambda _s: None))
    monkeypatch.setattr(ai, "post", lambda inbox, text, **kw:
                        posts.append((inbox.session_id, text)) or True)
    monkeypatch.setattr(ai, "_LIMIT", ai.RateLimit(600))
    monkeypatch.setattr(reporter, "_webhooks", lambda sd: ["http://x/atfield/event"])
    monkeypatch.setattr(reporter, "_ensure_worker", lambda: None)

    class Q:
        def put_nowait(self, item):
            sent.append(item[1])
    monkeypatch.setattr(reporter, "_send_queue", Q())
    extra = {"notify": True} if notify else {}
    cfg = tmp_path / "config.toml"
    cfg.write_text(f"[general]\ntick_hz = 10\nstate_dir = {json.dumps(str(state))}\n\n"
                   "[api]\nenabled = false\n\n" + rule("gpu-hot", signal, 90.0, action, **extra),
                   encoding="utf-8")
    svc.run_service(config_path=cfg, state_dir=state, max_ticks=15)
    killed = set(provider.killed) | set(provider.terminated)
    return killed, posts, [p for p in sent if p.get("type") == "kill_report"]


def test_t1_the_incident_kills_nobody_when_no_gpu_use_is_measured(tmp_path, monkeypatch):
    killed, posts, payloads = run(tmp_path, monkeypatch, LHM_SIG, {0: {}, 1: {}})
    assert killed == set(), "an idle context-holder (or the fattest process) was killed"
    assert payloads and all(p["kill_root"] is None for p in payloads)
    assert "source not identified" in payloads[0]["skipped_reason"]
    assert posts == [] and not [o for p in payloads for o in p["owners"] if o["notified"]]


def test_t2_a_real_heater_is_killed_and_the_fat_viewer_is_not(tmp_path, monkeypatch):
    for sig, use in ((NVML_SIG, {0: {}, 1: {HEATER: 97.0}}),
                     (LHM_SIG, {0: {}, 1: {HEATER: 97.0}})):
        killed, _, payloads = run(tmp_path / sig, monkeypatch, sig, use)
        assert HEATER in killed and VIEWER not in killed, sig
        assert payloads[0]["kill_root"]["pid"] == HEATER


def test_t3_lhm_index_is_unmapped_but_nvml_index_is_device_scoped(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    killed, _, _ = run(tmp_path / "lhm", monkeypatch, LHM_SIG, {0: {HEATER: 97.0}, 1: {}})
    assert HEATER in killed, "the LHM-indexed rule must take the top user on ANY device"
    assert "cannot be mapped to an NVML device" in caplog.text
    killed, _, payloads = run(tmp_path / "nvml", monkeypatch, NVML_SIG, {0: {HEATER: 97.0}, 1: {}})
    assert killed == set(), "an NVML-indexed rule must look only at its own device"
    assert "source not identified" in payloads[0]["skipped_reason"]


def test_t4_below_the_floor_nobody_dies_and_the_reason_names_the_top_user(tmp_path, monkeypatch):
    killed, _, payloads = run(tmp_path, monkeypatch, NVML_SIG, {1: {HEATER: 15.0, VIEWER: 2.0}})
    assert killed == set()
    r = payloads[0]["skipped_reason"]
    assert "source not identified" in r and f"pid {HEATER}" in r and "15%" in r


def test_t5_the_loader_or_nobody(tmp_path, monkeypatch):
    killed, _, payloads = run(tmp_path, monkeypatch, NVML_SIG, {1: {HEATER: 97.0, 600: 30.0}},
                              procs_=procs(heater_name="blender.exe"))
    assert killed == set(), "a python at 30% must not die in the loader's place"
    r = payloads[0]["skipped_reason"]
    assert f"pid {HEATER}" in r and "blender.exe" in r and "not in killable_names" in r


def test_t6_a_gpu_warning_ranks_owners_by_gpu_use(tmp_path, monkeypatch):
    _, posts, payloads = run(tmp_path, monkeypatch, NVML_SIG, {1: {HEATER: 97.0}},
                             action="log", notify=True)
    assert [sid for sid, _ in posts] == ["aaaaaaaa-1111"]
    assert "97% GPU" in posts[0][1]
    owners = {o["id"]: (o["share"], o["notified"]) for o in payloads[0]["owners"]}
    assert owners == {"aaaaaaaa": (1.0, True), "bbbbbbbb": (0.0, False)}


def test_t7_unmeasurable_gpu_use_is_a_skip_not_an_rss_kill(tmp_path, monkeypatch):
    killed, _, payloads = run(tmp_path, monkeypatch, LHM_SIG, None)
    assert killed == set()
    assert "not measurable" in payloads[0]["skipped_reason"]


# -- P2: the collector method -----------------------------------------------------------

class _NotFound(Exception):
    pass


class _S:
    def __init__(self, pid, sm, ts):
        self.pid, self.smUtil, self.timeStamp = pid, sm, ts


def _collector(per_handle):
    from atfield.collectors.nvml import NvmlCollector

    class Py:
        NVMLError_NotFound = _NotFound

        @staticmethod
        def nvmlDeviceGetProcessUtilization(h, since):
            r = per_handle[h]
            if isinstance(r, Exception):
                raise r
            return [s for s in r]

    c = NvmlCollector.__new__(NvmlCollector)
    c._pynvml, c._handles, c._health = Py(), list(per_handle), HealthState.HEALTHY
    return c


def test_process_utilization_averages_per_pid_and_reads_notfound_as_no_samples():
    import time
    now = int(time.time() * 1e6)
    c = _collector({0: [_S(7, 100, now - 2_000_000), _S(7, 0, now - 1_000_000),
                        _S(8, 50, now - 60_000_000)],          # older than the window
                    1: _NotFound()})
    assert c.process_utilization(window_s=10.0) == {0: {7: 50.0}, 1: {}}


def test_process_utilization_is_none_when_unsupported():
    from atfield.collectors.nvml import NvmlCollector
    c = NvmlCollector.__new__(NvmlCollector)
    c._pynvml, c._handles, c._health = object(), [0], HealthState.HEALTHY
    assert c.process_utilization() is None


def test_t6b_a_gpu_user_under_the_floor_is_not_told(tmp_path, monkeypatch):
    _, posts, payloads = run(tmp_path, monkeypatch, NVML_SIG, {1: {HEATER: 15.0}},
                             action="log", notify=True)
    assert posts == [] and not [o for p in payloads for o in p["owners"] if o["notified"]]
