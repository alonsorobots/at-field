"""Contract for the consumer of GET /events: the Kiroshi worker brake (P10) polls it and cuts a slot per kill_report."""
import http.client
import json
import time

from atfield.actuator import KilledProcess, KillReport, ProcInfo
from atfield.agent_inbox import Owner
from atfield.audit import AuditWriter
from atfield.http_api import ApiServer, ServiceState
from atfield.policy import Action


def _action(rule, signal, value):
    return Action(kind="kill", rule_name=rule, base_rule_name=rule, signal=signal, threshold=90.0,
                  fraction_over=1.0, samples_considered=20, latest_value=value, triggered_at_ns=0,
                  cooldown_seconds=60)


def test_kill_and_gpu_skip_reports_carry_what_kiroshi_reads(tmp_path):
    audit = AuditWriter(tmp_path)
    root = ProcInfo(pid=4242, ppid=1, name="python.exe", cmdline=("python.exe", "slot.py"))
    worker = Owner("kiroshi_worker", "DEMETER-4c030c", share=None, pids=1, job="handgap-v2",
                   slots=22, notified=True)
    audit.write_kill_report(KillReport(                      # (a) a successful kill
        action=_action("cpu-pkg-hot", "system.cpu_package_temp_c", 93.0), offender_pid=4242,
        kill_root=root, killed=(KilledProcess(root, "terminate", False),), owners=(worker,)))
    time.sleep(0.05)
    since = time.time()
    time.sleep(0.05)
    audit.write_kill_report(KillReport(                      # (b) a 0.4.23 GPU skip
        action=_action("vram-junction-hot", "gpu.1.mem_junction_temp_c", 92.0), offender_pid=None,
        kill_root=None, skipped_reason="no process with measured GPU use >= 20% (source not "
        "identified; 0 measured, top: none)"))

    state = ServiceState(version="0.0.0-test", observe_only=False, events_path=audit.path,
                         watchdog_log_path=tmp_path / "w.log", state_dir=tmp_path)
    srv = ApiServer(state, host="127.0.0.1", port=0)
    srv.start()
    host, port = srv.address

    def get(path):
        c = http.client.HTTPConnection(host, port, timeout=2.0)
        c.request("GET", path)
        return json.loads(c.getresponse().read())
    try:
        both = get("/events?limit=200")["events"]
        after = get(f"/events?since={since}&limit=200")["events"]
        last = get("/events?limit=1")["events"]
    finally:
        srv.stop()

    assert [e["rule"] for e in both] == ["cpu-pkg-hot", "vram-junction-hot"]
    for e in both:
        assert e["type"] == "kill_report"
        assert isinstance(e["signal"], str) and isinstance(e["ts"], float)
        assert isinstance(e["ts_iso"], str)
        assert e["kill_root"] is None or isinstance(e["kill_root"]["pid"], int)
        assert isinstance(e["killed"], list) and isinstance(e["succeeded"], bool)
        assert e["skipped_reason"] is None or isinstance(e["skipped_reason"], str)
        assert isinstance(e["owners"], list)
        for o in e["owners"]:
            assert {"kind", "id", "share", "notified"} <= set(o), o
    kill, skip = both
    assert (kill["kill_root"]["pid"], kill["succeeded"], kill["skipped_reason"]) == (4242, True, None)
    assert [k["pid"] for k in kill["killed"]] == [4242]
    assert kill["owners"] == [{"kind": "kiroshi_worker", "id": "DEMETER-4c030c", "share": None,
                               "pids": 1, "job": "handgap-v2", "slots": 22, "notified": True}]
    assert (skip["kill_root"], skip["killed"], skip["succeeded"], skip["owners"]) == (None, [], False, [])
    assert "source not identified" in skip["skipped_reason"]
    assert [e["rule"] for e in after] == ["vram-junction-hot"], "since must exclude older events"
    assert [e["rule"] for e in last] == ["vram-junction-hot"], "limit keeps the newest"
