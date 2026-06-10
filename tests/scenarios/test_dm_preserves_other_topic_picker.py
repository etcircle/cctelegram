"""Scenario: a DM / General-topic text must not destroy another topic's picker flow.

PTB ``user_data`` is per-user across chats, so while the user is mid
directory-browser (or window/session picker) in topic A, a stray text typed
into General or a DM arrives with ``thread_id is None``. Review finding 8:
the cross-thread stale-picker guards ran BEFORE the named-topic rejection, so
``pending_tid == None`` evaluated False → the "stale picker" branch cleared
topic A's browse state AND deleted its pending attachment files — then the
message dead-ended with "use a named topic" anyway. ``photo_handler`` /
``document_handler`` reject ``thread_id is None`` first; the text handler
must match. A DM/General message must touch NOTHING.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cctelegram import bot as bot_module
from cctelegram.handlers import inbound_telegram as inbound_module
from cctelegram.handlers.directory_browser import (
    BROWSE_PATH_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_SELECTING_SESSION,
    STATE_SELECTING_WINDOW,
)
from tests.conftest import ScenarioHarness, make_update_text


pytestmark = pytest.mark.scenario


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "picker_state",
    [
        STATE_BROWSING_DIRECTORY,
        STATE_SELECTING_WINDOW,
        STATE_SELECTING_SESSION,
    ],
)
async def test_dm_text_mid_picker_preserves_other_topics_flow(
    scenario: ScenarioHarness,
    tmp_path: Path,
    picker_state: str,
) -> None:
    """Text with thread_id=None rejects WITHOUT touching topic A's pending state."""
    payload = tmp_path / "pending-photo.jpg"
    payload.write_bytes(b"image")
    scenario.user_data.update(
        {
            STATE_KEY: picker_state,
            BROWSE_PATH_KEY: "/tmp/browse",
            "_pending_thread_id": 42,
            "_pending_thread_text": "hello from topic A",
            "_pending_thread_attachments": [
                inbound_module.PendingAttachment(str(payload), "caption", None)
            ],
        }
    )

    update = make_update_text("stray dm text", thread_id=None)
    await bot_module.text_handler(update, scenario.context)

    # The DM gets the named-topic rejection.
    update.message.reply_text.assert_awaited()
    reply_text = update.message.reply_text.await_args.args[0]
    assert "named topic" in reply_text

    # Topic A's picker flow is fully intact — state, text, AND files.
    assert scenario.user_data[STATE_KEY] == picker_state
    assert scenario.user_data["_pending_thread_id"] == 42
    assert scenario.user_data["_pending_thread_text"] == "hello from topic A"
    assert scenario.user_data["_pending_thread_attachments"] == [
        inbound_module.PendingAttachment(str(payload), "caption", None)
    ]
    assert payload.exists()
    # Topic A was NOT marked as a stale thread.
    assert 42 not in scenario.user_data.get("_ignored_stale_thread_ids", [])
    # Nothing was forwarded to tmux.
    assert scenario.tmux.sent_keys == []
