"""Hermetic pytest/CI bootstrap coverage for import-time Config use."""

from pathlib import Path

from cctelegram.config import config

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BOOTSTRAP_TOKEN = config.telegram_bot_token
_BOOTSTRAP_ALLOWED_USERS = set(config.allowed_users)
_BOOTSTRAP_CONFIG_DIR = config.config_dir


def test_pytest_bootstrap_uses_dummy_config_for_import_time_singleton():
    assert _BOOTSTRAP_TOKEN == "0000000000:pytest-dummy-token"
    assert _BOOTSTRAP_ALLOWED_USERS == {12345}
    assert _BOOTSTRAP_CONFIG_DIR != Path.home() / ".cc-telegram"
    assert _BOOTSTRAP_CONFIG_DIR.name.startswith("cc-telegram-pytest-")
    assert _BOOTSTRAP_CONFIG_DIR.is_dir()


def test_check_workflow_supplies_dummy_config_env():
    workflow = (_REPO_ROOT / ".github" / "workflows" / "check.yml").read_text()

    assert 'TELEGRAM_BOT_TOKEN: "0000000000:ci-dummy-token"' in workflow
    assert 'ALLOWED_USERS: "12345"' in workflow
    assert "CC_TELEGRAM_DIR: ${{ runner.temp }}/cc-telegram-config" in workflow
