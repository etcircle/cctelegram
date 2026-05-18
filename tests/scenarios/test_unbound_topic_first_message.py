"""Scenario: first message in an unbound topic opens the directory browser.

When a user sends text in a topic with no ``thread_bindings`` entry, the bot
must:
  - reply with the directory browser keyboard,
  - stash the text in ``_pending_thread_text`` so it can be flushed once the
    user picks a directory,
  - record ``_pending_thread_id`` so callbacks know which thread owns the
    pending payload.

A separate scenario (``test_stale_pending_replacement``) covers the case
where a *second* unbound topic shows up while the first still has a pending
payload — the bot must replace ownership without leaking the prior thread's
file attachments.
"""

from __future__ import annotations

import pytest

from cctelegram import bot as bot_module
from cctelegram.handlers.directory_browser import (
    BROWSE_PATH_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
)
from tests.conftest import ScenarioHarness, make_update_text


pytestmark = pytest.mark.scenario


@pytest.mark.asyncio
async def test_unbound_topic_text_opens_browser_and_stashes_text(
    scenario: ScenarioHarness,
) -> None:
    update = make_update_text("hello claude", thread_id=42)

    await bot_module.text_handler(update, scenario.context)

    # Browser reply was sent (with an inline keyboard).
    update.message.reply_text.assert_awaited()
    sent_kwargs = update.message.reply_text.await_args.kwargs
    assert "reply_markup" in sent_kwargs
    # Pending payload is stashed for the directory pick.
    assert scenario.user_data["_pending_thread_id"] == 42
    assert scenario.user_data["_pending_thread_text"] == "hello claude"
    assert scenario.user_data[STATE_KEY] == STATE_BROWSING_DIRECTORY
    assert BROWSE_PATH_KEY in scenario.user_data
    # No tmux send_keys: nothing is forwarded until the directory is picked.
    assert scenario.tmux.sent_keys == []


@pytest.mark.asyncio
async def test_unbound_topic_no_thread_id_rejects(
    scenario: ScenarioHarness,
) -> None:
    """Text outside a named topic rejects rather than auto-creating a window."""
    update = make_update_text("hello", thread_id=None)

    await bot_module.text_handler(update, scenario.context)

    update.message.reply_text.assert_awaited()
    reply_text = update.message.reply_text.await_args.args[0]
    assert "named topic" in reply_text


@pytest.mark.asyncio
async def test_bound_topic_with_dead_window_unbinds_and_warns(
    scenario: ScenarioHarness,
) -> None:
    """Bound topic but window gone → unbind + plain error, no tmux send."""
    scenario.session_manager.thread_bindings.setdefault(scenario.user_id, {})[42] = "@9"
    scenario.session_manager.window_display_names["@9"] = "ghost"

    update = make_update_text("hello", thread_id=42)
    await bot_module.text_handler(update, scenario.context)

    update.message.reply_text.assert_awaited()
    reply_text = update.message.reply_text.await_args.args[0]
    assert "no longer exists" in reply_text
    # Binding was removed.
    bindings = scenario.session_manager.thread_bindings.get(scenario.user_id, {})
    assert 42 not in bindings
