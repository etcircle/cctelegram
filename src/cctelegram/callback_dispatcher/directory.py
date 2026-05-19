"""Execute directory, session, and existing-window callback flows.

Core responsibilities:
  - Own CB_DIR_*, CB_SESSION_*, and CB_WIN_* callback execution.
  - Keep pending-topic picker revalidation next to picker mutations.
  - Transition between directory browser, session picker, and window picker UI.

Key components:
  - execute_directory_callback()
"""

from __future__ import annotations

from typing import Any

import logging
from pathlib import Path
from cctelegram.handlers.callback_data import (
    CB_DIR_BIND_EXISTING,
    CB_DIR_CANCEL,
    CB_DIR_CONFIRM,
    CB_DIR_PAGE,
    CB_DIR_SELECT,
    CB_DIR_UP,
    CB_SESSION_CANCEL,
    CB_SESSION_NEW,
    CB_SESSION_SELECT,
    CB_WIN_BIND,
    CB_WIN_CANCEL,
    CB_WIN_NEW,
)
from cctelegram.handlers.directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    BROWSE_UNBOUND_COUNT_KEY,
    SESSIONS_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_SELECTING_SESSION,
    STATE_SELECTING_WINDOW,
    UNBOUND_WINDOWS_KEY,
    build_directory_browser,
    build_session_picker,
    build_window_picker,
    clear_browse_state,
    clear_session_picker_state,
    clear_window_picker_state,
)
from cctelegram.handlers.inbound_telegram import (
    _clear_pending_route_payload,
    _create_and_bind_window,
    _flush_pending_route_payload,
    _get_thread_id,
    _list_unbound_windows,
)
from cctelegram.handlers.message_sender import safe_edit

from . import (
    _answer_invalid_pending_picker_callback,
    _validate_pending_picker_callback,
    revalidate_before_mutation,
    window_lease,
)

logger = logging.getLogger(__name__)


async def execute_directory_callback(authorized: Any, adapters: Any) -> None:
    update = authorized.ctx.update
    context = authorized.ctx.context
    user = authorized.ctx.user
    query = authorized.ctx.query
    data = authorized.command.data
    cb_thread_id = authorized.ctx.thread_id
    lease = window_lease(authorized, adapters)
    session_manager = adapters.session_manager
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

    # Directory browser handlers
    if data.startswith(CB_DIR_SELECT):
        stale, pending_tid = await reject_invalid_pending_picker(
            (STATE_BROWSING_DIRECTORY,), "Stale browser (topic mismatch)"
        )
        if stale:
            return
        # callback_data contains index, not dir name (to avoid 64-byte limit)
        try:
            idx = int(data[len(CB_DIR_SELECT) :])
        except ValueError:
            await query.answer("Invalid data")
            return

        # Look up dir name from cached subdirs
        cached_dirs: list[str] = (
            context.user_data.get(BROWSE_DIRS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_dirs):
            await query.answer(
                "Directory list changed, please refresh", show_alert=True
            )
            return
        subdir_name = cached_dirs[idx]

        default_path = str(Path.cwd())
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        new_path = (Path(current_path) / subdir_name).resolve()

        if not new_path.exists() or not new_path.is_dir():
            await query.answer("Directory not found", show_alert=True)
            return

        new_path_str = str(new_path)
        logger.info(
            "CB_DIR_SELECT: idx=%d name=%s current=%s -> new=%s (user=%d, thread=%s)",
            idx,
            subdir_name,
            current_path,
            new_path_str,
            user.id,
            pending_tid,
        )
        if context.user_data is not None:
            context.user_data[BROWSE_PATH_KEY] = new_path_str
            context.user_data[BROWSE_PAGE_KEY] = 0

        unbound_count = (
            context.user_data.get(BROWSE_UNBOUND_COUNT_KEY, 0)
            if context.user_data
            else 0
        )
        msg_text, keyboard, subdirs = build_directory_browser(
            new_path_str, unbound_count=unbound_count
        )
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data == CB_DIR_UP:
        stale, _pending_tid = await reject_invalid_pending_picker(
            (STATE_BROWSING_DIRECTORY,), "Stale browser (topic mismatch)"
        )
        if stale:
            return
        default_path = str(Path.cwd())
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        current = Path(current_path).resolve()
        parent = current.parent
        # No restriction - allow navigating anywhere

        parent_path = str(parent)
        if context.user_data is not None:
            context.user_data[BROWSE_PATH_KEY] = parent_path
            context.user_data[BROWSE_PAGE_KEY] = 0

        unbound_count = (
            context.user_data.get(BROWSE_UNBOUND_COUNT_KEY, 0)
            if context.user_data
            else 0
        )
        msg_text, keyboard, subdirs = build_directory_browser(
            parent_path, unbound_count=unbound_count
        )
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data.startswith(CB_DIR_PAGE):
        stale, _pending_tid = await reject_invalid_pending_picker(
            (STATE_BROWSING_DIRECTORY,), "Stale browser (topic mismatch)"
        )
        if stale:
            return
        try:
            pg = int(data[len(CB_DIR_PAGE) :])
        except ValueError:
            await query.answer("Invalid data")
            return
        default_path = str(Path.cwd())
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        if context.user_data is not None:
            context.user_data[BROWSE_PAGE_KEY] = pg

        unbound_count = (
            context.user_data.get(BROWSE_UNBOUND_COUNT_KEY, 0)
            if context.user_data
            else 0
        )
        msg_text, keyboard, subdirs = build_directory_browser(
            current_path, pg, unbound_count=unbound_count
        )
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data == CB_DIR_CONFIRM:
        stale, pending_thread_id = await reject_invalid_pending_picker(
            (STATE_BROWSING_DIRECTORY,),
            "Stale browser (topic mismatch)",
        )
        if stale:
            return
        default_path = str(Path.cwd())
        selected_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )

        clear_browse_state(context.user_data)

        # Check for existing sessions in this directory
        sessions = await session_manager.list_sessions_for_directory(selected_path)
        if not await revalidate_before_mutation(
            query,
            context,
            pending_thread_id,
            "Stale browser (topic mismatch)",
        ):
            return
        if sessions:
            # Show session picker — store state for later
            if context.user_data is not None:
                context.user_data[STATE_KEY] = STATE_SELECTING_SESSION
                context.user_data[SESSIONS_KEY] = sessions
                context.user_data["_selected_path"] = selected_path
            text, keyboard = build_session_picker(sessions)
            await safe_edit(query, text, reply_markup=keyboard)
            await query.answer()
            return

        # No existing sessions — create new window directly
        await _create_and_bind_window(
            query,
            context,
            user,
            selected_path,
            pending_thread_id,
            tmux_mgr=adapters.tmux_manager,
            session_mgr=adapters.session_manager,
        )

    elif data == CB_DIR_CANCEL:
        stale, _pending_tid = await reject_invalid_pending_picker(
            (STATE_BROWSING_DIRECTORY,), "Stale browser (topic mismatch)"
        )
        if stale:
            return
        clear_browse_state(context.user_data)
        if context.user_data is not None:
            _clear_pending_route_payload(context.user_data, delete_files=True)
        await safe_edit(query, "Cancelled")
        await query.answer("Cancelled")

    # Session picker: resume existing session
    elif data.startswith(CB_SESSION_SELECT):
        stale, pending_tid = await reject_invalid_pending_picker(
            (STATE_SELECTING_SESSION,), "Stale picker (topic mismatch)"
        )
        if stale:
            return
        try:
            idx = int(data[len(CB_SESSION_SELECT) :])
        except ValueError:
            await query.answer("Invalid data")
            return

        cached_sessions = (
            context.user_data.get(SESSIONS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_sessions):
            await query.answer("Session not found")
            return

        session = cached_sessions[idx]
        selected_path = (
            context.user_data.get("_selected_path", str(Path.cwd()))
            if context.user_data
            else str(Path.cwd())
        )
        clear_session_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_selected_path", None)

        await _create_and_bind_window(
            query,
            context,
            user,
            selected_path,
            pending_tid,
            tmux_mgr=adapters.tmux_manager,
            session_mgr=adapters.session_manager,
            resume_session_id=session.session_id,
        )

    elif data == CB_SESSION_NEW:
        stale, pending_tid = await reject_invalid_pending_picker(
            (STATE_SELECTING_SESSION,), "Stale picker (topic mismatch)"
        )
        if stale:
            return
        selected_path = (
            context.user_data.get("_selected_path", str(Path.cwd()))
            if context.user_data
            else str(Path.cwd())
        )
        clear_session_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_selected_path", None)

        await _create_and_bind_window(
            query,
            context,
            user,
            selected_path,
            pending_tid,
            tmux_mgr=adapters.tmux_manager,
            session_mgr=adapters.session_manager,
        )

    elif data == CB_SESSION_CANCEL:
        stale, _pending_tid = await reject_invalid_pending_picker(
            (STATE_SELECTING_SESSION,), "Stale picker (topic mismatch)"
        )
        if stale:
            return
        clear_session_picker_state(context.user_data)
        if context.user_data is not None:
            _clear_pending_route_payload(context.user_data, delete_files=True)
        await safe_edit(query, "Cancelled")
        await query.answer("Cancelled")

    # Window picker: bind existing window
    elif data.startswith(CB_WIN_BIND):
        stale, _pending_tid = await reject_invalid_pending_picker(
            (STATE_SELECTING_WINDOW,), "Stale picker (topic mismatch)"
        )
        if stale:
            return
        try:
            idx = int(data[len(CB_WIN_BIND) :])
        except ValueError:
            await query.answer("Invalid data")
            return

        cached_windows: list[str] = (
            context.user_data.get(UNBOUND_WINDOWS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_windows):
            await query.answer("Window list changed, please retry", show_alert=True)
            return
        selected_wid = cached_windows[idx]

        # Verify window still exists
        w = await tmux_manager.find_window_by_id(selected_wid)
        if not w:
            display = session_manager.get_display_name(selected_wid)
            await query.answer(f"Window '{display}' no longer exists", show_alert=True)
            return

        thread_id = _get_thread_id(update)
        if thread_id is None:
            await query.answer("Not in a topic", show_alert=True)
            return

        current_unbound_ids = {
            wid
            for wid, _, _ in await _list_unbound_windows(
                adapters.tmux_manager, adapters.session_manager
            )
        }
        if selected_wid not in current_unbound_ids:
            await query.answer(
                "Window is no longer unbound, please retry", show_alert=True
            )
            return

        ok, _pending_tid, _reason = _validate_pending_picker_callback(
            context.user_data,
            cb_thread_id,
            (STATE_SELECTING_WINDOW,),
        )
        if not ok:
            await _answer_invalid_pending_picker_callback(
                query,
                "Stale picker (topic mismatch)",
            )
            return

        display = w.window_name
        clear_window_picker_state(context.user_data)
        session_manager.bind_thread(
            user.id, thread_id, selected_wid, window_name=display
        )

        # Replay pending text and/or attachments through the synchronous
        # aggregator helper so §2.8.2 formatting is preserved without
        # offer-path background/intermediate flushes hiding failures.
        route = (user.id, thread_id, selected_wid)
        pending_delivered = await _flush_pending_route_payload(route, context.user_data)
        if pending_delivered is False:
            await safe_edit(
                query,
                f"✅ Bound to window `{display}`\n\n"
                "⚠️ First message failed to send. The pending payload was "
                "cleared; please resend it here.",
            )
            await query.answer("Bound; first message failed", show_alert=True)
            return

        first_turn_note = "\n\nFirst message sent." if pending_delivered is True else ""
        await safe_edit(
            query,
            f"✅ Bound to window `{display}`{first_turn_note}",
        )
        await query.answer("Bound")

    # Window picker: new session → transition to directory browser
    elif data == CB_WIN_NEW:
        stale, pending_tid = await reject_invalid_pending_picker(
            (STATE_SELECTING_WINDOW,), "Stale picker (topic mismatch)"
        )
        if stale:
            return
        # Preserve pending thread info, clear only picker state
        clear_window_picker_state(context.user_data)
        unbound_count = len(
            await _list_unbound_windows(adapters.tmux_manager, adapters.session_manager)
        )
        start_path = str(adapters.config.browse_root)
        msg_text, keyboard, subdirs = build_directory_browser(
            start_path, unbound_count=unbound_count
        )
        logger.info(
            "CB_WIN_NEW: opening directory browser at %s (subdirs=%d, user=%d, thread=%s)",
            start_path,
            len(subdirs),
            user.id,
            pending_tid,
        )
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = start_path
            context.user_data[BROWSE_PAGE_KEY] = 0
            context.user_data[BROWSE_DIRS_KEY] = subdirs
            context.user_data[BROWSE_UNBOUND_COUNT_KEY] = unbound_count
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    # Directory browser: opt-in pivot to window picker
    elif data == CB_DIR_BIND_EXISTING:
        stale, _pending_tid = await reject_invalid_pending_picker(
            (STATE_BROWSING_DIRECTORY,), "Stale browser (topic mismatch)"
        )
        if stale:
            return
        unbound = await _list_unbound_windows(
            adapters.tmux_manager, adapters.session_manager
        )
        if not unbound:
            await query.answer("No unbound windows available", show_alert=True)
            return
        msg_text, keyboard, win_ids = build_window_picker(unbound)
        # Swap state from browse → picker. Keep pending thread/text/attachments
        # so the bind handler can flush them once a window is chosen.
        clear_browse_state(context.user_data)
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_SELECTING_WINDOW
            context.user_data[UNBOUND_WINDOWS_KEY] = win_ids
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    # Window picker: cancel
    elif data == CB_WIN_CANCEL:
        stale, _pending_tid = await reject_invalid_pending_picker(
            (STATE_SELECTING_WINDOW,), "Stale picker (topic mismatch)"
        )
        if stale:
            return
        clear_window_picker_state(context.user_data)
        if context.user_data is not None:
            _clear_pending_route_payload(context.user_data, delete_files=True)
        await safe_edit(query, "Cancelled")
        await query.answer("Cancelled")
