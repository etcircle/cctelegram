"""Scenario: closing a Telegram topic kills the tmux window + unbinds.

When the user closes a forum topic, ``topic_closed_handler`` must:
  - find the window via ``session_manager``,
  - kill the tmux window through the substrate,
  - clear the binding from ``thread_bindings``.

The downstream ``clear_topic_state`` then drains route queues and frees the
status / tool message ID maps. Wave A asserts the substrate calls — the
deeper queue drain is exercised by the route lifecycle scenario.
"""

from __future__ import annotations

import pytest

from cctelegram import bot as bot_module
from tests.conftest import ScenarioHarness, make_update_topic_closed


pytestmark = pytest.mark.scenario


@pytest.mark.asyncio
async def test_topic_close_kills_bound_window_and_unbinds(
    scenario: ScenarioHarness,
) -> None:
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")

    update = make_update_topic_closed(thread_id=42)
    await bot_module.topic_closed_handler(update, scenario.context)

    assert scenario.tmux.kill_calls == [wid]
    assert wid not in scenario.tmux.windows
    bindings = scenario.session_manager.thread_bindings.get(scenario.user_id, {})
    assert 42 not in bindings


@pytest.mark.asyncio
async def test_topic_close_unbound_topic_is_a_noop(
    scenario: ScenarioHarness,
) -> None:
    """No binding means there's nothing to clean up — handler must short-circuit."""
    update = make_update_topic_closed(thread_id=42)
    await bot_module.topic_closed_handler(update, scenario.context)

    assert scenario.tmux.kill_calls == []
    assert not scenario.session_manager.thread_bindings


@pytest.mark.asyncio
async def test_topic_close_window_already_gone_still_unbinds(
    scenario: ScenarioHarness,
) -> None:
    """If tmux killed the window externally, the bot still unbinds cleanly."""
    scenario.session_manager.thread_bindings.setdefault(scenario.user_id, {})[42] = "@9"
    scenario.session_manager.window_display_names["@9"] = "ghost"

    update = make_update_topic_closed(thread_id=42)
    await bot_module.topic_closed_handler(update, scenario.context)

    # No tmux kill because find_window_by_id returns None.
    assert scenario.tmux.kill_calls == []
    bindings = scenario.session_manager.thread_bindings.get(scenario.user_id, {})
    assert 42 not in bindings
