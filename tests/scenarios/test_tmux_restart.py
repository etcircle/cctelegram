"""Scenario: bot restart re-resolves stale window IDs by display name.

Tmux window IDs (``@0``, ``@12``) reset when the tmux server restarts. On
startup, ``SessionManager.resolve_stale_ids`` walks ``window_states`` /
``thread_bindings`` and, for each stale id, looks for a live window with
the matching display name. Bindings get re-pointed at the new id; entries
whose display name no longer matches get dropped.
"""

from __future__ import annotations

import pytest

from cctelegram.session import WindowState
from tests.conftest import ScenarioHarness


pytestmark = pytest.mark.scenario


@pytest.mark.asyncio
async def test_stale_window_id_remapped_by_display_name(
    scenario: ScenarioHarness,
) -> None:
    # Persisted state from before restart: thread 42 → "@0" (now stale).
    scenario.session_manager.window_states["@0"] = WindowState(
        session_id="sess-old",
        cwd="/repo",
        window_name="repo",
    )
    scenario.session_manager.window_display_names["@0"] = "repo"
    scenario.session_manager.thread_bindings.setdefault(scenario.user_id, {})[42] = "@0"

    # After tmux restart, the same window exists but at a new id.
    new_wid = scenario.add_window(window_id="@7", window_name="repo", cwd="/repo")

    await scenario.session_manager.resolve_stale_ids()

    # The state migrated to the new id.
    assert "@0" not in scenario.session_manager.window_states
    assert new_wid in scenario.session_manager.window_states
    assert scenario.session_manager.thread_bindings[scenario.user_id][42] == new_wid
    assert scenario.session_manager.window_display_names[new_wid] == "repo"


@pytest.mark.asyncio
async def test_window_states_only_path_migrates_cleanly(
    scenario: ScenarioHarness,
) -> None:
    """Sanity: window_states-only migration (no thread_bindings) succeeds.

    Confirms the smell above is specifically about migration ordering, not
    the underlying display-name re-resolution mechanism.
    """
    scenario.session_manager.window_states["@0"] = WindowState(
        session_id="sess-old",
        cwd="/repo",
        window_name="repo",
    )
    scenario.session_manager.window_display_names["@0"] = "repo"
    new_wid = scenario.add_window(window_id="@7", window_name="repo", cwd="/repo")

    await scenario.session_manager.resolve_stale_ids()

    assert "@0" not in scenario.session_manager.window_states
    assert new_wid in scenario.session_manager.window_states
    assert scenario.session_manager.window_display_names[new_wid] == "repo"


@pytest.mark.asyncio
async def test_stale_window_id_dropped_when_no_live_match(
    scenario: ScenarioHarness,
) -> None:
    """If no live window has the matching display name, the entry is dropped."""
    scenario.session_manager.window_states["@5"] = WindowState(
        session_id="sess",
        cwd="/old",
        window_name="ghost",
    )
    scenario.session_manager.window_display_names["@5"] = "ghost"
    scenario.session_manager.thread_bindings.setdefault(scenario.user_id, {})[42] = "@5"
    # Only an unrelated window is live.
    scenario.add_window(window_id="@1", window_name="something-else", cwd="/x")

    await scenario.session_manager.resolve_stale_ids()

    assert "@5" not in scenario.session_manager.window_states
    bindings = scenario.session_manager.thread_bindings.get(scenario.user_id, {})
    assert 42 not in bindings
