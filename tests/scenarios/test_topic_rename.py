"""Scenario: topic rename syncs tmux window + display name.

When the user renames a Telegram topic bound to a tmux window, the bot's
``topic_edited_handler`` must propagate the new name to both the tmux window
(``rename_window``) and the session manager display-name map.
"""

from __future__ import annotations

import pytest

from cctelegram import bot as bot_module
from tests.conftest import ScenarioHarness, make_update_topic_renamed


pytestmark = pytest.mark.scenario


@pytest.mark.asyncio
async def test_topic_rename_propagates_to_tmux_and_display_name(
    scenario: ScenarioHarness,
) -> None:
    wid = scenario.add_window(window_name="repo-old", cwd="/repo")
    scenario.bind_thread(
        thread_id=42, window_id=wid, display_name="repo-old", cwd="/repo"
    )

    update = make_update_topic_renamed("repo-new", thread_id=42)
    await bot_module.topic_edited_handler(update, scenario.context)

    assert scenario.tmux.rename_calls == [(wid, "repo-new")]
    assert scenario.session_manager.window_display_names[wid] == "repo-new"
    assert scenario.tmux.windows[wid].window_name == "repo-new"


@pytest.mark.asyncio
async def test_topic_rename_idempotent_when_name_unchanged(
    scenario: ScenarioHarness,
) -> None:
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")

    update = make_update_topic_renamed("repo", thread_id=42)
    await bot_module.topic_edited_handler(update, scenario.context)

    assert scenario.tmux.rename_calls == []


@pytest.mark.asyncio
async def test_topic_rename_no_binding_is_a_noop(
    scenario: ScenarioHarness,
) -> None:
    update = make_update_topic_renamed("repo", thread_id=42)
    await bot_module.topic_edited_handler(update, scenario.context)

    assert scenario.tmux.rename_calls == []
    assert not scenario.session_manager.window_display_names
