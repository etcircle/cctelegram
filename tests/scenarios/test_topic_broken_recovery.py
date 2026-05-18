"""Scenario: Telegram delivery fails for a deleted topic → reactive cleanup.

Telegram doesn't emit ``forum_topic_deleted`` to bots. The only signal a
topic was deleted while we were idle is a topic-shaped error
(``TOPIC_NOT_FOUND``) on the next send. ``probe_topic_liveness`` and
``_emergency_dm`` recognize this and:
  - mark the (user, thread) in ``_bad_topic_threads`` so future sends DM
    instead of fighting Telegram,
  - kill the orphan tmux window,
  - unbind the thread,
  - clear topic state via ``clear_topic_state``.
"""

from __future__ import annotations

import pytest
from telegram.error import BadRequest

from cctelegram.handlers import message_queue
from tests.conftest import ScenarioHarness


pytestmark = pytest.mark.scenario


@pytest.mark.asyncio
async def test_probe_topic_liveness_cleans_orphan_window_on_topic_not_found(
    scenario: ScenarioHarness,
) -> None:
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")

    # The probe sends send_chat_action; raise TOPIC_NOT_FOUND.
    async def raise_not_found(**_: object) -> bool:
        raise BadRequest("message thread not found")

    scenario.bot.send_chat_action = raise_not_found  # type: ignore[assignment]

    await message_queue.probe_topic_liveness(scenario.bot)

    # Orphan tmux window killed.
    assert wid in scenario.tmux.kill_calls
    # Thread binding removed.
    bindings = scenario.session_manager.thread_bindings.get(scenario.user_id, {})
    assert 42 not in bindings
    # Bad-topic mark persists so future sends DM instead.
    assert (scenario.user_id, 42) in message_queue._bad_topic_threads


@pytest.mark.asyncio
async def test_probe_topic_liveness_leaves_healthy_topic_alone(
    scenario: ScenarioHarness,
) -> None:
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")

    await message_queue.probe_topic_liveness(scenario.bot)

    assert scenario.tmux.kill_calls == []
    bindings = scenario.session_manager.thread_bindings.get(scenario.user_id, {})
    assert bindings == {42: wid}
    assert (scenario.user_id, 42) not in message_queue._bad_topic_threads
