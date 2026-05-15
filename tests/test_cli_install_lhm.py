"""Tests for ``atf install-lhm`` -- the self-service LHM installer.

Covers the dev/manual-install path (the bundled NSIS installer ships LHM
out of the box and doesn't need this command).

We don't actually hit GitHub here; ``urllib.request.urlopen`` is monkey-
patched to return a tiny in-memory zip file with the right names so we
exercise the extract/flatten/config-drop logic without network flakes.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from atfield import cli


def _make_fake_lhm_zip(*, nested: bool = False) -> bytes:
    """Build an in-memory zip mimicking the LHM release layout.

    The real zip ships flat; some builds wrap the files in a
    ``LibreHardwareMonitor/`` subdirectory. The flatten path in
    install-lhm covers the second case.
    """
    buf = io.BytesIO()
    prefix = "LibreHardwareMonitor/" if nested else ""
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{prefix}LibreHardwareMonitor.exe", b"FAKE EXE BYTES")
        z.writestr(f"{prefix}LibreHardwareMonitorLib.dll", b"FAKE DLL")
        z.writestr(f"{prefix}README.md", b"vendor-supplied README")
    return buf.getvalue()


class _FakeUrlopenResponse(io.BytesIO):
    """Context-managed BytesIO so ``with urlopen(...) as resp:`` works."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestInstallLhm:
    def test_flat_zip_extracts_to_install_dir(self, tmp_path, runner):
        zip_bytes = _make_fake_lhm_zip(nested=False)
        with patch(
            "urllib.request.urlopen",
            return_value=_FakeUrlopenResponse(zip_bytes),
        ):
            result = runner.invoke(
                cli.app,
                ["install-lhm", "--dir", str(tmp_path), "--no-env-hint"],
            )
        assert result.exit_code == 0, result.output
        assert (tmp_path / "LibreHardwareMonitor.exe").is_file()
        assert (tmp_path / "LibreHardwareMonitorLib.dll").is_file()

    def test_nested_zip_is_flattened(self, tmp_path, runner):
        # The flatten branch runs only when the binary isn't directly in
        # install_dir after extraction.
        zip_bytes = _make_fake_lhm_zip(nested=True)
        with patch(
            "urllib.request.urlopen",
            return_value=_FakeUrlopenResponse(zip_bytes),
        ):
            result = runner.invoke(
                cli.app,
                ["install-lhm", "--dir", str(tmp_path), "--no-env-hint"],
            )
        assert result.exit_code == 0, result.output
        # After flattening, the binary should be at install_dir, not nested.
        assert (tmp_path / "LibreHardwareMonitor.exe").is_file()
        assert not (tmp_path / "LibreHardwareMonitor").exists(), (
            "nested directory should have been collapsed"
        )

    def test_skips_when_already_present(self, tmp_path, runner):
        # Pre-populate; install-lhm should detect and skip the download.
        (tmp_path / "LibreHardwareMonitor.exe").write_bytes(b"existing")
        with patch("urllib.request.urlopen") as urlopen_mock:
            result = runner.invoke(
                cli.app,
                ["install-lhm", "--dir", str(tmp_path), "--no-env-hint"],
            )
        assert result.exit_code == 0
        urlopen_mock.assert_not_called()
        # Original bytes are preserved.
        assert (tmp_path / "LibreHardwareMonitor.exe").read_bytes() == b"existing"

    def test_force_reinstalls_over_existing(self, tmp_path, runner):
        (tmp_path / "LibreHardwareMonitor.exe").write_bytes(b"existing")
        zip_bytes = _make_fake_lhm_zip(nested=False)
        with patch(
            "urllib.request.urlopen",
            return_value=_FakeUrlopenResponse(zip_bytes),
        ):
            result = runner.invoke(
                cli.app,
                ["install-lhm", "--dir", str(tmp_path), "--no-env-hint", "--force"],
            )
        assert result.exit_code == 0
        # Bytes from the fake zip clobbered the existing exe.
        assert (
            (tmp_path / "LibreHardwareMonitor.exe").read_bytes()
            == b"FAKE EXE BYTES"
        )

    def test_drops_prebaked_config_when_available(self, tmp_path, runner):
        zip_bytes = _make_fake_lhm_zip(nested=False)
        # Run from the repo root so the vendor/lhm/.../config fallback hits.
        # (The vendored config is tracked in git; see vendor/lhm/.gitignore.)
        repo_root = Path(__file__).parents[1]
        original_cwd = Path.cwd()
        try:
            import os
            os.chdir(repo_root)
            with patch(
                "urllib.request.urlopen",
                return_value=_FakeUrlopenResponse(zip_bytes),
            ):
                result = runner.invoke(
                    cli.app,
                    ["install-lhm", "--dir", str(tmp_path), "--no-env-hint"],
                )
        finally:
            os.chdir(original_cwd)
        assert result.exit_code == 0
        config = tmp_path / "LibreHardwareMonitor.config"
        assert config.is_file(), "pre-baked config should be staged next to LHM"
        # Sanity: the config file has the WebServerEnabled key we ship.
        assert "WebServer" in config.read_text(), config.read_text()[:200]

    def test_env_hint_prints_setx_on_windows(self, tmp_path, runner, monkeypatch):
        zip_bytes = _make_fake_lhm_zip(nested=False)
        # Pretend we're on Windows even when the test runs on Linux/macOS.
        monkeypatch.setattr("sys.platform", "win32")
        with patch(
            "urllib.request.urlopen",
            return_value=_FakeUrlopenResponse(zip_bytes),
        ):
            result = runner.invoke(
                cli.app,
                ["install-lhm", "--dir", str(tmp_path)],
            )
        assert result.exit_code == 0
        assert "setx ATFIELD_LHM_EXE" in result.output
