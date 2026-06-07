"""Pinning tests for ``message_queue.reset_for_tests()``.

These guard the R3 reset-seam invariant: after ``reset_for_tests()`` no
module-level state and no scheduled asyncio task survives into the next test.
A future change that adds a module-level dict/set/task map but forgets to
clear it here — or adds a task map without the cancel-before-clear
ordering — must fail one of these assertions loudly.
"""

from __future__ import annotations

import asyncio

import pytest

from cctelegram.handlers import message_queue as mq


# Every plain dict / OrderedDict cleared by the seam.
# NOTE: ``_route_workers`` is deliberately NOT here — it is a task map (see
# _TASK_MAP_NAMES) so it is seeded with a REAL task and its cancellation is
# asserted, not just its ``.clear()``.
_DICT_NAMES = (
    "_route_queues",
    "_route_locks",
    "_route_pending_ephemeral",
    "_route_ephemeral_kick",
    "_route_inflight",
    "_status_msg_info",
    "_route_last_user_message",
    "_route_user_turn_at",
    "_tool_msg_ids",
    "_agent_tool_ids",
    "_activity_msg_info",
    "_tool_activity_indices",
    "_activity_locks",
    "_subagent_msg_info",
    "_subagent_tool_indices",
    "_subagent_locks",
    "_todo_locks",
    "_todo_msg_info",
    "_todo_pending_snapshot",
    "_todo_tool_ids",
    "_flood_until",
)

# Set-typed module state cleared by the seam.
_SET_NAMES = ("_route_tearing_down", "_bad_topic_threads")

# Task maps: cancel-then-clear. ``_route_workers`` (live per-route queue
# workers) plus the three debounce-flush maps. Each must have its tasks
# cancelled, not merely the map cleared.
_TASK_MAP_NAMES = (
    "_route_workers",
    "_activity_flush_tasks",
    "_subagent_flush_tasks",
    "_todo_flush_tasks",
)


@pytest.mark.asyncio
async def test_reset_clears_all_maps_and_cancels_tasks() -> None:
    """Seed every map/set with a sentinel and a real un-awaited task in each
    task map (``_route_workers`` + the flush maps), then assert the seam
    empties everything and cancelled the scheduled tasks.

    The test holds its own references to the seeded tasks, so the cancel
    assertion proves the real invariant — no task survives uncancelled — and
    would fail a regression that clears a map BEFORE cancelling (the handles
    would be lost and the tasks left running), not merely cancel-before-clear
    by inspection."""
    sentinel = object()

    # Seed every plain dict / OrderedDict with a sentinel key.
    for name in _DICT_NAMES:
        getattr(mq, name)[("sentinel", name)] = sentinel
        assert getattr(mq, name), f"seed failed for {name}"

    # Seed each set.
    for name in _SET_NAMES:
        getattr(mq, name).add(("sentinel", name))
        assert getattr(mq, name), f"seed failed for {name}"

    # Schedule a REAL un-awaited task in each task map (incl. _route_workers).
    tasks: list[asyncio.Task[None]] = []
    for name in _TASK_MAP_NAMES:
        task = asyncio.create_task(asyncio.sleep(3600))
        tasks.append(task)
        getattr(mq, name)[("sentinel", name)] = task
        assert getattr(mq, name), f"seed failed for {name}"

    mq.reset_for_tests()

    # (a) Every map / set is empty.
    for name in _DICT_NAMES:
        assert getattr(mq, name) == {} or len(getattr(mq, name)) == 0, (
            f"{name} not cleared by reset_for_tests()"
        )
    for name in _SET_NAMES:
        assert len(getattr(mq, name)) == 0, f"{name} not cleared by reset_for_tests()"
    for name in _TASK_MAP_NAMES:
        assert len(getattr(mq, name)) == 0, f"{name} not cleared by reset_for_tests()"

    # (b) Each scheduled task was cancelled — no task survived uncancelled.
    for task in tasks:
        assert task.cancelled() or task.cancelling() > 0, (
            "a seeded task was not cancelled by reset_for_tests() "
            "(its map may have been cleared before the task was cancelled)"
        )

    # Let the cancellations propagate so no warning leaks into the next test.
    for task in tasks:
        with pytest.raises(asyncio.CancelledError):
            await task
