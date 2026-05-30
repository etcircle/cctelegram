"""Regression guard: every ``cctelegram`` submodule must import standalone.

Background: a circular import among the handlers
(``interactive_ui`` ↔ ``callback_dispatcher`` ↔ ``inbound_telegram``) made
several modules fail to import unless ``cctelegram.bot`` was imported first,
warming ``sys.modules`` in a resolving order. The same class of defect has now
recurred twice — ``route_runtime`` (Wave 3 cleanup, fixed by relocating
``INTERACTIVE_TOOL_NAMES``) and ``checked_callback_data`` (relocated to the
``handlers.callback_data`` leaf) — so it gets a permanent guard.

Each module is imported in a FRESH subprocess on purpose: importing modules in
one process warms ``sys.modules`` so partially-initialised modules resolve,
which masks the cycle. Only a clean interpreter per module reliably detects it.
"""

from __future__ import annotations

import os
import pkgutil
import subprocess
import sys

import pytest

import cctelegram

# ``cctelegram.config`` instantiates ``config = Config()`` at module level,
# which requires TELEGRAM_BOT_TOKEN + ALLOWED_USERS. Supply dummy values to the
# subprocess (matching the per-test ``monkeypatch.setenv`` values used across
# the suite) so a missing-env failure can't masquerade as a circular import —
# the only failure this guard should catch is a genuine import cycle.
_IMPORT_ENV = {
    **os.environ,
    "TELEGRAM_BOT_TOKEN": "test:token",
    "ALLOWED_USERS": "12345",
}


def _all_submodules() -> list[str]:
    names: list[str] = []
    for info in pkgutil.walk_packages(
        cctelegram.__path__, prefix="cctelegram.", onerror=lambda _name: None
    ):
        names.append(info.name)
    return sorted(names)


@pytest.mark.parametrize("module_name", _all_submodules())
def test_submodule_imports_standalone(module_name: str) -> None:
    """`import <module>` in a clean interpreter must succeed (exit 0)."""
    result = subprocess.run(
        [sys.executable, "-c", f"import {module_name}"],
        capture_output=True,
        text=True,
        env=_IMPORT_ENV,
    )
    assert result.returncode == 0, (
        f"`import {module_name}` failed standalone — likely a circular import.\n"
        f"--- stderr ---\n{result.stderr}"
    )
