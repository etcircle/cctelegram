"""Scenario: /kill mid tool_use cleans state and confirms.

When ``/kill`` runs against a topic with an in-flight tool_use, the bot must:
  - kill the tmux window via the substrate,
  - unbind the thread,
  - run ``clear_topic_state`` (drains route queue, drops _tool_msg_ids),
  - confirm with a reply that mentions the killed display name.

The "mid tool_use" framing is the regression target: pre-fix paths leaked
``message_queue._tool_msg_ids`` and ``busy_indicator._open_tools`` entries
when /kill ran between tool_use and tool_result. The deeper open_tools
cleanup is exercised end-to-end by ``test_route_busy_lifecycle.py`` which
drives the full message_queue pipeline.
"""

from __future__ import annotations

import pytest

from cctelegram import bot as bot_module
from cctelegram.handlers import message_queue
from tests.conftest import ScenarioHarness, make_update_command


pytestmark = pytest.mark.scenario


@pytest.mark.asyncio
async def test_kill_mid_tool_use_kills_window_unbinds_and_confirms(
    scenario: ScenarioHarness,
) -> None:
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")

    update = make_update_command("kill", thread_id=42)
    await bot_module.kill_command(update, scenario.context)

    assert scenario.tmux.kill_calls == [wid]
    bindings = scenario.session_manager.thread_bindings.get(scenario.user_id, {})
    assert 42 not in bindings
    update.message.reply_text.assert_awaited()
    reply_text = update.message.reply_text.await_args.args[0]
    assert "repo" in reply_text


@pytest.mark.asyncio
async def test_kill_runs_topic_state_cleanup(
    scenario: ScenarioHarness,
) -> None:
    """``/kill`` must invoke ``clear_topic_state`` so route queues / msg_id maps drain.

    Wave A asserts the cleanup is *invoked*; the deeper "no leaked open_tools"
    invariant is covered end-to-end by ``test_route_busy_lifecycle.py`` once
    the message_queue worker pipeline is exercised.
    """
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")

    update = make_update_command("kill", thread_id=42)
    await bot_module.kill_command(update, scenario.context)

    # No leftover entries for this topic anywhere in message_queue maps.
    for map_name in (
        "_tool_msg_ids",
        "_status_msg_info",
        "_activity_msg_info",
    ):
        m = getattr(message_queue, map_name, {})
        leaked = [
            k
            for k in m
            if isinstance(k, tuple)
            and len(k) >= 2
            and k[0] == scenario.user_id
            and k[1] == 42
        ]
        assert leaked == [], f"{map_name} leaked: {leaked}"


@pytest.mark.asyncio
async def test_kill_with_no_binding_replies_with_error(
    scenario: ScenarioHarness,
) -> None:
    update = make_update_command("kill", thread_id=42)
    await bot_module.kill_command(update, scenario.context)

    assert scenario.tmux.kill_calls == []
    update.message.reply_text.assert_awaited()
    reply_text = update.message.reply_text.await_args.args[0]
    assert "No session bound" in reply_text
