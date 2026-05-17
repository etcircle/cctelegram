"""Tests for CC Telegram doctor migration, preflight, and health checks.

Covers retry-safe migration staging, the rewritten preflight error message,
the CC_TELEGRAM_DIR legacy-state warning, and the fresh-setup health-check
readout. All tests run against tmp_path; HOME is monkeypatched where needed.
"""

import json
from pathlib import Path

import pytest

from cctelegram import doctor


def _write_required_legacy_state(legacy: Path) -> None:
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "state.json").write_text('{"ok": true}', encoding="utf-8")
    (legacy / "session_map.json").write_text("{}", encoding="utf-8")
    (legacy / "message_refs.db").write_bytes(b"sqlite-placeholder")


class TestMigrationNeeded:
    def test_true_when_legacy_exists_and_target_missing(self, tmp_path: Path) -> None:
        legacy = tmp_path / ".ccbot"
        target = tmp_path / ".cc-telegram"
        legacy.mkdir()

        assert doctor.migration_needed(legacy, target) is True

    def test_false_when_target_exists(self, tmp_path: Path) -> None:
        legacy = tmp_path / ".ccbot"
        target = tmp_path / ".cc-telegram"
        legacy.mkdir()
        target.mkdir()

        assert doctor.migration_needed(legacy, target) is False

    def test_command_points_at_explicit_copy(self, tmp_path: Path) -> None:
        legacy = tmp_path / ".ccbot"
        target = tmp_path / ".cc-telegram"

        assert doctor.migration_command(legacy, target) == (
            f"mkdir -p {target} && cp -R {legacy}/. {target}/"
        )


class TestPreflight:
    def test_blocks_when_legacy_exists_and_target_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        legacy = tmp_path / ".ccbot"
        target = tmp_path / ".cc-telegram"
        legacy.mkdir()
        monkeypatch.delenv("CC_TELEGRAM_DIR", raising=False)

        with pytest.raises(SystemExit) as exc:
            doctor.preflight_or_exit(legacy, target)

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "State migration required." in err
        assert "cc-telegram doctor --migrate" in err
        assert "Manual fallback:" in err
        assert f"cp -R {legacy}/. {target}/" in err
        assert "CC_TELEGRAM_DIR=" in err

    def test_skips_guard_when_env_dir_explicit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        legacy = tmp_path / ".ccbot"
        target = tmp_path / ".cc-telegram"
        legacy.mkdir()
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(target))

        doctor.preflight_or_exit(legacy, target)
        assert capsys.readouterr().err == ""

    def test_warns_when_env_dir_resolves_to_legacy_named_dir(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        legacy = tmp_path / ".ccbot"
        target = tmp_path / ".ccbot.alt"
        target.mkdir()
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(target))

        doctor.preflight_or_exit(legacy, target)

        err = capsys.readouterr().err
        assert "Warning: CC_TELEGRAM_DIR=" in err
        assert str(target) in err
        assert "cc-telegram doctor --migrate" in err

    def test_warns_when_env_dir_has_legacy_session_map_keys(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        legacy = tmp_path / ".ccbot"
        target = tmp_path / "custom-config"
        target.mkdir()
        (target / "session_map.json").write_text(
            json.dumps({"ccbot:@0": {"session_id": "x"}}), encoding="utf-8"
        )
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(target))

        doctor.preflight_or_exit(legacy, target)

        err = capsys.readouterr().err
        assert "Warning: CC_TELEGRAM_DIR=" in err


def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    legacy = home / ".ccbot"
    target = home / ".cc-telegram"
    monkeypatch.setenv("CC_TELEGRAM_DIR", str(target))
    return legacy, target


def _stub_environment_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("TELEGRAM_BOT_TOKEN", "ALLOWED_USERS"):
        monkeypatch.delenv(key, raising=False)


def _stub_environment_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "stub-token")
    monkeypatch.setenv("ALLOWED_USERS", "1234")
    monkeypatch.setattr(
        doctor.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"tmux", "claude"} else None,
    )
    settings = Path.home() / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"command": "cc-telegram hook"}]},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )


class TestDoctorMigrate:
    def test_migrate_stages_and_finalizes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        legacy, target = _isolate_home(tmp_path, monkeypatch)
        _write_required_legacy_state(legacy)
        _stub_environment_clean(monkeypatch)

        assert doctor.doctor_main(["--migrate"]) == 0

        assert (target / "state.json").read_text(encoding="utf-8") == '{"ok": true}'
        assert (target / "session_map.json").is_file()
        assert (target / "message_refs.db").is_file()
        assert (target / doctor.MIGRATION_SENTINEL).is_file()
        out = capsys.readouterr().out
        assert f"Migrated {legacy} -> {target}" in out
        # No staging dirs left behind
        assert not any(
            p.name.startswith(doctor.STAGING_PREFIX) for p in target.parent.iterdir()
        )

    def test_migrate_retry_after_sentinel_is_noop(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        legacy, target = _isolate_home(tmp_path, monkeypatch)
        _write_required_legacy_state(legacy)
        target.mkdir()
        (target / doctor.MIGRATION_SENTINEL).write_text("ok\n", encoding="utf-8")
        _stub_environment_clean(monkeypatch)

        assert doctor.doctor_main(["--migrate"]) == 0

        out = capsys.readouterr().out
        assert "Migration already complete." in out

    def test_migrate_cleans_orphan_staging_dirs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        legacy, target = _isolate_home(tmp_path, monkeypatch)
        _write_required_legacy_state(legacy)
        _stub_environment_clean(monkeypatch)

        orphan = target.parent / f"{doctor.STAGING_PREFIX}99999"
        orphan.mkdir()
        (orphan / "junk.txt").write_text("partial", encoding="utf-8")

        assert doctor.doctor_main(["--migrate"]) == 0

        assert not orphan.exists()
        assert (target / doctor.MIGRATION_SENTINEL).is_file()
        # The final pid-staging dir must also be gone (renamed into target)
        assert not any(
            p.name.startswith(doctor.STAGING_PREFIX) for p in target.parent.iterdir()
        )

    def test_migrate_fails_when_staged_state_incomplete(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        legacy, target = _isolate_home(tmp_path, monkeypatch)
        legacy.mkdir()
        (legacy / "state.json").write_text("{}", encoding="utf-8")
        # Intentionally omit session_map.json and message_refs.db
        _stub_environment_clean(monkeypatch)

        assert doctor.doctor_main(["--migrate"]) == 1

        out = capsys.readouterr().out
        assert "ERROR" in out
        assert "missing required files" in out
        assert "session_map.json" in out
        assert "message_refs.db" in out
        assert not target.exists()
        # Staging dir cleaned up
        assert not any(
            p.name.startswith(doctor.STAGING_PREFIX) for p in target.parent.iterdir()
        )

    def test_migrate_refuses_when_target_exists_without_sentinel(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        legacy, target = _isolate_home(tmp_path, monkeypatch)
        _write_required_legacy_state(legacy)
        target.mkdir()
        (target / "state.json").write_text("existing", encoding="utf-8")
        _stub_environment_clean(monkeypatch)

        assert doctor.doctor_main(["--migrate"]) == 1

        out = capsys.readouterr().out
        assert "ERROR: migration skipped because target state dir already exists" in out
        assert (target / "state.json").read_text(encoding="utf-8") == "existing"


class TestDoctorReport:
    def test_reports_migration_available_without_copying(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        legacy, target = _isolate_home(tmp_path, monkeypatch)
        legacy.mkdir()
        _stub_environment_healthy(monkeypatch)

        assert doctor.doctor_main([]) == 1

        out = capsys.readouterr().out
        assert "Migration available" in out
        assert f"cp -R {legacy}/. {target}/" in out
        assert not target.exists()

    def test_non_migrate_is_ok_when_legacy_and_target_both_exist(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        legacy, target = _isolate_home(tmp_path, monkeypatch)
        legacy.mkdir()
        target.mkdir()
        _stub_environment_healthy(monkeypatch)

        assert doctor.doctor_main([]) == 0

        out = capsys.readouterr().out
        assert "OK: both legacy and new state dirs exist" in out
        assert "migration skipped" not in out


class TestHealthChecks:
    def test_health_happy_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _, target = _isolate_home(tmp_path, monkeypatch)
        target.mkdir()
        (target / ".env").write_text(
            'TELEGRAM_BOT_TOKEN="abc"\nALLOWED_USERS=123\n',
            encoding="utf-8",
        )

        # tmux_manager.py monkey-patches process-wide shutil.which to cache the
        # tmux binary path. Override the doctor module's shutil.which directly
        # so the health probe sees what we want regardless of import order.
        fake_which = {"tmux": "/usr/local/bin/tmux", "claude": "/usr/local/bin/claude"}
        monkeypatch.setattr(
            doctor.shutil, "which", lambda cmd, *a, **k: fake_which.get(cmd)
        )

        # Hook installed in fake home settings.json.
        settings = tmp_path / "home" / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "cc-telegram hook",
                                    }
                                ]
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        _stub_environment_clean(monkeypatch)
        assert doctor.doctor_main([]) == 0

        out = capsys.readouterr().out
        assert "OK   TELEGRAM_BOT_TOKEN" in out
        assert "OK   ALLOWED_USERS" in out
        assert "OK   tmux on PATH" in out
        assert "OK   claude on PATH" in out
        assert "OK   SessionStart hook" in out
        assert "OK   config dir writable" in out
        # Summary line: 6 ok / 0 warn / 0 fail
        assert "6 ok / 0 warn / 0 fail" in out

    def test_health_reports_missing_token_and_missing_tools(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _, target = _isolate_home(tmp_path, monkeypatch)
        target.mkdir()
        # No .env, no env vars, no tmux/claude binaries discoverable.
        _stub_environment_clean(monkeypatch)
        monkeypatch.setattr(doctor.shutil, "which", lambda cmd, *a, **k: None)

        # Settings file absent so hook check fails too.
        assert doctor.doctor_main([]) == 1

        out = capsys.readouterr().out
        assert "FAIL TELEGRAM_BOT_TOKEN" in out
        assert "FAIL ALLOWED_USERS" in out
        assert "FAIL tmux not on PATH (fix: brew install tmux)" in out
        assert "FAIL claude not on PATH (fix: install Claude Code CLI)" in out
        assert "FAIL SessionStart hook" in out
        # config dir is writable; that one is OK.
        assert "OK   config dir writable" in out
        summary = [line for line in out.splitlines() if line.endswith(" fail")][-1]
        assert summary.startswith("1 ok / 0 warn / 5 fail")

    def test_health_warns_when_hook_missing_with_settings_present(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _, target = _isolate_home(tmp_path, monkeypatch)
        target.mkdir()
        _stub_environment_healthy(monkeypatch)
        # Override the healthy stub: settings file exists but contains no hook.
        settings = Path.home() / ".claude" / "settings.json"
        settings.write_text(json.dumps({"hooks": {}}), encoding="utf-8")

        assert doctor.doctor_main([]) == 0

        out = capsys.readouterr().out
        assert "WARN SessionStart hook" in out
        assert "run `cc-telegram hook --install`" in out
