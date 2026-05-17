"""Pytest bootstrap for hermetic config-bearing imports.

This root-level conftest is imported before test modules are collected, so it
must provide safe dummy configuration for modules that instantiate
``cctelegram.config.config`` at import time.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

_DUMMY_CONFIG_DIR = Path(tempfile.mkdtemp(prefix="cc-telegram-pytest-"))


def _cleanup_dummy_config_dir() -> None:
    shutil.rmtree(_DUMMY_CONFIG_DIR, ignore_errors=True)


atexit.register(_cleanup_dummy_config_dir)

# Force these values so local developer secrets cannot influence test imports.
os.environ["TELEGRAM_BOT_TOKEN"] = "0000000000:pytest-dummy-token"
os.environ["ALLOWED_USERS"] = "12345"
os.environ["CC_TELEGRAM_DIR"] = str(_DUMMY_CONFIG_DIR)

# Prevent Config() from reading a real repo/cwd .env during collection-time
# singleton creation. Tests that intentionally validate .env loading should
# remove this env var with monkeypatch before constructing Config().
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
