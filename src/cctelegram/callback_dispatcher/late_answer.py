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

Round-1 review folds:
  - [Codex P1] the freshness guards are RE-CHECKED at the last possible
    point — after the aggregator flush, immediately before the user-turn
    stamp + send, with NO await between the (sync) re-check, the stamp, and
    the send call. Residual: the sub-await window between that sync re-check
    and the keys actually landing in tmux is the plan's disclosed A10.3
    hook-write residual (the PreToolUse hook writes the side file BEFORE a
    new picker renders, so the exposure is the hook-write instant only; a
    loss delivers literal text into a just-rendering picker's composer —
    bounded, no digits dispatched).
  - [Codex P2] an explicit ambiguity boundary around the in-flight region:
    a raise provably BEFORE the send attempt re-enables the card
    (finish_send False); a raise at/after the send attempt leaves the row
    as-committed (in_flight = honest brake / consumed = delivered) with one
    WARNING — mirroring the not_advanced vs commit_unconfirmed dispatch
    precedent. Success is committed synchronously the moment send_to_window
    returns True, before any further await.

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
    # second tap already hit begin_send=False). The callback ACK is deferred
    # to after the late re-check below so a late-block can still answer the
    # newer-prompt MODAL (a query can only be answered once).
    #
    # [Codex P2 fold] the whole post-begin_send region runs under an explicit
    # ambiguity boundary (mirrors the not_advanced vs commit_unconfirmed
    # dispatch precedent): a raise BEFORE the send attempt provably delivered
    # nothing → finish_send(False) re-enables the card; a raise AT/AFTER the
    # send attempt is ambiguous (the keys may have landed) → the row is left
    # as-committed (in_flight = the honest brake; text-reply is the escape
    # hatch) with one WARNING. A raise after a CONFIRMED success leaves the
    # row consumed — finish_send(True) commits synchronously the moment the
    # send returns success, BEFORE mark_inbound_sent / the ✅ edit, so a
    # delivered answer can never reset to live (double-send risk).
    base_text = late_answer.card_text(row.question, with_keyboard=True)
    send_attempted = False
    try:
        await safe_edit(query, f"{base_text}\n⏳ Sending: {label}…", reply_markup=None)

        # 8. Delivery — the effort.py route-ordering subsequence: flush the
        # aggregator, stamp the user turn PRE-SEND (the late answer is a
        # genuine user turn — live-prose turn-boundary + dashboard 🔔
        # semantics identical to a typed message), send, then
        # mark_inbound_sent on success.
        route = (user.id, cb_thread_id or 0, window_id)
        await aggregator_flush_route(route)

        # [Codex P1 fold] LATE freshness re-check: the step-5 guards ran
        # before several awaits (the sending edit, the flush), so a new AUQ
        # surface / PreToolUse side file appearing DURING those awaits would
        # still receive the late answer. Re-check both guards at the LAST
        # possible point — both are sync, and NOTHING awaits between this
        # re-check, the user-turn stamp, and the send call. The residual
        # sub-await window between this sync re-check and the keys landing
        # in tmux is the plan's disclosed A10.3 hook-write residual (see the
        # module docstring).
        if has_interactive_surface(user.id, cb_thread_id) or (
            auq_source.side_file_live_for_window(window_id)
        ):
            late_answer.finish_send(token, False)  # row back to live
            await safe_edit(
                query,
                base_text,
                reply_markup=_keyboard(window_id, row.labels, token),
            )
            await safe_answer(query, NEWER_PROMPT_LIVE_TEXT, show_alert=True)
            return

        set_route_user_turn_at(user.id, cb_thread_id or 0, window_id)
        text = late_answer.correction_message(row.question, label)
        send_attempted = True
        success, send_msg = await session_manager.send_to_window(window_id, text)
        if success:
            # Commit the consumption FIRST (sync) — the answer WAS delivered;
            # a later raise (mark_inbound_sent / the ✅ edit) must never
            # reset the row to live.
            late_answer.finish_send(token, True)
            await safe_answer(query)
            await route_runtime.mark_inbound_sent(route)
            await safe_edit(query, f"✅ Late answer sent: {label}", reply_markup=None)
        else:
            # send_to_window returns (bool, str) — the False branch MUST be
            # honored (feedback_tmux_send_keys_returns_false). Reset the
            # single-use gate and re-attach the ORIGINAL keyboard so the user
            # can retry.
            late_answer.finish_send(token, False)
            logger.warning(
                "aql late-answer send failed window=%s: %s", window_id, send_msg
            )
            await safe_answer(query)
            await safe_edit(
                query,
                f"{base_text}\n❌ {send_msg} — tap again to retry",
                reply_markup=_keyboard(window_id, row.labels, token),
            )
    except BaseException:
        if not send_attempted:
            # Provably nothing was sent — safe to re-enable the card.
            late_answer.finish_send(token, False)
        else:
            # Ambiguous (raise from the send itself) or post-success cleanup
            # failure (row already consumed): leave the row as-committed.
            logger.warning(
                "aql late-answer interrupted at/after the send attempt "
                "window=%s token_state=%s — row left as-committed "
                "(in_flight = ambiguous brake, consumed = delivered); "
                "text-reply is the escape hatch",
                window_id,
                getattr(late_answer.lookup(token), "state", "gone"),
            )
        raise
