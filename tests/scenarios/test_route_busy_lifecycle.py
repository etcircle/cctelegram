"""Scenario: route busy lifecycle (c313657 regression).

Commit ``c313657 busy: wire activity callback to re-arm idle-clear on events``
fixed a class of bugs where:

  - A route reached ``IDLE_CLEARED`` (idle delay elapsed, pane scrape said
    no spinner), so ``status_polling._idle_state[key] = "cleared"``.
  - The next sub-agent / quick-tool turn finished between two 10-second
    pane scrapes — the only re-arm signal under V1 was a pane scrape that
    showed ``is_running == True``.
  - ``busy_indicator._open_tools`` accumulated, the typing indicator ran
    forever, no card was ever published.

The fix was a dedicated ``register_activity_callback`` channel from
``busy_indicator.on_transcript_event`` to ``status_polling._on_busy_activity``
so each real event drops ``_idle_state[key]`` directly.

This scenario asserts the activity callback chain is wired and fires on a
transcript event, without re-implementing what ``test_busy_indicator.py``
already covers for the state machine itself.
"""

from __future__ import annotations

from typing import Any

import pytest

from cctelegram.handlers import busy_indicator, status_polling
from cctelegram.handlers.busy_indicator import RunState
from cctelegram.session_monitor import TranscriptEvent
from tests.conftest import ScenarioHarness


pytestmark = pytest.mark.scenario


def _event(**kw: Any) -> TranscriptEvent:
    defaults: dict[str, Any] = dict(
        session_id="sess-1",
        role="assistant",
        block_type="text",
        tool_use_id=None,
        tool_name=None,
        stop_reason=None,
        timestamp=None,
        text="",
        image_data=None,
    )
    defaults.update(kw)
    return TranscriptEvent(**defaults)


@pytest.mark.asyncio
async def test_transcript_event_fires_activity_callback_after_idle(
    scenario: ScenarioHarness,
) -> None:
    """A transcript event after the route reached IDLE_CLEARED re-arms idle-clear.

    Pre-c313657 regression: this signal didn't exist; ``_idle_state`` stayed
    "cleared" until a pane scrape (10s away) noticed activity.
    """
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")
    # The harness reset cleared busy_indicator callbacks; re-register
    # status_polling's consumer the way module-import does in production.
    busy_indicator.register_activity_callback(status_polling._on_busy_activity)

    route = (scenario.user_id, 42, wid)
    key = (scenario.user_id, 42)
    status_polling._idle_state[key] = "cleared"

    # New transcript event arrives — should re-arm idle (drop the "cleared" mark).
    await busy_indicator.on_transcript_event(
        _event(
            block_type="tool_use",
            tool_use_id="t1",
            tool_name="Bash",
            stop_reason="tool_use",
        ),
        [route],
    )

    assert key not in status_polling._idle_state, (
        "c313657 regression: idle_state still 'cleared' after a transcript event "
        "— activity callback chain is broken."
    )
    assert busy_indicator.state(route) is RunState.RUNNING_TOOL


@pytest.mark.asyncio
async def test_mark_inbound_sent_fires_activity_callback(
    scenario: ScenarioHarness,
) -> None:
    """Inbound prompt delivery is also a real activity signal."""
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")
    busy_indicator.register_activity_callback(status_polling._on_busy_activity)

    route = (scenario.user_id, 42, wid)
    key = (scenario.user_id, 42)
    status_polling._idle_state[key] = "cleared"

    await busy_indicator.mark_inbound_sent(route)

    assert key not in status_polling._idle_state


@pytest.mark.asyncio
async def test_full_tool_turn_walks_states(
    scenario: ScenarioHarness,
) -> None:
    """Single-tool turn walks RUNNING_TOOL → RUNNING → IDLE_RECENT public states."""
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")
    route = (scenario.user_id, 42, wid)

    # tool_use → RUNNING_TOOL
    await busy_indicator.on_transcript_event(
        _event(
            block_type="tool_use",
            tool_use_id="t1",
            tool_name="Bash",
            stop_reason="tool_use",
        ),
        [route],
    )
    assert busy_indicator.state(route) is RunState.RUNNING_TOOL

    # tool_result → RUNNING
    await busy_indicator.on_transcript_event(
        _event(block_type="tool_result", tool_use_id="t1"),
        [route],
    )
    assert busy_indicator.state(route) is RunState.RUNNING

    # end_turn → IDLE_RECENT
    await busy_indicator.on_transcript_event(
        _event(block_type="text", stop_reason="end_turn", text="done"),
        [route],
    )
    assert busy_indicator.state(route) is RunState.IDLE_RECENT
