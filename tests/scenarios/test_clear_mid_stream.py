"""Scenario: /clear rotates the session_id mid-stream.

When the user sends ``/clear`` between a ``tool_use`` and its
``tool_result``, the bot must:
  - forward "/clear" to tmux,
  - call ``session_manager.clear_window_session(wid)`` so the persisted
    ``window_states[wid].session_id`` is empty,
  - stop routing messages from the *old* session_id to this window (a
    SessionStart hook will install the new session_id).

After /clear, a NewMessage carrying the old session_id should no longer
land in this topic.
"""

from __future__ import annotations

import pytest

from cctelegram import bot as bot_module
from cctelegram.session_monitor import NewMessage
from tests.conftest import ScenarioHarness, make_update_command


pytestmark = pytest.mark.scenario


@pytest.mark.asyncio
async def test_clear_command_rotates_window_session(
    scenario: ScenarioHarness,
) -> None:
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(
        thread_id=42,
        window_id=wid,
        display_name="repo",
        cwd="/repo",
        session_id="sess-old",
    )
    assert scenario.session_manager.window_states[wid].session_id == "sess-old"

    update = make_update_command("clear", thread_id=42)
    await bot_module.forward_command_handler(update, scenario.context)

    # /clear was forwarded to tmux.
    assert any(
        sent_wid == wid and "/clear" in keys
        for sent_wid, keys, _, _ in scenario.tmux.sent_keys
    )
    # The session_id was cleared.
    assert scenario.session_manager.window_states[wid].session_id == ""


@pytest.mark.asyncio
async def test_post_clear_old_session_id_no_longer_routes(
    scenario: ScenarioHarness,
) -> None:
    """After /clear, NewMessage with the old session_id does not reach this topic."""
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(
        thread_id=42,
        window_id=wid,
        display_name="repo",
        cwd="/repo",
        session_id="sess-old",
    )

    update = make_update_command("clear", thread_id=42)
    await bot_module.forward_command_handler(update, scenario.context)

    # A NewMessage tagged with the OLD session_id arrives — should drop, not route.
    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-old",
            text="stale from old session",
            content_type="text",
            role="assistant",
            stop_reason="end_turn",
        ),
        scenario.bot,
    )
    # No send went out for the topic; nothing was routed to this user.
    text_sends = [
        s
        for s in scenario.bot.sent
        if s.method == "send_message"
        and (s.kwargs.get("text") or "").startswith("stale")
    ]
    assert text_sends == []
