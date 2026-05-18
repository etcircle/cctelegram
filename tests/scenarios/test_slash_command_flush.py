"""Scenario: aggregator flushes before /command is forwarded.

When a user types text followed quickly by a slash command, the aggregator
holds the text in a debounce window. ``forward_command_handler`` must
flush that bundle *before* sending the slash command, otherwise arrival
order at the tmux pane would be wrong (slash lands before the text it was
meant to follow).
"""

from __future__ import annotations

import pytest

from cctelegram import bot as bot_module
from cctelegram.handlers.inbound_aggregator import aggregator_offer_text, has_pending
from tests.conftest import ScenarioHarness, make_update_command


pytestmark = pytest.mark.scenario


@pytest.mark.asyncio
async def test_slash_command_drains_pending_aggregator_first(
    scenario: ScenarioHarness,
) -> None:
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")
    route = (scenario.user_id, 42, wid)

    # Stage a pending bundle that hasn't fired the debounce yet.
    await aggregator_offer_text(route, "pre-command text")
    assert has_pending(route)

    update = make_update_command("clear", thread_id=42)
    await bot_module.forward_command_handler(update, scenario.context)

    # Bundle was flushed (no pending content left).
    assert not has_pending(route)

    # The pending text reached tmux first, then /clear.
    text_indexes = [
        i
        for i, (sent_wid, keys, _, _) in enumerate(scenario.tmux.sent_keys)
        if sent_wid == wid and "pre-command text" in keys
    ]
    cmd_indexes = [
        i
        for i, (sent_wid, keys, _, _) in enumerate(scenario.tmux.sent_keys)
        if sent_wid == wid and keys.startswith("/clear")
    ]
    assert text_indexes and cmd_indexes
    assert text_indexes[0] < cmd_indexes[0]


@pytest.mark.asyncio
async def test_slash_command_with_no_pending_just_sends(
    scenario: ScenarioHarness,
) -> None:
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")

    update = make_update_command("clear", thread_id=42)
    await bot_module.forward_command_handler(update, scenario.context)

    cmd_sends = [
        (sent_wid, keys)
        for sent_wid, keys, _, _ in scenario.tmux.sent_keys
        if sent_wid == wid and keys.startswith("/clear")
    ]
    assert cmd_sends


@pytest.mark.asyncio
async def test_command_in_unbound_topic_replies_with_error(
    scenario: ScenarioHarness,
) -> None:
    update = make_update_command("clear", thread_id=42)
    await bot_module.forward_command_handler(update, scenario.context)

    update.message.reply_text.assert_awaited()
    reply_text = update.message.reply_text.await_args.args[0]
    assert "No session bound" in reply_text
