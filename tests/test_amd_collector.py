"""Tests for the AMD ROCm-SMI collector probe.

Live sampling is intentionally unimplemented (we don't have an AMD rig
in CI); these tests cover the probe's parsing logic + the absent-binary
branch so the collector is at least safe to load.
"""

from __future__ import annotations

import json

import pytest

from atfield.collectors import HealthState
from atfield.collectors.amd import (
    AmdCollector,
    _looks_like_junction_capable,
    _maybe_card_index,
    find_rocm_smi,
)


class TestCardIndexParsing:
    def test_cardN_form(self):
        assert _maybe_card_index("card0") == 0
        assert _maybe_card_index("card17") == 17

    def test_bare_int_form(self):
        assert _maybe_card_index("0") == 0
        assert _maybe_card_index("3") == 3

    def test_unrecognized_key(self):
        assert _maybe_card_index("system") is None
        assert _maybe_card_index("driver_version") is None
        assert _maybe_card_index("cardX") is None


class TestJunctionDetection:
    def test_modern_label(self):
        assert _looks_like_junction_capable({"Temperature (Sensor memory)": 65})

    def test_short_label(self):
        assert _looks_like_junction_capable({"temp_mem": 60})

    def test_temperature_with_junction(self):
        assert _looks_like_junction_capable({"GPU Junction Temperature (C)": 70})

    def test_no_mem_no_junction_returns_false(self):
        assert not _looks_like_junction_capable({"Temperature (Edge)": 55})

    def test_empty_payload(self):
        assert not _looks_like_junction_capable({})


class TestProbeWithoutBinary:
    def test_probe_returns_unavailable_when_rocm_smi_missing(self, monkeypatch):
        monkeypatch.setattr("atfield.collectors.amd.find_rocm_smi", lambda: None)
        c = AmdCollector()
        result = c.probe()
        assert result.available is False
        assert "rocm-smi not found" in result.reason
        assert result.signals == ()


class TestProbeWithFakeRocm:
    def _patch_rocm(self, monkeypatch, payload: dict | None, *, returncode: int = 0):
        """Fake out subprocess.run for probe()."""
        monkeypatch.setattr("atfield.collectors.amd.find_rocm_smi", lambda: "/fake/rocm-smi")

        def fake_run(*args, **kwargs):
            class _Result:
                pass
            r = _Result()
            r.returncode = returncode
            r.stdout = json.dumps(payload) if payload is not None else ""
            r.stderr = "fake stderr" if returncode != 0 else ""
            return r

        monkeypatch.setattr("subprocess.run", fake_run)

    def test_probe_one_card_with_junction(self, monkeypatch):
        self._patch_rocm(
            monkeypatch,
            {
                "card0": {
                    "Temperature (Edge)": 55,
                    "Temperature (Sensor memory)": 65,
                },
            },
        )
        c = AmdCollector()
        result = c.probe()
        assert result.available is True
        assert "1 GPU(s) detected" in result.reason
        assert "gpu.0.core_temp_c" in result.signals
        assert "gpu.0.mem_junction_temp_c" in result.signals
        assert c.health() == HealthState.HEALTHY

    def test_probe_one_card_without_junction_skips_signal(self, monkeypatch):
        self._patch_rocm(
            monkeypatch,
            {"card0": {"Temperature (Edge)": 55}},
        )
        c = AmdCollector()
        result = c.probe()
        assert result.available is True
        assert "gpu.0.core_temp_c" in result.signals
        assert "gpu.0.mem_junction_temp_c" not in result.signals

    def test_probe_no_cards_returns_unavailable(self, monkeypatch):
        self._patch_rocm(monkeypatch, {"system": {"driver": "x"}})
        c = AmdCollector()
        result = c.probe()
        assert result.available is False
        assert "no recognizable GPU entries" in result.reason

    def test_probe_invalid_json_marks_failed(self, monkeypatch):
        monkeypatch.setattr("atfield.collectors.amd.find_rocm_smi", lambda: "/fake/rocm-smi")

        def bad_run(*args, **kwargs):
            class _R:
                returncode = 0
                stdout = "not-json"
                stderr = ""
            return _R()

        monkeypatch.setattr("subprocess.run", bad_run)
        c = AmdCollector()
        result = c.probe()
        assert result.available is False
        assert c.health() == HealthState.FAILED
        assert "did not return valid JSON" in result.reason


class TestSampleNotImplemented:
    def test_sample_raises_with_actionable_message(self, monkeypatch):
        # Fake a successful probe so sample() is the part being tested.
        monkeypatch.setattr("atfield.collectors.amd.find_rocm_smi", lambda: "/fake/rocm-smi")

        def fake_run(*args, **kwargs):
            class _R:
                returncode = 0
                stdout = json.dumps({"card0": {"Temperature (Edge)": 55}})
                stderr = ""
            return _R()

        monkeypatch.setattr("subprocess.run", fake_run)

        c = AmdCollector()
        c.probe()
        with pytest.raises(NotImplementedError) as excinfo:
            c.sample()
        msg = str(excinfo.value)
        # Must point users at the working alternative, not just say "TODO".
        assert "LibreHardwareMonitor" in msg
        assert "v0.3" in msg.lower() or "future" in msg.lower() or "track" in msg.lower()


class TestFindRocmSmi:
    def test_returns_none_when_neither_variant_on_path(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda x: None)
        assert find_rocm_smi() is None

    def test_returns_path_when_unix_variant_present(self, monkeypatch):
        def fake_which(name: str) -> str | None:
            return "/usr/bin/rocm-smi" if name == "rocm-smi" else None
        monkeypatch.setattr("shutil.which", fake_which)
        assert find_rocm_smi() == "/usr/bin/rocm-smi"
