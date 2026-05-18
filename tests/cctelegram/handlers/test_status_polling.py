"""Tests for status_polling — Settings UI detection via the poller path.

Simulates the user workflow: /model is sent to Claude Code, the Settings
model picker renders in the terminal, and the status poller detects it
on its next 1s tick.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest

from cctelegram.handlers.message_sender import TopicSendOutcome, _classify_bad_request
from cctelegram.handlers.status_polling import update_status_message


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 999
    bot.send_message.return_value = sent_msg
    return bot


@pytest.fixture
def _clear_interactive_state():
    """Ensure interactive state is clean before and after each test."""
    from cctelegram.handlers import status_polling
    from cctelegram.handlers.interactive_ui import _interactive_mode, _interactive_msgs

    _interactive_mode.clear()
    _interactive_msgs.clear()
    status_polling._last_pane_capture.clear()
    status_polling._idle_state.clear()
    yield
    _interactive_mode.clear()
    _interactive_msgs.clear()
    status_polling._last_pane_capture.clear()
    status_polling._idle_state.clear()


@pytest.mark.usefixtures("_clear_interactive_state")
class TestStatusPollerSettingsDetection:
    """Simulate the status poller detecting a Settings UI in the terminal.

    This is the actual code path for /model: no JSONL tool_use entry exists,
    so the status poller (update_status_message) is the only detector.
    """

    @pytest.mark.asyncio
    async def test_settings_ui_detected_and_keyboard_sent(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """Poller captures Settings pane → handle_interactive_ui sends keyboard."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("cctelegram.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "cctelegram.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_handle_ui.return_value = True

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_handle_ui.assert_called_once_with(
                mock_bot, 1, window_id, 42, from_poller=True
            )

    @pytest.mark.asyncio
    async def test_idle_clears_stale_busy_after_delay(self, mock_bot: AsyncMock):
        """Pane with no spinner: wait IDLE_CLEAR_DELAY_SECONDS, then clear once.

        Regression: previously ``update_status_message`` simply skipped the
        clear path when ``parse_status_line`` returned ``None``, so the
        "🟡 Busy — … / Cooked for 2s" message hung around forever after Claude
        finished and its post-completion summary line rolled off.
        """
        from cctelegram.handlers import status_polling

        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        idle_pane = (
            "$ echo done\n"
            "done\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  [Opus 4.6] Context: 50%\n"
        )

        status_polling.reset_idle_counter(1, 42)
        fake_now = [1000.0]

        def fake_monotonic() -> float:
            return fake_now[0]

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "enqueue_status_update", new_callable=AsyncMock
            ) as mock_enqueue,
            patch.object(
                status_polling, "handle_interactive_ui", new_callable=AsyncMock
            ),
            patch.object(status_polling.time, "monotonic", side_effect=fake_monotonic),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=idle_pane)

            # First idle observation just records the timestamp.
            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )
            assert mock_enqueue.await_count == 0

            # Still inside the delay window — no clear yet.
            fake_now[0] += status_polling.IDLE_CLEAR_DELAY_SECONDS - 0.5
            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )
            assert mock_enqueue.await_count == 0

            # Past the delay — clears exactly once.
            fake_now[0] += 1.0
            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )
            assert mock_enqueue.await_count == 1
            args, kwargs = mock_enqueue.await_args
            assert args[3] is None
            assert kwargs.get("thread_id") == 42

            # Further idle polls don't re-trigger.
            fake_now[0] += 5.0
            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )
            assert mock_enqueue.await_count == 1

        status_polling.reset_idle_counter(1, 42)

    @pytest.mark.asyncio
    async def test_busy_status_resets_idle_state(self, mock_bot: AsyncMock):
        """A real active status resets the idle state, so a subsequent idle
        stretch must wait the full delay again before clearing."""
        from cctelegram.handlers import status_polling

        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        idle_pane = (
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
        )
        # "esc to interrupt" in the bottom chrome bar = Claude actively running.
        busy_pane = (
            "✻ Cooking for 2s\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt\n"
        )

        status_polling.reset_idle_counter(1, 42)
        fake_now = [1000.0]

        def fake_monotonic() -> float:
            return fake_now[0]

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "enqueue_status_update", new_callable=AsyncMock
            ) as mock_enqueue,
            patch.object(
                status_polling, "handle_interactive_ui", new_callable=AsyncMock
            ),
            patch.object(status_polling.time, "monotonic", side_effect=fake_monotonic),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)

            # Idle for a while.
            mock_tmux.capture_pane = AsyncMock(return_value=idle_pane)
            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )
            # Wave 2: bump past the WATCHDOG_INTERVAL so the next poll actually
            # scrapes the pane and sees the busy transition. Within the
            # watchdog window the cleanup-only path runs and the pane-derived
            # status doesn't refresh — the V2 indicator covers this gap via
            # JSONL events.
            fake_now[0] += status_polling.WATCHDOG_INTERVAL + 0.1

            # Active poll arrives — drops idle state, enqueues real status.
            mock_tmux.capture_pane = AsyncMock(return_value=busy_pane)
            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )
            assert mock_enqueue.await_args.args[3] == "Cooking for 2s"
            enqueue_count_after_busy = mock_enqueue.await_count

            # Idle again — must wait the FULL delay before clearing. The
            # capture below scrapes (watchdog reset by the busy poll → still
            # within window, but the in_interactive / V1 paths don't apply
            # here so we need to elapse the watchdog again to capture idle).
            fake_now[0] += status_polling.WATCHDOG_INTERVAL + 0.1
            mock_tmux.capture_pane = AsyncMock(return_value=idle_pane)
            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )
            fake_now[0] += status_polling.IDLE_CLEAR_DELAY_SECONDS - 0.5
            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )
            assert mock_enqueue.await_count == enqueue_count_after_busy

            # Cross the delay — clear fires.
            fake_now[0] += 1.0
            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )
            assert mock_enqueue.await_count == enqueue_count_after_busy + 1
            assert mock_enqueue.await_args.args[3] is None

        status_polling.reset_idle_counter(1, 42)

    @pytest.mark.asyncio
    async def test_post_completion_summary_treated_as_idle(self, mock_bot: AsyncMock):
        """A static "✻ Worked for 2s" line with a blank line above chrome
        is a post-completion summary, NOT an active status. Must NOT be
        re-enqueued as busy — Claude is actually idle.

        Regression: this is the exact pane state captured in the wild when
        "🟡 Busy — di-copilot-3 / Worked for 2s" hung around forever after
        Claude finished responding.
        """
        from cctelegram.handlers import status_polling

        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        # Spinner line, then BLANK line, then chrome — Claude's idle summary.
        post_completion_pane = (
            "⏺ Doing well, ready to help.\n"
            "\n"
            "✻ Worked for 2s\n"
            "\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
        )

        status_polling.reset_idle_counter(1, 42)
        fake_now = [1000.0]

        def fake_monotonic() -> float:
            return fake_now[0]

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "enqueue_status_update", new_callable=AsyncMock
            ) as mock_enqueue,
            patch.object(
                status_polling, "handle_interactive_ui", new_callable=AsyncMock
            ),
            patch.object(status_polling.time, "monotonic", side_effect=fake_monotonic),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=post_completion_pane)

            # First idle observation just records the timestamp.
            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )
            assert mock_enqueue.await_count == 0

            # Past the delay — clears.
            fake_now[0] += status_polling.IDLE_CLEAR_DELAY_SECONDS + 0.1
            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )
            assert mock_enqueue.await_count == 1
            assert mock_enqueue.await_args.args[3] is None

        status_polling.reset_idle_counter(1, 42)

    @pytest.mark.asyncio
    async def test_normal_pane_no_interactive_ui(self, mock_bot: AsyncMock):
        """Normal pane text → no handle_interactive_ui call, just status check."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        normal_pane = (
            "some output\n"
            "✻ Reading file\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  [Opus 4.6] Context: 50%\n"
        )

        with (
            patch("cctelegram.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "cctelegram.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
            patch(
                "cctelegram.handlers.status_polling.enqueue_status_update",
                new_callable=AsyncMock,
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=normal_pane)

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_handle_ui.assert_not_called()

    @pytest.mark.asyncio
    async def test_settings_ui_end_to_end_sends_telegram_keyboard(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """Full end-to-end: poller → is_interactive_ui → handle_interactive_ui
        → bot.send_message with keyboard.

        Uses real handle_interactive_ui (not mocked) to verify the full path.
        """
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        from cctelegram.handlers import attention

        attention.reset_for_tests()
        with (
            patch("cctelegram.handlers.status_polling.tmux_manager") as mock_tmux_poll,
            patch("cctelegram.handlers.interactive_ui.tmux_manager") as mock_tmux_ui,
            patch("cctelegram.handlers.interactive_ui.session_manager") as mock_sm,
            patch("cctelegram.handlers.attention.session_manager") as mock_sm_att,
        ):
            mock_tmux_poll.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux_poll.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_tmux_ui.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux_ui.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_sm.resolve_chat_id.return_value = 100
            mock_sm_att.resolve_chat_id.return_value = 100
            mock_sm_att.get_display_name.return_value = "etcircle-dev"

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            # The interactive keyboard send goes to the topic.
            keyboard_calls = [
                c
                for c in mock_bot.send_message.call_args_list
                if c.kwargs.get("reply_markup") is not None
            ]
            assert len(keyboard_calls) == 1
            kw = keyboard_calls[0].kwargs
            assert kw["chat_id"] == 100
            assert kw["message_thread_id"] == 42
            assert "Select model" in kw["text"]
            # Topic-first attention card lands in the same topic, not a DM.
            for call in mock_bot.send_message.call_args_list:
                assert call.kwargs["chat_id"] == 100, (
                    f"unexpected DM-shaped send_message: {call.kwargs}"
                )
        attention.reset_for_tests()


@pytest.mark.usefixtures("_clear_interactive_state")
class TestInteractiveModeRaceGuard:
    """The 1Hz poller must not clear a just-published interactive mode that
    has not yet rendered a Telegram message. ``bot.handle_new_message``
    sets ``_interactive_mode`` BEFORE awaiting the route's content queue
    and ``handle_interactive_ui``; the poller can tick during that window
    and would otherwise call ``clear_interactive_msg`` with ``msg_id=None``,
    dropping the AskUserQuestion card to plain-text fallback.
    """

    @pytest.mark.asyncio
    async def test_poll_does_not_clear_pending_interactive_mode(
        self, mock_bot: AsyncMock
    ):
        """Mode set but no msg_id yet → poller must skip the clear path."""
        from cctelegram.handlers import status_polling
        from cctelegram.handlers.interactive_ui import (
            _interactive_mode,
            _interactive_msgs,
        )

        window_id = "@24"
        user_id = 1
        thread_id = 42
        ikey = (user_id, thread_id)

        _interactive_mode[ikey] = window_id
        assert _interactive_msgs.get(ikey) is None

        mock_window = MagicMock()
        mock_window.window_id = window_id
        non_interactive_pane = (
            "some output\n"
            "✻ Reading file\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "  [Opus 4.6] Context: 50%\n"
        )

        with (
            patch("cctelegram.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "cctelegram.handlers.status_polling.clear_interactive_msg",
                new_callable=AsyncMock,
            ) as mock_clear,
            patch(
                "cctelegram.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle,
            patch(
                "cctelegram.handlers.status_polling.enqueue_status_update",
                new_callable=AsyncMock,
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=non_interactive_pane)

            await status_polling.update_status_message(
                mock_bot, user_id=user_id, window_id=window_id, thread_id=thread_id
            )

            mock_clear.assert_not_called()
            mock_handle.assert_not_called()

        assert _interactive_mode.get(ikey) == window_id

    @pytest.mark.asyncio
    async def test_poll_clears_when_interactive_msg_already_rendered(
        self, mock_bot: AsyncMock
    ):
        """Mode set AND msg_id present → poller clears as before."""
        from cctelegram.handlers import status_polling
        from cctelegram.handlers.interactive_ui import (
            _interactive_mode,
            _interactive_msgs,
        )

        window_id = "@24"
        user_id = 1
        thread_id = 42
        ikey = (user_id, thread_id)

        _interactive_mode[ikey] = window_id
        _interactive_msgs[ikey] = 12345

        mock_window = MagicMock()
        mock_window.window_id = window_id
        non_interactive_pane = "some output\n❯ \n  [Opus 4.6] Context: 50%\n"

        with (
            patch("cctelegram.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "cctelegram.handlers.status_polling.clear_interactive_msg",
                new_callable=AsyncMock,
            ) as mock_clear,
            patch(
                "cctelegram.handlers.status_polling.enqueue_status_update",
                new_callable=AsyncMock,
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=non_interactive_pane)

            await status_polling.update_status_message(
                mock_bot, user_id=user_id, window_id=window_id, thread_id=thread_id
            )

            mock_clear.assert_called_once_with(user_id, mock_bot, thread_id)


# ── Topic existence probe (status_poll_loop) ────────────────────────────────


class TestTopicErrorClassification:
    """The classifier maps Telegram BadRequest bodies into structured outcomes
    so reactive topic_send/topic_edit failures can route to emergency DMs and
    unbinding. Telegram uses several wordings for the same logical error;
    these tests pin the fragments so a Telegram-side rename can't silently
    re-route everything to ``OTHER``.
    """

    @pytest.mark.parametrize(
        "telegram_message",
        [
            "Bad Request: message thread not found",
            "MESSAGE THREAD NOT FOUND",  # case-insensitive
            "Bad Request: TOPIC_ID_INVALID",
            "Bad Request: topic not found",
        ],
    )
    def test_thread_not_found_variants_classified(self, telegram_message: str):
        outcome = _classify_bad_request(BadRequest(telegram_message))
        assert outcome is TopicSendOutcome.TOPIC_NOT_FOUND, (
            f"Telegram message {telegram_message!r} must classify as "
            f"TOPIC_NOT_FOUND so reactive failures route to emergency DMs"
        )

    def test_topic_closed_variant_classified(self):
        outcome = _classify_bad_request(BadRequest("Bad Request: TOPIC_CLOSED"))
        assert outcome is TopicSendOutcome.TOPIC_CLOSED

    def test_message_not_modified_classified(self):
        # Distinct outcome so attention.notify_waiting can short-circuit
        # benign no-op edits without falling through to a fresh card.
        outcome = _classify_bad_request(
            BadRequest("Bad Request: message is not modified")
        )
        assert outcome is TopicSendOutcome.MESSAGE_NOT_MODIFIED

    def test_unknown_bad_request_falls_through_to_other(self):
        outcome = _classify_bad_request(BadRequest("some unrelated error"))
        assert outcome is TopicSendOutcome.OTHER


class TestStatusPollLoopDoesNotProbeTelegram:
    """Regression: ``status_poll_loop`` must NOT call any Telegram API as a
    proactive existence probe. The previous implementation ran
    ``unpin_all_forum_topic_messages`` every 60s for every bound topic — but
    that endpoint is destructive on success (it clears any pinned messages in
    the topic), not a no-op. Topic existence is now detected reactively from
    real ``topic_send``/``topic_edit`` failures.
    """

    @pytest.mark.asyncio
    async def test_loop_iteration_does_not_call_unpin(self):
        from cctelegram.handlers import status_polling

        bot = AsyncMock()
        # If the test ever sees this called, the probe was reintroduced.
        bot.unpin_all_forum_topic_messages = AsyncMock()

        mock_window = MagicMock()
        mock_window.window_id = "@7"

        with (
            patch.object(status_polling, "session_manager") as mock_sm,
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "update_status_message", new_callable=AsyncMock
            ),
            patch.object(status_polling, "clear_topic_state", new_callable=AsyncMock),
        ):
            mock_sm.iter_thread_bindings.return_value = [(1, 42, "@7")]
            mock_sm.resolve_chat_id.return_value = -100123
            mock_sm.unbind_thread = MagicMock()
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)

            # Run one tick of the loop and cancel before the sleep returns.
            # status_poll_loop is `while True:` so we need a timeout.
            task = asyncio.create_task(status_polling.status_poll_loop(bot))
            try:
                await asyncio.wait_for(task, timeout=0.1)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        bot.unpin_all_forum_topic_messages.assert_not_called()


class TestActivityCallbackReArmsIdleState:
    """An activity event (transcript / inbound prompt delivery) for a route
    sitting at ``_idle_state[key] == "cleared"`` must pop the entry directly,
    without waiting for the next ``WATCHDOG_INTERVAL`` pane scrape.

    Regression: ``busy_indicator.register_activity_callback`` had a producer
    (``_fire_activity``) but no consumer, so the re-arm path documented in
    its docstring was inert. Sub-agent / quick tool turns that finished
    between two 10s pane scrapes left ``_idle_state[key] == "cleared"``
    permanently — ``_open_tools`` kept accumulating in ``busy_indicator``,
    state pinned at ``RUNNING_TOOL``, and ``typing_action_loop`` refreshed
    the native Telegram typing indicator forever.
    """

    @pytest.mark.asyncio
    async def test_transcript_event_re_arms_cleared_idle_state(self):
        from cctelegram.handlers import busy_indicator, status_polling
        from cctelegram.session_monitor import TranscriptEvent

        # Hermetic setup: reset clears _activity_callbacks, so re-register
        # the consumer that's normally bound at module import time.
        busy_indicator.reset_for_tests()
        busy_indicator.register_activity_callback(status_polling._on_busy_activity)
        status_polling._idle_state.clear()
        status_polling._idle_state[(1, 42)] = "cleared"

        event = TranscriptEvent(
            session_id="sess-1",
            role="assistant",
            block_type="tool_use",
            tool_use_id="t1",
            tool_name="Bash",
            stop_reason="tool_use",
            timestamp=None,
            text="",
            image_data=None,
        )
        await busy_indicator.on_transcript_event(event, [(1, 42, "@7")])

        assert (1, 42) not in status_polling._idle_state

        busy_indicator.reset_for_tests()
        status_polling._idle_state.clear()

    @pytest.mark.asyncio
    async def test_inbound_send_re_arms_cleared_idle_state(self):
        """``mark_inbound_sent`` (Telegram-originated prompt delivered to the
        tmux window) also fires the activity callback. Without this, /effort /
        /clear / arbitrary slash commands — which often produce no JSONL
        events at all — never get their cleared idle entry popped, so the
        next idle observation can't start a fresh delay timer."""
        from cctelegram.handlers import busy_indicator, status_polling

        busy_indicator.reset_for_tests()
        busy_indicator.register_activity_callback(status_polling._on_busy_activity)
        status_polling._idle_state.clear()
        status_polling._idle_state[(1, 42)] = "cleared"

        await busy_indicator.mark_inbound_sent((1, 42, "@7"))

        assert (1, 42) not in status_polling._idle_state

        busy_indicator.reset_for_tests()
        status_polling._idle_state.clear()
