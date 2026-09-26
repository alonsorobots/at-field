"""An IDLE host's temperatures must reach the hub (0.4.17).

Until 0.4.17 AT-Field pushed only EVENTS off the machine (a kill, a guard gone
inert). Temperatures reached Kiroshi's hub only inside a worker's heartbeat, so
the dashboard card for a host with no job running showed no temperature at all
-- exactly the machine you want to see before asking it to work ("a box sitting
idle at 80 C").

Now, every ``general.telemetry_interval_s`` (default 60 s; 0 = off), AT-Field
posts ONE compact row -- this host's thermal and percent-reservoir signals, each
with its reading time -- to the SAME webhook it already posts kills to
(``atfield_event_webhook`` in a client manifest), typed ``kind="telemetry"`` so
no consumer reads it as a kill. No subscriber, no post, no thread.

The rule name is ``atfield.telemetry``, deliberately NOT ``fleet.*``: on the hub
``fleet.*`` rows are posted ABOUT a host by fleet_liveness and are not the host
speaking; this row IS the host speaking.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from atfield import reporter  # noqa: E402
from atfield.config import ConfigError, load_config_from_dict  # noqa: E402
from atfield.signals import Sample, monotonic_ns  # noqa: E402

HUB = "http://192.0.2.1:8900/atfield/event"


def _manifest(sd: Path, name: str, url: str | None, age_s: float = 0.0) -> Path:
    d = sd / "clients" / "kiroshi"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.json"
    p.write_text(json.dumps({"name": name, reporter._WEBHOOK_FIELD: url}), encoding="utf-8")
    t = time.time() - age_s
    os.utime(p, (t, t))
    return sd


def _sent(monkeypatch, calls=None) -> list:
    out = []
    monkeypatch.setattr(reporter, "_cached_at", 0.0, raising=False)
    monkeypatch.setattr(reporter, "_cached_webhooks", [], raising=False)
    monkeypatch.setattr(reporter, "_ensure_worker",
                        lambda: (calls.append(1) if calls is not None else None))
    monkeypatch.setattr(reporter._send_queue, "put_nowait", lambda item: out.append(item))
    return out


def _samples():
    now = monotonic_ns()
    return {
        "system.cpu_package_temp_c": Sample(50.375, now, "lhm", "celsius"),
        "gpu.0.core_temp_c": Sample(41.0, now, "nvml", "celsius"),
        "gpu.0.mem_junction_temp_c": Sample(52.0, now - 2_000_000_000, "lhm", "celsius"),
        "system.ram_used_percent": Sample(14.5, now, "system", "percent"),
        "system.commit_percent": Sample(4.3454, now, "system", "percent"),
        # NOT telemetry: throughput, bytes, watts
        "gpu.0.util_percent": Sample(0.0, now, "nvml", "percent"),
        "gpu.0.vram_used_bytes": Sample(8.8e8, now, "nvml", "bytes"),
        "system.cpu_package_power_w": Sample(55.1, now, "lhm", "watts"),
    }


# ---------------------------------------------------------------- the row
def test_the_row_carries_thermal_and_reservoir_signals_with_their_times():
    s = _samples()
    now_unix = 1_790_000_000.0
    row = reporter.telemetry_signals(s, credible=set(s), now_unix=now_unix)
    assert set(row) == {"system.cpu_package_temp_c", "gpu.0.core_temp_c",
                        "gpu.0.mem_junction_temp_c", "system.ram_used_percent",
                        "system.commit_percent"}, sorted(row)
    assert row["system.cpu_package_temp_c"][0] == 50.4
    assert row["system.commit_percent"][0] == 4.3
    # each reading carries WHEN it was read, in unix time
    assert abs(row["gpu.0.core_temp_c"][1] - now_unix) < 1.0
    assert abs(row["gpu.0.mem_junction_temp_c"][1] - (now_unix - 2.0)) < 1.0


def test_an_unbelievable_reading_is_not_published():
    """A wedged NVML session reports a constant 0.0 C (or 885510 C). The tick
    loop already withholds it from the rules; the hub must not show it as the
    host's temperature either."""
    s = _samples()
    credible = set(s) - {"gpu.0.core_temp_c"}
    row = reporter.telemetry_signals(s, credible=credible, now_unix=time.time())
    assert "gpu.0.core_temp_c" not in row


# ---------------------------------------------------------------- delivery
def test_the_row_goes_to_the_hub_typed_as_telemetry(tmp_path, monkeypatch):
    sent = _sent(monkeypatch)
    s = _samples()
    ok = reporter.report_telemetry(_manifest(tmp_path, "worker-1", HUB), s,
                                   credible=set(s), now_unix=time.time())
    assert ok is True
    assert len(sent) == 1, sent
    url, p = sent[0]
    assert url == HUB
    assert p["kind"] == "telemetry" and p["type"] == "telemetry"
    assert p["rule"] == "atfield.telemetry" and not p["rule"].startswith("fleet.")
    assert p["signal"] == "host"
    assert p["host"]
    d = json.loads(p["detail"])
    assert d["signals"]["system.cpu_package_temp_c"][0] == 50.4
    assert len(p["detail"]) < 1000, "the row is meant to be compact"


def test_only_the_NEWEST_subscriber_gets_it(tmp_path, monkeypatch):
    """Kill events go to every registered URL. Manifests of long-dead processes
    stay on disk (DEMETER holds 116, pointing at two hubs), and a once-a-minute
    ping to each would feed a hub that no longer serves this host. The newest
    manifest is the hub this host most recently worked for."""
    sent = _sent(monkeypatch)
    sd = _manifest(tmp_path, "worker-old", "http://192.0.2.9:8787/atfield/event", age_s=86400)
    _manifest(sd, "worker-new", HUB, age_s=5)
    _manifest(sd, "worker-none", None, age_s=0)       # newest file, but no webhook
    s = _samples()
    reporter.report_telemetry(sd, s, credible=set(s), now_unix=time.time())
    assert [u for u, _ in sent] == [HUB], sent


def test_no_subscriber_means_no_post_and_no_thread(tmp_path, monkeypatch):
    started: list = []
    sent = _sent(monkeypatch, calls=started)
    s = _samples()
    assert reporter.report_telemetry(tmp_path, s, credible=set(s),
                                     now_unix=time.time()) is False
    assert sent == [] and started == []


# ---------------------------------------------------------------- cadence
def test_the_ping_is_periodic_and_the_first_is_immediate(tmp_path, monkeypatch):
    sent = _sent(monkeypatch)
    sd = _manifest(tmp_path, "worker-1", HUB)
    s = _samples()
    p = reporter.TelemetryPinger(interval_s=60)
    for t in (1000.0, 1030.0, 1059.9, 1060.0, 1100.0, 1121.0):
        p.maybe_report(sd, s, credible=set(s), now_unix=t)
    assert len(sent) == 3, [json.loads(x[1]["detail"])["ts"] for x in sent]


def test_interval_zero_is_off(tmp_path, monkeypatch):
    sent = _sent(monkeypatch)
    sd = _manifest(tmp_path, "worker-1", HUB)
    s = _samples()
    p = reporter.TelemetryPinger(interval_s=0)
    for t in (1000.0, 2000.0, 3000.0):
        p.maybe_report(sd, s, credible=set(s), now_unix=t)
    assert sent == []


# ---------------------------------------------------------------- config
def test_config_default_is_60s_and_zero_is_allowed():
    assert load_config_from_dict({}).general.telemetry_interval_s == 60
    cfg = load_config_from_dict({"general": {"telemetry_interval_s": 0}})
    assert cfg.general.telemetry_interval_s == 0
    with pytest.raises(ConfigError):
        load_config_from_dict({"general": {"telemetry_interval_s": -1}})


# ---------------------------------------------------------------- the loop
CONFIG = """
[general]
tick_hz = 1
{extra}

[api]
enabled = false

[[rules]]
name = "cpu-hot"
signal = "system.cpu_package_temp_c"
threshold = 95.0
window_s = 30
min_fraction_over = 0.67
action = "log"
"""


class _FakeThermal:
    name = "fake"

    def probe(self):
        from atfield.collectors import ProbeResult
        return ProbeResult(available=True, reason="fake",
                           signals=("system.cpu_package_temp_c",))

    def health(self):
        from atfield.collectors import HealthState
        return HealthState.HEALTHY

    def sample(self):
        return {"system.cpu_package_temp_c": Sample(47.0, monotonic_ns(), self.name, "celsius")}


def _loop(tmp_path, monkeypatch, extra: str, ticks: int = 2) -> list:
    import atfield.service as s
    sent = _sent(monkeypatch)
    cfg = tmp_path / "config.toml"
    cfg.write_text(CONFIG.format(extra=extra), encoding="utf-8")
    sd = _manifest(tmp_path / "state", "worker-1", HUB)
    c = _FakeThermal()
    monkeypatch.setattr(s, "_probe_all_collectors", lambda audit: ([c], {c.name: c.probe()}))
    s.run_service(config_path=cfg, state_dir=sd, max_ticks=ticks)
    return [p for _u, p in sent if p.get("kind") == "telemetry"]


def test_the_service_loop_sends_it(tmp_path, monkeypatch):
    rows = _loop(tmp_path, monkeypatch, "")
    assert len(rows) == 1, rows
    assert json.loads(rows[0]["detail"])["signals"]["system.cpu_package_temp_c"][0] == 47.0


def test_the_service_loop_honours_off(tmp_path, monkeypatch):
    assert _loop(tmp_path, monkeypatch, "telemetry_interval_s = 0") == []
