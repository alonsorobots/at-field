"""Tests for :mod:`atfield.lhm_config`.

Verify the central robustness property: after a single call to
``ensure_lhm_config`` -- whatever was on disk before -- AT-Field's
required keys are present at the correct values, unrelated keys
survive, and the write is atomic.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from atfield.lhm_config import (
    LHM_CONFIG_FILENAME,
    REQUIRED_KEYS,
    ensure_lhm_config,
)


def _parse(cfg_path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for app in ET.parse(cfg_path).getroot().iter("appSettings"):
        for add in app.findall("add"):
            k = add.get("key")
            v = add.get("value")
            if k is not None and v is not None:
                out[k] = v
    return out


class TestEnsureFromScratch:
    def test_creates_file_when_missing(self, tmp_path):
        out = ensure_lhm_config(tmp_path, port=8085)
        assert out == tmp_path / LHM_CONFIG_FILENAME
        assert out.exists()

    def test_creates_parent_dir_when_missing(self, tmp_path):
        nested = tmp_path / "deep" / "lhm-bundle"
        out = ensure_lhm_config(nested, port=8085)
        assert out.exists()
        assert out.parent == nested

    def test_writes_all_required_keys(self, tmp_path):
        ensure_lhm_config(tmp_path, port=8085)
        settings = _parse(tmp_path / LHM_CONFIG_FILENAME)
        for key in REQUIRED_KEYS:
            assert key in settings

    def test_writes_correct_values_for_required_keys(self, tmp_path):
        ensure_lhm_config(tmp_path, port=8085)
        settings = _parse(tmp_path / LHM_CONFIG_FILENAME)
        assert settings["runWebServerMenuItem"] == "True"
        assert settings["webServerPortNumeric.Value"] == "8085"
        assert settings["startMinMenuItem"] == "True"
        assert settings["minimizeToTrayMenuItem"] == "True"
        assert settings["checkUpdatesAtStartMenuItem"] == "False"

    def test_port_is_interpolated(self, tmp_path):
        ensure_lhm_config(tmp_path, port=9090)
        settings = _parse(tmp_path / LHM_CONFIG_FILENAME)
        assert settings["webServerPortNumeric.Value"] == "9090"


class TestPatchExisting:
    def _write_existing(self, tmp_path: Path, **settings: str) -> Path:
        path = tmp_path / LHM_CONFIG_FILENAME
        lines = [
            '<?xml version="1.0" encoding="utf-8"?>',
            "<configuration>",
            "  <appSettings>",
        ]
        for k, v in settings.items():
            lines.append(f'    <add key="{k}" value="{v}" />')
        lines.append("  </appSettings>")
        lines.append("</configuration>")
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def test_overwrites_wrong_value_for_required_key(self, tmp_path):
        self._write_existing(tmp_path, runWebServerMenuItem="False")
        ensure_lhm_config(tmp_path, port=8085)
        settings = _parse(tmp_path / LHM_CONFIG_FILENAME)
        assert settings["runWebServerMenuItem"] == "True"

    def test_changes_port_to_match_supervisor(self, tmp_path):
        self._write_existing(tmp_path, **{"webServerPortNumeric.Value": "1234"})
        ensure_lhm_config(tmp_path, port=8085)
        settings = _parse(tmp_path / LHM_CONFIG_FILENAME)
        assert settings["webServerPortNumeric.Value"] == "8085"

    def test_preserves_unrelated_keys(self, tmp_path):
        self._write_existing(
            tmp_path,
            userFavoriteSensor="GPU Core Temp",
            customFanCurveProfile="silent",
            runWebServerMenuItem="False",  # we'll fix this
        )
        ensure_lhm_config(tmp_path, port=8085)
        settings = _parse(tmp_path / LHM_CONFIG_FILENAME)
        assert settings["userFavoriteSensor"] == "GPU Core Temp"
        assert settings["customFanCurveProfile"] == "silent"
        # And ours got fixed
        assert settings["runWebServerMenuItem"] == "True"

    def test_idempotent(self, tmp_path):
        ensure_lhm_config(tmp_path, port=8085)
        first = (tmp_path / LHM_CONFIG_FILENAME).read_bytes()
        ensure_lhm_config(tmp_path, port=8085)
        second = (tmp_path / LHM_CONFIG_FILENAME).read_bytes()
        assert first == second, "second call must produce identical bytes"

    def test_corrupt_xml_is_replaced_not_propagated(self, tmp_path):
        path = tmp_path / LHM_CONFIG_FILENAME
        path.write_text("not <valid> xml &!@#", encoding="utf-8")
        # Should not raise.
        ensure_lhm_config(tmp_path, port=8085)
        settings = _parse(tmp_path / LHM_CONFIG_FILENAME)
        # Required keys still got written
        for key in REQUIRED_KEYS:
            assert key in settings


class TestAtomicWrite:
    def test_no_stale_temp_files_left_behind(self, tmp_path):
        ensure_lhm_config(tmp_path, port=8085)
        leftover = list(tmp_path.glob(".lhmcfg.*.tmp"))
        assert leftover == [], (
            f"atomic write should clean up its temp files; saw {leftover}"
        )

    def test_temp_file_is_cleaned_up_on_write_failure(self, tmp_path, monkeypatch):
        # Force os.replace to fail; verify the .tmp file is removed.
        import os

        original = os.replace
        call_count = {"n": 0}

        def boom(*args, **kwargs):
            call_count["n"] += 1
            raise OSError("simulated rename failure")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            ensure_lhm_config(tmp_path, port=8085)
        leftover = list(tmp_path.glob(".lhmcfg.*.tmp"))
        assert leftover == [], (
            f"temp file should be removed when os.replace fails; saw {leftover}"
        )
        assert call_count["n"] >= 1, "monkeypatched os.replace should have been called"
        # Restore for safety (monkeypatch handles it, but explicit).
        monkeypatch.setattr(os, "replace", original)


class TestSchemaCompatibility:
    """The output must round-trip through standard XML parsers so LHM
    (which is a .NET app reading via System.Configuration) accepts it.
    """

    def test_output_parses_as_well_formed_xml(self, tmp_path):
        ensure_lhm_config(tmp_path, port=8085)
        # Will raise on malformed XML.
        tree = ET.parse(tmp_path / LHM_CONFIG_FILENAME)
        root = tree.getroot()
        assert root.tag == "configuration"
        app_settings = root.find("appSettings")
        assert app_settings is not None
        adds = app_settings.findall("add")
        assert len(adds) == len(REQUIRED_KEYS)

    def test_handles_special_characters_in_values(self, tmp_path):
        # If a future required key ever contains <, >, &, ", they must
        # be properly XML-escaped. We exercise this by writing an
        # existing file with such a value in an UNRELATED key (which
        # we preserve verbatim) and then re-asserting.
        path = tmp_path / LHM_CONFIG_FILENAME
        path.write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<configuration><appSettings>'
            '<add key="userNote" value="hi &amp; bye &lt;3" />'
            "</appSettings></configuration>",
            encoding="utf-8",
        )
        ensure_lhm_config(tmp_path, port=8085)
        # Reparse to verify our escaping round-trips.
        settings = _parse(tmp_path / LHM_CONFIG_FILENAME)
        assert settings["userNote"] == "hi & bye <3"
