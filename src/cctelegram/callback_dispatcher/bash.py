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

from . import safe_answer, window_lease
from .interactive import WINDOW_BUSY_TEXT, _lock_busy, _window_send_lock
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
            await safe_answer(query, "Invalid data")
            return
        key_id = rest[:colon_idx]
        window_id = rest[colon_idx + 1 :]

        key_info = KEYS_SEND_MAP.get(key_id)
        if not key_info:
            await safe_answer(query, "Unknown key")
            return

        tmux_key, enter, literal = key_info
        if await reject_stale_window_callback(window_id):
            return
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await safe_answer(query, "Window not found", show_alert=True)
            return

        # Wave 3b compound transaction (Hermes P2-5): hold the window send
        # lock across key-send → settle → pane capture, so no other writer
        # (a pick dispatch, user text) can land between the quick-key and the
        # capture that screenshots its effect — and the quick-key can't
        # interleave into someone else's in-flight transaction. Reject-if-held
        # via the shared busy answer. The capture is read-only but kept INSIDE
        # the lock so the refreshed screenshot reflects this key's effect, not
        # a concurrent writer's; the Telegram I/O (answer + media edit) runs
        # strictly AFTER release (the lock is a leaf). The ``_lock_busy``
        # check (held OR live waiters — the release→waiter-wakeup gap counts
        # as busy, Hermes Wave-3b P2-1) + acquire pair has no await between
        # them (atomic on the event loop — a genuine try-acquire).
        lock = _window_send_lock(tmux_manager, w.window_id)
        if _lock_busy(lock):
            await safe_answer(query, WINDOW_BUSY_TEXT)
            return
        text: str | None = None
        async with lock:
            send_ok = await tmux_manager.send_keys(
                w.window_id, tmux_key, enter=enter, literal=literal
            )
            if send_ok:
                # Settle, then capture for the screenshot refresh.
                await asyncio.sleep(0.5)
                text = await tmux_manager.capture_pane(w.window_id, with_ansi=True)
        if not send_ok:
            # send_keys returns False when the dispatch never reached tmux —
            # answer honestly and skip the dependent screenshot refresh.
            await safe_answer(
                query, "❌ Failed to send — window may be gone", show_alert=True
            )
            return
        await safe_answer(query, KEY_LABELS.get(key_id, key_id))

        if text:
            png_bytes = await text_to_image(text)
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
