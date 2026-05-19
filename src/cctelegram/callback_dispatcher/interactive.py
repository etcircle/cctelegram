"""Execute interactive AskUserQuestion navigation and pick callbacks.

Core responsibilities:
  - Own CB_ASK_* navigation, refresh, and tokenized pick callbacks.
  - Preserve wrong-user and stale-form safety for interactive picks.
  - Re-render interactive cards after dispatch through injected adapters.

Key components:
  - execute_interactive_callback()
"""

from __future__ import annotations

from typing import Any

import asyncio
import logging
from cctelegram.handlers import interactive_ui
from cctelegram.handlers.callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_PICK,
    CB_ASK_REFRESH,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_UP,
)
from cctelegram.handlers.inbound_telegram import _get_thread_id
from cctelegram.handlers.interactive_ui import (
    NAV_ESC_CLEAR,
    assert_nav_dispatchable,
    clear_interactive_msg,
    consume_pick_token,
    get_interactive_window,
    handle_interactive_ui,
    peek_pick_token,
)

from . import (
    WRONG_USER_PICK_TEXT,
    _answer_invalid_pending_picker_callback,
    _validate_pending_picker_callback,
    owner_matches,
    window_lease,
)

logger = logging.getLogger(__name__)
resolve_ask_tool_input = interactive_ui.resolve_ask_tool_input
_ask_tool_input_digest = interactive_ui._ask_tool_input_digest


async def execute_interactive_callback(authorized: Any, adapters: Any) -> None:
    update = authorized.ctx.update
    context = authorized.ctx.context
    user = authorized.ctx.user
    query = authorized.ctx.query
    data = authorized.command.data
    cb_thread_id = authorized.ctx.thread_id
    lease = window_lease(authorized, adapters)
    tmux_manager = adapters.tmux_manager

    async def reject_stale_window_callback(window_id: str) -> bool:
        return await lease.reject_stale_window(window_id)

    async def reject_invalid_pending_picker(
        expected_states: tuple[str, ...],
        answer_text: str,
    ) -> tuple[bool, int | None]:
        ok, pending_tid, _reason = _validate_pending_picker_callback(
            context.user_data,
            cb_thread_id,
            expected_states,
        )
        if ok:
            return False, pending_tid
        await _answer_invalid_pending_picker_callback(query, answer_text)
        return True, pending_tid

    # Interactive UI: Up arrow
    if data.startswith(CB_ASK_UP):
        window_id = data[len(CB_ASK_UP) :]
        thread_id = _get_thread_id(update)
        if await reject_stale_window_callback(window_id):
            return
        w = await assert_nav_dispatchable(
            query, user.id, thread_id, window_id, tmux_mgr=tmux_manager
        )
        if w is None:
            return
        await tmux_manager.send_keys(w.window_id, "Up", enter=False, literal=False)
        await asyncio.sleep(0.5)
        await handle_interactive_ui(
            context.bot,
            user.id,
            window_id,
            thread_id,
            tmux_mgr=tmux_manager,
            session_mgr=adapters.session_manager,
        )
        await query.answer()

    # Interactive UI: Down arrow
    elif data.startswith(CB_ASK_DOWN):
        window_id = data[len(CB_ASK_DOWN) :]
        thread_id = _get_thread_id(update)
        if await reject_stale_window_callback(window_id):
            return
        w = await assert_nav_dispatchable(
            query, user.id, thread_id, window_id, tmux_mgr=tmux_manager
        )
        if w is None:
            return
        await tmux_manager.send_keys(w.window_id, "Down", enter=False, literal=False)
        await asyncio.sleep(0.5)
        await handle_interactive_ui(
            context.bot,
            user.id,
            window_id,
            thread_id,
            tmux_mgr=tmux_manager,
            session_mgr=adapters.session_manager,
        )
        await query.answer()

    # Interactive UI: Left arrow
    elif data.startswith(CB_ASK_LEFT):
        window_id = data[len(CB_ASK_LEFT) :]
        thread_id = _get_thread_id(update)
        if await reject_stale_window_callback(window_id):
            return
        w = await assert_nav_dispatchable(
            query, user.id, thread_id, window_id, tmux_mgr=tmux_manager
        )
        if w is None:
            return
        await tmux_manager.send_keys(w.window_id, "Left", enter=False, literal=False)
        await asyncio.sleep(0.5)
        await handle_interactive_ui(
            context.bot,
            user.id,
            window_id,
            thread_id,
            tmux_mgr=tmux_manager,
            session_mgr=adapters.session_manager,
        )
        await query.answer()

    # Interactive UI: Right arrow
    elif data.startswith(CB_ASK_RIGHT):
        window_id = data[len(CB_ASK_RIGHT) :]
        thread_id = _get_thread_id(update)
        if await reject_stale_window_callback(window_id):
            return
        w = await assert_nav_dispatchable(
            query, user.id, thread_id, window_id, tmux_mgr=tmux_manager
        )
        if w is None:
            return
        await tmux_manager.send_keys(w.window_id, "Right", enter=False, literal=False)
        await asyncio.sleep(0.5)
        await handle_interactive_ui(
            context.bot,
            user.id,
            window_id,
            thread_id,
            tmux_mgr=tmux_manager,
            session_mgr=adapters.session_manager,
        )
        await query.answer()

    # Interactive UI: Escape
    elif data.startswith(CB_ASK_ESC):
        window_id = data[len(CB_ASK_ESC) :]
        thread_id = _get_thread_id(update)
        if await reject_stale_window_callback(window_id):
            return
        # F2: ESC carve-out. On a stale picker, still reap the Telegram card.
        w = await assert_nav_dispatchable(
            query, user.id, thread_id, window_id, tmux_mgr=tmux_manager, is_esc=True
        )
        if w == NAV_ESC_CLEAR:
            await clear_interactive_msg(
                user.id, context.bot, thread_id, session_mgr=adapters.session_manager
            )
            await query.answer("⎋ Esc")
            return
        if w is None:
            return
        await tmux_manager.send_keys(w.window_id, "Escape", enter=False, literal=False)
        await clear_interactive_msg(
            user.id, context.bot, thread_id, session_mgr=adapters.session_manager
        )
        await query.answer("⎋ Esc")

    # Interactive UI: Enter
    elif data.startswith(CB_ASK_ENTER):
        window_id = data[len(CB_ASK_ENTER) :]
        thread_id = _get_thread_id(update)
        if await reject_stale_window_callback(window_id):
            return
        w = await assert_nav_dispatchable(
            query, user.id, thread_id, window_id, tmux_mgr=tmux_manager
        )
        if w is None:
            return
        await tmux_manager.send_keys(w.window_id, "Enter", enter=False, literal=False)
        await asyncio.sleep(0.5)
        await handle_interactive_ui(
            context.bot,
            user.id,
            window_id,
            thread_id,
            tmux_mgr=tmux_manager,
            session_mgr=adapters.session_manager,
        )
        await query.answer("⏎ Enter")

    # Interactive UI: Space
    elif data.startswith(CB_ASK_SPACE):
        window_id = data[len(CB_ASK_SPACE) :]
        thread_id = _get_thread_id(update)
        if await reject_stale_window_callback(window_id):
            return
        w = await assert_nav_dispatchable(
            query, user.id, thread_id, window_id, tmux_mgr=tmux_manager
        )
        if w is None:
            return
        await tmux_manager.send_keys(w.window_id, "Space", enter=False, literal=False)
        await asyncio.sleep(0.5)
        await handle_interactive_ui(
            context.bot,
            user.id,
            window_id,
            thread_id,
            tmux_mgr=tmux_manager,
            session_mgr=adapters.session_manager,
        )
        await query.answer("␣ Space")

    # Interactive UI: Tab
    elif data.startswith(CB_ASK_TAB):
        window_id = data[len(CB_ASK_TAB) :]
        thread_id = _get_thread_id(update)
        if await reject_stale_window_callback(window_id):
            return
        w = await assert_nav_dispatchable(
            query, user.id, thread_id, window_id, tmux_mgr=tmux_manager
        )
        if w is None:
            return
        await tmux_manager.send_keys(w.window_id, "Tab", enter=False, literal=False)
        await asyncio.sleep(0.5)
        await handle_interactive_ui(
            context.bot,
            user.id,
            window_id,
            thread_id,
            tmux_mgr=tmux_manager,
            session_mgr=adapters.session_manager,
        )
        await query.answer("⇥ Tab")

    # Interactive UI: refresh display (F1: included in the nav-guard family)
    elif data.startswith(CB_ASK_REFRESH):
        window_id = data[len(CB_ASK_REFRESH) :]
        thread_id = _get_thread_id(update)
        if await reject_stale_window_callback(window_id):
            return
        w = await assert_nav_dispatchable(
            query, user.id, thread_id, window_id, tmux_mgr=tmux_manager
        )
        if w is None:
            return
        await handle_interactive_ui(
            context.bot,
            user.id,
            window_id,
            thread_id,
            tmux_mgr=tmux_manager,
            session_mgr=adapters.session_manager,
        )
        await query.answer("🔄")

    # Interactive UI: structured option pick (PR 2b)
    elif data.startswith(CB_ASK_PICK):
        token = data[len(CB_ASK_PICK) :]
        # CB3: peek BEFORE consume. The old consume_pick_token-only flow
        # destroyed the token + its sibling cache row even on user-id
        # mismatch, letting a wrong user click another user's button and
        # burn the legitimate owner's tokens. Validate ownership first,
        # consume only after.
        entry = peek_pick_token(token)
        if entry is None:
            # Token never existed, was already used, or has aged past the
            # 5-minute TTL. Refresh the card so the user sees the live form
            # state and can click a fresh button.
            await query.answer("Card expired, refreshing.", show_alert=False)
            thread_id = _get_thread_id(update)
            window_id = get_interactive_window(user.id, thread_id) or ""
            if window_id:
                await handle_interactive_ui(
                    context.bot,
                    user.id,
                    window_id,
                    thread_id,
                    tmux_mgr=tmux_manager,
                    session_mgr=adapters.session_manager,
                )
            return
        thread_id = entry.thread_id
        window_id = entry.window_id
        # Guard ordering is intentional (R4 option a): first verify the
        # token owner without side effects, then let the lease reject stale
        # windows without consuming or double-answering, and only consume
        # immediately before dispatch on a fresh owner click.
        if not owner_matches(entry, user.id):
            await query.answer(WRONG_USER_PICK_TEXT, show_alert=True)
            return
        if await reject_stale_window_callback(window_id):
            return
        consume_pick_token(token)
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await query.answer("Window not found", show_alert=True)
            return

        # Staleness check: re-capture the pane and re-resolve before dispatching
        # any key. If the form has shifted under us (user navigated, skill
        # advanced, Claude Code redrew, /clear fired), the minted fingerprint
        # won't match and we MUST NOT send a digit — picking "1" on a new
        # form could submit the wrong answer.
        #
        # PR 2: use ``resolve_ask_form`` with the same cached JSONL payload
        # the render path saw (via ``resolve_ask_tool_input``). Without
        # this, a multi-tab form rendered with the JSONL overlay would
        # mint fingerprints the pane-only re-parse here could never match,
        # bouncing every click to "Form changed, refreshing".
        # Capture with the SAME scrollback as the render path
        # (handlers/interactive_ui.py uses scrollback_lines=500). A
        # smaller scrollback here produces a different pane slice from
        # what render saw → different ``current_tab_inferred`` /
        # ``current_question_title`` / options → fingerprint mismatch at
        # validate vs mint, causing taps on long pickers (where options
        # were only recoverable in the 500-line capture) to bounce with
        # "Form changed, refreshing".
        pane = await tmux_manager.capture_pane(w.window_id, scrollback_lines=500)
        cached_input = resolve_ask_tool_input(window_id)
        current_form = (
            adapters.terminal_parser.resolve_ask_form(cached_input, pane)
            if pane
            else None
        )
        if current_form is None or current_form.fingerprint() != entry.fingerprint:
            logger.info(
                "Pick-token staleness reject: user=%d window=%s opt=%d "
                "minted_fp=%s current_fp=%s",
                user.id,
                window_id,
                entry.option_number,
                entry.fingerprint,
                current_form.fingerprint() if current_form else "none",
            )
            await query.answer("Form changed, refreshing.", show_alert=False)
            await handle_interactive_ui(
                context.bot,
                user.id,
                window_id,
                thread_id,
                tmux_mgr=tmux_manager,
                session_mgr=adapters.session_manager,
            )
            return

        # Submit-button guardrail: a click flagged ``is_review_submit`` only
        # fires when the live parse still says we're on the review screen
        # with the cursor on the submit row, AND the label matches. The
        # fingerprint check above already encodes is_review_screen + cursor
        # + option number + option label, so a mismatch would already have
        # bounced — Hermes review asked for an explicit label compare here
        # as belt-and-braces, so a future fingerprint-format change can't
        # accidentally let an off-screen Submit dispatch.
        if entry.is_review_submit:
            cursor_on_submit_one = (
                current_form.is_review_screen
                and current_form.options
                and current_form.options[0].cursor
                and current_form.options[0].number == 1
                and current_form.options[0].label == entry.option_label
            )
            if not cursor_on_submit_one:
                logger.info(
                    "Pick-token submit-guard reject: user=%d window=%s",
                    user.id,
                    window_id,
                )
                await query.answer("Review screen moved, refreshing.", show_alert=False)
                await handle_interactive_ui(
                    context.bot,
                    user.id,
                    window_id,
                    thread_id,
                    tmux_mgr=tmux_manager,
                    session_mgr=adapters.session_manager,
                )
                return

        # Dispatch: send the literal digit. Claude Code's AskUserQuestion
        # picker accepts ``1``-``9`` as shortcuts; the digit moves the
        # cursor to that option and Enter submits. We send digit + Enter
        # in two passes (no auto-Enter on the digit) so the picker has
        # time to register the selection before the Enter key arrives.
        # 500ms matches the gap tmux_manager uses internally for the
        # literal-text-then-Enter path — boring beats flaky.
        await tmux_manager.send_keys(
            w.window_id, str(entry.option_number), enter=False, literal=True
        )
        await asyncio.sleep(0.5)
        await tmux_manager.send_keys(w.window_id, "Enter", enter=False, literal=False)
        await query.answer(f"{entry.option_number}. {entry.option_label[:32]}")
        await asyncio.sleep(0.5)
        # PR 3: snapshot the JSONL cache digest BEFORE re-rendering. If a
        # concurrent ``tool_result`` clears the cache between this point
        # and ``handle_interactive_ui`` reacquiring the route lock, the
        # re-render sees the guard mismatch and aborts — no orphan card
        # posted after the prompt has already advanced.
        rerender_guard = _ask_tool_input_digest(resolve_ask_tool_input(window_id))
        await handle_interactive_ui(
            context.bot,
            user.id,
            window_id,
            thread_id,
            rerender_guard=rerender_guard,
            tmux_mgr=tmux_manager,
            session_mgr=adapters.session_manager,
        )
