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
from cctelegram.handlers import auq_ledger, interactive_ui
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
    CB_ASK_TOGGLE,
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
    safe_answer,
    window_lease,
)

logger = logging.getLogger(__name__)
resolve_ask_tool_input = interactive_ui.resolve_ask_tool_input


def _stable_key_of(entry: interactive_ui._PickTokenEntry) -> str:
    """Reconstruct the Wave 3 ledger key from a live pick-token entry.

    Used by the collision-defense branch in the pick callback to decide
    whether a live token for the clicker maps to the same ledger key the
    callback_data carries. Mirrors the mint-side construction in
    ``interactive_ui._build_pick_button_rows``.
    """
    return auq_ledger.make_ledger_key(
        auq_ledger.make_route_hash(entry.user_id, entry.thread_id, entry.window_id),
        entry.fingerprint[:8],
        entry.option_number,
    )


async def _refresh_pick_card(
    query: Any,
    context: Any,
    update: Any,
    user: Any,
    tmux_manager: Any,
    adapters: Any,
    *,
    text: str,
    fallback_window_id: str | None = None,
) -> None:
    """Answer the callback with ``text`` and re-render the live picker card.

    Used by every short-circuit branch in the pick handler (legacy/new
    expired token, malformed callback_data, ledger projection that wants
    the user to retry). Resolves the route's current window via
    ``get_interactive_window``; falls back to ``fallback_window_id`` when
    the ledger row pointed at a window that's no longer bound.
    """
    await safe_answer(query, text, show_alert=False)
    thread_id = _get_thread_id(update)
    window_id = get_interactive_window(user.id, thread_id) or fallback_window_id or ""
    if window_id:
        await handle_interactive_ui(
            context.bot,
            user.id,
            window_id,
            thread_id,
            tmux_mgr=tmux_manager,
            session_mgr=adapters.session_manager,
        )


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
        await safe_answer(query)

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
        await safe_answer(query)

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
        await safe_answer(query)

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
        await safe_answer(query)

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
            await safe_answer(query, "⎋ Esc")
            return
        if w is None:
            return
        await tmux_manager.send_keys(w.window_id, "Escape", enter=False, literal=False)
        await clear_interactive_msg(
            user.id, context.bot, thread_id, session_mgr=adapters.session_manager
        )
        await safe_answer(query, "⎋ Esc")

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
        await safe_answer(query, "⏎ Enter")

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
        await safe_answer(query, "␣ Space")

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
        await safe_answer(query, "⇥ Tab")

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
        await safe_answer(query, "🔄")

    # Interactive UI: multi-select toggle (digit-only, no ledger, token not consumed)
    elif data.startswith(CB_ASK_TOGGLE):
        payload = data[len(CB_ASK_TOGGLE) :]
        parts = payload.split(":")
        if len(parts) != 4:
            await _refresh_pick_card(
                query,
                context,
                update,
                user,
                tmux_manager,
                adapters,
                text="Card expired, refreshing.",
            )
            return
        _route_hash, _fp8, opt_str, token = parts
        try:
            opt_num = int(opt_str)
        except ValueError:
            await _refresh_pick_card(
                query,
                context,
                update,
                user,
                tmux_manager,
                adapters,
                text="Card expired, refreshing.",
            )
            return

        entry = peek_pick_token(token)
        if entry is None:
            await _refresh_pick_card(
                query,
                context,
                update,
                user,
                tmux_manager,
                adapters,
                text="Card expired, refreshing.",
            )
            return
        thread_id = entry.thread_id
        window_id = entry.window_id
        if not owner_matches(entry, user.id):
            await safe_answer(query, WRONG_USER_PICK_TEXT, show_alert=True)
            return
        if opt_num != entry.option_number:
            await _refresh_pick_card(
                query,
                context,
                update,
                user,
                tmux_manager,
                adapters,
                text="Card expired, refreshing.",
                fallback_window_id=window_id,
            )
            return
        if await reject_stale_window_callback(window_id):
            return
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await safe_answer(query, "Window not found", show_alert=True)
            return

        pane = await tmux_manager.capture_pane(w.window_id, scrollback_lines=500)
        resolved_input = interactive_ui._resolve_auq_source(window_id, None, pane or "")
        current_form = (
            adapters.terminal_parser.resolve_ask_form(resolved_input, pane)
            if pane
            else None
        )
        if (
            current_form is None
            or current_form.fingerprint() != entry.fingerprint
            or current_form.select_mode != "multi"
            or not current_form.options_complete
        ):
            logger.info(
                "Toggle-token staleness reject: user=%d window=%s opt=%d minted_fp=%s current_fp=%s",
                user.id,
                window_id,
                entry.option_number,
                entry.fingerprint,
                current_form.fingerprint() if current_form else "none",
            )
            await safe_answer(query, "Form changed, refreshing.", show_alert=False)
            await handle_interactive_ui(
                context.bot,
                user.id,
                window_id,
                thread_id,
                tmux_mgr=tmux_manager,
                session_mgr=adapters.session_manager,
            )
            return

        toggle_ok = await tmux_manager.send_keys(
            w.window_id, str(entry.option_number), enter=False, literal=True
        )
        if not toggle_ok:
            logger.warning(
                "Toggle-token dispatch: tmux send_keys(digit=%d) returned False for window=%s user=%d",
                entry.option_number,
                window_id,
                user.id,
            )
            await safe_answer(query, "toggle failed; refreshing", show_alert=False)
            await handle_interactive_ui(
                context.bot,
                user.id,
                window_id,
                thread_id,
                tmux_mgr=tmux_manager,
                session_mgr=adapters.session_manager,
            )
            return

        await asyncio.sleep(0.3)
        await handle_interactive_ui(
            context.bot,
            user.id,
            window_id,
            thread_id,
            tmux_mgr=tmux_manager,
            session_mgr=adapters.session_manager,
        )
        await safe_answer(query, f"Toggled {entry.option_number}")

    # Interactive UI: structured option pick (PR 2b + Wave 3 ledger)
    elif data.startswith(CB_ASK_PICK):
        payload = data[len(CB_ASK_PICK) :]
        parts = payload.split(":")
        # Parse shape:
        #   len == 4 → keyed ``aqp:<route_hash>:<fp8>:<opt>:<token>``;
        #              the leading triplet feeds the restart-safe ledger.
        #   anything else → malformed → refresh card.
        # ``ledger_key`` stays ``str | None`` because the collision-suppression
        # paths below (wrong-user/live-token collision and same-user route/window
        # drift) reset it to ``None`` to avoid clobbering another route's row.
        ledger_key: str | None = None
        token: str
        if len(parts) == 4:
            route_hash, fp8, opt_str, token = parts
            try:
                opt_num = int(opt_str)
            except ValueError:
                await _refresh_pick_card(
                    query,
                    context,
                    update,
                    user,
                    tmux_manager,
                    adapters,
                    text="Card expired, refreshing.",
                )
                return
            ledger_key = auq_ledger.make_ledger_key(route_hash, fp8, opt_num)
        else:
            await _refresh_pick_card(
                query,
                context,
                update,
                user,
                tmux_manager,
                adapters,
                text="Card expired, refreshing.",
            )
            return

        # Ledger lookup FIRST (restart recovery). Wave 3 §7.2 contract:
        # ledger consulted BEFORE token validate so a post-restart duplicate
        # tap can be detected even when the in-memory _pick_tokens cache
        # has been wiped.
        existing = auq_ledger.lookup(ledger_key)

        # v4 §7.2 owner-mismatch handling. Could be (a) wrong-user replay
        # (owner already dispatched; another user in the topic clicks the
        # same callback_data); or (b) legitimate live-token collision (two
        # routes hashed to the same triplet AND the clicker owns a live
        # pick token for the same stable key). Distinguish by peeking the
        # current user's live token: if it reconstructs the same key, this
        # is collision → clear ledger gate for this click, fall through to
        # the in-process token path. Otherwise wrong-user → reject.
        if existing is not None and existing.user_id != user.id:
            live = peek_pick_token(token)
            is_collision = (
                live is not None
                and live.user_id == user.id
                and ledger_key is not None
                and _stable_key_of(live) == ledger_key
            )
            if not is_collision:
                await safe_answer(query, WRONG_USER_PICK_TEXT, show_alert=True)
                return
            # Plan v4 §7.2: "ledger entry from the other route stays put
            # (its owner can still see 'Action already received' on
            # retry)." Drop both the local gate AND the ledger_key so the
            # follow-up dispatch writes go to nothing — otherwise the
            # accepted/digit_sent/dispatched writes below would overwrite
            # the owner's row at the same key.
            existing = None
            ledger_key = None

        # Same-user defensive collision check: the route_hash matches but
        # the stored window_id differs from this route's current binding.
        # The hashes can collide across (user, thread, window) triplets;
        # if the bound window has drifted, treat the ledger row as a
        # collision, fall through to the token path, and likewise drop
        # ledger_key so this dispatch doesn't clobber a row that legitimately
        # belongs to a different window's lifecycle.
        if existing is not None:
            bound_window = get_interactive_window(user.id, _get_thread_id(update))
            if bound_window and existing.window_id != bound_window:
                existing = None
                ledger_key = None

        # Apply the §7.1 per-state behavior matrix.
        if existing is not None:
            proj_state = existing.state
            if (
                existing.state in ("accepted", "digit_sent")
                and existing.accepted_at < auq_ledger.process_start_time()
            ):
                proj_state = "unknown"
            if proj_state == "dispatched":
                await safe_answer(
                    query,
                    f"Action already received: {existing.option_label[:32]}",
                    show_alert=False,
                )
                return
            if proj_state in ("accepted", "digit_sent"):
                await safe_answer(query, "Action in progress", show_alert=False)
                return
            if proj_state == "unknown":
                await _refresh_pick_card(
                    query,
                    context,
                    update,
                    user,
                    tmux_manager,
                    adapters,
                    text="Action interrupted; please re-tap.",
                    fallback_window_id=existing.window_id,
                )
                return
            if proj_state == "failed_before_digit":
                await _refresh_pick_card(
                    query,
                    context,
                    update,
                    user,
                    tmux_manager,
                    adapters,
                    text="Action failed previously; refreshing.",
                    fallback_window_id=existing.window_id,
                )
                return
            if proj_state == "failed_after_digit":
                await _refresh_pick_card(
                    query,
                    context,
                    update,
                    user,
                    tmux_manager,
                    adapters,
                    text=("Action sent but interrupted; refreshing — verify in tmux."),
                    fallback_window_id=existing.window_id,
                )
                return

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
            await _refresh_pick_card(
                query,
                context,
                update,
                user,
                tmux_manager,
                adapters,
                text="Card expired, refreshing.",
            )
            return
        thread_id = entry.thread_id
        window_id = entry.window_id
        # Guard ordering is intentional (R4 option a): first verify the
        # token owner without side effects, then let the lease reject stale
        # windows without consuming or double-answering, and only consume
        # immediately before dispatch on a fresh owner click.
        if not owner_matches(entry, user.id):
            await safe_answer(query, WRONG_USER_PICK_TEXT, show_alert=True)
            return
        if await reject_stale_window_callback(window_id):
            return
        consume_pick_token(token)
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await safe_answer(query, "Window not found", show_alert=True)
            return

        # Staleness check: re-capture the pane and re-resolve before dispatching
        # any key. If the form has shifted under us (user navigated, skill
        # advanced, Claude Code redrew, /clear fired), the minted fingerprint
        # won't match and we MUST NOT send a digit — picking "1" on a new
        # form could submit the wrong answer.
        #
        # PR 2: use ``resolve_ask_form`` with the same AUQ source the render
        # path saw (via ``_resolve_auq_source``). For live pending AUQs Claude
        # buffers JSONL until the question is answered, so the PreToolUse side
        # file is the authoritative dict source while the live pane remains the
        # staleness check. Falling back to ``resolve_ask_tool_input`` here would
        # miss that side file and mint/validate against different forms.
        # Capture with the SAME scrollback as the render path
        # (handlers/interactive_ui.py uses scrollback_lines=500). A
        # smaller scrollback here produces a different pane slice from
        # what render saw → different ``current_tab_inferred`` /
        # ``current_question_title`` / options → fingerprint mismatch at
        # validate vs mint, causing taps on long pickers (where options
        # were only recoverable in the 500-line capture) to bounce with
        # "Form changed, refreshing".
        pane = await tmux_manager.capture_pane(w.window_id, scrollback_lines=500)
        resolved_input = interactive_ui._resolve_auq_source(window_id, None, pane or "")
        current_form = (
            adapters.terminal_parser.resolve_ask_form(resolved_input, pane)
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
            await safe_answer(query, "Form changed, refreshing.", show_alert=False)
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
                await safe_answer(
                    query, "Review screen moved, refreshing.", show_alert=False
                )
                await handle_interactive_ui(
                    context.bot,
                    user.id,
                    window_id,
                    thread_id,
                    tmux_mgr=tmux_manager,
                    session_mgr=adapters.session_manager,
                )
                return

        # Write-ahead ledger BEFORE dispatch. ``ledger_key`` is None ONLY on a
        # collision-suppression fall-through (set above at the wrong-user/
        # live-token and same-user window-drift checks): the ledger row belongs
        # to a DIFFERENT route, so we must NOT write to that key here or we'd
        # clobber the rightful owner's lifecycle. These guards therefore
        # protect collision suppression — do not remove them. (The legacy
        # one-part ``aqp:<token>`` callback shape that also used to leave
        # ledger_key None was removed in Wave 4; only the collision path
        # remains.)
        if ledger_key is not None:
            auq_ledger.record(
                ledger_key,
                state="accepted",
                user_id=user.id,
                window_id=window_id,
                full_fingerprint=entry.fingerprint,
                option_number=entry.option_number,
                option_label=entry.option_label,
            )

        # Dispatch: send the literal digit. Claude Code's AskUserQuestion
        # picker accepts ``1``-``9`` as shortcuts; the digit moves the
        # cursor to that option and Enter submits. We send digit + Enter
        # in two passes (no auto-Enter on the digit) so the picker has
        # time to register the selection before the Enter key arrives.
        # 500ms matches the gap tmux_manager uses internally for the
        # literal-text-then-Enter path — boring beats flaky.
        #
        # ``tmux_manager.send_keys`` returns ``False`` (does not raise) on
        # missing session/window/pane or libtmux exceptions. Codex Wave 3
        # P1: a silent False return used to fall through to
        # ``auq_ledger.record(state="dispatched")``, which then made every
        # subsequent retry tap answer "Action already received" even
        # though tmux had never received the digit. Check both returns.
        digit_landed = False
        try:
            digit_ok = await tmux_manager.send_keys(
                w.window_id, str(entry.option_number), enter=False, literal=True
            )
            if not digit_ok:
                if ledger_key is not None:
                    auq_ledger.record(
                        ledger_key,
                        state="failed_before_digit",
                        failed_reason="tmux send_keys(digit) returned False",
                    )
                logger.warning(
                    "Pick-token dispatch: tmux send_keys(digit=%d) "
                    "returned False for window=%s user=%d",
                    entry.option_number,
                    window_id,
                    user.id,
                )
                await safe_answer(
                    query, "Action failed; refreshing card.", show_alert=False
                )
                await handle_interactive_ui(
                    context.bot,
                    user.id,
                    window_id,
                    thread_id,
                    tmux_mgr=tmux_manager,
                    session_mgr=adapters.session_manager,
                )
                return
            digit_landed = True
            if ledger_key is not None:
                auq_ledger.record(ledger_key, state="digit_sent")
            await asyncio.sleep(0.5)
            enter_ok = await tmux_manager.send_keys(
                w.window_id, "Enter", enter=False, literal=False
            )
            if not enter_ok:
                if ledger_key is not None:
                    auq_ledger.record(
                        ledger_key,
                        state="failed_after_digit",
                        failed_reason="tmux send_keys(Enter) returned False",
                    )
                logger.warning(
                    "Pick-token dispatch: digit landed but tmux "
                    "send_keys(Enter) returned False for window=%s user=%d",
                    window_id,
                    user.id,
                )
                await safe_answer(
                    query,
                    "Action sent but Enter failed; refreshing — verify in tmux.",
                    show_alert=False,
                )
                await handle_interactive_ui(
                    context.bot,
                    user.id,
                    window_id,
                    thread_id,
                    tmux_mgr=tmux_manager,
                    session_mgr=adapters.session_manager,
                )
                return
            if ledger_key is not None:
                auq_ledger.record(ledger_key, state="dispatched")
        except Exception as exc:
            if ledger_key is not None:
                auq_ledger.record(
                    ledger_key,
                    state=(
                        "failed_after_digit" if digit_landed else "failed_before_digit"
                    ),
                    failed_reason=str(exc),
                )
            await safe_answer(
                query, "Action failed; refreshing card.", show_alert=False
            )
            raise

        await safe_answer(query, f"{entry.option_number}. {entry.option_label[:32]}")
        await asyncio.sleep(0.5)
        # Re-render the picker after the digit lands so the card reflects the
        # advanced screen (next question / review / completion). Orphan-card
        # safety is provided by the visible-pane liveness bail inside
        # ``handle_interactive_ui`` (it reads the live tmux pane and returns
        # without rendering when no picker is on screen).
        await handle_interactive_ui(
            context.bot,
            user.id,
            window_id,
            thread_id,
            tmux_mgr=tmux_manager,
            session_mgr=adapters.session_manager,
        )
