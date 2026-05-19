"""Execute screenshot quick-key callbacks.

Core responsibilities:
  - Own CB_KEYS_PREFIX screenshot control-key callbacks.
  - Re-check topic/window ownership before sending keys to tmux.
  - Refresh screenshot media after successful key delivery when possible.

Key components:
  - execute_bash_callback()
"""

from __future__ import annotations

from typing import Any

import asyncio
import io
from telegram import InputMediaDocument

from cctelegram.handlers.callback_data import CB_KEYS_PREFIX
from cctelegram.screenshot import text_to_image

from . import window_lease
from .screenshot import KEY_LABELS, KEYS_SEND_MAP, build_screenshot_keyboard


async def execute_bash_callback(authorized: Any, adapters: Any) -> None:
    query = authorized.ctx.query
    data = authorized.command.data
    lease = window_lease(authorized, adapters)
    tmux_manager = adapters.tmux_manager

    async def reject_stale_window_callback(window_id: str) -> bool:
        return await lease.reject_stale_window(window_id)

    # Screenshot quick keys: send key to tmux window
    if data.startswith(CB_KEYS_PREFIX):
        rest = data[len(CB_KEYS_PREFIX) :]
        colon_idx = rest.find(":")
        if colon_idx < 0:
            await query.answer("Invalid data")
            return
        key_id = rest[:colon_idx]
        window_id = rest[colon_idx + 1 :]

        key_info = KEYS_SEND_MAP.get(key_id)
        if not key_info:
            await query.answer("Unknown key")
            return

        tmux_key, enter, literal = key_info
        if await reject_stale_window_callback(window_id):
            return
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await query.answer("Window not found", show_alert=True)
            return

        await tmux_manager.send_keys(
            w.window_id, tmux_key, enter=enter, literal=literal
        )
        await query.answer(KEY_LABELS.get(key_id, key_id))

        # Refresh screenshot after key press
        await asyncio.sleep(0.5)
        text = await tmux_manager.capture_pane(w.window_id, with_ansi=True)
        if text:
            png_bytes = await text_to_image(text, with_ansi=True)
            keyboard = build_screenshot_keyboard(window_id)
            try:
                await query.edit_message_media(
                    media=InputMediaDocument(
                        media=io.BytesIO(png_bytes),
                        filename="screenshot.png",
                    ),
                    reply_markup=keyboard,
                )
            except Exception:
                pass  # Screenshot unchanged or message too old
