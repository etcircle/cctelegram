"""Wave 4 regression tests: worker cancel race + RetryAfter resume state.

Covers review findings 2 and 10 (+ Hermes P2-3):

  - Finding 2: the worker's pending-branch cleanup (``for p in pending:
    await p`` under ``except BaseException: pass``) ate the worker's OWN
    CancelledError when ``teardown_route``'s ``worker.cancel()`` landed in
    the window between ``asyncio.wait`` returning and ``inflight.clear()``.
    Cancellation is one-shot — the worker resumed, ``await worker`` hung
    forever, and every future message for the topic was silently dropped.

  - Finding 10: ``_run_with_retry`` re-invokes the processor on RetryAfter
    with no resume state. Three loss sites: ``_tool_msg_ids`` popped before
    the edit awaited (retry posts a NEW bubble instead of editing in
    place); ``_agent_tool_ids`` popped before the promoted content awaited
    (retry loses the Agent routing key and falls into the activity digest
    with the wrong rendering — Hermes P2-3); multipart sends restart at
    part 0 (retry on part 2/3 re-sends part 1 as a duplicate).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import RetryAfter

from cctelegram.handlers import message_queue
from cctelegram.handlers.message_sender import TopicSendOutcome


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 999
    bot.send_message.return_value = sent_msg
    return bot


@pytest.fixture
def _clear_queue_state():
    """Drop per-route queue/worker state between tests via the module seam."""
    message_queue.reset_for_tests()
    yield
    message_queue.reset_for_tests()


@contextlib.contextmanager
def _noop_patches(*, patch_status: bool = True):
    """The standard boundary patches for driving _process_content_task.

    ``patch_status=False`` leaves ``_check_and_send_status`` unpatched so a
    test can install its own flaky version (the post-success RetryAfter
    shape from Hermes P2-1).
    """
    with contextlib.ExitStack() as stack:
        cms: list = []
        if patch_status:
            cms.append(
                patch.object(
                    message_queue, "_check_and_send_status", new_callable=AsyncMock
                )
            )
        for cm in (
            *cms,
            patch.object(
                message_queue, "_finalize_activity_digest", new_callable=AsyncMock
            ),
            patch.object(
                message_queue, "_maybe_attention_or_dismiss", new_callable=AsyncMock
            ),
            patch.object(
                message_queue,
                "_convert_status_to_content",
                AsyncMock(return_value=None),
            ),
            patch.object(
                message_queue, "_do_clear_status_message", new_callable=AsyncMock
            ),
            patch.object(
                message_queue.session_manager, "resolve_chat_id", return_value=1
            ),
        ):
            stack.enter_context(cm)
        yield


@pytest.mark.usefixtures("_clear_queue_state")
class TestWorkerCancelDuringPendingDrain:
    """Finding 2: a cancel landing in the pending-drain window must not be eaten."""

    @pytest.mark.asyncio
    async def test_teardown_completes_when_cancel_lands_in_drain_window(
        self, mock_bot: AsyncMock
    ):
        """Park the worker in its pending-branch drain, then teardown_route.

        Pre-fix: the worker's own CancelledError raises at ``await p`` and is
        swallowed by ``except BaseException: pass`` — teardown hangs forever
        and the route is permanently poisoned. Post-fix: the drain helper
        collects via ``asyncio.wait`` (which never raises the tasks'
        exceptions into the waiter), so the worker's own cancel propagates
        and teardown completes; the route then accepts new messages.
        """
        route = (1, 100, "@0")
        real_wait = asyncio.wait
        reached_drain = asyncio.Event()
        hold_gate = asyncio.Event()
        stub_holder: list[asyncio.Task] = []
        calls = {"n": 0}

        async def stubborn():
            # A pending-branch stand-in that absorbs its first cancel and
            # parks, so the worker deterministically suspends in the drain
            # (at `await p` pre-fix / inside the drain helper's wait
            # post-fix) with inflight still SET.
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                reached_drain.set()
                await hold_gate.wait()
                raise

        async def fake_wait(tasks, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                # Cancel the real branches so they don't leak, and hand the
                # worker a pending set containing only the stubborn task.
                for t in tasks:
                    t.cancel()
                stub = asyncio.ensure_future(stubborn())
                stub_holder.append(stub)
                # Let the stub start and park at its inner sleep, so the
                # worker's cancel is delivered INTO the running coroutine
                # (a never-started task would just die without absorbing it).
                await asyncio.sleep(0)
                return set(), {stub}
            return await real_wait(tasks, **kwargs)

        processed: list[message_queue.MessageTask] = []

        async def record_content(bot, user_id, task):
            processed.append(task)

        with (
            patch.object(message_queue.asyncio, "wait", side_effect=fake_wait),
            patch.object(
                message_queue, "_process_content_task", side_effect=record_content
            ),
        ):
            message_queue._get_or_create_route(mock_bot, route)
            # Wait until the worker is parked in the pending-drain window.
            await asyncio.wait_for(reached_drain.wait(), timeout=1.0)

            teardown = asyncio.create_task(
                message_queue.teardown_route(route, drop_pending=True)
            )
            try:
                await asyncio.wait_for(asyncio.shield(teardown), timeout=1.0)
            except asyncio.TimeoutError:
                # Pre-fix failure mode: unhang the worker so the test can
                # clean up, then fail loudly.
                hold_gate.set()
                worker = message_queue._route_workers.get(route)
                if worker is not None:
                    worker.cancel()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(teardown, timeout=1.0)
                pytest.fail(
                    "teardown_route hung — the worker swallowed its own "
                    "CancelledError in the pending-drain window"
                )

            # Route fully torn down; it must accept + process new messages.
            assert route not in message_queue._route_tearing_down
            await message_queue.enqueue_content_message(
                mock_bot,
                user_id=1,
                window_id="@0",
                parts=["after teardown"],
                content_type="text",
                thread_id=100,
            )
            queue = message_queue._route_queues[route]
            await asyncio.wait_for(queue.join(), timeout=1.0)
            assert len(processed) == 1
            assert processed[0].parts == ["after teardown"]

            # Release the stub so it can finish its (re-raised) cancellation.
            hold_gate.set()
            for stub in stub_holder:
                with contextlib.suppress(asyncio.CancelledError):
                    await stub


@pytest.mark.usefixtures("_clear_queue_state")
class TestRetryAfterResumeState:
    """Finding 10: RetryAfter retries must not lose per-task resume state."""

    @pytest.mark.asyncio
    async def test_retryafter_on_part2_sends_part1_exactly_once(
        self, mock_bot: AsyncMock
    ):
        """A RetryAfter on part 2 of 3 must not re-send part 1 on retry."""
        sent_texts: list[str] = []
        raised = {"done": False}

        async def fake_send(
            bot, *, op, user_id, chat_id, thread_id, window_id, text, **kw
        ):
            if text == "part2" and not raised["done"]:
                raised["done"] = True
                raise RetryAfter(timedelta(seconds=1))
            sent_texts.append(text)
            sent = MagicMock()
            sent.message_id = 100 + len(sent_texts)
            return sent, TopicSendOutcome.OK

        task = message_queue.MessageTask(
            task_type="content",
            window_id="@0",
            parts=["part1", "part2", "part3"],
            content_type="text",
            thread_id=100,
        )
        with (
            patch.object(message_queue, "topic_send", side_effect=fake_send),
            patch("asyncio.sleep", new_callable=AsyncMock),
            _noop_patches(),
        ):
            await message_queue._run_with_retry(
                mock_bot, 1, asyncio.Queue(), asyncio.Lock(), task
            )

        assert sent_texts.count("part1") == 1, f"part1 re-sent on retry: {sent_texts}"
        assert sent_texts == ["part1", "part2", "part3"]

    @pytest.mark.asyncio
    async def test_retryafter_on_tool_result_edit_retries_same_msg_id(
        self, mock_bot: AsyncMock
    ):
        """A RetryAfter on the tool_result edit must retry the SAME msg_id."""
        edit_ids: list[int] = []
        send_texts: list[str] = []
        raised = {"done": False}

        async def fake_edit(
            bot, *, op, user_id, chat_id, thread_id, window_id, message_id, text, **kw
        ):
            if not raised["done"]:
                raised["done"] = True
                raise RetryAfter(timedelta(seconds=1))
            edit_ids.append(message_id)
            return TopicSendOutcome.OK

        async def fake_send(
            bot, *, op, user_id, chat_id, thread_id, window_id, text, **kw
        ):
            send_texts.append(text)
            sent = MagicMock()
            sent.message_id = 4242
            return sent, TopicSendOutcome.OK

        message_queue._tool_msg_ids[("tu-ra-1", 1, 100)] = 31337
        task = message_queue.MessageTask(
            task_type="content",
            window_id="@0",
            parts=["✅ done"],
            content_type="tool_result",
            tool_use_id="tu-ra-1",
            thread_id=100,
        )
        with (
            patch.object(message_queue, "topic_edit", side_effect=fake_edit),
            patch.object(message_queue, "topic_send", side_effect=fake_send),
            patch.object(message_queue.attention, "dismiss", new_callable=AsyncMock),
            patch("asyncio.sleep", new_callable=AsyncMock),
            _noop_patches(),
        ):
            # First attempt raises out of the edit (the retry wrapper would
            # catch this); the retry re-invokes the processor on the SAME
            # task — exactly what _run_with_retry does on RetryAfter.
            with pytest.raises(RetryAfter):
                await message_queue._process_content_task(mock_bot, 1, task)
            await message_queue._process_content_task(mock_bot, 1, task)

        assert send_texts == [], (
            "tool_result posted a NEW bubble on retry instead of editing "
            f"in place: {send_texts}"
        )
        assert edit_ids == [31337], f"retry did not edit the same msg_id: {edit_ids}"
        # The id is consumed by the successful edit.
        assert ("tu-ra-1", 1, 100) not in message_queue._tool_msg_ids

    @pytest.mark.asyncio
    async def test_retryafter_on_agent_tool_result_preserves_context(
        self, mock_bot: AsyncMock
    ):
        """A RetryAfter mid-Agent-promotion must keep the routing/render context.

        Pre-fix: ``_process_agent_task`` pops ``_agent_tool_ids`` before
        awaiting the promoted content task, so the retry's
        ``_is_agent_tool_result`` misses, the task re-routes to the generic
        activity digest, and the recorded tool_use bubble is never edited
        with the Subagent rendering.
        """
        edit_calls: list[dict] = []
        send_calls: list[str] = []
        activity_calls: list[message_queue.MessageTask] = []
        raised = {"done": False}

        async def fake_send(
            bot, *, op, user_id, chat_id, thread_id, window_id, text, **kw
        ):
            send_calls.append(text)
            sent = MagicMock()
            sent.message_id = 500
            return sent, TopicSendOutcome.OK

        async def fake_edit(
            bot, *, op, user_id, chat_id, thread_id, window_id, message_id, text, **kw
        ):
            if not raised["done"]:
                raised["done"] = True
                raise RetryAfter(timedelta(seconds=1))
            edit_calls.append({"message_id": message_id, "text": text})
            return TopicSendOutcome.OK

        async def record_activity(bot, user_id, task):
            activity_calls.append(task)

        with (
            patch.object(message_queue, "topic_send", side_effect=fake_send),
            patch.object(message_queue, "topic_edit", side_effect=fake_edit),
            patch.object(
                message_queue, "_process_activity_task", side_effect=record_activity
            ),
            patch.object(
                message_queue,
                "_bump_agent_activity_counter",
                new_callable=AsyncMock,
            ),
            patch.object(message_queue.attention, "dismiss", new_callable=AsyncMock),
            patch("asyncio.sleep", new_callable=AsyncMock),
            _noop_patches(),
        ):
            # 1. Agent tool_use — promoted top-level send, records both maps.
            await message_queue._run_with_retry(
                mock_bot,
                1,
                asyncio.Queue(),
                asyncio.Lock(),
                message_queue.MessageTask(
                    task_type="content",
                    window_id="@0",
                    parts=["dispatch"],
                    content_type="tool_use",
                    tool_use_id="tu-agent-1",
                    tool_name="Agent",
                    tool_input={
                        "subagent_type": "researcher",
                        "description": "dig deep",
                    },
                    thread_id=100,
                ),
            )
            assert message_queue._tool_msg_ids[("tu-agent-1", 1, 100)] == 500
            assert ("tu-agent-1", 1, 100) in message_queue._agent_tool_ids

            # 2. Agent tool_result — first edit raises RetryAfter, retry must
            # re-route through the Agent path with the recorded input intact.
            await message_queue._run_with_retry(
                mock_bot,
                1,
                asyncio.Queue(),
                asyncio.Lock(),
                message_queue.MessageTask(
                    task_type="content",
                    window_id="@0",
                    text="all done",
                    parts=["all done"],
                    content_type="tool_result",
                    tool_use_id="tu-agent-1",
                    thread_id=100,
                ),
            )

        assert activity_calls == [], (
            "Agent tool_result retry fell into the generic activity digest — "
            "routing context lost"
        )
        assert len(edit_calls) == 1, (
            f"Agent tool_result was not edited in place on retry: {edit_calls}"
        )
        assert edit_calls[0]["message_id"] == 500
        assert "researcher" in edit_calls[0]["text"], (
            "render context (subagent_type) lost on retry"
        )
        assert "dig deep" in edit_calls[0]["text"], (
            "render context (description) lost on retry"
        )
        # The routing key is consumed only after the promotion succeeded.
        assert ("tu-agent-1", 1, 100) not in message_queue._agent_tool_ids

    @pytest.mark.asyncio
    async def test_retryafter_after_agent_tool_use_send_does_not_resend(
        self, mock_bot: AsyncMock
    ):
        """Hermes P2-1 (a): a RetryAfter AFTER the Agent tool_use bubble was
        successfully sent must not re-send the bubble on retry.

        Pre-fix: ``_process_agent_task`` mints a FRESH promoted MessageTask on
        every invocation, so the retry's promoted task restarts at
        ``parts_sent = 0`` and sends the "Subagent dispatched" bubble twice.
        """
        send_texts: list[str] = []
        status_calls = {"n": 0}

        async def fake_send(
            bot, *, op, user_id, chat_id, thread_id, window_id, text, **kw
        ):
            send_texts.append(text)
            sent = MagicMock()
            sent.message_id = 700
            return sent, TopicSendOutcome.OK

        async def flaky_status(bot, user_id, wid, thread_id):
            # Raises AFTER the send loop delivered the bubble — the
            # post-success await Hermes's repro exercises.
            status_calls["n"] += 1
            if status_calls["n"] == 1:
                raise RetryAfter(timedelta(seconds=1))

        with (
            patch.object(message_queue, "topic_send", side_effect=fake_send),
            patch.object(
                message_queue, "_check_and_send_status", side_effect=flaky_status
            ),
            patch.object(
                message_queue,
                "_bump_agent_activity_counter",
                new_callable=AsyncMock,
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
            _noop_patches(patch_status=False),
        ):
            await message_queue._run_with_retry(
                mock_bot,
                1,
                asyncio.Queue(),
                asyncio.Lock(),
                message_queue.MessageTask(
                    task_type="content",
                    window_id="@0",
                    parts=["dispatch"],
                    content_type="tool_use",
                    tool_use_id="tu-agent-2",
                    tool_name="Agent",
                    tool_input={
                        "subagent_type": "researcher",
                        "description": "dig",
                    },
                    thread_id=100,
                ),
            )

        assert len(send_texts) == 1, (
            f"Agent tool_use bubble re-sent on retry: {send_texts}"
        )
        assert "researcher" in send_texts[0]
        # The edit target stays recorded for the eventual tool_result.
        assert message_queue._tool_msg_ids[("tu-agent-2", 1, 100)] == 700

    @pytest.mark.asyncio
    async def test_retryafter_after_agent_tool_result_edit_does_not_duplicate(
        self, mock_bot: AsyncMock
    ):
        """Hermes P2-1 (b): a RetryAfter AFTER the Agent tool_result was
        successfully edited into the bubble must neither re-edit nor send a
        duplicate result bubble on retry.

        Pre-fix interleaving: the first promoted task edits msg 900 OK, pops
        ``_tool_msg_ids``, saturates ITS OWN ``parts_sent`` — then
        ``_check_and_send_status`` raises. The retry mints a fresh promoted
        task with ``parts_sent = 0``; ``_tool_msg_ids`` is gone, so the
        result falls through to a fresh duplicate send.
        """
        edit_ids: list[int] = []
        send_texts: list[str] = []
        status_calls = {"n": 0}

        async def fake_edit(
            bot, *, op, user_id, chat_id, thread_id, window_id, message_id, text, **kw
        ):
            edit_ids.append(message_id)
            return TopicSendOutcome.OK

        async def fake_send(
            bot, *, op, user_id, chat_id, thread_id, window_id, text, **kw
        ):
            send_texts.append(text)
            sent = MagicMock()
            sent.message_id = 4242
            return sent, TopicSendOutcome.OK

        async def flaky_status(bot, user_id, wid, thread_id):
            status_calls["n"] += 1
            if status_calls["n"] == 1:
                raise RetryAfter(timedelta(seconds=1))

        message_queue._tool_msg_ids[("tu-agent-3", 1, 100)] = 900
        message_queue._agent_tool_ids[("tu-agent-3", 1, 100)] = {
            "subagent_type": "researcher",
            "description": "dig",
        }
        with (
            patch.object(message_queue, "topic_edit", side_effect=fake_edit),
            patch.object(message_queue, "topic_send", side_effect=fake_send),
            patch.object(
                message_queue, "_check_and_send_status", side_effect=flaky_status
            ),
            patch.object(
                message_queue,
                "_bump_agent_activity_counter",
                new_callable=AsyncMock,
            ),
            patch.object(message_queue.attention, "dismiss", new_callable=AsyncMock),
            patch("asyncio.sleep", new_callable=AsyncMock),
            _noop_patches(patch_status=False),
        ):
            await message_queue._run_with_retry(
                mock_bot,
                1,
                asyncio.Queue(),
                asyncio.Lock(),
                message_queue.MessageTask(
                    task_type="content",
                    window_id="@0",
                    text="done",
                    parts=["done"],
                    content_type="tool_result",
                    tool_use_id="tu-agent-3",
                    thread_id=100,
                ),
            )

        assert send_texts == [], (
            f"Agent tool_result sent a duplicate bubble on retry: {send_texts}"
        )
        assert edit_ids == [900], (
            f"Agent tool_result must edit exactly once: {edit_ids}"
        )
        # Routing key consumed only after the promotion fully succeeded.
        assert ("tu-agent-3", 1, 100) not in message_queue._agent_tool_ids
