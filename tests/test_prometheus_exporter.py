"""Tests for the Prometheus exposition renderer.

We don't try to validate the full Prometheus parser; we assert the
shape of the output (HELP/TYPE comments present, label-quoting correct,
counter vs gauge type for the right metrics) and check a handful of
known-good lines roundtrip.
"""

from __future__ import annotations

import pytest

from atfield.prometheus_exporter import PROMETHEUS_CONTENT_TYPE, render_metrics


def _sample_health() -> dict:
    return {
        "version": "0.1.0",
        "mode": "armed",
        "paused": False,
        "uptime_s": 123.4,
        "tick_count": 100,
        "last_tick_at": 1000.0,
        "heartbeat_age_s": 0.5,
        "collectors": [
            {"name": "system", "available": True, "health": "HEALTHY", "reason": "", "signals": []},
            {"name": "nvml", "available": True, "health": "HEALTHY", "reason": "", "signals": []},
            {"name": "lhm", "available": False, "health": "UNPROBED", "reason": "", "signals": []},
        ],
    }


def _sample_signals() -> dict:
    return {
        "latest": {
            "gpu.0.core_temp_c": {
                "value": 67.0, "ts": 999.0, "source": "nvml", "unit": "celsius",
            },
            "system.ram_used_percent": {
                "value": 42.5, "ts": 1000.0, "source": "psutil", "unit": "percent",
            },
        },
        "history": {},
    }


def _sample_rules() -> dict:
    return {
        "effective": [
            {
                "name": "gpu-core-hot[gpu.0.core_temp_c]",
                "base_rule": "gpu-core-hot",
                "signal": "gpu.0.core_temp_c",
                "threshold": 83.0,
                "fraction_over": 0.0,
                "triggers": 0,
                "cooldown_remaining_s": 0.0,
                "verdict": "BELOW",
            },
        ],
        "disabled": [],
    }


class TestRenderMetrics:
    def test_returns_bytes_with_trailing_newline(self):
        out = render_metrics(
            health=_sample_health(),
            signals=_sample_signals(),
            rules=_sample_rules(),
        )
        assert isinstance(out, bytes)
        assert out.endswith(b"\n"), "Prometheus parsers require a final newline"

    def test_emits_required_help_and_type_lines(self):
        out = render_metrics(
            health=_sample_health(),
            signals=_sample_signals(),
            rules=_sample_rules(),
        ).decode()
        assert "# HELP atfield_uptime_seconds" in out
        assert "# TYPE atfield_uptime_seconds gauge" in out
        assert "# HELP atfield_signal " in out
        assert "# TYPE atfield_signal gauge" in out
        assert "# TYPE atfield_rule_triggers_total counter" in out

    def test_signal_line_has_unit_and_source_labels(self):
        out = render_metrics(
            health=_sample_health(),
            signals=_sample_signals(),
            rules=_sample_rules(),
        ).decode()
        # gpu.0.core_temp_c sample: value 67.0, unit celsius, source nvml.
        assert 'atfield_signal{signal="gpu.0.core_temp_c",unit="celsius",source="nvml"} 67' in out

    def test_paused_emits_one_when_paused(self):
        h = _sample_health()
        h["paused"] = True
        out = render_metrics(health=h, signals=_sample_signals(), rules=_sample_rules()).decode()
        assert "atfield_paused 1" in out

    def test_paused_emits_zero_when_running(self):
        out = render_metrics(
            health=_sample_health(),
            signals=_sample_signals(),
            rules=_sample_rules(),
        ).decode()
        assert "atfield_paused 0" in out

    def test_collector_health_one_per_collector(self):
        out = render_metrics(
            health=_sample_health(),
            signals=_sample_signals(),
            rules=_sample_rules(),
        ).decode()
        assert 'atfield_collector_healthy{collector="system"} 1' in out
        assert 'atfield_collector_healthy{collector="nvml"} 1' in out
        # lhm is unavailable -> 0
        assert 'atfield_collector_healthy{collector="lhm"} 0' in out

    def test_rule_threshold_emitted_per_effective_rule(self):
        out = render_metrics(
            health=_sample_health(),
            signals=_sample_signals(),
            rules=_sample_rules(),
        ).decode()
        # Threshold is 83 (int-equivalent).
        assert 'rule="gpu-core-hot[gpu.0.core_temp_c]"' in out
        assert "atfield_rule_threshold{" in out

    def test_signal_age_uses_health_clock(self):
        # last_tick_at=1000, heartbeat_age_s=0.5 => effective now=1000.5
        # gpu.0.core_temp_c ts=999 => age=1.5s
        out = render_metrics(
            health=_sample_health(),
            signals=_sample_signals(),
            rules=_sample_rules(),
        ).decode()
        assert 'atfield_signal_age_seconds{signal="gpu.0.core_temp_c"} 1.5' in out

    def test_label_value_quotes_are_escaped(self):
        # Use a contrived signal name with a quote to exercise the escaper.
        signals = {
            "latest": {
                'weird"name': {
                    "value": 1.0, "ts": 1000.0, "source": 'sr"c', "unit": 'u"',
                },
            },
            "history": {},
        }
        out = render_metrics(
            health=_sample_health(),
            signals=signals,
            rules={"effective": [], "disabled": []},
        ).decode()
        # Each occurrence of a literal " inside the label value MUST be
        # escaped as \" -- otherwise Prometheus refuses to parse the line.
        assert 'signal="weird\\"name"' in out
        assert 'source="sr\\"c"' in out

    def test_skips_signals_with_non_numeric_value(self):
        signals = {
            "latest": {
                "bad": {"value": "not-a-number", "ts": 1000.0, "source": "x", "unit": "x"},
                "ok": {"value": 1.0, "ts": 1000.0, "source": "x", "unit": "x"},
            },
            "history": {},
        }
        out = render_metrics(
            health=_sample_health(),
            signals=signals,
            rules={"effective": [], "disabled": []},
        ).decode()
        assert "atfield_signal{signal=\"ok\"" in out
        assert "atfield_signal{signal=\"bad\"" not in out


class TestPrometheusEndpoint:
    """Black-box probe of /metrics over a real HTTP server."""

    @pytest.fixture
    def running_api(self, tmp_path):
        from dataclasses import dataclass

        from atfield.http_api import ApiServer, ServiceState

        state = ServiceState(
            version="0.0.0-test",
            observe_only=False,
            events_path=tmp_path / "events.jsonl",
            watchdog_log_path=tmp_path / "watchdog.log",
            state_dir=tmp_path,
        )

        @dataclass
        class _S:
            value: float
            source_id: str
            unit: str

        state.record_tick(
            now_unix=1000.0,
            samples={"system.ram_used_percent": _S(50.0, "psutil", "percent")},
        )
        server = ApiServer(state=state, host="127.0.0.1", port=0)
        server.start()
        try:
            yield server
        finally:
            server.stop()

    def test_metrics_returns_text_plain(self, running_api):
        import urllib.request

        host, port = running_api.address
        url = f"http://{host}:{port}/metrics"
        with urllib.request.urlopen(url, timeout=2) as resp:
            assert resp.status == 200
            ctype = resp.headers.get("Content-Type", "")
            assert ctype == PROMETHEUS_CONTENT_TYPE
            body = resp.read().decode()
        assert "atfield_uptime_seconds " in body
        assert 'atfield_signal{signal="system.ram_used_percent"' in body
