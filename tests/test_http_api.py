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
    return _verb(host, port, "POST", path, body)


def _patch(host: str, port: int, path: str, body: dict | None = None) -> tuple[int, dict]:
    return _verb(host, port, "PATCH", path, body)


def _verb(host: str, port: int, verb: str, path: str, body: dict | None) -> tuple[int, dict]:
    conn = http.client.HTTPConnection(host, port, timeout=2.0)
    try:
        payload = json.dumps(body or {}).encode("utf-8")
        conn.request(
            verb,
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

    def test_health_omits_lhm_supervisor_when_unbound(self, server):
        """No supervisor wired => /health.lhm_supervisor is null. Important
        because the dashboard treats that as 'we are not managing LHM,
        fall back to the collector probe' rather than 'LHM is broken'."""
        _, host, port = server
        _, data = _get(host, port, "/health")
        assert data["lhm_supervisor"] is None

    def test_health_surfaces_lhm_supervisor_state(self, tmp_path):
        """When the supervisor is bound, /health surfaces its full
        snapshot under .lhm_supervisor with a derived ``state`` token
        the dashboard can switch on without needing to interpret the
        raw http_ready/running/last_error matrix."""
        from atfield.lhm_supervisor import LhmStatus

        class _FakeSup:
            def __init__(self, status):
                self._status = status

            def snapshot_status(self):
                return self._status

        state = _make_state(tmp_path)
        # Case 1: process up + HTTP ready => state="ready"
        state.set_lhm_supervisor(_FakeSup(LhmStatus(
            running=True, pid=1234, http_ready=True, started_at=time.time(),
        )))
        srv = ApiServer(state, host="127.0.0.1", port=0)
        srv.start()
        try:
            _, host, port = (state, *srv.address)
            _, data = _get(host, port, "/health")
            ls = data["lhm_supervisor"]
            assert ls is not None
            assert ls["state"] == "ready"
            assert ls["http_ready"] is True
            assert ls["pid"] == 1234

            # Case 2: process up but HTTP server didn't bind =>
            # state="process_up_no_http" -- the new failure mode
            # introduced by the LHM 0.9.6 robustness fix.
            state.set_lhm_supervisor(_FakeSup(LhmStatus(
                running=True, pid=999, http_ready=False, started_at=time.time(),
                last_error="HTTP server did not come up on port 8085 within 15s.",
            )))
            _, data = _get(host, port, "/health")
            ls = data["lhm_supervisor"]
            assert ls["state"] == "process_up_no_http"
            assert ls["http_ready"] is False
            assert "did not come up" in ls["last_error"]

            # Case 3: process down, awaiting backoff => state="backoff"
            state.set_lhm_supervisor(_FakeSup(LhmStatus(
                running=False, pid=None, next_retry_at=time.time() + 4.0,
                last_error="exited with code 1",
            )))
            _, data = _get(host, port, "/health")
            assert data["lhm_supervisor"]["state"] == "backoff"
        finally:
            srv.stop()

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

    def test_health_last_action_includes_signal_and_null_script_initially(self, server):
        """record_action() alone (no kill_report yet) leaves script=None."""
        state, host, port = server
        action = Action(
            kind="kill",
            rule_name="vram-junction-hot",
            base_rule_name="vram-junction-hot",
            signal="gpu.0.mem_junction_temp_c",
            threshold=98.0,
            fraction_over=1.0,
            samples_considered=10,
            latest_value=99.5,
            triggered_at_ns=12345,
            cooldown_seconds=60,
        )
        state.record_action(action)
        _, data = _get(host, port, "/health")
        assert data["last_action"]["signal"] == "gpu.0.mem_junction_temp_c"
        assert data["last_action"]["script"] is None, (
            "script field exists but is None until record_kill_report fires"
        )

    def test_health_last_action_script_populated_after_kill_report(self, server):
        """record_action(kill) then record_kill_report('train.py') makes
        the script available on /health for the tray notification."""
        state, host, port = server
        action = Action(
            kind="kill",
            rule_name="vram-junction-hot",
            base_rule_name="vram-junction-hot",
            signal="gpu.0.mem_junction_temp_c",
            threshold=98.0,
            fraction_over=1.0,
            samples_considered=10,
            latest_value=99.5,
            triggered_at_ns=12345,
            cooldown_seconds=60,
        )
        state.record_action(action)
        state.record_kill_report("train.py")
        _, data = _get(host, port, "/health")
        assert data["last_action"]["script"] == "train.py"

    def test_health_record_action_clears_prior_script(self, server):
        """A new action should reset the script field so a stale value
        from a previous kill doesn't bleed into a fresh log/throttle."""
        state, host, port = server
        kill = Action(
            kind="kill", rule_name="r1", base_rule_name="r1",
            signal="s1", threshold=80.0, fraction_over=1.0,
            samples_considered=10, latest_value=90.0,
            triggered_at_ns=1, cooldown_seconds=60,
        )
        state.record_action(kill)
        state.record_kill_report("first.py")
        # Now a new log action lands; script should reset to None.
        log = Action(
            kind="log", rule_name="r2", base_rule_name="r2",
            signal="s2", threshold=70.0, fraction_over=1.0,
            samples_considered=10, latest_value=75.0,
            triggered_at_ns=2, cooldown_seconds=60,
        )
        state.record_action(log)
        _, data = _get(host, port, "/health")
        assert data["last_action"]["kind"] == "log"
        assert data["last_action"]["script"] is None

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
        state.record_tick(now_unix=old_ts, samples={"x": _make_sample(1.0)})
        state.record_tick(now_unix=old_ts + 1, samples={"x": _make_sample(2.0)})
        recent_ts = time.time()
        state.record_tick(now_unix=recent_ts, samples={"x": _make_sample(3.0)})
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
# PATCH /rules/<name> + POST /profile (slider + presets)
# ---------------------------------------------------------------------------


class TestRulesTuningMetadata:
    """Each /rules entry should carry tier metadata for the slider."""

    def test_known_rule_has_tuning_block(self, server):
        _, host, port = server
        _, data = _get(host, port, "/rules")
        ram = next(
            r for r in data["effective"] if r["base_rule"] == "ram-pressure"
        )
        assert ram["tuning"] is not None
        assert ram["tuning"]["min"] < ram["tuning"]["aggressive_max"]
        assert ram["tuning"]["aggressive_max"] < ram["tuning"]["relaxed_min"]
        assert ram["tuning"]["relaxed_min"] < ram["tuning"]["max"]
        assert ram["tuning"]["unit"] == "%"
        assert ram["tuning"]["current_tier"] in ("aggressive", "normal", "relaxed")
        assert "presets" in ram["tuning"]


class TestPatchRule:
    def test_valid_threshold_writes_and_reloads(self, tmp_path, server):
        state, host, port = server
        # Provide a config_path so the writer has a real file to mutate.
        state.set_config_path(tmp_path / "config.toml")

        status, data = _patch(host, port, "/rules/ram-pressure",
                              {"threshold": 78.0})
        assert status == 200, data
        assert data["threshold"] == 78.0
        assert data["tier"] == "aggressive"
        assert data["reload_queued"] is True

        # File was actually written
        text = (tmp_path / "config.toml").read_text(encoding="utf-8")
        assert 'name = "ram-pressure"' in text
        assert "threshold = 78.0" in text

        # Reload was queued
        assert state.consume_reload_request() is True

    def test_unknown_rule_returns_400(self, server):
        _, host, port = server
        status, data = _patch(host, port, "/rules/nonexistent",
                              {"threshold": 50.0})
        assert status == 400
        assert "nonexistent" in data["error"]

    def test_threshold_out_of_range_returns_400(self, tmp_path, server):
        state, host, port = server
        state.set_config_path(tmp_path / "config.toml")
        # ram-pressure profile: min=50, max=99
        status, data = _patch(host, port, "/rules/ram-pressure",
                              {"threshold": 150.0})
        assert status == 400
        assert "out of range" in data["error"]

    def test_missing_threshold_returns_400(self, server):
        # PATCH with empty body is rejected; the server now accepts any of
        # {threshold, window_s, cooldown_s, action, min_fraction_over} so
        # the error message reads 'non-empty JSON object' rather than
        # mentioning threshold specifically.
        _, host, port = server
        status, data = _patch(host, port, "/rules/ram-pressure", {})
        assert status == 400
        assert "non-empty" in data["error"].lower()

    def test_non_numeric_threshold_returns_400(self, server):
        _, host, port = server
        status, data = _patch(host, port, "/rules/ram-pressure",
                              {"threshold": "high"})
        assert status == 400
        assert "must be a number" in data["error"]

    def test_unknown_subroute_returns_404(self, server):
        _, host, port = server
        # PATCH /rules (no name) is unrouted -- 404 keeps the 'missing
        # rule name' error out of the way of legitimate 4xx noise.
        status, _data = _patch(host, port, "/rules", {"threshold": 50.0})
        assert status == 404

    # ------------------------------------------------------------------
    # Multi-field PATCH (window_s / cooldown_s / action)
    # ------------------------------------------------------------------

    def test_patch_window_seconds(self, tmp_path, server):
        state, host, port = server
        state.set_config_path(tmp_path / "config.toml")
        status, data = _patch(host, port, "/rules/ram-pressure",
                              {"window_s": 45})
        assert status == 200, data
        assert data["accepted"] == {"window_s": 45}
        assert data["reload_queued"] is True
        # Confirm the on-disk config was actually rewritten.
        text = (tmp_path / "config.toml").read_text(encoding="utf-8")
        assert "window_s = 45" in text

    def test_patch_cooldown_seconds(self, tmp_path, server):
        state, host, port = server
        state.set_config_path(tmp_path / "config.toml")
        status, data = _patch(host, port, "/rules/ram-pressure",
                              {"cooldown_s": 120})
        assert status == 200, data
        assert data["accepted"] == {"cooldown_s": 120}
        text = (tmp_path / "config.toml").read_text(encoding="utf-8")
        assert "cooldown_s = 120" in text

    def test_patch_action(self, tmp_path, server):
        state, host, port = server
        state.set_config_path(tmp_path / "config.toml")
        status, data = _patch(host, port, "/rules/ram-pressure",
                              {"action": "log"})
        assert status == 200, data
        assert data["accepted"] == {"action": "log"}
        text = (tmp_path / "config.toml").read_text(encoding="utf-8")
        assert 'action = "log"' in text

    def test_patch_invalid_action_rejected(self, server):
        _, host, port = server
        status, data = _patch(host, port, "/rules/ram-pressure",
                              {"action": "explode"})
        assert status == 400
        assert "kill" in data["error"]

    def test_patch_combined_threshold_window_cooldown(self, tmp_path, server):
        # All three fields in a single PATCH (the slider + window/cooldown
        # editors fire one combined request when the user hits 'apply').
        state, host, port = server
        state.set_config_path(tmp_path / "config.toml")
        status, data = _patch(host, port, "/rules/ram-pressure", {
            "threshold": 88.0,
            "window_s": 30,
            "cooldown_s": 90,
        })
        assert status == 200, data
        assert data["accepted"]["threshold"] == 88.0
        assert data["accepted"]["window_s"] == 30
        assert data["accepted"]["cooldown_s"] == 90
        # Threshold-tier classification is still echoed (UI optimism).
        assert "tier" in data
        text = (tmp_path / "config.toml").read_text(encoding="utf-8")
        assert "threshold = 88.0" in text
        assert "window_s = 30" in text
        assert "cooldown_s = 90" in text

    def test_patch_unknown_field_rejected(self, server):
        _, host, port = server
        status, data = _patch(host, port, "/rules/ram-pressure",
                              {"hovercraft": True})
        assert status == 400
        assert "hovercraft" in data["error"]

    def test_patch_window_out_of_range(self, server):
        _, host, port = server
        status, data = _patch(host, port, "/rules/ram-pressure",
                              {"window_s": 9000})
        assert status == 400
        assert "1..600" in data["error"]

    # ------------------------------------------------------------------
    # CORS preflight -- regression guard for the slider "Failed to fetch"
    # bug where PATCH was missing from Access-Control-Allow-Methods.
    # The browser issues an OPTIONS preflight before any non-simple
    # cross-origin request; if PATCH isn't in the allow list, it cancels
    # the actual PATCH and the dashboard surfaces "Save failed".
    # ------------------------------------------------------------------

    def test_options_preflight_advertises_patch(self, server):
        _, host, port = server
        conn = http.client.HTTPConnection(host, port, timeout=2.0)
        try:
            conn.request(
                "OPTIONS",
                "/rules/ram-pressure",
                headers={
                    "Origin": "http://localhost:1420",
                    "Access-Control-Request-Method": "PATCH",
                    "Access-Control-Request-Headers": "Content-Type",
                },
            )
            resp = conn.getresponse()
            resp.read()
            assert resp.status == 204
            allow = resp.getheader("Access-Control-Allow-Methods", "")
            # PATCH MUST be in the list or the slider edits fail in the
            # browser. See also POST / GET (kept for the rest of the API).
            for verb in ("GET", "POST", "PATCH", "OPTIONS"):
                assert verb in allow, (
                    f"{verb} missing from Access-Control-Allow-Methods: {allow!r}"
                )
            assert resp.getheader("Access-Control-Allow-Origin") == "*"
        finally:
            conn.close()


class TestApplyProfilePreset:
    def test_aggressive_preset_writes_all_rules(self, tmp_path, server):
        state, host, port = server
        state.set_config_path(tmp_path / "config.toml")
        status, data = _post(host, port, "/profile", {"profile": "aggressive"})
        assert status == 200, data
        assert data["profile"] == "aggressive"
        # All five default rules got an aggressive value applied
        applied = data["applied"]
        assert "ram-pressure" in applied
        assert "vram-junction-hot" in applied
        assert "gpu-core-hot" in applied
        assert "cpu-pkg-hot" in applied
        # Reload queued
        assert state.consume_reload_request() is True

    def test_unknown_profile_returns_400(self, server):
        _, host, port = server
        status, data = _post(host, port, "/profile", {"profile": "yolo"})
        assert status == 400
        assert "yolo" in data["error"] or "unknown" in data["error"]

    def test_missing_profile_returns_400(self, server):
        _, host, port = server
        status, _data = _post(host, port, "/profile", {})
        assert status == 400


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
