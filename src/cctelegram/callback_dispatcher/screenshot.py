"""Build and execute screenshot refresh callbacks.

Core responsibilities:
  - Build screenshot keyboards and fail loudly if callback_data exceeds 64 bytes.
  - Own CB_SCREENSHOT_REFRESH execution.
  - Refresh screenshot media only after topic/window ownership is revalidated.

Key components:
  - KEYS_SEND_MAP
  - KEY_LABELS
  - build_screenshot_keyboard()
  - execute_screenshot_callback()
"""

from __future__ import annotations

from typing import Any

import io
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaDocument

from cctelegram.handlers.callback_data import CB_KEYS_PREFIX, CB_SCREENSHOT_REFRESH
from cctelegram.screenshot import text_to_image

from . import checked_callback_data, window_lease

logger = logging.getLogger(__name__)

KEYS_SEND_MAP: dict[str, tuple[str, bool, bool]] = {
    "up": ("Up", False, False),
    "dn": ("Down", False, False),
    "lt": ("Left", False, False),
    "rt": ("Right", False, False),
    "esc": ("Escape", False, False),
    "ent": ("Enter", False, False),
    "spc": ("Space", False, False),
    "tab": ("Tab", False, False),
    "cc": ("C-c", False, False),
}
KEY_LABELS: dict[str, str] = {
    "up": "↑",
    "dn": "↓",
    "lt": "←",
    "rt": "→",
    "esc": "⎋ Esc",
    "ent": "⏎ Enter",
    "spc": "␣ Space",
    "tab": "⇥ Tab",
    "cc": "^C",
}


def build_screenshot_keyboard(window_id: str) -> InlineKeyboardMarkup:
    """Build inline keyboard for screenshot controls and refresh."""

    def btn(label: str, key_id: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            label,
            callback_data=checked_callback_data(
                f"{CB_KEYS_PREFIX}{key_id}:{window_id}"
            ),
        )

    return InlineKeyboardMarkup(
        [
            [btn("␣ Space", "spc"), btn("↑", "up"), btn("⇥ Tab", "tab")],
            [btn("←", "lt"), btn("↓", "dn"), btn("→", "rt")],
            [btn("⎋ Esc", "esc"), btn("^C", "cc"), btn("⏎ Enter", "ent")],
            [
                InlineKeyboardButton(
                    "🔄 Refresh",
                    callback_data=checked_callback_data(
                        f"{CB_SCREENSHOT_REFRESH}{window_id}"
                    ),
                )
            ],
        ]
    )


async def execute_screenshot_callback(authorized: Any, adapters: Any) -> None:
    query = authorized.ctx.query
    data = authorized.command.data
    lease = window_lease(authorized, adapters)
    tmux_manager = adapters.tmux_manager

    async def reject_stale_window_callback(window_id: str) -> bool:
        return await lease.reject_stale_window(window_id)

    # Screenshot: Refresh
    if data.startswith(CB_SCREENSHOT_REFRESH):
        window_id = data[len(CB_SCREENSHOT_REFRESH) :]
        if await reject_stale_window_callback(window_id):
            return
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await query.answer("Window no longer exists", show_alert=True)
            return

        text = await tmux_manager.capture_pane(w.window_id, with_ansi=True)
        if not text:
            await query.answer("Failed to capture pane", show_alert=True)
            return

        png_bytes = await text_to_image(text, with_ansi=True)
        keyboard = build_screenshot_keyboard(window_id)
        try:
            await query.edit_message_media(
                media=InputMediaDocument(
                    media=io.BytesIO(png_bytes), filename="screenshot.png"
                ),
                reply_markup=keyboard,
            )
            await query.answer("Refreshed")
        except Exception as e:
            logger.error(f"Failed to refresh screenshot: {e}")
            await query.answer("Failed to refresh", show_alert=True)
