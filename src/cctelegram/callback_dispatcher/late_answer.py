"""Execute ``aql:`` late-answer callbacks (Wave A AFK auto-resolve adaptation).

Core responsibilities:
  - Own CB_ASK_LATE execution: parse → registry lookup → owner + stale-window
    auth → freshness guards (a NEWER live prompt wins) → begin_send single-use
    → sending-state edit (keyboard removed) → the effort.py route-ordering
    delivery subsequence (flush → PRE-SEND user-turn stamp → send_to_window →
    mark_inbound_sent) → success/failure edits.
  - Deliver the late answer as a NORMAL user text message (the CLI's own
    "re-ask later" pattern); never a picker keystroke — the aqp:/aqt:/
    pick_token/ledger machinery is byte-untouched.

NOT "line-for-line" from ``effort.py`` [R1 both P2]: effort clears the
keyboard BEFORE delivery, which would make the retry-on-failure branch
impossible — only its route-ordering delivery subsequence and its
window-revalidation are copied; a send FAILURE re-attaches the ORIGINAL
keyboard (rebuilt from the registry row) and resets the single-use gate.

Key components: execute_late_answer_callback().
"""

from __future__ import annotations

import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from cctelegram.handlers import auq_source, late_answer
from cctelegram.handlers.callback_data import CB_ASK_LATE
from cctelegram.handlers.inbound_aggregator import aggregator_flush_route
from cctelegram.handlers.interactive_ui import has_interactive_surface
from cctelegram.handlers.message_queue import set_route_user_turn_at
from cctelegram.handlers.message_sender import safe_edit

from . import STALE_CALLBACK_TEXT, WRONG_USER_PICK_TEXT, safe_answer, window_lease

logger = logging.getLogger(__name__)

EXPIRED_LATE_ANSWER_TEXT = (
    "This late-answer card has expired (bot restarted or superseded) — "
    "reply in text instead."
)
_EXPIRED_CARD_NOTICE = "⏰ This late-answer card has expired — reply in text instead."
NEWER_PROMPT_LIVE_TEXT = "A newer prompt is live in this topic — answer that instead."
ALREADY_SENT_TEXT = "Late answer already sent."


def _keyboard(window_id: str, labels: dict[int, str], token: str) -> Any:
    """Wrap the leaf's (label, callback_data) rows into an inline keyboard."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(label, callback_data=data)]
            for label, data in late_answer.keyboard_rows(window_id, labels, token)
        ]
    )


async def execute_late_answer_callback(authorized: Any, adapters: Any) -> None:
    user = authorized.ctx.user
    query = authorized.ctx.query
    data = authorized.command.data
    cb_thread_id = authorized.ctx.thread_id
    lease = window_lease(authorized, adapters)
    session_manager = adapters.session_manager
    tmux_manager = adapters.tmux_manager
    route_runtime = adapters.route_runtime

    if not data.startswith(CB_ASK_LATE):
        return

    # 1. Parse aql:<window_id>:<opt>:<token> — window ids never contain ':'.
    parts = data[len(CB_ASK_LATE) :].split(":")
    if len(parts) != 3 or not all(parts):
        await safe_answer(query, "Invalid data")
        return
    window_id, opt_str, token = parts
    try:
        opt = int(opt_str)
    except ValueError:
        await safe_answer(query, "Invalid data")
        return

    # 2. Registry lookup. None → post-restart / superseded — the registry
    # cannot reconstruct the card, so answer the graceful expired modal and
    # best-effort clear the dead keyboard. The Telegram message itself is
    # the only text source [R1 Codex P3].
    row = late_answer.lookup(token)
    if row is None:
        await safe_answer(query, EXPIRED_LATE_ANSWER_TEXT, show_alert=True)
        message = getattr(query, "message", None)
        existing_text = getattr(message, "text", None) if message else None
        await safe_edit(
            query,
            existing_text if existing_text else _EXPIRED_CARD_NOTICE,
            reply_markup=None,
        )
        return

    # 3. Owner check (the aqp:/stg: precedent) — BEFORE the lease check.
    if user.id != row.owner_id:
        await safe_answer(query, WRONG_USER_PICK_TEXT, show_alert=True)
        return

    # 4. Stale window: payload/registry parity, then the topic-ownership
    # lease, then live-window existence (the effort.py precedent — a gone
    # window is stale, not an error).
    if window_id != row.window_id:
        await safe_answer(query, STALE_CALLBACK_TEXT, show_alert=True)
        return
    if await lease.reject_stale_window(window_id):
        return
    if not await tmux_manager.find_window_by_id(window_id):
        await safe_answer(query, STALE_CALLBACK_TEXT, show_alert=True)
        return

    label = row.labels.get(opt)
    if label is None:
        await safe_answer(query, "Invalid data")
        return

    # 5. Freshness guards (read-only, cheap): a NEWER live prompt owns the
    # topic — a late answer must not be typed into it. The PreToolUse hook
    # writes the side file BEFORE a new picker renders, closing the
    # JSONL-buffered-tool_use gap.
    if has_interactive_surface(user.id, cb_thread_id):
        await safe_answer(query, NEWER_PROMPT_LIVE_TEXT, show_alert=True)
        return
    if auq_source.side_file_live_for_window(window_id):
        await safe_answer(query, NEWER_PROMPT_LIVE_TEXT, show_alert=True)
        return

    # 6. Single-use: live → in_flight (a concurrent second tap lands here).
    if not late_answer.begin_send(token):
        await safe_answer(query, ALREADY_SENT_TEXT, show_alert=False)
        return

    # 7. Sending state [R1 Hermes P2]: keyboard REMOVED while in flight so
    # the card is visually un-tappable (no concurrent-tap ambiguity — a
    # second tap already hit begin_send=False).
    base_text = late_answer.card_text(row.question, with_keyboard=True)
    await safe_edit(query, f"{base_text}\n⏳ Sending: {label}…", reply_markup=None)
    await safe_answer(query)

    # 8. Delivery — the effort.py route-ordering subsequence: flush the
    # aggregator, stamp the user turn PRE-SEND (the late answer is a genuine
    # user turn — live-prose turn-boundary + dashboard 🔔 semantics identical
    # to a typed message), send, then mark_inbound_sent on success.
    route = (user.id, cb_thread_id or 0, window_id)
    await aggregator_flush_route(route)
    set_route_user_turn_at(user.id, cb_thread_id or 0, window_id)
    text = late_answer.correction_message(row.question, label)
    success, send_msg = await session_manager.send_to_window(window_id, text)
    if success:
        await route_runtime.mark_inbound_sent(route)
        late_answer.finish_send(token, True)
        await safe_edit(query, f"✅ Late answer sent: {label}", reply_markup=None)
    else:
        # send_to_window returns (bool, str) — the False branch MUST be
        # honored (feedback_tmux_send_keys_returns_false). Reset the
        # single-use gate and re-attach the ORIGINAL keyboard so the user
        # can retry.
        late_answer.finish_send(token, False)
        logger.warning("aql late-answer send failed window=%s: %s", window_id, send_msg)
        await safe_edit(
            query,
            f"{base_text}\n❌ {send_msg} — tap again to retry",
            reply_markup=_keyboard(window_id, row.labels, token),
        )
