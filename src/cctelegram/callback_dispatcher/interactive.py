"""Execute interactive AskUserQuestion navigation and pick callbacks.

Core responsibilities:
  - Own CB_ASK_* navigation, refresh, and tokenized pick callbacks.
  - Preserve wrong-user and stale-form safety for interactive picks.
  - Re-render interactive cards after dispatch through injected adapters.

Key components:
  - execute_interactive_callback()
"""

from __future__ import annotations

from typing import Any, Literal, cast

import asyncio
import logging
from cctelegram.handlers import (
    auq_ledger,
    auq_source,
    interactive_ui,
    pick_intent,
    pick_token,
)
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
    get_interactive_window,
    handle_interactive_ui,
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


async def _refresh_pick_card(
    query: Any,
    context: Any,
    update: Any,
    user: Any,
    tmux_manager: Any,
    adapters: Any,
    *,
    text: str,
    show_alert: bool = False,
    fallback_window_id: str | None = None,
) -> None:
    """Answer the callback with ``text`` and re-render the live picker card.

    Used by every short-circuit branch in the pick handler (legacy/new
    expired token, malformed callback_data, ledger projection that wants
    the user to retry). Resolves the route's current window via
    ``get_interactive_window``; falls back to ``fallback_window_id`` when
    the ledger row pointed at a window that's no longer bound.

    ``show_alert`` is passed through to the callback answer: the dead-token
    (``peek_none`` / ``expired``) callers set it ``True`` so their honest
    "tap again" prompt is a MODAL the user can't miss, while the ledger-state
    callers keep the default ``False`` so their specific warnings (e.g.
    ``failed_before_digit`` "Action failed previously; refreshing.") stay as
    non-modal toasts.
    """
    await safe_answer(query, text, show_alert=show_alert)
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


async def _dispatch_pick_digit(
    *,
    query: Any,
    context: Any,
    user: Any,
    tmux_manager: Any,
    adapters: Any,
    w: Any,
    window_id: str,
    thread_id: int | None,
    option_number: int,
    option_label: str,
    ledger_key: str | None,
) -> None:
    """Send the option digit (no Enter), record the ledger lifecycle, answer, re-render.

    On Claude Code v2.1.167 a BARE DIGIT is the universal select+advance (and, on
    the review screen, submit) action — the trailing ``Enter`` the bot used to
    send over-advanced multi-question forms past Q2 (it auto-answered the next
    question with its cursor-default). So only the bare digit is dispatched; the
    digit landing IS the complete dispatch (``accepted`` → ``dispatched``).

    Shared by the live ``ok`` path and D2 restart-recovery. The caller writes the
    ``accepted`` claim BEFORE calling this (the live path inline; recovery inside
    ``pick_token.recover_and_consume``). ``ledger_key`` is None only on a
    collision-suppression fall-through — the ``if ledger_key is not None`` guards
    keep those writes off another route's row. ``send_keys`` returns False (does
    not raise) on failure; the return is checked (Wave-3 P1).

    Ledger-downgrade safety (codex+hermes diff-review P1): the ``try`` wraps ONLY
    the digit ``send_keys`` — the single operation that can leave the digit
    un-landed. ``failed_before_digit`` is therefore written *only* when the digit
    provably never reached tmux (send_keys raised or returned False). Once the
    digit lands, the TUI has already consumed the selection/submission, so the
    terminal ``dispatched`` is recorded OUTSIDE the ``try`` and a later failure
    (the ``record`` write, ``safe_answer``, or the re-render) can NEVER downgrade
    a landed digit back to a retryable state — that downgrade would re-open the
    duplicate-tap double-dispatch the ledger exists to prevent. A post-digit
    failure that prevents recording ``dispatched`` leaves the row at ``accepted``
    (honest: "in progress" / post-restart ``unknown`` → refresh, never re-dispatch).
    """
    try:
        digit_ok = await tmux_manager.send_keys(
            w.window_id, str(option_number), enter=False, literal=True
        )
    except Exception as exc:
        # send_keys raised → the digit never landed; a retryable failure is honest.
        if ledger_key is not None:
            auq_ledger.record(
                ledger_key,
                state="failed_before_digit",
                failed_reason=str(exc),
            )
        await safe_answer(query, "Action failed; refreshing card.", show_alert=False)
        raise
    if not digit_ok:
        if ledger_key is not None:
            auq_ledger.record(
                ledger_key,
                state="failed_before_digit",
                failed_reason="tmux send_keys(digit) returned False",
            )
        logger.warning(
            "Pick-token dispatch: tmux send_keys(digit=%d) returned False "
            "for window=%s user=%d",
            option_number,
            window_id,
            user.id,
        )
        await safe_answer(query, "Action failed; refreshing card.", show_alert=False)
        await handle_interactive_ui(
            context.bot,
            user.id,
            window_id,
            thread_id,
            tmux_mgr=tmux_manager,
            session_mgr=adapters.session_manager,
        )
        return

    # The bare digit landed — this IS the complete dispatch (v2.1.167
    # single-keystroke model). Record the terminal ``dispatched`` OUTSIDE the
    # ``try`` so no post-digit failure can downgrade a landed digit (see the
    # ledger-downgrade-safety note above).
    if ledger_key is not None:
        auq_ledger.record(ledger_key, state="dispatched")
    logger.info(
        "AUQ_PICK dispatch_ok user=%d window=%s opt=%d label=%s",
        user.id,
        window_id,
        option_number,
        option_label[:24],
    )
    await safe_answer(query, f"{option_number}. {option_label[:32]}")
    await asyncio.sleep(0.5)
    # Re-render the picker after the digit lands so the card reflects the
    # advanced screen. Orphan-card safety is the visible-pane liveness bail
    # inside ``handle_interactive_ui``.
    await handle_interactive_ui(
        context.bot,
        user.id,
        window_id,
        thread_id,
        tmux_mgr=tmux_manager,
        session_mgr=adapters.session_manager,
    )


async def _attempt_pick_recovery(
    token: str,
    sender_id: int,
    route_hash: str,
    fp8: str,
    opt_num: int,
    *,
    query: Any,
    context: Any,
    user: Any,
    tmux_manager: Any,
    adapters: Any,
    reject_stale_window: Any,
) -> bool:
    """D2 restart-recovery at a token-less dead branch (peek_none / expired).

    Returns True iff this took over the click (dispatched the recovered option OR
    answered a decline that has its own message); False to fall through to the
    caller's default honest refresh modal. Reached only AFTER the top ledger gate,
    so a recoverable tap provably has no blocking ledger row for its own option.
    """
    intent = pick_intent.lookup_intent(token)
    if intent is None:
        return False
    # Callback-payload parity: the immutable callback_data must agree with the
    # stored intent's derived key — else a corrupt/tampered store row could map a
    # real button token to a different option/route. Mismatch → no recovery.
    if (
        route_hash
        != auq_ledger.make_route_hash(
            intent.user_id, intent.thread_id, intent.window_id
        )
        or fp8 != intent.full_fingerprint[:8]
        or opt_num != intent.option_number
    ):
        logger.info(
            "AUQ_PICK recover parity_mismatch user=%d token=%s", sender_id, token[:6]
        )
        return False
    # Owner-auth (the historic peek_none branch had none) BEFORE the lease check,
    # mirroring the live path's 785→789 ordering.
    if intent.user_id != sender_id:
        logger.info("AUQ_PICK recover wrong_user user=%d", sender_id)
        await safe_answer(query, WRONG_USER_PICK_TEXT, show_alert=True)
        return True
    if await reject_stale_window(intent.window_id):
        logger.info(
            "AUQ_PICK recover stale_window user=%d window=%s",
            sender_id,
            intent.window_id,
        )
        return True

    async def _capture(wid: str, scrollback: int) -> str | None:
        return await tmux_manager.capture_pane(wid, scrollback_lines=scrollback)

    result = await pick_token.recover_and_consume(
        token,
        intent,
        sender_id,
        capture_pane=_capture,
        find_window_by_id=tmux_manager.find_window_by_id,
    )
    logger.info(
        "AUQ_PICK recover outcome=%s user=%d window=%s opt=%d",
        result.outcome,
        sender_id,
        intent.window_id,
        intent.option_number,
    )
    if result.outcome == "wrong_user":
        await safe_answer(query, WRONG_USER_PICK_TEXT, show_alert=True)
        return True
    if result.outcome == "already":
        await safe_answer(
            query,
            f"Action already received: {intent.option_label[:32]}",
            show_alert=False,
        )
        return True
    if result.outcome == "in_progress":
        await safe_answer(query, "Action in progress", show_alert=False)
        return True
    if result.outcome in ("superseded", "stale_form", "source_drift", "window_gone"):
        # The on-screen keyboard is current/changed — fall through to the honest
        # refresh modal so the user taps the live card.
        return False
    # outcome == "ok": the accepted claim is already written inside
    # recover_and_consume; dispatch the digit.
    assert (
        result.window_id is not None
        and result.option_number is not None
        and result.option_label is not None
        and result.current_form is not None
    )
    w = await tmux_manager.find_window_by_id(result.window_id)
    if not w:
        # The window vanished between recover_and_consume's phase-B find and now.
        # The ``accepted`` claim is already written (inside the reservation), so
        # record ``failed_before_digit`` (a re-tappable projection) rather than
        # leaving the ledger stuck at ``accepted`` → "Action in progress" forever.
        if result.ledger_key is not None:
            auq_ledger.record(
                result.ledger_key,
                state="failed_before_digit",
                failed_reason="window gone before recovery dispatch",
            )
        await safe_answer(query, "Window not found", show_alert=True)
        return True
    # The review-Submit cursor guard runs INSIDE recover_and_consume (before its
    # accepted claim), so an ``ok`` result has already passed it — dispatch.
    await _dispatch_pick_digit(
        query=query,
        context=context,
        user=user,
        tmux_manager=tmux_manager,
        adapters=adapters,
        w=w,
        window_id=result.window_id,
        thread_id=result.thread_id,
        option_number=result.option_number,
        option_label=result.option_label,
        ledger_key=result.ledger_key,
    )
    return True


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
        nav_ok = await tmux_manager.send_keys(
            w.window_id, "Up", enter=False, literal=False
        )
        logger.info(
            "AUQ_TAP nav_dispatch user=%d window=%s key=%s send_keys_ok=%s",
            user.id,
            window_id,
            "Up",
            nav_ok,
        )
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
        nav_ok = await tmux_manager.send_keys(
            w.window_id, "Down", enter=False, literal=False
        )
        logger.info(
            "AUQ_TAP nav_dispatch user=%d window=%s key=%s send_keys_ok=%s",
            user.id,
            window_id,
            "Down",
            nav_ok,
        )
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
        nav_ok = await tmux_manager.send_keys(
            w.window_id, "Left", enter=False, literal=False
        )
        logger.info(
            "AUQ_TAP nav_dispatch user=%d window=%s key=%s send_keys_ok=%s",
            user.id,
            window_id,
            "Left",
            nav_ok,
        )
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
        nav_ok = await tmux_manager.send_keys(
            w.window_id, "Right", enter=False, literal=False
        )
        logger.info(
            "AUQ_TAP nav_dispatch user=%d window=%s key=%s send_keys_ok=%s",
            user.id,
            window_id,
            "Right",
            nav_ok,
        )
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
        nav_ok = await tmux_manager.send_keys(
            w.window_id, "Escape", enter=False, literal=False
        )
        logger.info(
            "AUQ_TAP nav_dispatch user=%d window=%s key=%s send_keys_ok=%s",
            user.id,
            window_id,
            "Escape",
            nav_ok,
        )
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
        nav_ok = await tmux_manager.send_keys(
            w.window_id, "Enter", enter=False, literal=False
        )
        logger.info(
            "AUQ_TAP nav_dispatch user=%d window=%s key=%s send_keys_ok=%s",
            user.id,
            window_id,
            "Enter",
            nav_ok,
        )
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
        nav_ok = await tmux_manager.send_keys(
            w.window_id, "Space", enter=False, literal=False
        )
        logger.info(
            "AUQ_TAP nav_dispatch user=%d window=%s key=%s send_keys_ok=%s",
            user.id,
            window_id,
            "Space",
            nav_ok,
        )
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
        nav_ok = await tmux_manager.send_keys(
            w.window_id, "Tab", enter=False, literal=False
        )
        logger.info(
            "AUQ_TAP nav_dispatch user=%d window=%s key=%s send_keys_ok=%s",
            user.id,
            window_id,
            "Tab",
            nav_ok,
        )
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

        entry = pick_token.peek(token)
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
        # Source-stickiness: re-resolve using the SAME source this toggle button
        # was minted against, if it is still live + unchanged. A transient pane
        # degradation can make resolve_auq_source flip side_file→pane at tap;
        # that flip changes the resolved form's fingerprint and silently rejects
        # the toggle. Pinning the minted source keeps the toggle dispatching as
        # long as the underlying question hasn't actually changed (a replaced
        # side file has a different canonical fingerprint → no pin → fall back).
        sticky_input = auq_source.peek_sticky_source(
            window_id, entry.source_kind, entry.source_fingerprint
        )
        if sticky_input is not None:
            resolved_input = sticky_input
            # peek_sticky_source only returns non-None for the side_file /
            # jsonl_cache kinds (it returns None for "pane"), so the minted
            # kind here is always a valid ResolvedAuqSource.kind literal.
            resolved_src = auq_source.ResolvedAuqSource(
                kind=cast(Literal["side_file", "jsonl_cache"], entry.source_kind),
                payload=sticky_input,
                source_fingerprint=entry.source_fingerprint,
            )
        else:
            resolved_src = auq_source.resolve_auq_source(window_id, None, pane or "")
            resolved_input = resolved_src.payload
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
                "AUQ_TAP toggle_reject user=%d window=%s opt=%d minted_fp=%s live_fp=%s "
                "reason_form_none=%s reason_fp=%s reason_mode=%s reason_incomplete=%s "
                "minted_src=%s live_src=%s minted_src_fp=%s live_src_fp=%s "
                "live_sel_mode=%s live_opts_complete=%s live_cursor=%s live_selected=%s",
                user.id,
                window_id,
                entry.option_number,
                entry.fingerprint[:8],
                current_form.fingerprint()[:8] if current_form else "none",
                current_form is None,
                bool(
                    current_form is not None
                    and current_form.fingerprint() != entry.fingerprint
                ),
                bool(current_form is not None and current_form.select_mode != "multi"),
                bool(current_form is not None and not current_form.options_complete),
                entry.source_kind,
                resolved_src.kind,
                entry.source_fingerprint[:8],
                resolved_src.source_fingerprint[:8],
                current_form.select_mode if current_form else "none",
                current_form.options_complete if current_form else "none",
                [o.number for o in current_form.options if o.cursor]
                if current_form
                else None,
                {o.number: o.selected for o in current_form.options}
                if current_form
                else None,
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

        logger.info(
            "AUQ_TAP toggle_dispatch_ok user=%d window=%s opt=%d send_keys_ok=%s "
            "minted_fp=%s live_fp=%s minted_src=%s live_src=%s "
            "live_sel_mode=%s live_opts_complete=%s live_cursor=%s live_selected=%s",
            user.id,
            window_id,
            entry.option_number,
            toggle_ok,
            entry.fingerprint[:8],
            current_form.fingerprint()[:8],
            entry.source_kind,
            resolved_src.kind,
            current_form.select_mode,
            current_form.options_complete,
            [o.number for o in current_form.options if o.cursor],
            {o.number: o.selected for o in current_form.options},
        )
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
                logger.info("AUQ_PICK malformed user=%d", user.id)
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
            logger.info(
                "AUQ_PICK entry user=%d window=%s opt=%d fp8=%s token=%s",
                user.id,
                "?",
                opt_num,
                fp8,
                token[:6],
            )
        else:
            logger.info("AUQ_PICK malformed user=%d", user.id)
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
        # tap can be detected even when the in-memory pick-token store has
        # been wiped.
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
            live = pick_token.peek(token)
            is_collision = (
                live is not None
                and live.user_id == user.id
                and ledger_key is not None
                and pick_token.stable_key(live) == ledger_key
            )
            if not is_collision:
                logger.info("AUQ_PICK wrong_user user=%d window=%s", user.id, "?")
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
            logger.info(
                "AUQ_PICK ledger_hit user=%d window=%s opt=%d proj_state=%s raw_state=%s",
                user.id,
                existing.window_id,
                existing.option_number,
                proj_state,
                existing.state,
            )
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

        # R4: side-effect-free peek to read entry.window_id (and entry.user_id
        # for the wrong-user gate). The pane capture, source/form re-resolve,
        # and single-use consume all move INSIDE
        # pick_token.validate_and_consume (atomic by exclusive reservation).
        # The stale-window lease check stays here — it needs safe_answer — and
        # fires BEFORE validate_and_consume so a stale-window tap never reserves
        # or burns the owner's token.
        peeked = pick_token.peek(token)
        if peeked is None:
            # Token never existed, was already used, aged past the TTL, or — the
            # D2 case — was wiped by a bot RESTART while the published card kept
            # its old keyboard (dead token strings baked into callback_data). Try
            # restart-recovery first; if it doesn't take over, refresh the card so
            # the user taps a fresh button. (The ledger gate above already
            # answered any real SEQUENTIAL duplicate.)
            logger.info("AUQ_PICK peek_none user=%d token=%s", user.id, token[:6])
            if await _attempt_pick_recovery(
                token,
                user.id,
                route_hash,
                fp8,
                opt_num,
                query=query,
                context=context,
                user=user,
                tmux_manager=tmux_manager,
                adapters=adapters,
                reject_stale_window=reject_stale_window_callback,
            ):
                return
            await _refresh_pick_card(
                query,
                context,
                update,
                user,
                tmux_manager,
                adapters,
                text="↻ Refreshed — tap your choice again.",
                show_alert=True,
            )
            return
        thread_id = peeked.thread_id
        window_id = peeked.window_id
        # Wrong-user gate, BEFORE the lease check — preserves the authorization
        # invariant (a shared-topic intruder gets WRONG_USER_PICK_TEXT, never
        # the option label or a stale-window message) and matches the prior
        # owner-before-lease ordering. Side-effect-free: no reserve, no consume,
        # so it cannot burn the owner's token. validate_and_consume's own phase
        # (a) owner check is the authoritative, race-safe re-check.
        if not owner_matches(peeked, user.id):
            logger.info("AUQ_PICK wrong_user user=%d window=%s", user.id, window_id)
            await safe_answer(query, WRONG_USER_PICK_TEXT, show_alert=True)
            return
        if await reject_stale_window_callback(window_id):
            logger.info("AUQ_PICK stale_window user=%d window=%s", user.id, window_id)
            return

        # Atomic validate + single-use consume. Re-resolves the AUQ source via
        # the SAME auq_source.resolve_auq_source the minter used (measurable
        # source parity), re-parses the live pane (fingerprint staleness), and
        # wins-or-loses the consume by exclusive reservation — without holding
        # the store lock across capture_pane / find_window_by_id. Capture with
        # the SAME 500-line scrollback as the render path so the validate pane
        # slice matches the mint pane slice (a smaller capture would shift
        # current_tab_inferred / options and bounce long pickers).
        async def _capture(wid: str, scrollback: int) -> str | None:
            return await tmux_manager.capture_pane(wid, scrollback_lines=scrollback)

        result = await pick_token.validate_and_consume(
            token,
            user.id,
            capture_pane=_capture,
            find_window_by_id=tmux_manager.find_window_by_id,
        )
        entry = result.entry
        current_form = result.current_form
        logger.info(
            "AUQ_PICK validate user=%d window=%s opt=%d outcome=%s is_review_submit=%s",
            user.id,
            window_id,
            peeked.option_number,
            result.outcome,
            peeked.is_review_submit,
        )
        if result.outcome == "wrong_user":
            await safe_answer(query, WRONG_USER_PICK_TEXT, show_alert=True)
            return
        if result.outcome == "already_consumed":
            # In-flight CONCURRENT duplicate (a second tap arrived while the
            # first held the reservation, or a losing sibling whose row was
            # tombstoned). The sequential duplicate was already answered by the
            # ledger gate above; this is the concurrent-race UX.
            await safe_answer(query, "Action already received.", show_alert=False)
            return
        if result.outcome == "expired":
            # A token that survived peek but lost the consume race / TTL-pruned
            # mid-flight. Same restart-recovery net as peek_none (gated identically
            # — a tombstoned/live row declines via the cache-row proof).
            if await _attempt_pick_recovery(
                token,
                user.id,
                route_hash,
                fp8,
                opt_num,
                query=query,
                context=context,
                user=user,
                tmux_manager=tmux_manager,
                adapters=adapters,
                reject_stale_window=reject_stale_window_callback,
            ):
                return
            await _refresh_pick_card(
                query,
                context,
                update,
                user,
                tmux_manager,
                adapters,
                text="↻ Refreshed — tap your choice again.",
                show_alert=True,
                fallback_window_id=window_id,
            )
            return
        if result.outcome == "window_gone":
            await safe_answer(query, "Window not found", show_alert=True)
            return
        if result.outcome in ("stale_form", "source_drift"):
            logger.info(
                "Pick-token %s reject: user=%d window=%s opt=%d minted_fp=%s",
                result.outcome,
                user.id,
                window_id,
                peeked.option_number,
                peeked.fingerprint,
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
        # outcome == "ok": entry + current_form are present (validate_and_consume
        # hands the live re-parse back on a winning consume).
        assert entry is not None and current_form is not None
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await safe_answer(query, "Window not found", show_alert=True)
            return

        # Submit-button guardrail: a click flagged ``is_review_submit`` only
        # fires when the live parse still says we're on the review screen with
        # the literal "Submit answers" row as option 1 AND a matching minted
        # label — CURSOR-BLIND. The digit `1` activates Submit regardless of the
        # terminal cursor (verified on Claude Code v2.1.161), so we no longer
        # require the cursor on Submit; the review-screen + option#1 + literal
        # "Submit answers" + minted-label anchors mean a non-review screen, a
        # relabeled Submit, or a reordered review layout all SAFELY DECLINE
        # rather than dispatching the wrong action.
        if entry.is_review_submit and not current_form.review_submit_dispatchable(
            entry.option_label
        ):
            logger.info(
                "AUQ_PICK submit_guard_reject user=%d window=%s",
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

        # Dispatch the bare digit (no Enter — v2.1.167 select+advance/submit) and
        # record the ledger lifecycle via the shared helper (also used by D2
        # restart-recovery). The ``accepted`` claim was already written above.
        await _dispatch_pick_digit(
            query=query,
            context=context,
            user=user,
            tmux_manager=tmux_manager,
            adapters=adapters,
            w=w,
            window_id=window_id,
            thread_id=thread_id,
            option_number=entry.option_number,
            option_label=entry.option_label,
            ledger_key=ledger_key,
        )
