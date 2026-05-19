"""Regression tests for stale window-id callback rejection.

Inline controls encode a tmux window id, but a Telegram topic can later be
rebound or unbound. These tests ensure old screenshot/interactive/window-picker
callbacks do not act on a stale window after the topic binding changed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cctelegram import bot as bot_module
from cctelegram.handlers.callback_data import (
    CB_ASK_UP,
    CB_HISTORY_NEXT,
    CB_HISTORY_PREV,
    CB_KEYS_PREFIX,
    CB_SCREENSHOT_REFRESH,
    CB_WIN_BIND,
)
from cctelegram.handlers.directory_browser import (
    STATE_KEY,
    STATE_SELECTING_WINDOW,
    UNBOUND_WINDOWS_KEY,
)


def _make_callback_update(data: str, *, thread_id: int = 10) -> MagicMock:
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_media = AsyncMock()
    query.message = MagicMock()
    query.message.message_thread_id = thread_id
    query.message.chat = MagicMock()
    query.message.chat.id = -100123
    query.message.chat.type = "supergroup"

    update = MagicMock()
    update.message = None
    update.callback_query = query
    update.effective_user = MagicMock()
    update.effective_user.id = 1
    update.effective_chat = query.message.chat
    return update


def _make_context(user_data: dict[str, object] | None = None) -> MagicMock:
    context = MagicMock()
    context.bot = MagicMock()
    context.user_data = {} if user_data is None else user_data
    return context


@pytest.mark.asyncio
@pytest.mark.parametrize("current_window", ["@1", None], ids=["rebound", "unbound"])
async def test_stale_screenshot_refresh_rejected_after_topic_rebound_or_unbound(
    current_window: str | None,
):
    update = _make_callback_update(f"{CB_SCREENSHOT_REFRESH}@0")
    context = _make_context()

    with (
        patch.object(bot_module, "is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        patch.object(
            bot_module.session_manager,
            "resolve_window_for_thread",
            return_value=current_window,
        ),
        patch.object(
            bot_module.tmux_manager, "find_window_by_id", new_callable=AsyncMock
        ) as mock_find,
        patch.object(
            bot_module.tmux_manager, "capture_pane", new_callable=AsyncMock
        ) as mock_capture,
        patch.object(bot_module, "text_to_image", new_callable=AsyncMock) as mock_image,
    ):
        await bot_module.callback_handler(update, context)

    update.callback_query.answer.assert_awaited_once_with(
        "This button is stale for this topic — refresh the picker.", show_alert=True
    )
    mock_find.assert_not_called()
    mock_capture.assert_not_called()
    mock_image.assert_not_called()
    update.callback_query.edit_message_media.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("current_window", ["@1", None], ids=["rebound", "unbound"])
@pytest.mark.parametrize("history_prefix", [CB_HISTORY_PREV, CB_HISTORY_NEXT])
async def test_stale_history_pagination_rejected_before_tmux_lookup(
    current_window: str | None, history_prefix: str
):
    update = _make_callback_update(f"{history_prefix}1:@0:10:20")
    context = _make_context()

    with (
        patch.object(bot_module, "is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        patch.object(
            bot_module.session_manager,
            "resolve_window_for_thread",
            return_value=current_window,
        ),
        patch.object(
            bot_module.tmux_manager, "find_window_by_id", new_callable=AsyncMock
        ) as mock_find,
        patch.object(
            bot_module, "send_history", new_callable=AsyncMock
        ) as mock_history,
    ):
        await bot_module.callback_handler(update, context)

    update.callback_query.answer.assert_awaited_once_with(
        "This button is stale for this topic — refresh the picker.", show_alert=True
    )
    mock_find.assert_not_called()
    mock_history.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("current_window", ["@1", None], ids=["rebound", "unbound"])
async def test_stale_screenshot_quick_key_rejected_after_topic_rebound_or_unbound(
    current_window: str | None,
):
    update = _make_callback_update(f"{CB_KEYS_PREFIX}up:@0")
    context = _make_context()

    with (
        patch.object(bot_module, "is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        patch.object(
            bot_module.session_manager,
            "resolve_window_for_thread",
            return_value=current_window,
        ),
        patch.object(
            bot_module.tmux_manager, "find_window_by_id", new_callable=AsyncMock
        ) as mock_find,
        patch.object(
            bot_module.tmux_manager, "send_keys", new_callable=AsyncMock
        ) as mock_send_keys,
        patch.object(
            bot_module.tmux_manager, "capture_pane", new_callable=AsyncMock
        ) as mock_capture,
    ):
        await bot_module.callback_handler(update, context)

    update.callback_query.answer.assert_awaited_once_with(
        "This button is stale for this topic — refresh the picker.", show_alert=True
    )
    mock_find.assert_not_called()
    mock_send_keys.assert_not_called()
    mock_capture.assert_not_called()
    update.callback_query.edit_message_media.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("current_window", ["@1", None], ids=["rebound", "unbound"])
async def test_stale_interactive_key_rejected_after_topic_rebound_or_unbound(
    current_window: str | None,
):
    update = _make_callback_update(f"{CB_ASK_UP}@0")
    context = _make_context()

    with (
        patch.object(bot_module, "is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        patch.object(
            bot_module.session_manager,
            "resolve_window_for_thread",
            return_value=current_window,
        ),
        patch.object(
            bot_module.tmux_manager, "find_window_by_id", new_callable=AsyncMock
        ) as mock_find,
        patch.object(
            bot_module.tmux_manager, "send_keys", new_callable=AsyncMock
        ) as mock_send_keys,
        patch.object(
            bot_module, "handle_interactive_ui", new_callable=AsyncMock
        ) as mock_handle_ui,
    ):
        await bot_module.callback_handler(update, context)

    update.callback_query.answer.assert_awaited_once_with(
        "This button is stale for this topic — refresh the picker.", show_alert=True
    )
    mock_find.assert_not_called()
    mock_send_keys.assert_not_called()
    mock_handle_ui.assert_not_called()


@pytest.mark.asyncio
async def test_window_picker_rejects_window_that_became_bound_after_render():
    update = _make_callback_update(f"{CB_WIN_BIND}0")
    context = _make_context(
        {
            STATE_KEY: STATE_SELECTING_WINDOW,
            UNBOUND_WINDOWS_KEY: ["@0"],
            "_pending_thread_id": 10,
            "_pending_thread_text": "hello",
        }
    )
    window = MagicMock()
    window.window_id = "@0"
    window.window_name = "stale-window"

    with (
        patch.object(bot_module, "is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        patch.object(bot_module.session_manager, "bind_thread") as mock_bind,
        patch.object(
            bot_module.tmux_manager,
            "find_window_by_id",
            new_callable=AsyncMock,
            return_value=window,
        ),
        patch.object(
            bot_module,
            "_list_unbound_windows",
            new_callable=AsyncMock,
            return_value=[("@1", "other", "/tmp")],
        ),
        patch.object(bot_module, "safe_edit", new_callable=AsyncMock) as mock_safe_edit,
        patch.object(
            bot_module, "aggregator_replay_payload", new_callable=AsyncMock
        ) as mock_replay,
    ):
        await bot_module.callback_handler(update, context)

    update.callback_query.answer.assert_awaited_once_with(
        "Window is no longer unbound, please retry", show_alert=True
    )
    mock_bind.assert_not_called()
    mock_safe_edit.assert_not_called()
    mock_replay.assert_not_called()
    assert context.user_data[UNBOUND_WINDOWS_KEY] == ["@0"]


@pytest.mark.asyncio
async def test_window_picker_bind_without_pending_owner_rejects_before_tmux_lookup():
    update = _make_callback_update(f"{CB_WIN_BIND}0")
    context = _make_context(
        {
            STATE_KEY: STATE_SELECTING_WINDOW,
            UNBOUND_WINDOWS_KEY: ["@0"],
        }
    )
    window = MagicMock()
    window.window_id = "@0"
    window.window_name = "unbound-window"

    with (
        patch.object(bot_module, "is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        patch.object(bot_module.session_manager, "bind_thread") as mock_bind,
        patch.object(
            bot_module.tmux_manager,
            "find_window_by_id",
            new_callable=AsyncMock,
            return_value=window,
        ) as mock_find,
        patch.object(
            bot_module,
            "_list_unbound_windows",
            new_callable=AsyncMock,
            return_value=[("@0", "unbound-window", "/tmp")],
        ) as mock_list_unbound,
        patch.object(bot_module, "safe_edit", new_callable=AsyncMock) as mock_safe_edit,
        patch.object(
            bot_module, "aggregator_replay_payload", new_callable=AsyncMock
        ) as mock_replay,
    ):
        await bot_module.callback_handler(update, context)

    update.callback_query.answer.assert_awaited_once_with(
        "Stale picker (topic mismatch)", show_alert=True
    )
    mock_find.assert_not_called()
    mock_list_unbound.assert_not_called()
    mock_bind.assert_not_called()
    mock_safe_edit.assert_not_called()
    mock_replay.assert_not_called()
    assert context.user_data[UNBOUND_WINDOWS_KEY] == ["@0"]
