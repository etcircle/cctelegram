"""Regression tests for unbound-topic pending payload cleanup.

Unbound text/photo/document payloads live in ``context.user_data`` while the
user is choosing a directory, existing session, or tmux window. Cancel paths
must clear the whole bundle, including downloaded files. Stale picker callbacks
must be rejected without acting on or deleting the active pending owner.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import CallbackQuery, User

from cctelegram import bot as bot_module
from cctelegram.callback_dispatcher import directory as dispatcher_directory
from cctelegram.callback_dispatcher import effort as dispatcher_effort
from cctelegram.callback_dispatcher import history as dispatcher_history
from cctelegram.callback_dispatcher import interactive as dispatcher_interactive
from cctelegram.callback_dispatcher import screenshot as dispatcher_screenshot
from cctelegram.callback_dispatcher import bash as dispatcher_bash
from cctelegram.handlers import inbound_telegram as inbound_module
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
)
from cctelegram.session import ClaudeSession


@contextmanager
def _patch_both(name: str, *args, **kwargs) -> Iterator[object]:
    """Patch ``name`` on both ``bot_module`` and ``inbound_module``.

    Wave C.1 split inbound handlers into ``handlers.inbound_telegram``.
    Names fall into three groups after the split:

    1. **Shared aliases** still re-exported from ``bot`` AND defined in
       ``inbound_telegram`` (``safe_edit``, ``safe_reply``,
       ``is_user_allowed``, ``_apply_reply_context``, ``_list_unbound_windows``,
       ``aggregator_offer_photo``/``_document``/``_voice``/``_text``,
       ``aggregator_replay_payload``, ``_create_and_bind_window``,
       ``build_directory_browser``). Callers in either module resolve
       through their own globals; this helper patches both to the same
       mock so ``assert_called_once_with(...)`` sees calls from either side.
    2. **Inbound-only** (no longer aliased from ``bot``):
       ``_IMAGES_DIR``/``_FILES_DIR``, ``clear_status_msg_info``,
       ``set_route_last_user_message``, ``aggregator_clear_route``.
       Only the inbound patch is installed; the helper returns the
       inbound-side mock so ``as mock_X`` still binds.
    3. **Bot-only** (``session_monitor``, ``clear_topic_state``): the
       test sites use ``patch.object(bot_module, ...)`` directly — this
       helper is not called for them.
    """
    # Build one shared mock so test assertions on the returned mock pick up
    # calls from either module's namespace. Without a shared instance, the
    # two `patch.object` calls would auto-create separate MagicMocks and
    # ``mock.assert_called_once_with(...)`` would miss the inbound side
    # (where the moved handler actually does the lookup).
    # Skip if the caller already passed `new` (positional or keyword) —
    # ``_patch_both("_IMAGES_DIR", media_dir)`` treats ``media_dir`` itself
    # as the patch value for both modules.
    if not args and "new" not in kwargs:
        new_callable = kwargs.pop("new_callable", None) or MagicMock
        mock = new_callable()
        for cfg_key in ("return_value", "side_effect"):
            if cfg_key in kwargs:
                setattr(mock, cfg_key, kwargs.pop(cfg_key))
        kwargs["new"] = mock
    with ExitStack() as stack:
        result = None
        for module in (
            bot_module,
            inbound_module,
            dispatcher_directory,
            dispatcher_effort,
            dispatcher_history,
            dispatcher_interactive,
            dispatcher_screenshot,
            dispatcher_bash,
        ):
            if hasattr(module, name):
                module_result = stack.enter_context(
                    patch.object(module, name, *args, **kwargs)
                )
                if result is None:
                    result = module_result
        yield result


def _attachment(path: Path) -> bot_module.PendingAttachment:
    return bot_module.PendingAttachment(str(path), "caption", None)


def _replay_attachment(path: Path) -> bot_module.AggregatorReplayAttachment:
    return bot_module.AggregatorReplayAttachment(path=path, caption="caption")


def _pending_user_data(path: Path, *, thread_id: int = 10) -> dict[str, object]:
    return {
        STATE_KEY: STATE_BROWSING_DIRECTORY,
        "_pending_thread_id": thread_id,
        "_pending_thread_text": "hello",
        "_pending_thread_attachments": [_attachment(path)],
        "_selected_path": "/tmp/selected",
    }


def test_clear_pending_route_payload_deletes_cancelled_files(tmp_path: Path):
    payload = tmp_path / "cancelled.jpg"
    payload.write_bytes(b"image")
    user_data = _pending_user_data(payload)
    user_data["_ignored_stale_thread_ids"] = [99]

    attachments = bot_module._clear_pending_route_payload(user_data, delete_files=True)

    assert attachments == [_attachment(payload)]
    assert not payload.exists()
    assert "_pending_thread_id" not in user_data
    assert "_pending_thread_text" not in user_data
    assert "_pending_thread_attachments" not in user_data
    assert "_selected_path" not in user_data
    assert "_ignored_stale_thread_ids" not in user_data


def test_clear_pending_route_payload_preserves_files_for_successful_flush(
    tmp_path: Path,
):
    payload = tmp_path / "flush.jpg"
    payload.write_bytes(b"image")
    user_data = _pending_user_data(payload)

    attachments = bot_module._clear_pending_route_payload(user_data, delete_files=False)

    assert attachments == [_attachment(payload)]
    assert payload.exists()
    assert "_pending_thread_attachments" not in user_data


def _make_topic_closed_update(*, thread_id: int = 10) -> MagicMock:
    message = MagicMock()
    message.message_thread_id = thread_id
    message.chat = MagicMock()
    message.chat.id = -100123
    message.chat.type = "supergroup"
    message.forum_topic_closed = MagicMock()

    update = MagicMock()
    update.message = message
    update.callback_query = None
    update.effective_user = MagicMock()
    update.effective_user.id = 1
    update.effective_chat = message.chat
    return update


@pytest.mark.asyncio
async def test_topic_close_unbound_matching_pending_file_deletes_and_clears_state(
    tmp_path: Path,
):
    payload = tmp_path / "unbound-close.bin"
    payload.write_bytes(b"data")
    context = MagicMock()
    context.user_data = _pending_user_data(payload, thread_id=10)
    context.bot = MagicMock()
    update = _make_topic_closed_update(thread_id=10)

    with (
        _patch_both("is_user_allowed", return_value=True),
        patch.object(
            bot_module.session_manager, "get_window_for_thread", return_value=None
        ) as mock_get_window,
        patch.object(
            bot_module, "clear_topic_state", new_callable=AsyncMock
        ) as mock_clear,
    ):
        await bot_module.topic_closed_handler(update, context)

    mock_get_window.assert_called_once_with(1, 10)
    mock_clear.assert_not_called()
    assert not payload.exists()
    assert STATE_KEY not in context.user_data
    assert BROWSE_PATH_KEY not in context.user_data
    assert "_pending_thread_attachments" not in context.user_data
    assert "_pending_thread_text" not in context.user_data
    assert "_pending_thread_id" not in context.user_data


@pytest.mark.asyncio
async def test_topic_close_bound_matching_pending_attachments_deletes_and_clears_state(
    tmp_path: Path,
):
    payload = tmp_path / "bound-close.bin"
    payload.write_bytes(b"data")
    context = MagicMock()
    context.user_data = _pending_user_data(payload, thread_id=10)
    context.bot = MagicMock()
    update = _make_topic_closed_update(thread_id=10)
    window = MagicMock()
    window.window_id = "@0"

    with (
        _patch_both("is_user_allowed", return_value=True),
        patch.object(
            bot_module.session_manager, "get_window_for_thread", return_value="@0"
        ),
        patch.object(
            bot_module.session_manager, "get_display_name", return_value="bound-window"
        ),
        patch.object(bot_module.session_manager, "unbind_thread") as mock_unbind,
        patch.object(
            bot_module.tmux_manager,
            "find_window_by_id",
            new_callable=AsyncMock,
            return_value=window,
        ) as mock_find,
        patch.object(
            bot_module.tmux_manager, "kill_window", new_callable=AsyncMock
        ) as mock_kill,
        patch.object(
            bot_module, "clear_topic_state", new_callable=AsyncMock
        ) as mock_clear,
    ):
        await bot_module.topic_closed_handler(update, context)

    mock_find.assert_awaited_once_with("@0")
    mock_kill.assert_awaited_once_with("@0")
    mock_unbind.assert_called_once_with(1, 10)
    mock_clear.assert_awaited_once_with(1, 10, context.bot, context.user_data)
    assert not payload.exists()
    assert STATE_KEY not in context.user_data
    assert "_pending_thread_attachments" not in context.user_data
    assert "_pending_thread_text" not in context.user_data
    assert "_pending_thread_id" not in context.user_data


@pytest.mark.asyncio
async def test_topic_close_different_thread_preserves_active_pending_payload(
    tmp_path: Path,
):
    payload = tmp_path / "other-topic.bin"
    payload.write_bytes(b"data")
    context = MagicMock()
    context.user_data = _pending_user_data(payload, thread_id=99)
    context.bot = MagicMock()
    update = _make_topic_closed_update(thread_id=10)

    with (
        _patch_both("is_user_allowed", return_value=True),
        patch.object(
            bot_module.session_manager, "get_window_for_thread", return_value=None
        ),
        patch.object(
            bot_module, "clear_topic_state", new_callable=AsyncMock
        ) as mock_clear,
    ):
        await bot_module.topic_closed_handler(update, context)

    mock_clear.assert_not_called()
    assert payload.exists()
    assert context.user_data[STATE_KEY] == STATE_BROWSING_DIRECTORY
    assert context.user_data["_pending_thread_id"] == 99
    assert context.user_data["_pending_thread_text"] == "hello"
    assert context.user_data["_pending_thread_attachments"] == [_attachment(payload)]


def _make_callback_update(data: str, *, thread_id: int = 10) -> MagicMock:
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
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


def _make_real_callback_query(*, user_id: int = 1) -> tuple[CallbackQuery, User]:
    user = User(id=user_id, first_name="Test", is_bot=False)
    query = CallbackQuery(id="cbq", from_user=user, chat_instance="chat", data="x")
    return query, user


@pytest.mark.asyncio
async def test_create_and_bind_non_resume_hook_timeout_kills_created_window() -> None:
    query, user = _make_real_callback_query()
    context = MagicMock()
    context.user_data = {"_pending_thread_id": 10, "_pending_thread_text": "hello"}

    with (
        patch.object(
            bot_module.tmux_manager,
            "create_window",
            new_callable=AsyncMock,
            return_value=(True, "Created window 'repo' at /repo", "repo", "@42"),
        ),
        patch.object(
            bot_module.session_manager,
            "wait_for_session_map_entry",
            new_callable=AsyncMock,
            return_value=False,
        ) as wait_for_map,
        patch.object(
            bot_module.tmux_manager,
            "kill_window",
            new_callable=AsyncMock,
            return_value=True,
        ) as kill_window,
        patch.object(
            bot_module.session_manager, "get_window_state"
        ) as get_window_state,
        patch.object(bot_module.session_manager, "bind_thread") as bind_thread,
        _patch_both("safe_edit", new_callable=AsyncMock) as safe_edit,
        patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as answer,
    ):
        await bot_module._create_and_bind_window(
            query,
            context,
            user,
            "/repo",
            pending_thread_id=10,
            tmux_mgr=bot_module.tmux_manager,
            session_mgr=bot_module.session_manager,
        )

    wait_for_map.assert_awaited_once_with("@42", timeout=5.0)
    kill_window.assert_awaited_once_with("@42")
    get_window_state.assert_not_called()
    bind_thread.assert_not_called()
    safe_edit.assert_awaited_once()
    edited_text = safe_edit.await_args.args[1]
    assert "Claude session didn't register in time" in edited_text
    assert "unmonitored tmux window was cleaned up" in edited_text
    assert "_pending_thread_id" not in context.user_data
    assert "_pending_thread_text" not in context.user_data
    answer.assert_awaited_once_with("Hook timeout", show_alert=False)


@pytest.mark.asyncio
async def test_create_and_bind_hook_timeout_surfaces_cleanup_failure() -> None:
    query, user = _make_real_callback_query()
    context = MagicMock()
    context.user_data = {"_pending_thread_id": 10, "_pending_thread_text": "hello"}

    with (
        patch.object(
            bot_module.tmux_manager,
            "create_window",
            new_callable=AsyncMock,
            return_value=(True, "Created window 'repo' at /repo", "repo", "@43"),
        ),
        patch.object(
            bot_module.session_manager,
            "wait_for_session_map_entry",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch.object(
            bot_module.tmux_manager,
            "kill_window",
            new_callable=AsyncMock,
            return_value=False,
        ) as kill_window,
        patch.object(bot_module.session_manager, "bind_thread") as bind_thread,
        _patch_both("safe_edit", new_callable=AsyncMock) as safe_edit,
        patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as answer,
    ):
        await bot_module._create_and_bind_window(
            query,
            context,
            user,
            "/repo",
            pending_thread_id=10,
            tmux_mgr=bot_module.tmux_manager,
            session_mgr=bot_module.session_manager,
        )

    kill_window.assert_awaited_once_with("@43")
    bind_thread.assert_not_called()
    safe_edit.assert_awaited_once()
    edited_text = safe_edit.await_args.args[1]
    assert "Claude session didn't register in time" in edited_text
    assert "hook timeout remains the primary failure" in edited_text
    assert "could not be cleaned up automatically" in edited_text
    answer.assert_awaited_once_with("Hook timeout; cleanup failed", show_alert=True)


@pytest.mark.asyncio
async def test_create_and_bind_resume_timeout_does_not_kill_created_resume_window() -> (
    None
):
    query, user = _make_real_callback_query()
    context = MagicMock()
    context.user_data = {"_pending_thread_id": 10}
    window_state = SimpleNamespace(session_id="", cwd="", window_name="")

    with (
        patch.object(
            bot_module.tmux_manager,
            "create_window",
            new_callable=AsyncMock,
            return_value=(True, "Created window 'repo' at /repo", "repo", "@44"),
        ),
        patch.object(
            bot_module.session_manager,
            "wait_for_session_map_entry",
            new_callable=AsyncMock,
            return_value=False,
        ) as wait_for_map,
        patch.object(
            bot_module.tmux_manager,
            "kill_window",
            new_callable=AsyncMock,
        ) as kill_window,
        patch.object(
            bot_module.session_manager,
            "get_window_state",
            return_value=window_state,
        ),
        patch.object(bot_module.session_manager, "_save_state") as save_state,
        patch.object(bot_module.session_manager, "bind_thread") as bind_thread,
        _patch_both(
            "_flush_pending_route_payload",
            new_callable=AsyncMock,
            return_value=None,
        ),
        _patch_both("safe_edit", new_callable=AsyncMock) as safe_edit,
        patch.object(CallbackQuery, "answer", new_callable=AsyncMock),
    ):
        await bot_module._create_and_bind_window(
            query,
            context,
            user,
            "/repo",
            pending_thread_id=10,
            resume_session_id="resume-123",
            tmux_mgr=bot_module.tmux_manager,
            session_mgr=bot_module.session_manager,
        )

    wait_for_map.assert_awaited_once_with("@44", timeout=15.0)
    kill_window.assert_not_awaited()
    assert window_state.session_id == "resume-123"
    assert window_state.cwd == "/repo"
    assert window_state.window_name == "repo"
    save_state.assert_called_once()
    bind_thread.assert_called_once_with(1, 10, "@44", window_name="repo")
    safe_edit.assert_awaited_once()
    assert "Resumed." in safe_edit.await_args.args[1]


class _DownloadedFile:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def download_to_drive(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.payload)


def _make_photo_update(
    *,
    thread_id: int = 99,
    caption: str = "new caption",
    media_group_id: str | None = None,
    reply_text: str | None = None,
    file_unique_id: str = "new-photo",
) -> MagicMock:
    photo = MagicMock()
    photo.file_unique_id = file_unique_id
    photo.get_file = AsyncMock(return_value=_DownloadedFile(b"new photo"))

    message = MagicMock()
    message.photo = [photo]
    message.document = None
    message.caption = caption
    message.media_group_id = media_group_id
    message.message_thread_id = thread_id
    message.message_id = 123
    message.chat = MagicMock()
    message.chat.id = -100123
    message.chat.type = "supergroup"
    message.chat.send_action = AsyncMock()
    message.quote = None
    if reply_text is None:
        message.reply_to_message = None
    else:
        original = MagicMock()
        original.message_id = 42
        original.text = reply_text
        original.caption = None
        message.reply_to_message = original

    update = MagicMock()
    update.message = message
    update.callback_query = None
    update.effective_user = MagicMock()
    update.effective_user.id = 1
    update.effective_chat = message.chat
    return update


def _make_document_update(
    *,
    thread_id: int = 99,
    caption: str = "new caption",
    media_group_id: str | None = None,
    reply_text: str | None = None,
    file_unique_id: str = "new-doc",
) -> MagicMock:
    document = MagicMock()
    document.file_unique_id = file_unique_id
    document.file_name = "report.txt"
    document.file_size = 11
    document.get_file = AsyncMock(return_value=_DownloadedFile(b"new doc"))

    message = MagicMock()
    message.photo = None
    message.document = document
    message.caption = caption
    message.media_group_id = media_group_id
    message.message_thread_id = thread_id
    message.message_id = 124
    message.chat = MagicMock()
    message.chat.id = -100123
    message.chat.type = "supergroup"
    message.chat.send_action = AsyncMock()
    message.quote = None
    if reply_text is None:
        message.reply_to_message = None
    else:
        original = MagicMock()
        original.message_id = 42
        original.text = reply_text
        original.caption = None
        message.reply_to_message = original

    update = MagicMock()
    update.message = message
    update.callback_query = None
    update.effective_user = MagicMock()
    update.effective_user.id = 1
    update.effective_chat = message.chat
    return update


def _make_text_update(*, thread_id: int = 99, text: str = "topic b text") -> MagicMock:
    message = MagicMock()
    message.text = text
    message.photo = None
    message.document = None
    message.message_thread_id = thread_id
    message.message_id = 122
    message.chat = MagicMock()
    message.chat.id = -100123
    message.chat.type = "supergroup"

    update = MagicMock()
    update.message = message
    update.callback_query = None
    update.effective_user = MagicMock()
    update.effective_user.id = 1
    update.effective_chat = message.chat
    return update


def _cross_topic_picker_user_data(
    stale_file: Path, *, stale_state: str, thread_id: int = 10
) -> dict[str, object]:
    user_data = _pending_user_data(stale_file, thread_id=thread_id)
    user_data[STATE_KEY] = stale_state
    if stale_state == STATE_BROWSING_DIRECTORY:
        user_data[BROWSE_PATH_KEY] = "/old/topic-a"
        user_data[BROWSE_PAGE_KEY] = 7
        user_data[BROWSE_DIRS_KEY] = ["old-dir"]
        user_data[BROWSE_UNBOUND_COUNT_KEY] = 3
    elif stale_state == STATE_SELECTING_WINDOW:
        user_data[UNBOUND_WINDOWS_KEY] = ["old-window"]
    elif stale_state == STATE_SELECTING_SESSION:
        user_data[SESSIONS_KEY] = ["old-session"]
    return user_data


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "callback_data", [CB_DIR_CANCEL, CB_SESSION_CANCEL, CB_WIN_CANCEL]
)
async def test_picker_cancel_clears_pending_attachments_and_deletes_file(
    tmp_path: Path, callback_data: str
):
    payload = tmp_path / "pending.bin"
    payload.write_bytes(b"data")
    context = MagicMock()
    context.user_data = _pending_user_data(payload, thread_id=10)
    if callback_data == CB_SESSION_CANCEL:
        context.user_data[STATE_KEY] = STATE_SELECTING_SESSION
        context.user_data[SESSIONS_KEY] = []
    elif callback_data == CB_WIN_CANCEL:
        context.user_data[STATE_KEY] = STATE_SELECTING_WINDOW
        context.user_data[UNBOUND_WINDOWS_KEY] = []
    update = _make_callback_update(callback_data, thread_id=10)

    with (
        _patch_both("is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        _patch_both("safe_edit", new_callable=AsyncMock) as mock_safe_edit,
    ):
        await bot_module.callback_handler(update, context)

    assert not payload.exists()
    assert "_pending_thread_attachments" not in context.user_data
    assert "_pending_thread_text" not in context.user_data
    assert "_pending_thread_id" not in context.user_data
    mock_safe_edit.assert_awaited_once()
    update.callback_query.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_session_picker_mismatch_preserves_pending_attachments(
    tmp_path: Path,
):
    payload = tmp_path / "stale.bin"
    payload.write_bytes(b"data")
    context = MagicMock()
    context.user_data = _pending_user_data(payload, thread_id=10)
    context.user_data[STATE_KEY] = STATE_SELECTING_SESSION
    update = _make_callback_update(CB_SESSION_NEW, thread_id=99)

    with (
        _patch_both("is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        _patch_both("_create_and_bind_window", new_callable=AsyncMock) as mock_create,
    ):
        await bot_module.callback_handler(update, context)

    assert payload.exists()
    assert context.user_data["_pending_thread_attachments"] == [_attachment(payload)]
    assert context.user_data["_pending_thread_text"] == "hello"
    assert context.user_data["_pending_thread_id"] == 10
    mock_create.assert_not_called()
    update.callback_query.answer.assert_awaited_once_with(
        "Stale picker (topic mismatch)", show_alert=True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "callback_data",
    [
        f"{CB_DIR_SELECT}0",
        CB_DIR_UP,
        f"{CB_DIR_PAGE}1",
        CB_DIR_CANCEL,
        CB_DIR_BIND_EXISTING,
    ],
)
async def test_stale_directory_browser_callbacks_preserve_pending_attachments(
    tmp_path: Path, callback_data: str
):
    payload = tmp_path / "stale-dir.bin"
    payload.write_bytes(b"data")
    context = MagicMock()
    context.user_data = _pending_user_data(payload, thread_id=10)
    update = _make_callback_update(callback_data, thread_id=99)

    with (
        _patch_both("is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        _patch_both("safe_edit", new_callable=AsyncMock) as mock_safe_edit,
    ):
        await bot_module.callback_handler(update, context)

    assert payload.exists()
    assert context.user_data["_pending_thread_attachments"] == [_attachment(payload)]
    assert context.user_data["_pending_thread_text"] == "hello"
    assert context.user_data["_pending_thread_id"] == 10
    mock_safe_edit.assert_not_called()
    update.callback_query.answer.assert_awaited_once_with(
        "Stale browser (topic mismatch)", show_alert=True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stale_state",
    [
        STATE_BROWSING_DIRECTORY,
        STATE_SELECTING_WINDOW,
        STATE_SELECTING_SESSION,
    ],
)
async def test_photo_from_new_topic_clears_cross_topic_picker_state_and_opens_picker(
    tmp_path: Path, stale_state: str
):
    stale_file = tmp_path / "topic-a-photo.bin"
    stale_file.write_bytes(b"stale")
    context = MagicMock()
    context.user_data = _cross_topic_picker_user_data(
        stale_file, stale_state=stale_state, thread_id=10
    )
    update = _make_photo_update(thread_id=99)
    media_dir = tmp_path / "images"

    with (
        _patch_both("_IMAGES_DIR", media_dir),
        _patch_both("is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        patch.object(
            bot_module.session_manager, "get_window_for_thread", return_value=None
        ),
        _patch_both("_list_unbound_windows", new_callable=AsyncMock, return_value=[]),
        _patch_both(
            "build_directory_browser",
            return_value=("picker", MagicMock(), ["new-dir"]),
        ) as mock_build_picker,
        _patch_both("safe_reply", new_callable=AsyncMock) as mock_reply,
    ):
        await bot_module.photo_handler(update, context)

    assert not stale_file.exists()
    assert context.user_data["_pending_thread_id"] == 99
    assert context.user_data[STATE_KEY] == STATE_BROWSING_DIRECTORY
    assert context.user_data[BROWSE_PATH_KEY] == str(bot_module.config.browse_root)
    assert context.user_data[BROWSE_PAGE_KEY] == 0
    assert context.user_data[BROWSE_DIRS_KEY] == ["new-dir"]
    assert context.user_data[BROWSE_UNBOUND_COUNT_KEY] == 0
    assert "_pending_thread_text" not in context.user_data
    assert "_selected_path" not in context.user_data
    assert UNBOUND_WINDOWS_KEY not in context.user_data
    assert SESSIONS_KEY not in context.user_data
    pending_attachments = context.user_data["_pending_thread_attachments"]
    assert len(pending_attachments) == 1
    pending_path = Path(pending_attachments[0].path)
    assert pending_path.parent == media_dir
    assert pending_path.exists()
    assert pending_attachments[0].caption == "new caption"
    mock_build_picker.assert_called_once_with(
        str(bot_module.config.browse_root), unbound_count=0
    )
    mock_reply.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stale_state",
    [
        STATE_BROWSING_DIRECTORY,
        STATE_SELECTING_WINDOW,
        STATE_SELECTING_SESSION,
    ],
)
async def test_document_from_new_topic_clears_cross_topic_picker_state_and_opens_picker(
    tmp_path: Path, stale_state: str
):
    stale_file = tmp_path / "topic-a-doc.bin"
    stale_file.write_bytes(b"stale")
    context = MagicMock()
    context.user_data = _cross_topic_picker_user_data(
        stale_file, stale_state=stale_state, thread_id=10
    )
    update = _make_document_update(thread_id=99)
    media_dir = tmp_path / "files"

    with (
        _patch_both("_FILES_DIR", media_dir),
        _patch_both("is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        patch.object(
            bot_module.session_manager, "get_window_for_thread", return_value=None
        ),
        _patch_both("_list_unbound_windows", new_callable=AsyncMock, return_value=[]),
        _patch_both(
            "build_directory_browser",
            return_value=("picker", MagicMock(), ["new-dir"]),
        ) as mock_build_picker,
        _patch_both("safe_reply", new_callable=AsyncMock) as mock_reply,
    ):
        await bot_module.document_handler(update, context)

    assert not stale_file.exists()
    assert context.user_data["_pending_thread_id"] == 99
    assert context.user_data[STATE_KEY] == STATE_BROWSING_DIRECTORY
    assert context.user_data[BROWSE_PATH_KEY] == str(bot_module.config.browse_root)
    assert context.user_data[BROWSE_PAGE_KEY] == 0
    assert context.user_data[BROWSE_DIRS_KEY] == ["new-dir"]
    assert context.user_data[BROWSE_UNBOUND_COUNT_KEY] == 0
    assert "_pending_thread_text" not in context.user_data
    assert "_selected_path" not in context.user_data
    assert UNBOUND_WINDOWS_KEY not in context.user_data
    assert SESSIONS_KEY not in context.user_data
    pending_attachments = context.user_data["_pending_thread_attachments"]
    assert len(pending_attachments) == 1
    pending_path = Path(pending_attachments[0].path)
    assert pending_path.parent == media_dir
    assert pending_path.exists()
    assert pending_attachments[0].caption == "new caption"
    mock_build_picker.assert_called_once_with(
        str(bot_module.config.browse_root), unbound_count=0
    )
    mock_reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_unbound_photo_caption_reply_context_is_stashed_rendered(
    tmp_path: Path,
):
    context = MagicMock()
    context.user_data = {}
    update = _make_photo_update(
        caption="please apply this",
        reply_text="prior assistant guidance",
    )
    media_dir = tmp_path / "images"

    with (
        _patch_both("_IMAGES_DIR", media_dir),
        patch.object(bot_module.config, "reply_context_enabled", True),
        _patch_both("is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        patch.object(
            bot_module.session_manager, "get_window_for_thread", return_value=None
        ),
        patch.object(
            bot_module.session_manager, "resolve_window_for_thread", return_value=None
        ),
        patch.object(
            bot_module.reply_context_mod, "resolve", new_callable=AsyncMock
        ) as mock_resolve,
        _patch_both("_list_unbound_windows", new_callable=AsyncMock, return_value=[]),
        _patch_both(
            "build_directory_browser",
            return_value=("picker", MagicMock(), []),
        ),
        _patch_both("safe_reply", new_callable=AsyncMock),
    ):
        mock_resolve.side_effect = lambda reply_ctx, _chat_id: reply_ctx
        await bot_module.photo_handler(update, context)

    pending_attachments = context.user_data["_pending_thread_attachments"]
    assert len(pending_attachments) == 1
    caption = pending_attachments[0].caption
    assert "[Telegram reply context]" in caption
    assert "prior assistant guidance" in caption
    assert "Telegram message id: 42" in caption
    assert "[User message]\nplease apply this" in caption
    mock_resolve.assert_awaited_once()


@pytest.mark.asyncio
async def test_unbound_document_caption_reply_context_is_stashed_rendered(
    tmp_path: Path,
):
    context = MagicMock()
    context.user_data = {}
    update = _make_document_update(
        caption="please apply this to the file",
        reply_text="prior document guidance",
    )
    media_dir = tmp_path / "files"

    with (
        _patch_both("_FILES_DIR", media_dir),
        patch.object(bot_module.config, "reply_context_enabled", True),
        _patch_both("is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        patch.object(
            bot_module.session_manager, "get_window_for_thread", return_value=None
        ),
        patch.object(
            bot_module.session_manager, "resolve_window_for_thread", return_value=None
        ),
        patch.object(
            bot_module.reply_context_mod, "resolve", new_callable=AsyncMock
        ) as mock_resolve,
        _patch_both("_list_unbound_windows", new_callable=AsyncMock, return_value=[]),
        _patch_both(
            "build_directory_browser",
            return_value=("picker", MagicMock(), []),
        ),
        _patch_both("safe_reply", new_callable=AsyncMock),
    ):
        mock_resolve.side_effect = lambda reply_ctx, _chat_id: reply_ctx
        await bot_module.document_handler(update, context)

    pending_attachments = context.user_data["_pending_thread_attachments"]
    assert len(pending_attachments) == 1
    caption = pending_attachments[0].caption
    assert "[Telegram reply context]" in caption
    assert "prior document guidance" in caption
    assert "Telegram message id: 42" in caption
    assert "[User message]\nplease apply this to the file" in caption
    mock_resolve.assert_awaited_once()


@pytest.mark.asyncio
async def test_unbound_photo_media_group_caption_guard_avoids_duplicate_context(
    tmp_path: Path,
):
    context = MagicMock()
    context.user_data = {}
    first = _make_photo_update(
        caption="album caption",
        media_group_id="album-1",
        reply_text="quoted album context",
        file_unique_id="album-photo-1",
    )
    second = _make_photo_update(
        caption="",
        media_group_id="album-1",
        reply_text="quoted album context",
        file_unique_id="album-photo-2",
    )
    media_dir = tmp_path / "images"

    with (
        _patch_both("_IMAGES_DIR", media_dir),
        patch.object(bot_module.config, "reply_context_enabled", True),
        _patch_both("is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        patch.object(
            bot_module.session_manager, "get_window_for_thread", return_value=None
        ),
        patch.object(
            bot_module.session_manager, "resolve_window_for_thread", return_value=None
        ),
        patch.object(
            bot_module.reply_context_mod, "resolve", new_callable=AsyncMock
        ) as mock_resolve,
        _patch_both("_list_unbound_windows", new_callable=AsyncMock, return_value=[]),
        _patch_both(
            "build_directory_browser",
            return_value=("picker", MagicMock(), []),
        ),
        _patch_both("safe_reply", new_callable=AsyncMock),
    ):
        mock_resolve.side_effect = lambda reply_ctx, _chat_id: reply_ctx
        await bot_module.photo_handler(first, context)
        await bot_module.photo_handler(second, context)

    captions = [
        attachment.caption
        for attachment in context.user_data["_pending_thread_attachments"]
    ]
    assert len(captions) == 2
    assert captions[0].count("[Telegram reply context]") == 1
    assert "[User message]\nalbum caption" in captions[0]
    assert captions[1] == ""
    mock_resolve.assert_awaited_once()


@pytest.mark.asyncio
async def test_unbound_document_media_group_caption_guard_avoids_duplicate_context(
    tmp_path: Path,
):
    context = MagicMock()
    context.user_data = {}
    first = _make_document_update(
        caption="album caption",
        media_group_id="album-1",
        reply_text="quoted album context",
        file_unique_id="album-doc-1",
    )
    second = _make_document_update(
        caption="",
        media_group_id="album-1",
        reply_text="quoted album context",
        file_unique_id="album-doc-2",
    )
    media_dir = tmp_path / "files"

    with (
        _patch_both("_FILES_DIR", media_dir),
        patch.object(bot_module.config, "reply_context_enabled", True),
        _patch_both("is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        patch.object(
            bot_module.session_manager, "get_window_for_thread", return_value=None
        ),
        patch.object(
            bot_module.session_manager, "resolve_window_for_thread", return_value=None
        ),
        patch.object(
            bot_module.reply_context_mod, "resolve", new_callable=AsyncMock
        ) as mock_resolve,
        _patch_both("_list_unbound_windows", new_callable=AsyncMock, return_value=[]),
        _patch_both(
            "build_directory_browser",
            return_value=("picker", MagicMock(), []),
        ),
        _patch_both("safe_reply", new_callable=AsyncMock),
    ):
        mock_resolve.side_effect = lambda reply_ctx, _chat_id: reply_ctx
        await bot_module.document_handler(first, context)
        await bot_module.document_handler(second, context)

    captions = [
        attachment.caption
        for attachment in context.user_data["_pending_thread_attachments"]
    ]
    assert len(captions) == 2
    assert captions[0].count("[Telegram reply context]") == 1
    assert "[User message]\nalbum caption" in captions[0]
    assert captions[1] == ""
    mock_resolve.assert_awaited_once()


@pytest.mark.asyncio
async def test_bound_photo_caption_still_uses_apply_reply_context(
    tmp_path: Path,
):
    context = MagicMock()
    context.user_data = {}
    update = _make_photo_update(caption="bound caption")
    media_dir = tmp_path / "images"
    window = MagicMock()

    with (
        _patch_both("_IMAGES_DIR", media_dir),
        _patch_both("is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        patch.object(
            bot_module.session_manager, "get_window_for_thread", return_value="@0"
        ),
        patch.object(
            bot_module.tmux_manager,
            "find_window_by_id",
            new_callable=AsyncMock,
            return_value=window,
        ),
        _patch_both("clear_status_msg_info"),
        _patch_both("set_route_last_user_message"),
        _patch_both("_apply_reply_context", new_callable=AsyncMock) as mock_apply,
        _patch_both("aggregator_offer_photo", new_callable=AsyncMock) as mock_offer,
        _patch_both("safe_reply", new_callable=AsyncMock),
    ):
        mock_apply.return_value = "rendered bound caption"
        await bot_module.photo_handler(update, context)

    mock_apply.assert_awaited_once_with(update.message, 1, 99, "bound caption")
    mock_offer.assert_awaited_once()
    assert mock_offer.await_args.args[0] == (1, 99, "@0")
    assert mock_offer.await_args.args[2] == "rendered bound caption"


@pytest.mark.asyncio
async def test_bound_document_caption_still_uses_apply_reply_context(
    tmp_path: Path,
):
    context = MagicMock()
    context.user_data = {}
    update = _make_document_update(caption="bound caption")
    media_dir = tmp_path / "files"
    window = MagicMock()

    with (
        _patch_both("_FILES_DIR", media_dir),
        _patch_both("is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        patch.object(
            bot_module.session_manager, "get_window_for_thread", return_value="@0"
        ),
        patch.object(
            bot_module.tmux_manager,
            "find_window_by_id",
            new_callable=AsyncMock,
            return_value=window,
        ),
        _patch_both("clear_status_msg_info"),
        _patch_both("set_route_last_user_message"),
        _patch_both("_apply_reply_context", new_callable=AsyncMock) as mock_apply,
        _patch_both("aggregator_offer_document", new_callable=AsyncMock) as mock_offer,
        _patch_both("safe_reply", new_callable=AsyncMock),
    ):
        mock_apply.return_value = "rendered bound caption"
        await bot_module.document_handler(update, context)

    mock_apply.assert_awaited_once_with(update.message, 1, 99, "bound caption")
    mock_offer.assert_awaited_once()
    assert mock_offer.await_args.args[0] == (1, 99, "@0")
    assert mock_offer.await_args.args[2] == "rendered bound caption"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("callback_data", "expected_answer"),
    [
        (f"{CB_DIR_SELECT}0", "Stale browser (topic mismatch)"),
        (CB_DIR_UP, "Stale browser (topic mismatch)"),
        (f"{CB_DIR_PAGE}1", "Stale browser (topic mismatch)"),
        (CB_DIR_CANCEL, "Stale browser (topic mismatch)"),
        (CB_DIR_BIND_EXISTING, "Stale browser (topic mismatch)"),
        (f"{CB_SESSION_SELECT}0", "Stale picker (topic mismatch)"),
        (CB_SESSION_NEW, "Stale picker (topic mismatch)"),
        (CB_SESSION_CANCEL, "Stale picker (topic mismatch)"),
        (f"{CB_WIN_BIND}0", "Stale picker (topic mismatch)"),
        (CB_WIN_NEW, "Stale picker (topic mismatch)"),
        (CB_WIN_CANCEL, "Stale picker (topic mismatch)"),
    ],
)
async def test_replaced_topic_stale_callback_does_not_clear_new_photo_payload(
    tmp_path: Path, callback_data: str, expected_answer: str
):
    stale_file = tmp_path / "topic-a-photo.bin"
    stale_file.write_bytes(b"stale")
    context = MagicMock()
    context.user_data = _cross_topic_picker_user_data(
        stale_file, stale_state=STATE_BROWSING_DIRECTORY, thread_id=10
    )
    media_dir = tmp_path / "images"

    with (
        _patch_both("_IMAGES_DIR", media_dir),
        _patch_both("is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        patch.object(
            bot_module.session_manager, "get_window_for_thread", return_value=None
        ),
        _patch_both("_list_unbound_windows", new_callable=AsyncMock, return_value=[]),
        _patch_both(
            "build_directory_browser",
            return_value=("picker", MagicMock(), ["new-dir"]),
        ),
        _patch_both("safe_reply", new_callable=AsyncMock),
    ):
        await bot_module.photo_handler(_make_photo_update(thread_id=99), context)

    assert not stale_file.exists()
    pending_attachments = context.user_data["_pending_thread_attachments"]
    assert len(pending_attachments) == 1
    pending_path = Path(pending_attachments[0].path)
    assert pending_path.exists()

    stale_callback = _make_callback_update(callback_data, thread_id=10)
    with (
        _patch_both("is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        _patch_both("safe_reply", new_callable=AsyncMock) as mock_reply,
        _patch_both("safe_edit", new_callable=AsyncMock) as mock_edit,
        _patch_both("_create_and_bind_window", new_callable=AsyncMock) as mock_create,
    ):
        await bot_module.callback_handler(stale_callback, context)

    stale_callback.callback_query.answer.assert_awaited_once_with(
        expected_answer, show_alert=True
    )
    mock_reply.assert_not_called()
    mock_edit.assert_not_called()
    mock_create.assert_not_called()
    assert context.user_data["_pending_thread_id"] == 99
    assert context.user_data["_pending_thread_attachments"] == pending_attachments
    assert pending_path.exists()


@pytest.mark.asyncio
async def test_text_replaced_topic_stale_cancel_does_not_clear_new_text_photo_payload(
    tmp_path: Path,
):
    stale_file = tmp_path / "topic-a-pending.bin"
    stale_file.write_bytes(b"stale")
    context = MagicMock()
    context.user_data = _cross_topic_picker_user_data(
        stale_file, stale_state=STATE_BROWSING_DIRECTORY, thread_id=10
    )
    media_dir = tmp_path / "images"

    with (
        _patch_both("_IMAGES_DIR", media_dir),
        _patch_both("is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        patch.object(
            bot_module.session_manager, "get_window_for_thread", return_value=None
        ),
        _patch_both("_apply_reply_context", new_callable=AsyncMock) as apply_reply,
        _patch_both("_list_unbound_windows", new_callable=AsyncMock, return_value=[]),
        _patch_both(
            "build_directory_browser",
            return_value=("picker", MagicMock(), ["new-dir"]),
        ),
        _patch_both("safe_reply", new_callable=AsyncMock),
    ):
        apply_reply.side_effect = lambda _message, _user_id, _thread_id, text: text
        await bot_module.text_handler(
            _make_text_update(thread_id=99, text="topic b text"), context
        )
        await bot_module.photo_handler(_make_photo_update(thread_id=99), context)

    assert not stale_file.exists()
    assert context.user_data["_pending_thread_id"] == 99
    assert context.user_data["_pending_thread_text"] == "topic b text"
    assert context.user_data["_ignored_stale_thread_ids"] == [10]
    pending_attachments = context.user_data["_pending_thread_attachments"]
    assert len(pending_attachments) == 1
    pending_path = Path(pending_attachments[0].path)
    assert pending_path.parent == media_dir
    assert pending_path.exists()

    stale_callback = _make_callback_update(CB_DIR_CANCEL, thread_id=10)
    with (
        _patch_both("is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        patch.object(bot_module.session_manager, "bind_thread") as mock_bind,
        _patch_both("safe_reply", new_callable=AsyncMock) as mock_reply,
        _patch_both("safe_edit", new_callable=AsyncMock) as mock_edit,
        _patch_both("_create_and_bind_window", new_callable=AsyncMock) as mock_create,
    ):
        await bot_module.callback_handler(stale_callback, context)

    stale_callback.callback_query.answer.assert_awaited_once_with(
        "Stale browser (topic mismatch)", show_alert=True
    )
    mock_reply.assert_not_called()
    mock_edit.assert_not_called()
    mock_create.assert_not_called()
    mock_bind.assert_not_called()
    assert context.user_data["_pending_thread_id"] == 99
    assert context.user_data["_pending_thread_text"] == "topic b text"
    assert context.user_data["_pending_thread_attachments"] == pending_attachments
    assert pending_path.exists()


@pytest.mark.asyncio
async def test_dir_confirm_without_pending_owner_rejects_without_create_or_bind(
    tmp_path: Path,
):
    context = MagicMock()
    context.user_data = {
        STATE_KEY: STATE_BROWSING_DIRECTORY,
        BROWSE_PATH_KEY: str(tmp_path),
        BROWSE_DIRS_KEY: [],
        BROWSE_UNBOUND_COUNT_KEY: 0,
    }
    update = _make_callback_update(CB_DIR_CONFIRM, thread_id=10)

    with (
        _patch_both("is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        patch.object(
            bot_module.session_manager,
            "list_sessions_for_directory",
            new_callable=AsyncMock,
        ) as mock_list_sessions,
        patch.object(bot_module.session_manager, "bind_thread") as mock_bind,
        patch.object(
            bot_module.tmux_manager, "create_window", new_callable=AsyncMock
        ) as mock_create_window,
        _patch_both(
            "_create_and_bind_window", new_callable=AsyncMock
        ) as mock_create_and_bind,
    ):
        await bot_module.callback_handler(update, context)

    update.callback_query.answer.assert_awaited_once_with(
        "Stale browser (topic mismatch)", show_alert=True
    )
    mock_list_sessions.assert_not_called()
    mock_create_and_bind.assert_not_called()
    mock_create_window.assert_not_called()
    mock_bind.assert_not_called()


@pytest.mark.asyncio
async def test_session_new_without_pending_owner_does_not_recover_from_topic(
    tmp_path: Path,
):
    context = MagicMock()
    context.user_data = {
        STATE_KEY: STATE_SELECTING_SESSION,
        "_selected_path": str(tmp_path),
    }
    update = _make_callback_update(CB_SESSION_NEW, thread_id=10)

    with (
        _patch_both("is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        patch.object(bot_module.session_manager, "bind_thread") as mock_bind,
        patch.object(
            bot_module.tmux_manager, "create_window", new_callable=AsyncMock
        ) as mock_create_window,
        _patch_both(
            "_create_and_bind_window", new_callable=AsyncMock
        ) as mock_create_and_bind,
    ):
        await bot_module.callback_handler(update, context)

    update.callback_query.answer.assert_awaited_once_with(
        "Stale picker (topic mismatch)", show_alert=True
    )
    mock_create_and_bind.assert_not_called()
    mock_create_window.assert_not_called()
    mock_bind.assert_not_called()


@pytest.mark.asyncio
async def test_session_select_without_pending_owner_rejects(tmp_path: Path):
    context = MagicMock()
    context.user_data = {
        STATE_KEY: STATE_SELECTING_SESSION,
        SESSIONS_KEY: [ClaudeSession("sid", "summary", 1, str(tmp_path / "s.jsonl"))],
        "_selected_path": str(tmp_path),
    }
    update = _make_callback_update(f"{CB_SESSION_SELECT}0", thread_id=10)

    with (
        _patch_both("is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        patch.object(bot_module.session_manager, "bind_thread") as mock_bind,
        _patch_both("_create_and_bind_window", new_callable=AsyncMock) as mock_create,
    ):
        await bot_module.callback_handler(update, context)

    update.callback_query.answer.assert_awaited_once_with(
        "Stale picker (topic mismatch)", show_alert=True
    )
    mock_create.assert_not_called()
    mock_bind.assert_not_called()


@pytest.mark.asyncio
async def test_session_new_double_click_after_pending_owner_cleared_is_rejected(
    tmp_path: Path,
):
    context = MagicMock()
    context.user_data = {
        STATE_KEY: STATE_SELECTING_SESSION,
        "_pending_thread_id": 10,
        "_selected_path": str(tmp_path),
    }
    first_update = _make_callback_update(CB_SESSION_NEW, thread_id=10)
    second_update = _make_callback_update(CB_SESSION_NEW, thread_id=10)

    async def clear_pending_after_first_click(
        *_args: object, **_kwargs: object
    ) -> None:
        context.user_data.clear()

    with (
        _patch_both("is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        _patch_both("_create_and_bind_window", new_callable=AsyncMock) as mock_create,
    ):
        mock_create.side_effect = clear_pending_after_first_click
        await bot_module.callback_handler(first_update, context)
        await bot_module.callback_handler(second_update, context)

    mock_create.assert_awaited_once()
    second_update.callback_query.answer.assert_awaited_once_with(
        "Stale picker (topic mismatch)", show_alert=True
    )


@pytest.mark.asyncio
async def test_cancelled_pending_media_is_not_forwarded_on_later_bind(tmp_path: Path):
    payload = tmp_path / "cancel-then-bind.bin"
    payload.write_bytes(b"data")
    context = MagicMock()
    context.user_data = _pending_user_data(payload, thread_id=10)
    cancel_update = _make_callback_update(CB_DIR_CANCEL, thread_id=10)

    with (
        _patch_both("is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        _patch_both("safe_edit", new_callable=AsyncMock),
    ):
        await bot_module.callback_handler(cancel_update, context)

    assert not payload.exists()
    assert "_pending_thread_attachments" not in context.user_data

    context.user_data[STATE_KEY] = STATE_SELECTING_WINDOW
    context.user_data[UNBOUND_WINDOWS_KEY] = ["window-1"]
    bind_update = _make_callback_update(f"{CB_WIN_BIND}0", thread_id=10)
    window = MagicMock()
    window.window_id = "window-1"
    window.window_name = "window one"

    with (
        _patch_both("is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        patch.object(bot_module.session_manager, "bind_thread") as mock_bind,
        patch.object(
            bot_module.tmux_manager,
            "find_window_by_id",
            new_callable=AsyncMock,
            return_value=window,
        ) as mock_find,
        _patch_both(
            "_list_unbound_windows",
            new_callable=AsyncMock,
            return_value=[("window-1", "window one", "/tmp")],
        ) as mock_list_unbound,
        _patch_both("safe_edit", new_callable=AsyncMock) as mock_edit,
        _patch_both("aggregator_replay_payload", new_callable=AsyncMock) as mock_replay,
    ):
        await bot_module.callback_handler(bind_update, context)

    mock_bind.assert_not_called()
    mock_find.assert_not_called()
    mock_list_unbound.assert_not_called()
    mock_edit.assert_not_called()
    mock_replay.assert_not_called()
    bind_update.callback_query.answer.assert_awaited_once_with(
        "Stale picker (topic mismatch)", show_alert=True
    )


def _make_create_query() -> MagicMock:
    query = MagicMock(spec=CallbackQuery)
    query.answer = AsyncMock()
    return query


def _make_user() -> MagicMock:
    user = MagicMock(spec=User)
    user.id = 1
    return user


@pytest.mark.asyncio
async def test_create_and_bind_owner_replaced_after_await_does_not_flush_new_payload(
    tmp_path: Path,
):
    old_payload = tmp_path / "topic-10.bin"
    old_payload.write_bytes(b"old")
    new_payload = tmp_path / "topic-99.bin"
    new_payload.write_bytes(b"new")
    context = MagicMock()
    context.user_data = _pending_user_data(old_payload, thread_id=10)
    query = _make_create_query()
    user = _make_user()

    async def replace_owner_during_hook_wait(*_args: object, **_kwargs: object) -> bool:
        context.user_data = _pending_user_data(new_payload, thread_id=99)
        context.user_data["_pending_thread_text"] = "topic 99 text"
        return True

    with (
        patch.object(bot_module, "session_monitor", None),
        patch.object(
            bot_module.tmux_manager,
            "create_window",
            new_callable=AsyncMock,
            return_value=(True, "Window created", "created-window", "@10"),
        ),
        patch.object(
            bot_module.session_manager,
            "wait_for_session_map_entry",
            new_callable=AsyncMock,
            side_effect=replace_owner_during_hook_wait,
        ),
        patch.object(
            bot_module.tmux_manager,
            "kill_window",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_kill,
        patch.object(
            bot_module.session_manager, "get_window_state"
        ) as get_window_state,
        patch.object(bot_module.session_manager, "bind_thread") as mock_bind,
        _patch_both("safe_edit", new_callable=AsyncMock) as mock_edit,
        _patch_both("aggregator_replay_payload", new_callable=AsyncMock) as mock_replay,
    ):
        await bot_module._create_and_bind_window(
            query,
            context,
            user,
            str(tmp_path),
            pending_thread_id=10,
            tmux_mgr=bot_module.tmux_manager,
            session_mgr=bot_module.session_manager,
        )

    mock_kill.assert_awaited_once_with("@10")
    get_window_state.assert_not_called()
    mock_bind.assert_not_called()
    mock_replay.assert_not_called()
    edit_text = mock_edit.await_args.args[1]
    assert "stale" in edit_text
    assert "newly-created tmux window was cleaned up" in edit_text
    query.answer.assert_awaited_once_with("Stale picker", show_alert=False)
    assert context.user_data["_pending_thread_id"] == 99
    assert context.user_data["_pending_thread_text"] == "topic 99 text"
    assert context.user_data["_pending_thread_attachments"] == [
        _attachment(new_payload)
    ]
    assert new_payload.exists()


@pytest.mark.asyncio
async def test_existing_window_bind_owner_replaced_after_await_does_not_bind_or_flush(
    tmp_path: Path,
):
    old_payload = tmp_path / "topic-10-existing.bin"
    old_payload.write_bytes(b"old")
    new_payload = tmp_path / "topic-99-existing.bin"
    new_payload.write_bytes(b"new")
    context = MagicMock()
    context.user_data = _pending_user_data(old_payload, thread_id=10)
    context.user_data[STATE_KEY] = STATE_SELECTING_WINDOW
    context.user_data[UNBOUND_WINDOWS_KEY] = ["@0"]
    update = _make_callback_update(f"{CB_WIN_BIND}0", thread_id=10)
    window = MagicMock()
    window.window_id = "@0"
    window.window_name = "existing-window"

    async def replace_owner_during_unbound_list(
        tmux_mgr: object, session_mgr: object
    ) -> list[tuple[str, str, str]]:
        context.user_data = _pending_user_data(new_payload, thread_id=99)
        context.user_data["_pending_thread_text"] = "topic 99 text"
        return [("@0", "existing-window", str(tmp_path))]

    with (
        _patch_both("is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        patch.object(bot_module.session_manager, "bind_thread") as mock_bind,
        patch.object(
            bot_module.tmux_manager,
            "find_window_by_id",
            new_callable=AsyncMock,
            return_value=window,
        ),
        _patch_both(
            "_list_unbound_windows",
            new_callable=AsyncMock,
            side_effect=replace_owner_during_unbound_list,
        ),
        _patch_both("safe_edit", new_callable=AsyncMock) as mock_edit,
        _patch_both("aggregator_replay_payload", new_callable=AsyncMock) as mock_replay,
    ):
        await bot_module.callback_handler(update, context)

    mock_bind.assert_not_called()
    mock_replay.assert_not_called()
    mock_edit.assert_not_called()
    update.callback_query.answer.assert_awaited_once_with(
        "Stale picker (topic mismatch)", show_alert=True
    )
    assert context.user_data["_pending_thread_id"] == 99
    assert context.user_data["_pending_thread_text"] == "topic 99 text"
    assert context.user_data["_pending_thread_attachments"] == [
        _attachment(new_payload)
    ]
    assert new_payload.exists()


@pytest.mark.asyncio
async def test_flush_pending_route_payload_owner_mismatch_preserves_new_payload(
    tmp_path: Path,
):
    payload = tmp_path / "topic-99-flush.bin"
    payload.write_bytes(b"new")
    user_data = _pending_user_data(payload, thread_id=99)

    with (
        _patch_both("aggregator_replay_payload", new_callable=AsyncMock) as mock_replay,
        _patch_both("aggregator_clear_route") as mock_clear_route,
    ):
        delivered = await bot_module._flush_pending_route_payload(
            (1, 10, "@old"), user_data
        )

    assert delivered is None
    mock_replay.assert_not_called()
    mock_clear_route.assert_not_called()
    assert user_data["_pending_thread_id"] == 99
    assert user_data["_pending_thread_text"] == "hello"
    assert user_data["_pending_thread_attachments"] == [_attachment(payload)]
    assert payload.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "flush_failure",
    [False, RuntimeError("tmux send exploded")],
    ids=["flush-false", "flush-exception"],
)
async def test_create_and_bind_window_pending_flush_failure_is_explicit_and_cleans_up(
    tmp_path: Path, flush_failure: object
):
    payload = tmp_path / "create-flush-fail.bin"
    payload.write_bytes(b"data")
    context = MagicMock()
    context.user_data = _pending_user_data(payload, thread_id=10)
    query = _make_create_query()
    user = _make_user()
    window_state = MagicMock()
    window_state.session_id = "sid"
    window_state.cwd = str(tmp_path)

    with (
        patch.object(bot_module, "session_monitor", None),
        patch.object(
            bot_module.tmux_manager,
            "create_window",
            new_callable=AsyncMock,
            return_value=(True, "Window created", "created-window", "@0"),
        ),
        patch.object(
            bot_module.session_manager,
            "wait_for_session_map_entry",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch.object(
            bot_module.session_manager,
            "get_window_state",
            return_value=window_state,
        ),
        patch.object(bot_module.session_manager, "bind_thread") as mock_bind,
        _patch_both("safe_edit", new_callable=AsyncMock) as mock_edit,
        _patch_both("aggregator_replay_payload", new_callable=AsyncMock) as mock_replay,
        _patch_both("aggregator_clear_route") as mock_clear_route,
    ):
        if isinstance(flush_failure, BaseException):
            mock_replay.side_effect = flush_failure
        else:
            mock_replay.return_value = flush_failure
        await bot_module._create_and_bind_window(
            query,
            context,
            user,
            str(tmp_path),
            pending_thread_id=10,
            tmux_mgr=bot_module.tmux_manager,
            session_mgr=bot_module.session_manager,
        )

    mock_bind.assert_called_once_with(1, 10, "@0", window_name="created-window")
    mock_replay.assert_awaited_once_with(
        (1, 10, "@0"), text="hello", attachments=[_replay_attachment(payload)]
    )
    mock_clear_route.assert_called_once_with((1, 10, "@0"))
    edit_text = mock_edit.await_args.args[1]
    assert "Created, but the first message failed to send" in edit_text
    assert "pending payload was cleared" in edit_text
    assert "please resend" in edit_text
    query.answer.assert_awaited_once_with(
        "Created; first message failed", show_alert=True
    )
    assert "_pending_thread_text" not in context.user_data
    assert "_pending_thread_attachments" not in context.user_data
    assert "_pending_thread_id" not in context.user_data
    assert not payload.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "flush_failure",
    [False, RuntimeError("tmux send exploded")],
    ids=["flush-false", "flush-exception"],
)
async def test_existing_window_bind_pending_flush_failure_is_explicit_and_cleans_up(
    tmp_path: Path, flush_failure: object
):
    payload = tmp_path / "existing-flush-fail.bin"
    payload.write_bytes(b"data")
    context = MagicMock()
    context.user_data = _pending_user_data(payload, thread_id=10)
    context.user_data[STATE_KEY] = STATE_SELECTING_WINDOW
    context.user_data[UNBOUND_WINDOWS_KEY] = ["@0"]
    update = _make_callback_update(f"{CB_WIN_BIND}0", thread_id=10)
    window = MagicMock()
    window.window_id = "@0"
    window.window_name = "existing-window"

    with (
        _patch_both("is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        patch.object(bot_module.session_manager, "bind_thread") as mock_bind,
        patch.object(
            bot_module.tmux_manager,
            "find_window_by_id",
            new_callable=AsyncMock,
            return_value=window,
        ),
        _patch_both(
            "_list_unbound_windows",
            new_callable=AsyncMock,
            return_value=[("@0", "existing-window", str(tmp_path))],
        ),
        _patch_both("safe_edit", new_callable=AsyncMock) as mock_edit,
        _patch_both("aggregator_replay_payload", new_callable=AsyncMock) as mock_replay,
        _patch_both("aggregator_clear_route") as mock_clear_route,
    ):
        if isinstance(flush_failure, BaseException):
            mock_replay.side_effect = flush_failure
        else:
            mock_replay.return_value = flush_failure
        await bot_module.callback_handler(update, context)

    mock_bind.assert_called_once_with(1, 10, "@0", window_name="existing-window")
    mock_replay.assert_awaited_once_with(
        (1, 10, "@0"), text="hello", attachments=[_replay_attachment(payload)]
    )
    mock_clear_route.assert_called_once_with((1, 10, "@0"))
    edit_text = mock_edit.await_args.args[1]
    assert "Bound to window `existing-window`" in edit_text
    assert "First message failed to send" in edit_text
    assert "pending payload was cleared" in edit_text
    assert "please resend" in edit_text
    update.callback_query.answer.assert_awaited_once_with(
        "Bound; first message failed", show_alert=True
    )
    assert "_pending_thread_text" not in context.user_data
    assert "_pending_thread_attachments" not in context.user_data
    assert "_pending_thread_id" not in context.user_data
    assert not payload.exists()


@pytest.mark.asyncio
async def test_existing_window_bind_pending_flush_success_remains_normal(
    tmp_path: Path,
):
    payload = tmp_path / "existing-flush-ok.bin"
    payload.write_bytes(b"data")
    context = MagicMock()
    context.user_data = _pending_user_data(payload, thread_id=10)
    context.user_data[STATE_KEY] = STATE_SELECTING_WINDOW
    context.user_data[UNBOUND_WINDOWS_KEY] = ["@0"]
    update = _make_callback_update(f"{CB_WIN_BIND}0", thread_id=10)
    window = MagicMock()
    window.window_id = "@0"
    window.window_name = "existing-window"

    with (
        _patch_both("is_user_allowed", return_value=True),
        patch.object(bot_module.session_manager, "set_group_chat_id"),
        patch.object(bot_module.session_manager, "bind_thread") as mock_bind,
        patch.object(
            bot_module.tmux_manager,
            "find_window_by_id",
            new_callable=AsyncMock,
            return_value=window,
        ),
        _patch_both(
            "_list_unbound_windows",
            new_callable=AsyncMock,
            return_value=[("@0", "existing-window", str(tmp_path))],
        ),
        _patch_both("safe_edit", new_callable=AsyncMock) as mock_edit,
        _patch_both(
            "aggregator_replay_payload",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_replay,
    ):
        await bot_module.callback_handler(update, context)

    mock_bind.assert_called_once_with(1, 10, "@0", window_name="existing-window")
    mock_replay.assert_awaited_once_with(
        (1, 10, "@0"), text="hello", attachments=[_replay_attachment(payload)]
    )
    edit_text = mock_edit.await_args.args[1]
    assert edit_text == "✅ Bound to window `existing-window`\n\nFirst message sent."
    update.callback_query.answer.assert_awaited_once_with("Bound")
    assert "_pending_thread_text" not in context.user_data
    assert "_pending_thread_attachments" not in context.user_data
    assert "_pending_thread_id" not in context.user_data
    assert payload.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "max_attachments", "media_groups"),
    [
        ("media-boundary", 10, ["g1", "g1", "g2"]),
        ("attachment-cap", 2, ["g1", "g1", "g1"]),
    ],
)
async def test_pending_replay_observes_all_split_send_failures(
    tmp_path: Path,
    case: str,
    max_attachments: int,
    media_groups: list[str],
):
    paths = [tmp_path / f"{case}-{idx}.bin" for idx in range(3)]
    for path in paths:
        path.write_bytes(b"data")
    context = MagicMock()
    context.user_data = {
        "_pending_thread_id": 10,
        "_pending_thread_text": "hello",
        "_pending_thread_attachments": [
            bot_module.PendingAttachment(str(paths[0]), "caption one", media_groups[0]),
            bot_module.PendingAttachment(str(paths[1]), "", media_groups[1]),
            bot_module.PendingAttachment(str(paths[2]), "caption two", media_groups[2]),
        ],
    }
    route = (1, 10, "@0")

    with (
        patch.object(bot_module.config, "busy_indicator_v2", False),
        patch.object(bot_module.config, "aggregator_max_attachments", max_attachments),
        patch.object(
            bot_module.session_manager,
            "send_to_window",
            new_callable=AsyncMock,
            side_effect=[(False, "first split failed"), (True, "ok")],
        ) as mock_send,
    ):
        delivered = await bot_module._flush_pending_route_payload(
            route, context.user_data
        )

    assert delivered is False
    assert mock_send.await_count == 2
    first_send = mock_send.await_args_list[0].args[1]
    second_send = mock_send.await_args_list[1].args[1]
    assert "hello" in first_send
    assert str(paths[0]) in first_send
    assert str(paths[1]) in first_send
    assert str(paths[2]) not in first_send
    assert str(paths[2]) in second_send
    assert "_pending_thread_text" not in context.user_data
    assert "_pending_thread_attachments" not in context.user_data
    assert "_pending_thread_id" not in context.user_data
    assert all(not path.exists() for path in paths)
