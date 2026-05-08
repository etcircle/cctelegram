"""Tests for CCTelegram doctor diagnostics."""

from pathlib import Path

import pytest

from cctelegram import doctor


class TestDefaultStateDir:
    def test_points_at_canonical_home_dir(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(Path, "home", lambda: Path("/home/tester"))

        assert doctor.default_state_dir() == Path("/home/tester/.cctelegram")


class TestPreflight:
    def test_preflight_is_noop(self) -> None:
        assert doctor.preflight_or_exit() is None


class TestDoctorMain:
    def test_reports_canonical_paths(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("CCTELEGRAM_DIR", str(tmp_path))

        assert doctor.doctor_main([]) == 0

        out = capsys.readouterr().out
        assert f"OK: CCTelegram state dir is {tmp_path}" in out
        assert "Env override: CCTELEGRAM_DIR" in out
        assert "Hook install: cctelegram hook --install" in out
