"""Scenario: second unbound topic replaces the first's pending payload.

When user_data has a directory browser in flight for thread A and a new
unbound text arrives for thread B, the bot must:
  - clear thread A's pending payload (files deleted, browse state dropped),
  - record thread A in the ``_ignored_stale_thread_ids`` set so a late
    picker callback from A is answered as stale *without* nuking thread B's
    fresh pending payload (``bot.py:273-303``),
  - install fresh browser state for thread B.

Pre-fix: a late cancel from thread A would delete thread B's attachment.
"""

from __future__ import annotations

import pytest

from cctelegram import bot as bot_module
from cctelegram.handlers.callback_data import CB_DIR_CANCEL
from cctelegram.handlers.directory_browser import (
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
)
from tests.conftest import ScenarioHarness, make_update_callback, make_update_text


pytestmark = pytest.mark.scenario


@pytest.mark.asyncio
async def test_new_unbound_text_replaces_owner_and_marks_old_stale(
    scenario: ScenarioHarness,
) -> None:
    # Topic A: user is browsing for a directory after sending "first".
    update_a = make_update_text("first", thread_id=42)
    await bot_module.text_handler(update_a, scenario.context)
    assert scenario.user_data["_pending_thread_id"] == 42
    assert scenario.user_data["_pending_thread_text"] == "first"

    # Topic B arrives with a new unbound text — should take over.
    update_b = make_update_text("second", thread_id=43, message_id=200)
    await bot_module.text_handler(update_b, scenario.context)

    assert scenario.user_data["_pending_thread_id"] == 43
    assert scenario.user_data["_pending_thread_text"] == "second"
    assert scenario.user_data[STATE_KEY] == STATE_BROWSING_DIRECTORY
    # Thread A's id is remembered as stale so its leftover callbacks don't clobber B.
    assert 42 in scenario.user_data.get("_ignored_stale_thread_ids", [])


@pytest.mark.asyncio
async def test_stale_cancel_from_replaced_topic_preserves_new_payload(
    scenario: ScenarioHarness,
) -> None:
    """A cancel from thread A (now stale) must NOT clear thread B's pending payload."""
    # Replay the replacement sequence.
    await bot_module.text_handler(
        make_update_text("first", thread_id=42), scenario.context
    )
    await bot_module.text_handler(
        make_update_text("second", thread_id=43, message_id=200),
        scenario.context,
    )
    assert scenario.user_data["_pending_thread_id"] == 43

    # Late cancel callback fires from the OLD topic (thread 42).
    cancel_update = make_update_callback(CB_DIR_CANCEL, thread_id=42)
    await bot_module.callback_handler(cancel_update, scenario.context)

    # Thread B's payload survives.
    assert scenario.user_data["_pending_thread_id"] == 43
    assert scenario.user_data["_pending_thread_text"] == "second"
    # The stale cancel was acknowledged.
    cancel_update.callback_query.answer.assert_awaited()
