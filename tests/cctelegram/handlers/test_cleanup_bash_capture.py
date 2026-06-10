"""Review finding 20: topic teardown must cancel background bash-capture tasks.

The only cancellation site for a topic's ``!``-command pane-capture task used
to be a NEW text in the same topic; ``clear_topic_state`` (topic close /
delete / stale-binding GC / ``/unbind`` — which all route through it) never
touched the ``_bash_capture_tasks`` registry, so a ≤30s capture loop kept
posting the OLD window's output into a rebound topic.
"""

from __future__ import annotations

import asyncio

import pytest

from cctelegram.handlers import cleanup
from cctelegram.handlers import inbound_telegram as inbound_module


@pytest.mark.asyncio
async def test_clear_topic_state_cancels_bash_capture_task() -> None:
    """Topic teardown mid-capture cancels the task; nothing posts afterwards."""
    user_id, thread_id = 1, 42
    posted: list[str] = []

    async def fake_capture() -> None:
        # Stands in for _capture_bash_output: would post output if allowed
        # to keep running past teardown.
        try:
            await asyncio.sleep(0.05)
            posted.append("stale output into rebound topic")
        finally:
            inbound_module._bash_capture_tasks.pop((user_id, thread_id), None)

    task = asyncio.create_task(fake_capture())
    inbound_module._bash_capture_tasks[(user_id, thread_id)] = task
    try:
        await asyncio.sleep(0)  # let the task start

        await cleanup.clear_topic_state(user_id, thread_id, None, None)

        # Wait past the capture's next post tick; a cancelled task never posts.
        await asyncio.sleep(0.1)
        assert task.cancelled()
        assert posted == []
        assert (user_id, thread_id) not in inbound_module._bash_capture_tasks
    finally:
        if not task.done():
            task.cancel()
        inbound_module._bash_capture_tasks.pop((user_id, thread_id), None)


@pytest.mark.asyncio
async def test_clear_topic_state_leaves_other_topics_capture_running() -> None:
    """Teardown of topic 42 must not cancel topic 43's capture task."""
    user_id = 1

    async def long_capture() -> None:
        await asyncio.sleep(30)

    other_task = asyncio.create_task(long_capture())
    inbound_module._bash_capture_tasks[(user_id, 43)] = other_task
    try:
        await asyncio.sleep(0)

        await cleanup.clear_topic_state(user_id, 42, None, None)

        await asyncio.sleep(0)
        assert not other_task.cancelled()
        assert (user_id, 43) in inbound_module._bash_capture_tasks
    finally:
        other_task.cancel()
        inbound_module._bash_capture_tasks.pop((user_id, 43), None)
