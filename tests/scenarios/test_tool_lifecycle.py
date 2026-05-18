"""Scenario: a single tool turn renders as one activity digest + assistant text.

Claude's tool calls go through the activity-digest pipeline:
  - ``tool_use`` arrives → a compact line is appended to the digest state,
    no message is sent yet (10s debounce).
  - matching ``tool_result`` arrives → the *same* line is replaced in place
    with the result-shape text, still no send.
  - ``text`` block with ``stop_reason=end_turn`` arrives → the digest is
    *finalized* (one send_message with the full digest), then the assistant
    text is sent as its own message.

The earlier "tool_use card → tool_result edit" framing in the campaign
doc described the V1 model where each tool_use was its own Telegram
message; V2 collapses noisy tool calls into one editable digest, but the
campaign-level invariant ("a tool_result must update the same Telegram
surface as its tool_use") still holds — just inside the digest.
"""

from __future__ import annotations

import asyncio

import pytest

from cctelegram import bot as bot_module
from cctelegram.handlers import message_queue
from cctelegram.session_monitor import NewMessage
from tests.conftest import ScenarioHarness


pytestmark = pytest.mark.scenario


async def _drain_route(route: tuple[int, int, str]) -> None:
    queue = message_queue.get_content_queue(route)
    if queue is not None:
        await queue.join()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_full_tool_turn_finalizes_digest_then_sends_text(
    scenario: ScenarioHarness,
) -> None:
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(
        thread_id=42,
        window_id=wid,
        display_name="repo",
        cwd="/repo",
        session_id="sess-1",
    )
    route = (scenario.user_id, 42, wid)

    # 1. tool_use → digest state accumulates, no send.
    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="**Bash**\n```\nls -la\n```",
            content_type="tool_use",
            tool_use_id="t1",
            tool_name="Bash",
            role="assistant",
        ),
        scenario.bot,
    )
    await _drain_route(route)
    assert scenario.bot.sent == [], "tool_use must not send a message directly under V2"

    # 2. tool_result → digest line replaced in place, still no send.
    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="```\ntotal 0\n```",
            content_type="tool_result",
            tool_use_id="t1",
            role="assistant",
        ),
        scenario.bot,
    )
    await _drain_route(route)
    assert scenario.bot.sent == [], (
        "tool_result must not send a message directly under V2"
    )

    # The digest state remembers the open turn.
    digest_state = message_queue._activity_msg_info.get((scenario.user_id, 42))
    assert digest_state is not None
    assert digest_state.tool_count == 1
    assert digest_state.completed_count == 1

    # 3. assistant text with end_turn → finalize digest + send text.
    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="All done.",
            content_type="text",
            stop_reason="end_turn",
            role="assistant",
        ),
        scenario.bot,
    )
    await _drain_route(route)

    send_calls = [s for s in scenario.bot.sent if s.method == "send_message"]
    # One send for the digest (finalize), one send for the assistant text.
    assert len(send_calls) == 2, (
        f"expected digest + text sends, got {[s.method for s in scenario.bot.sent]}"
    )
    # The final text body landed in the last send.
    assert "All done" in (send_calls[-1].kwargs.get("text") or "")
