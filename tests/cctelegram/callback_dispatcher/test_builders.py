"""Unit tests for callback-data builders owned by the dispatcher.

Verifies callback keyboards fail loudly at construction time instead of
silently truncating Telegram callback_data over the 64-byte limit.
"""

import pytest

from cctelegram.callback_dispatcher.screenshot import build_screenshot_keyboard


def test_screenshot_keyboard_rejects_callback_data_over_64_bytes() -> None:
    with pytest.raises(
        RuntimeError,
        match="callback_data exceeds Telegram 64-byte limit:",
    ):
        build_screenshot_keyboard("@" + "w" * 70)
