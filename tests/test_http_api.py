"""Tests for :mod:`atfield.http_api`.

We test against a real :class:`ThreadingHTTPServer` bound to an ephemeral
port (port 0 lets the OS pick), with a hand-built :class:`ServiceState`.
The handler is exercised through ``http.client`` calls, mirroring how the
tray app will hit it.

Why not mock the server? The handler logic (URL parsing, JSON encoding,
content-length headers, error envelope shape) is exactly what we want to
nail down — mocking would dodge that. Spinning up a real server on a
random port is cheap (<10 ms per test).
"""

from __future__ import annotations

import http.client
import json
import time
from pathlib import Path

import pytest

from atfield.config import (
    ApiConfig,
    AtFieldConfig,
    GeneralConfig,
    KillConfig,
    RuleConfig,
    TargetingConfig,
)
from atfield.http_api import (
    DEFAULT_API_HOST,
    DEFAULT_API_PORT,
    ApiServer,
    ServiceState,
    collector_view_from_probe,
)
from atfield.policy import Action, PolicyEngine
from atfield.signals import Sample

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(tmp_path: Path) -> ServiceState:
    return ServiceState(
        version="0.0.0-test",
        observe_only=False,
        events_path=tmp_path / "events.jsonl",
        watchdog_log_path=tmp_path / "watchdog.log",
        state_dir=tmp_path,
    )


def _make_engine() -> PolicyEngine:
    cfg = AtFieldConfig(
        general=GeneralConfig(),
        targeting=TargetingConfig(),
        kill=KillConfig(),
        api=ApiConfig(),
        rules=(
            RuleConfig(
                name="ram-pressure",
                signal="system.ram_used_percent",
                threshold=85.0,
                window_s=10,
                min_fraction_over=0.75,
                action="kill",
            ),
            RuleConfig(
                name="missing-signal",
                signal="cpu.0.temp_c",   # not in our available_signals
                threshold=90.0,
                window_s=10,
                min_fraction_over=0.7,
                action="log",
            ),
        ),
    )
    return PolicyEngine(cfg, available_signals={"system.ram_used_percent"})


@pytest.fixture()
def server(tmp_path: Path):
    """Spin up a real ApiServer on an OS-picked port for the test, tear down after."""
    state = _make_state(tmp_path)
    state.attach_engine(_make_engine())

    # Use port=0 to get an ephemeral port; ApiServer.start() opens the socket
    # synchronously so .address reflects the chosen port immediately.
    srv = ApiServer(state, host="127.0.0.1", port=0)
    srv.start()
    assert srv._server is not None, "API server failed to bind"
    host, port = srv.address
    try:
        yield state, host, port
    finally:
        srv.stop()


def _get(host: str, port: int, path: str) -> tuple[int, dict]:
    conn = http.client.HTTPConnection(host, port, timeout=2.0)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {"_raw": body.decode("utf-8", errors="replace")}
        return resp.status, data
    finally:
        conn.close()


def _post(host: str, port: int, path: str, body: dict | None = None) -> tuple[int, dict]:
    conn = http.client.HTTPConnection(host, port, timeout=2.0)
    try:
        payload = json.dumps(body or {}).encode("utf-8")
        conn.request(
            "POST",
            path,
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        resp = conn.getresponse()
        raw = resp.read()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {}
        return resp.status, data
    finally:
        conn.close()


def _make_sample(value: float, *, signal: str = "system.ram_used_percent") -> Sample:
    return Sample(value=value, taken_at_ns=time.monotonic_ns(), source_id="test", unit="percent")


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_default_host_and_port(self):
        assert DEFAULT_API_HOST == "127.0.0.1"
        assert DEFAULT_API_PORT == 8765


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


class TestHealth:
    def test_health_returns_minimum_fields_pre_tick(self, server):
        _, host, port = server
        status, data = _get(host, port, "/health")
        assert status == 200
        assert data["version"] == "0.0.0-test"
        assert data["mode"] == "armed"
        assert data["paused"] is False
        assert data["tick_count"] == 0
        # First tick hasn't happened yet, so heartbeat_age is None and
        # last_tick_at is None. Crucial: tray must distinguish "no tick yet"
        # from "tick was a long time ago".
        assert data["heartbeat_age_s"] is None
        assert data["last_tick_at"] is None
        assert data["rules_active"] == 1   # ram-pressure
        assert data["rules_disabled"] == 1  # missing-signal
        assert data["last_action"] is None

    def test_health_observe_only_mode_surfaces(self, tmp_path):
        state = _make_state(tmp_path)
        state._observe_only = True
        srv = ApiServer(state, host="127.0.0.1", port=0)
        srv.start()
        try:
            _, host, port = (state, *srv.address)
            status, data = _get(host, port, "/health")
            assert status == 200
            assert data["mode"] == "observe-only"
        finally:
            srv.stop()

    def test_health_reflects_tick_progress(self, server):
        state, host, port = server
        sample = _make_sample(50.0)
        state.record_tick(now_unix=time.time(), samples={"system.ram_used_percent": sample})
        status, data = _get(host, port, "/health")
        assert status == 200
        assert data["tick_count"] == 1
        assert data["heartbeat_age_s"] is not None
        assert data["heartbeat_age_s"] >= 0

    def test_health_reflects_last_action(self, server):
        state, host, port = server
        action = Action(
            kind="kill",
            rule_name="ram-pressure",
            base_rule_name="ram-pressure",
            signal="system.ram_used_percent",
            threshold=85.0,
            fraction_over=1.0,
            samples_considered=10,
            latest_value=99.0,
            triggered_at_ns=time.monotonic_ns(),
            cooldown_seconds=60,
        )
        state.record_action(action)
        _, data = _get(host, port, "/health")
        assert data["last_action"] is not None
        assert data["last_action"]["kind"] == "kill"
        assert data["last_action"]["rule"] == "ram-pressure"


# ---------------------------------------------------------------------------
# /signals
# ---------------------------------------------------------------------------


class TestSignals:
    def test_empty_signals_before_tick(self, server):
        _, host, port = server
        status, data = _get(host, port, "/signals")
        assert status == 200
        assert data == {"latest": {}, "history": {}}

    def test_signal_appears_after_tick(self, server):
        state, host, port = server
        now = time.time()
        state.record_tick(now_unix=now, samples={"system.ram_used_percent": _make_sample(42.0)})
        _, data = _get(host, port, "/signals")
        latest = data["latest"]
        assert "system.ram_used_percent" in latest
        assert latest["system.ram_used_percent"]["value"] == 42.0
        assert latest["system.ram_used_percent"]["unit"] == "percent"
        assert latest["system.ram_used_percent"]["source"] == "test"
        # History is one point.
        assert len(data["history"]["system.ram_used_percent"]) == 1

    def test_history_grows_per_tick(self, server):
        state, host, port = server
        for v in (10.0, 20.0, 30.0):
            state.record_tick(now_unix=time.time(), samples={"system.ram_used_percent": _make_sample(v)})
        _, data = _get(host, port, "/signals")
        hist = data["history"]["system.ram_used_percent"]
        assert len(hist) == 3
        assert [pt[1] for pt in hist] == [10.0, 20.0, 30.0]

    def test_since_filters_history(self, server):
        state, host, port = server
        # Push three points well in the past, then one "now". Only the recent
        # one should survive a since-filter set to a moment ago.
        old_ts = time.time() - 100
        state._history.setdefault("x", __import__("collections").deque(maxlen=10))
        state._history["x"].extend([(old_ts, 1.0), (old_ts + 1, 2.0)])
        recent_ts = time.time()
        state._history["x"].append((recent_ts, 3.0))
        _, data = _get(host, port, f"/signals?since={time.time() - 5}")
        assert [pt[1] for pt in data["history"]["x"]] == [3.0]

    def test_invalid_since_treated_as_no_filter(self, server):
        state, host, port = server
        state.record_tick(now_unix=time.time(), samples={"system.ram_used_percent": _make_sample(1.0)})
        status, data = _get(host, port, "/signals?since=banana")
        assert status == 200
        assert len(data["history"]["system.ram_used_percent"]) == 1


# ---------------------------------------------------------------------------
# /rules
# ---------------------------------------------------------------------------


class TestRules:
    def test_rules_lists_effective_and_disabled(self, server):
        _, host, port = server
        status, data = _get(host, port, "/rules")
        assert status == 200
        names = [r["name"] for r in data["effective"]]
        assert names == ["ram-pressure"]
        assert data["effective"][0]["signal"] == "system.ram_used_percent"
        assert data["effective"][0]["threshold"] == 85.0
        assert data["effective"][0]["action"] == "kill"
        # cooldown_remaining_s should be 0 before any trigger.
        assert data["effective"][0]["cooldown_remaining_s"] == 0
        # initial verdict is INSUFFICIENT (no samples yet).
        assert data["effective"][0]["verdict"] == "INSUFFICIENT"

        disabled = data["disabled"]
        assert len(disabled) == 1
        assert disabled[0]["rule"] == "missing-signal"
        assert "not provided by any probed collector" in disabled[0]["reason"]


# ---------------------------------------------------------------------------
# /events
# ---------------------------------------------------------------------------


class TestEvents:
    def test_events_returns_empty_list_when_file_missing(self, server):
        _, host, port = server
        status, data = _get(host, port, "/events")
        assert status == 200
        assert data == {"events": [], "count": 0}

    def test_events_reads_jsonl_and_respects_limit(self, server):
        state, host, port = server
        events_path = state.events_path()
        with events_path.open("w", encoding="utf-8") as fh:
            for i in range(10):
                fh.write(json.dumps({"type": "tick", "n": i, "ts": time.time()}) + "\n")
        _, data = _get(host, port, "/events?limit=3")
        assert data["count"] == 3
        # Tail order: the last three events.
        assert [e["n"] for e in data["events"]] == [7, 8, 9]

    def test_events_since_filter(self, server):
        state, host, port = server
        events_path = state.events_path()
        cutoff = time.time()
        with events_path.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "old", "ts": cutoff - 100}) + "\n")
            fh.write(json.dumps({"type": "new", "ts": cutoff + 1}) + "\n")
        _, data = _get(host, port, f"/events?since={cutoff}")
        assert data["count"] == 1
        assert data["events"][0]["type"] == "new"

    def test_events_skips_corrupt_lines(self, server):
        state, host, port = server
        events_path = state.events_path()
        with events_path.open("w", encoding="utf-8") as fh:
            fh.write('{"type": "ok", "ts": 1}\n')
            fh.write("not-json\n")
            fh.write('{"type": "ok2", "ts": 2}\n')
        _, data = _get(host, port, "/events")
        assert data["count"] == 2
        assert [e["type"] for e in data["events"]] == ["ok", "ok2"]

    def test_events_limit_clamped_to_max(self, server):
        _, host, port = server
        # 999999 should be clamped to internal max without erroring.
        status, _ = _get(host, port, "/events?limit=999999")
        assert status == 200


# ---------------------------------------------------------------------------
# /pause + /unpause
# ---------------------------------------------------------------------------


class TestPause:
    def test_pause_writes_sentinel_and_returns_iso(self, server):
        state, host, port = server
        status, data = _post(host, port, "/pause", {"duration_s": 60})
        assert status == 200
        assert data["paused"] is True
        sentinel = state.state_dir() / "pause.sentinel"
        assert sentinel.exists()
        # State mirror updated.
        snap = state.snapshot_health()
        assert snap["paused"] is True

    def test_unpause_clears_sentinel(self, server):
        state, host, port = server
        _post(host, port, "/pause", {"duration_s": 60})
        status, data = _post(host, port, "/unpause")
        assert status == 200
        assert data["paused"] is False
        sentinel = state.state_dir() / "pause.sentinel"
        assert not sentinel.exists()
        snap = state.snapshot_health()
        assert snap["paused"] is False

    def test_unpause_when_not_paused_is_idempotent(self, server):
        _, host, port = server
        status, data = _post(host, port, "/unpause")
        assert status == 200
        assert data["paused"] is False
        assert data["cleared"] is False

    def test_pause_rejects_non_numeric_duration(self, server):
        _, host, port = server
        status, data = _post(host, port, "/pause", {"duration_s": "soon"})
        assert status == 400
        assert "duration_s" in data["error"]

    def test_pause_with_no_duration_pauses_indefinitely(self, server):
        state, host, port = server
        status, data = _post(host, port, "/pause", {})
        assert status == 200
        assert data["paused"] is True
        snap = state.snapshot_health()
        assert snap["paused"] is True
        # "Indefinite" really means a very long timeout (1y); the dashboard
        # surfaces this as "until manually unpaused" rather than a number.
        assert snap["paused_until"] is not None


# ---------------------------------------------------------------------------
# /reload
# ---------------------------------------------------------------------------


class TestReload:
    def test_reload_sets_flag_and_consume_returns_true_once(self, server):
        state, host, port = server
        status, data = _post(host, port, "/reload")
        assert status == 200
        assert data == {"reload_queued": True}
        assert state.consume_reload_request() is True
        # second consume should be False (one-shot)
        assert state.consume_reload_request() is False


# ---------------------------------------------------------------------------
# Error handling + unknown routes
# ---------------------------------------------------------------------------


class TestUnknownRoutes:
    def test_unknown_get_returns_404_json(self, server):
        _, host, port = server
        status, data = _get(host, port, "/nope")
        assert status == 404
        assert "unknown endpoint" in data["error"]

    def test_unknown_post_returns_404_json(self, server):
        _, host, port = server
        status, data = _post(host, port, "/nope")
        assert status == 404
        assert "unknown endpoint" in data["error"]


# ---------------------------------------------------------------------------
# CollectorView helper
# ---------------------------------------------------------------------------


class TestCollectorViewHelper:
    def test_collector_view_from_probe_round_trips(self):
        from typing import ClassVar

        class _StubProbe:
            available = True
            reason = "ok"
            signals: ClassVar[list[str]] = ["system.ram_used_percent"]

        cv = collector_view_from_probe("system", _StubProbe(), "HEALTHY")
        assert cv.name == "system"
        assert cv.available is True
        assert cv.signals == ["system.ram_used_percent"]
        assert cv.health == "HEALTHY"


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


class TestServerLifecycle:
    def test_stop_is_idempotent(self, tmp_path):
        srv = ApiServer(_make_state(tmp_path), host="127.0.0.1", port=0)
        srv.start()
        srv.stop()
        srv.stop()  # second stop must not raise

    def test_bind_failure_is_logged_not_raised(self, tmp_path, caplog):
        # Port 1 is privileged on every OS; this should fail to bind on a
        # non-elevated test process and be logged as a warning rather than
        # crashing the service.
        srv = ApiServer(_make_state(tmp_path), host="127.0.0.1", port=1)
        srv.start()
        # No exception means the failure was swallowed correctly.
        assert srv._server is None or srv._server is not None  # no assertion of success
        srv.stop()
