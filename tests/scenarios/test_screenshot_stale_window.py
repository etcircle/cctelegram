"""Scenario: /screenshot keyboard against a killed or rebound window.

Each button in the screenshot keyboard carries the originating ``window_id``
in its callback data. After a window is killed or the topic is rebound to a
different window, a tap must be rejected at the callback seam — never sent
to tmux. The two reject paths exist independently:

  - *topic mismatch* (``reject_stale_window_callback``) — the topic this
    callback came from no longer maps to this ``window_id``.
  - *window not found* — the topic still maps to this id but the tmux
    substrate has no such window.
"""

from __future__ import annotations

import pytest

from cctelegram import bot as bot_module
from cctelegram.handlers.callback_data import CB_KEYS_PREFIX, CB_SCREENSHOT_REFRESH
from tests.conftest import ScenarioHarness, make_update_callback


pytestmark = pytest.mark.scenario


@pytest.mark.asyncio
async def test_screenshot_key_against_killed_window_answers_window_not_found(
    scenario: ScenarioHarness,
) -> None:
    """Window still bound to topic but killed at the substrate → 'Window not found'."""
    # Bind topic to a window_id that the fake tmux has *no* entry for.
    scenario.session_manager.thread_bindings.setdefault(scenario.user_id, {})[42] = "@9"
    scenario.session_manager.window_display_names["@9"] = "ghost"

    update = make_update_callback(f"{CB_KEYS_PREFIX}up:@9", thread_id=42)
    await bot_module.callback_handler(update, scenario.context)

    update.callback_query.answer.assert_awaited()
    answer_text = update.callback_query.answer.await_args.args[0]
    assert "Window not found" in answer_text
    assert scenario.tmux.sent_keys == []


@pytest.mark.asyncio
async def test_screenshot_key_with_topic_mismatch_is_rejected(
    scenario: ScenarioHarness,
) -> None:
    """Topic now points to a different window → 'Stale controls'."""
    old_wid = scenario.add_window(window_name="old-repo", cwd="/old")
    new_wid = scenario.add_window(window_name="new-repo", cwd="/new")
    # Topic is now bound to new_wid; old_wid is what the (stale) callback points to.
    scenario.bind_thread(
        thread_id=42, window_id=new_wid, display_name="new-repo", cwd="/new"
    )

    update = make_update_callback(f"{CB_KEYS_PREFIX}up:{old_wid}", thread_id=42)
    await bot_module.callback_handler(update, scenario.context)

    update.callback_query.answer.assert_awaited()
    answer_text = update.callback_query.answer.await_args.args[0]
    assert "Stale controls" in answer_text
    assert scenario.tmux.sent_keys == []


@pytest.mark.asyncio
async def test_screenshot_refresh_against_killed_window_is_rejected(
    scenario: ScenarioHarness,
) -> None:
    """Refresh button after window dies → 'Window no longer exists'."""
    scenario.session_manager.thread_bindings.setdefault(scenario.user_id, {})[42] = "@9"
    scenario.session_manager.window_display_names["@9"] = "ghost"

    update = make_update_callback(f"{CB_SCREENSHOT_REFRESH}@9", thread_id=42)
    await bot_module.callback_handler(update, scenario.context)

    update.callback_query.answer.assert_awaited()
    answer_text = update.callback_query.answer.await_args.args[0]
    assert "Window no longer exists" in answer_text
    assert scenario.tmux.sent_keys == []


@pytest.mark.asyncio
async def test_screenshot_key_on_live_window_sends_to_tmux(
    scenario: ScenarioHarness,
) -> None:
    """Sanity: live window + matching topic → keystroke reaches the substrate."""
    wid = scenario.add_window(window_name="repo", cwd="/repo", pane_text="hi")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")

    update = make_update_callback(f"{CB_KEYS_PREFIX}up:{wid}", thread_id=42)
    await bot_module.callback_handler(update, scenario.context)

    assert any(
        sent[0] == wid and sent[1] == "Up" and sent[2] is False
        for sent in scenario.tmux.sent_keys
    )
