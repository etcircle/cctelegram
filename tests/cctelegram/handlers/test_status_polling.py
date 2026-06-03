"""Tests for status_polling — Settings UI detection via the poller path.

Simulates the user workflow: /model is sent to Claude Code, the Settings
model picker renders in the terminal, and the status poller detects it
on its next 1s tick.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest

from cctelegram.route_runtime import RunState
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

    from cctelegram import route_runtime

    _interactive_mode.clear()
    _interactive_msgs.clear()
    status_polling._last_pane_capture.clear()
    status_polling._last_published_ui_hash.clear()
    status_polling._absent_streak.clear()
    status_polling._prev_run_state.clear()
    route_runtime.reset_for_tests()
    yield
    _interactive_mode.clear()
    _interactive_msgs.clear()
    status_polling._last_pane_capture.clear()
    status_polling._last_published_ui_hash.clear()
    status_polling._absent_streak.clear()
    status_polling._prev_run_state.clear()
    route_runtime.reset_for_tests()


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
    async def test_first_picker_publish_drains_route_content_queue_before_render(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """First poller picker waits for same-route content before rendering."""
        from cctelegram.handlers import status_polling

        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        events: list[str] = []

        async def join_content_queue() -> None:
            events.append("join")

        async def render_picker(*_args, **_kwargs) -> bool:
            events.append("render")
            return True

        content_queue = MagicMock()
        content_queue.join = AsyncMock(side_effect=join_content_queue)

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "get_content_queue", return_value=content_queue
            ) as mock_get_queue,
            patch.object(
                status_polling,
                "handle_interactive_ui",
                new_callable=AsyncMock,
                side_effect=render_picker,
            ) as mock_handle_ui,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_settings)

            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_get_queue.assert_called_once_with((1, 42, window_id))
            content_queue.join.assert_awaited_once()
            mock_handle_ui.assert_awaited_once_with(
                mock_bot, 1, window_id, 42, from_poller=True
            )
            assert events == ["join", "render"]

    @pytest.mark.asyncio
    async def test_picker_refresh_skips_content_queue_barrier(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """Already-published picker refreshes must not drain content again."""
        from cctelegram.handlers import status_polling
        from cctelegram.handlers.interactive_ui import _interactive_mode

        window_id = "@5"
        route = (1, 42, window_id)
        mock_window = MagicMock()
        mock_window.window_id = window_id
        _interactive_mode[(1, 42)] = window_id
        status_polling._last_published_ui_hash[route] = "old-picker-hash"

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(status_polling, "get_content_queue") as mock_get_queue,
            patch.object(
                status_polling, "handle_interactive_ui", new_callable=AsyncMock
            ) as mock_handle_ui,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_settings)

            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_get_queue.assert_not_called()
            mock_handle_ui.assert_awaited_once_with(
                mock_bot, 1, window_id, 42, from_poller=True
            )

    @pytest.mark.asyncio
    async def test_same_hash_idle_refreshes_pick_token_deadlines(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """D3-β: a same-hash idle tick (live card, no re-render) re-stamps the
        route's pick-token deadlines so an idle tap never finds a pruned token."""
        import hashlib

        from cctelegram.handlers import status_polling
        from cctelegram.handlers.interactive_ui import _interactive_mode
        from cctelegram.handlers.status_polling import extract_interactive_content

        window_id = "@5"
        route = (1, 42, window_id)
        mock_window = MagicMock()
        mock_window.window_id = window_id
        _interactive_mode[(1, 42)] = window_id
        # Pin the published hash to the LIVE pane's hash so the same-hash
        # early-return fires (no re-render).
        ui_content = extract_interactive_content(sample_pane_settings)
        assert ui_content is not None
        ui_hash = hashlib.sha256(ui_content.content.encode("utf-8")).hexdigest()
        status_polling._last_published_ui_hash[route] = ui_hash

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling.pick_token,
                "refresh_route_deadlines",
                new_callable=AsyncMock,
            ) as mock_refresh,
            patch.object(
                status_polling, "handle_interactive_ui", new_callable=AsyncMock
            ) as mock_handle_ui,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_settings)

            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_handle_ui.assert_not_awaited()  # same-hash → no re-render
            mock_refresh.assert_awaited_once_with(
                1,
                42,
                window_id,
                min_remaining_s=status_polling._DEADLINE_REFRESH_MARGIN_S,
            )

    @pytest.mark.asyncio
    async def test_first_picker_content_queue_timeout_still_renders(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """The first-publish barrier is bounded: timeout logs and renders anyway."""
        from cctelegram.handlers import status_polling

        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        content_queue = MagicMock()
        content_queue.join = AsyncMock(side_effect=asyncio.TimeoutError)

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "get_content_queue", return_value=content_queue
            ),
            patch.object(
                status_polling, "handle_interactive_ui", new_callable=AsyncMock
            ) as mock_handle_ui,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_settings)

            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            content_queue.join.assert_awaited_once()
            mock_handle_ui.assert_awaited_once_with(
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
        """Mode set AND msg_id present → poller clears after the absent streak
        threshold is reached. The first ABSENT_STREAK_THRESHOLD-1 ticks must
        defer the clear (hysteresis); the threshold-th tick fires the delete.
        """
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

            # Drive THRESHOLD consecutive absent polls; only the last fires the
            # clear. Sub-threshold ticks must defer.
            for tick in range(status_polling.ABSENT_STREAK_THRESHOLD):
                await status_polling.update_status_message(
                    mock_bot,
                    user_id=user_id,
                    window_id=window_id,
                    thread_id=thread_id,
                )
                if tick + 1 < status_polling.ABSENT_STREAK_THRESHOLD:
                    mock_clear.assert_not_called()

            mock_clear.assert_called_once_with(
                user_id, mock_bot, thread_id, tombstone=True
            )


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


# The former ``TestActivityCallbackReArmsIdleState`` class exercised the
# legacy ``busy_indicator.register_activity_callback`` → ``_idle_state``
# re-arm channel. route_runtime now owns that re-arm inline
# (``_rearm_pane_idle_in_place``); the equivalent coverage lives in
# ``test_route_runtime.py`` (``test_transcript_activity_rearms_pending_clear``
# / ``test_inbound_sent_rearms_pending_clear``) and in the
# ``test_transcript_activity_cancels_pending_clear`` seam test below.


_IDLE_PANE = (
    "$ echo done\n"
    "done\n"
    "──────────────────────────────────────\n"
    "❯ \n"
    "──────────────────────────────────────\n"
    "  [Opus 4.6] Context: 50%\n"
)
# "esc to interrupt" in the bottom chrome bar = Claude actively running.
_BUSY_PANE = (
    "✻ Cooking for 2s\n"
    "──────────────────────────────────────\n"
    "❯ \n"
    "──────────────────────────────────────\n"
    "  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt\n"
)


@pytest.mark.usefixtures("_clear_interactive_state")
class TestRouteRuntimeIdleClearDebounce:
    """The debounced "🟡 Busy" card clear is owned by ``route_runtime``.
    These tests drive the public ``update_status_message`` seam and assert
    the debounce timing:

      - the card stays up during ``IDLE_CLEAR_DELAY_SECONDS`` of idle,
      - clears exactly once after the delay,
      - any activity (transcript / inbound) during the window cancels the
        pending clear (c313657 guard).
    """

    @pytest.fixture(autouse=True)
    def _reset_runtime(self):
        from cctelegram import route_runtime

        route_runtime.reset_for_tests()
        yield
        route_runtime.reset_for_tests()

    @pytest.mark.asyncio
    async def test_idle_clears_after_delay_once_and_legacy_state_untouched(
        self, mock_bot: AsyncMock
    ):
        from cctelegram import route_runtime
        from cctelegram.handlers import status_polling

        window_id = "@5"
        route = (1, 42, window_id)
        mock_window = MagicMock()
        mock_window.window_id = window_id
        fake_now = [1000.0]

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "enqueue_status_update", new_callable=AsyncMock
            ) as mock_enqueue,
            patch.object(
                status_polling, "handle_interactive_ui", new_callable=AsyncMock
            ),
            patch.object(
                status_polling.time, "monotonic", side_effect=lambda: fake_now[0]
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=_IDLE_PANE)

            # First idle observation arms the route_runtime deadline only.
            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )
            assert mock_enqueue.await_count == 0
            snap = route_runtime.snapshot(route)
            assert (
                snap.pane_idle_clear_at
                == 1000.0 + status_polling.IDLE_CLEAR_DELAY_SECONDS
            )

            # Still inside the delay window — no clear.
            fake_now[0] += status_polling.IDLE_CLEAR_DELAY_SECONDS - 0.5
            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )
            assert mock_enqueue.await_count == 0

            # Past the delay — clears exactly once with text=None.
            fake_now[0] += 1.0
            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )
            assert mock_enqueue.await_count == 1
            args, _kwargs = mock_enqueue.await_args
            assert args[3] is None
            # Run-state reconciled to IDLE_CLEARED.
            assert route_runtime.snapshot(route).run_state is RunState.IDLE_CLEARED

            # Further idle polls do not re-trigger.
            fake_now[0] += 5.0
            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )
            assert mock_enqueue.await_count == 1

    @pytest.mark.asyncio
    async def test_busy_pane_cancels_pending_clear(self, mock_bot: AsyncMock):
        """A busy pane mid-debounce cancels the pending route_runtime clear,
        so a subsequent idle stretch must wait the full delay again."""
        from cctelegram import route_runtime
        from cctelegram.handlers import status_polling

        window_id = "@5"
        route = (1, 42, window_id)
        mock_window = MagicMock()
        mock_window.window_id = window_id
        fake_now = [1000.0]

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "enqueue_status_update", new_callable=AsyncMock
            ) as mock_enqueue,
            patch.object(
                status_polling, "handle_interactive_ui", new_callable=AsyncMock
            ),
            patch.object(
                status_polling.time, "monotonic", side_effect=lambda: fake_now[0]
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)

            # Idle: arm the deadline.
            mock_tmux.capture_pane = AsyncMock(return_value=_IDLE_PANE)
            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )
            assert route_runtime.snapshot(route).pane_idle_clear_at is not None

            # Bump past WATCHDOG so the next tick actually scrapes the pane,
            # which now shows Claude running again → cancels the deadline.
            fake_now[0] += status_polling.WATCHDOG_INTERVAL + 0.1
            mock_tmux.capture_pane = AsyncMock(return_value=_BUSY_PANE)
            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )
            assert route_runtime.snapshot(route).pane_idle_clear_at is None
            # The busy poll enqueued a live status line (not a clear).
            assert mock_enqueue.await_count == 1
            assert mock_enqueue.await_args[0][3] is not None

            # Idle again — re-arm from this point; the old elapsed time
            # must not count, so no clear until a fresh full delay.
            fake_now[0] += status_polling.WATCHDOG_INTERVAL + 0.1
            mock_tmux.capture_pane = AsyncMock(return_value=_IDLE_PANE)
            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )
            armed_at = route_runtime.snapshot(route).pane_idle_clear_at
            assert armed_at == fake_now[0] + status_polling.IDLE_CLEAR_DELAY_SECONDS

    @pytest.mark.asyncio
    async def test_transcript_activity_cancels_pending_clear(self, mock_bot: AsyncMock):
        """c313657 guard at the seam: a transcript event during the debounce
        window cancels the pending clear, so the next idle tick does NOT
        clear the card (it re-arms)."""
        from cctelegram import route_runtime, transcript_event_adapter
        from cctelegram.handlers import status_polling
        from cctelegram.session_monitor import TranscriptEvent

        window_id = "@5"
        route = (1, 42, window_id)
        mock_window = MagicMock()
        mock_window.window_id = window_id
        fake_now = [1000.0]

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "enqueue_status_update", new_callable=AsyncMock
            ) as mock_enqueue,
            patch.object(
                status_polling, "handle_interactive_ui", new_callable=AsyncMock
            ),
            patch.object(
                status_polling.time, "monotonic", side_effect=lambda: fake_now[0]
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=_IDLE_PANE)

            # Arm the deadline.
            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )
            assert route_runtime.snapshot(route).pane_idle_clear_at is not None

            # Real transcript activity lands during the debounce window.
            await transcript_event_adapter.dispatch_transcript_event(
                TranscriptEvent(
                    session_id="sess-1",
                    role="assistant",
                    block_type="tool_use",
                    tool_use_id="t1",
                    tool_name="Bash",
                    stop_reason="tool_use",
                    timestamp=None,
                    text="",
                    image_data=None,
                ),
                [route],
            )
            # Pending clear cancelled.
            assert route_runtime.snapshot(route).pane_idle_clear_at is None

            # Next pane-scraping idle tick, past the ORIGINAL deadline: must
            # NOT clear — it re-arms from now. (Bump past WATCHDOG so the
            # pane is actually scraped instead of the cleanup-only path,
            # which deliberately never arms without pane confirmation.)
            fake_now[0] += status_polling.WATCHDOG_INTERVAL + 1.0
            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )
            assert mock_enqueue.await_count == 0
            re_armed = route_runtime.snapshot(route).pane_idle_clear_at
            assert re_armed == fake_now[0] + status_polling.IDLE_CLEAR_DELAY_SECONDS

    @pytest.mark.asyncio
    async def test_process_idle_clear_only_never_arms(self, mock_bot: AsyncMock):
        """The watchdog-skipped cleanup path commits a due deadline but never
        arms one (no pane confirmation)."""
        from cctelegram import route_runtime
        from cctelegram.handlers import status_polling

        window_id = "@5"
        route = (1, 42, window_id)
        fake_now = [1000.0]

        with (
            patch.object(
                status_polling, "enqueue_status_update", new_callable=AsyncMock
            ) as mock_enqueue,
            patch.object(
                status_polling.time, "monotonic", side_effect=lambda: fake_now[0]
            ),
        ):
            # No deadline armed → cleanup-only path is a no-op.
            await status_polling._process_idle_clear_only(
                mock_bot, 1, window_id, 42, skip_status=False
            )
            assert mock_enqueue.await_count == 0
            assert route_runtime.snapshot(route).pane_idle_clear_at is None

            # Arm a deadline (as a prior confirmed-idle tick would), advance
            # past it, then the cleanup path commits the clear once.
            route_runtime.arm_pane_idle_clear(route, now=1000.0)
            fake_now[0] += status_polling.IDLE_CLEAR_DELAY_SECONDS + 0.1
            await status_polling._process_idle_clear_only(
                mock_bot, 1, window_id, 42, skip_status=False
            )
            assert mock_enqueue.await_count == 1
            assert mock_enqueue.await_args[0][3] is None
            assert route_runtime.snapshot(route).run_state is RunState.IDLE_CLEARED


@pytest.mark.usefixtures("_clear_interactive_state")
class TestInteractiveUiTransitionRefresh:
    """Back-to-back AskUserQuestion (Q2 → Q3) must refresh Telegram via the
    poller path even when interactive_mode is already set for the route.

    Background: Claude Code buffers AskUserQuestion ``tool_use`` JSONL lines
    until the user answers, so when Q2 transitions to Q3 the bot can't
    rely on the JSONL-driven dispatch path to publish Q3. The poller has
    to detect the pane content change and refresh. Without the content-
    hash dedup added by this regression, the in-interactive-mode early-
    return would keep the stale Q2 keyboard pinned to Telegram while Q3
    is already live on the pane — exactly the bug observed in production
    on 2026-05-19 (CodeGraphAgent topic, 18-minute Q3 delivery delay).
    """

    _Q2_PANE = (
        "Q2 — Status Quo: pick every pain that bites\n"
        " > 1. Tool-surface bloat\n"
        "   2. Infra friction\n"
        "   3. Output bloat\n"
        "   4. Repo handling\n"
        "\n"
        " Enter to select · Tab/Arrow keys to navigate · Esc to cancel\n"
    )
    _Q3_PANE = (
        "Q3 — Desperate Specificity: name the human\n"
        " > 1. Agent power-user\n"
        "   2. Legacy-codebase inheritor\n"
        "   3. Agent-harness builder\n"
        "\n"
        " Enter to select · Tab/Arrow keys to navigate · Esc to cancel\n"
    )

    @pytest.mark.asyncio
    async def test_pane_content_change_refires_handle_interactive_ui(
        self, mock_bot: AsyncMock
    ):
        """Two consecutive ticks: first shows Q2, second shows Q3.
        handle_interactive_ui must be called BOTH times so Telegram gets the
        new question. interactive_mode is pre-set to simulate the live state
        after Q2 was already published.
        """
        from cctelegram.handlers import status_polling
        from cctelegram.handlers.interactive_ui import (
            _interactive_mode,
            _interactive_msgs,
        )

        window_id = "@7"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        # Simulate: Q2 already published, interactive_mode set, msg id known.
        _interactive_mode[(1, 42)] = window_id
        _interactive_msgs[(1, 42)] = 12345

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "handle_interactive_ui", new_callable=AsyncMock
            ) as mock_handle_ui,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_handle_ui.return_value = True

            # Tick 1: pane shows Q2. Hash not yet stored → publish.
            mock_tmux.capture_pane = AsyncMock(return_value=self._Q2_PANE)
            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )
            assert mock_handle_ui.await_count == 1

            # Tick 2: pane STILL shows Q2. Hash matches → no republish.
            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )
            assert mock_handle_ui.await_count == 1

            # Tick 3: pane transitioned to Q3. Hash differs → republish.
            mock_tmux.capture_pane = AsyncMock(return_value=self._Q3_PANE)
            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )
            assert mock_handle_ui.await_count == 2

            # Tick 4: still Q3. Hash matches the just-stored Q3 → no
            # republish (regression guard against an unconditional refresh
            # turning every tick into an edit).
            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )
            assert mock_handle_ui.await_count == 2

    @pytest.mark.asyncio
    async def test_ui_clear_drops_stored_hash(self, mock_bot: AsyncMock):
        """When the pane transitions from interactive UI to no UI, the stored
        hash is dropped so a subsequent UI is treated as fresh, not stale."""
        from cctelegram.handlers import status_polling
        from cctelegram.handlers.interactive_ui import (
            _interactive_mode,
            _interactive_msgs,
        )

        window_id = "@7"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        route = (1, 42, window_id)

        _interactive_mode[(1, 42)] = window_id
        _interactive_msgs[(1, 42)] = 12345

        no_ui_pane = (
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
        )

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "handle_interactive_ui", new_callable=AsyncMock
            ) as mock_handle_ui,
            patch.object(
                status_polling, "clear_interactive_msg", new_callable=AsyncMock
            ),
            patch.object(status_polling, "has_interactive_surface", return_value=True),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_handle_ui.return_value = True

            # Tick 1: Q2 shown, hash gets stored.
            mock_tmux.capture_pane = AsyncMock(return_value=self._Q2_PANE)
            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )
            assert route in status_polling._last_published_ui_hash

            # Ticks 2 → THRESHOLD: pane goes UI-less. interactive_mode still
            # set for the window so the clear-path runs, but hysteresis defers
            # until the streak threshold is reached. The hash is dropped on
            # the same poll that fires the clear, not before.
            mock_tmux.capture_pane = AsyncMock(return_value=no_ui_pane)
            for _ in range(status_polling.ABSENT_STREAK_THRESHOLD):
                await status_polling.update_status_message(
                    mock_bot, user_id=1, window_id=window_id, thread_id=42
                )
            assert route not in status_polling._last_published_ui_hash


@pytest.mark.usefixtures("_clear_interactive_state")
class TestAbsentStreakHysteresis:
    """Regression: a transient single-tick absent observation must not destroy
    a still-live interactive card. The 2026-05-19 22:30 → 2026-05-20 00:15:23
    cgc-fork incident proved that a long multi-Q AskUserQuestion on the
    Submit-confirmation step can render one redraw frame where
    ``extract_interactive_content`` returns None (visible-only capture, top
    tab anchor + bottom picker footer both off-screen) — and the prior
    fire-on-first-absent code path deleted msg 32835 ~3 minutes BEFORE the
    JSONL ``tool_result`` was flushed, leaving the user staring at a live
    picker on the pane with no Telegram card to dispatch from.
    """

    _LIVE_AUQ_PANE = (
        " Q1 — Default value for Config.calls_batch_size?\n"
        " ❯ 1. 2000 (recommended)\n"
        "   2. 5000\n"
        "   3. 500\n"
        " Enter to select · Tab/Arrow keys to navigate · Esc to cancel\n"
    )
    _BAD_FRAME_PANE = (
        # Bottom-of-pane mid-redraw: TaskList rows visible, picker anchors gone.
        # This is the exact shape observed at 2026-05-20 00:15:22.993 on @37.
        "  ◻ Implement batched _create_function_calls › blocked by #2\n"
        "  ◻ Update tests to cover semantic equivalence  › blocked by #3\n"
        "  ◻ Re-run proving runs, record v1.1 baseline   › blocked by #4\n"
    )

    @pytest.mark.asyncio
    async def test_single_absent_poll_does_not_clear_card(self, mock_bot: AsyncMock):
        """One bad-frame poll → defer, do NOT call ``clear_interactive_msg``."""
        from cctelegram.handlers import status_polling
        from cctelegram.handlers.interactive_ui import (
            _interactive_mode,
            _interactive_msgs,
        )

        window_id = "@37"
        user_id = 6427984308
        thread_id = 10636
        ikey = (user_id, thread_id)
        route = (user_id, thread_id, window_id)
        _interactive_mode[ikey] = window_id
        _interactive_msgs[ikey] = 32835  # the destroyed card in the real incident

        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "clear_interactive_msg", new_callable=AsyncMock
            ) as mock_clear,
            patch.object(
                status_polling, "handle_interactive_ui", new_callable=AsyncMock
            ),
            patch.object(
                status_polling, "enqueue_status_update", new_callable=AsyncMock
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=self._BAD_FRAME_PANE)

            await status_polling.update_status_message(
                mock_bot, user_id=user_id, window_id=window_id, thread_id=thread_id
            )

            mock_clear.assert_not_called()
            assert status_polling._absent_streak.get(route) == 1

    @pytest.mark.asyncio
    async def test_side_file_live_blocks_tombstone_indefinitely(
        self, mock_bot: AsyncMock
    ):
        """ROOT-CAUSE regression (2026-05-31 @4/msg48427): while the PreToolUse
        side file says the AUQ is genuinely live, an obstructing pane (here the
        TaskList overlay) must NEVER tombstone the card — not even past the
        absent-streak threshold. The pane is a display; the side file is the
        lifecycle authority. This is the test that would have caught the
        incident.
        """
        from cctelegram.handlers import status_polling
        from cctelegram.handlers.interactive_ui import (
            _interactive_mode,
            _interactive_msgs,
        )

        window_id = "@37"
        user_id = 6427984308
        thread_id = 10636
        ikey = (user_id, thread_id)
        route = (user_id, thread_id, window_id)
        _interactive_mode[ikey] = window_id
        _interactive_msgs[ikey] = 48427

        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "clear_interactive_msg", new_callable=AsyncMock
            ) as mock_clear,
            patch.object(
                status_polling, "handle_interactive_ui", new_callable=AsyncMock
            ),
            patch.object(
                status_polling, "enqueue_status_update", new_callable=AsyncMock
            ),
            patch.object(
                status_polling.auq_source,
                "side_file_live_for_window",
                return_value=True,
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=self._BAD_FRAME_PANE)

            # Far past the threshold — the side-file gate short-circuits every
            # tick before the streak can accumulate.
            for _ in range(status_polling.ABSENT_STREAK_THRESHOLD + 2):
                await status_polling.update_status_message(
                    mock_bot,
                    user_id=user_id,
                    window_id=window_id,
                    thread_id=thread_id,
                )

            mock_clear.assert_not_called()
            assert route not in status_polling._absent_streak

    @pytest.mark.asyncio
    async def test_side_file_absent_still_tombstones_after_threshold(
        self, mock_bot: AsyncMock
    ):
        """Complement: when the side file is gone (the question truly resolved
        on the Claude side — answered in tmux / auto-resolved / unlinked on
        tool_result), the legitimate non-Telegram-pick close must still fire
        after the threshold. The fix must not strand a dead card.
        """
        from cctelegram.handlers import status_polling
        from cctelegram.handlers.interactive_ui import (
            _interactive_mode,
            _interactive_msgs,
        )

        window_id = "@37"
        user_id = 6427984308
        thread_id = 10636
        ikey = (user_id, thread_id)
        _interactive_mode[ikey] = window_id
        _interactive_msgs[ikey] = 48427

        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "clear_interactive_msg", new_callable=AsyncMock
            ) as mock_clear,
            patch.object(
                status_polling, "handle_interactive_ui", new_callable=AsyncMock
            ),
            patch.object(
                status_polling, "enqueue_status_update", new_callable=AsyncMock
            ),
            patch.object(
                status_polling.auq_source,
                "side_file_live_for_window",
                return_value=False,
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=self._BAD_FRAME_PANE)

            for _ in range(status_polling.ABSENT_STREAK_THRESHOLD):
                await status_polling.update_status_message(
                    mock_bot,
                    user_id=user_id,
                    window_id=window_id,
                    thread_id=thread_id,
                )

            mock_clear.assert_called_once()
            assert mock_clear.call_args.kwargs.get("tombstone") is True

    @pytest.mark.asyncio
    async def test_absent_streak_resets_on_pane_recovery(self, mock_bot: AsyncMock):
        """absent → live → absent → absent → live: streak must reset on every
        live observation so transient flickers can't accumulate toward a false
        clear.
        """
        from cctelegram.handlers import status_polling
        from cctelegram.handlers.interactive_ui import (
            _interactive_mode,
            _interactive_msgs,
        )

        window_id = "@37"
        user_id = 6427984308
        thread_id = 10636
        ikey = (user_id, thread_id)
        route = (user_id, thread_id, window_id)
        _interactive_mode[ikey] = window_id
        _interactive_msgs[ikey] = 32835

        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "clear_interactive_msg", new_callable=AsyncMock
            ) as mock_clear,
            patch.object(
                status_polling, "handle_interactive_ui", new_callable=AsyncMock
            ) as mock_handle_ui,
            patch.object(
                status_polling, "enqueue_status_update", new_callable=AsyncMock
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_handle_ui.return_value = True

            # Tick 1: bad frame → streak=1.
            mock_tmux.capture_pane = AsyncMock(return_value=self._BAD_FRAME_PANE)
            await status_polling.update_status_message(
                mock_bot, user_id=user_id, window_id=window_id, thread_id=thread_id
            )
            assert status_polling._absent_streak.get(route) == 1

            # Tick 2: pane recovers → streak reset.
            mock_tmux.capture_pane = AsyncMock(return_value=self._LIVE_AUQ_PANE)
            await status_polling.update_status_message(
                mock_bot, user_id=user_id, window_id=window_id, thread_id=thread_id
            )
            assert route not in status_polling._absent_streak

            # Tick 3 + 4: two more bad frames — still below threshold because
            # the recovery on tick 2 reset the counter.
            mock_tmux.capture_pane = AsyncMock(return_value=self._BAD_FRAME_PANE)
            for _ in range(status_polling.ABSENT_STREAK_THRESHOLD - 1):
                await status_polling.update_status_message(
                    mock_bot,
                    user_id=user_id,
                    window_id=window_id,
                    thread_id=thread_id,
                )

            mock_clear.assert_not_called()

    @pytest.mark.asyncio
    async def test_streak_dropped_on_external_clear_callback(self, mock_bot: AsyncMock):
        """Codex P2 (2026-05-20 review, 2nd pass): the external clear path
        (``clear_interactive_msg`` from callback dispatcher / JSONL
        ``tool_result`` handler) must drop ``_absent_streak`` synchronously
        via the registered clear callback. Lazy reset on the next poll
        (`interactive_window != window_id`) is not sufficient because the
        external-clear → new-lifecycle transition can complete entirely
        between two polls, leaving zero opportunity for the lazy branch to
        fire before the new lifecycle's first absent observation.
        """
        from cctelegram.handlers import interactive_ui, status_polling

        window_id = "@37"
        user_id = 6427984308
        thread_id = 10636
        route = (user_id, thread_id, window_id)

        # Pre-populate streak as if a prior lifecycle had built one up.
        status_polling._absent_streak[route] = 2

        # Fire the callback directly — this is what ``clear_interactive_msg``
        # invokes via ``_fire_clear`` after popping the lock.
        interactive_ui._fire_clear(user_id, thread_id, window_id)

        assert route not in status_polling._absent_streak

    @pytest.mark.asyncio
    async def test_streak_does_not_survive_external_clear(self, mock_bot: AsyncMock):
        """Codex P2 (2026-05-20 review): if a prior interactive lifecycle ends
        via an external path (callback dispatcher / JSONL ``tool_result`` →
        ``clear_interactive_msg``) while ``_absent_streak`` is mid-build, the
        next lifecycle on the same route+window must NOT inherit that count.
        Inheritance would let a single bad-frame poll reach the threshold and
        delete the new live card — defeating the hysteresis this patch adds.
        """
        from cctelegram.handlers import status_polling
        from cctelegram.handlers.interactive_ui import (
            _interactive_mode,
            _interactive_msgs,
        )

        window_id = "@37"
        user_id = 6427984308
        thread_id = 10636
        ikey = (user_id, thread_id)
        route = (user_id, thread_id, window_id)

        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "clear_interactive_msg", new_callable=AsyncMock
            ) as mock_clear,
            patch.object(
                status_polling, "handle_interactive_ui", new_callable=AsyncMock
            ),
            patch.object(
                status_polling, "enqueue_status_update", new_callable=AsyncMock
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=self._BAD_FRAME_PANE)

            # Lifecycle 1: AUQ live, one bad-frame poll accumulates streak=1.
            _interactive_mode[ikey] = window_id
            _interactive_msgs[ikey] = 32835
            await status_polling.update_status_message(
                mock_bot,
                user_id=user_id,
                window_id=window_id,
                thread_id=thread_id,
            )
            assert status_polling._absent_streak.get(route) == 1
            mock_clear.assert_not_called()

            # External clear (e.g. callback dispatcher fires Submit after
            # accumulating answers). Both mode and msg_id reset by the
            # external code path; the poller does NOT see this transition.
            _interactive_mode.pop(ikey, None)
            _interactive_msgs.pop(ikey, None)

            # One poll lands while no interactive surface owns the route —
            # the cleanup branch must drop the stale streak.
            await status_polling.update_status_message(
                mock_bot,
                user_id=user_id,
                window_id=window_id,
                thread_id=thread_id,
            )
            assert route not in status_polling._absent_streak

            # Lifecycle 2: a fresh AUQ arrives on the same route+window.
            # Without the codex fix, the streak would already be 1, so two
            # more bad-frame polls would fire ``clear_interactive_msg`` on a
            # brand-new card. With the fix, the streak is 0 and only the
            # threshold-th absent poll triggers the clear.
            _interactive_mode[ikey] = window_id
            _interactive_msgs[ikey] = 99999  # fresh card id
            for _ in range(status_polling.ABSENT_STREAK_THRESHOLD - 1):
                await status_polling.update_status_message(
                    mock_bot,
                    user_id=user_id,
                    window_id=window_id,
                    thread_id=thread_id,
                )

            mock_clear.assert_not_called()

    @pytest.mark.asyncio
    async def test_streak_does_not_survive_publish_race(self, mock_bot: AsyncMock):
        """Variant of the codex P2 case where the external clear happens just
        before a fresh ``set_interactive_mode`` but before the new card is
        published. The poll lands in the publish race (mode set, msg_id
        unset). The leftover streak must be cleared on that race tick so the
        eventual threshold count starts from zero.
        """
        from cctelegram.handlers import status_polling
        from cctelegram.handlers.interactive_ui import (
            _interactive_mode,
            _interactive_msgs,
        )

        window_id = "@37"
        user_id = 6427984308
        thread_id = 10636
        ikey = (user_id, thread_id)
        route = (user_id, thread_id, window_id)

        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "clear_interactive_msg", new_callable=AsyncMock
            ),
            patch.object(
                status_polling, "handle_interactive_ui", new_callable=AsyncMock
            ),
            patch.object(
                status_polling, "enqueue_status_update", new_callable=AsyncMock
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=self._BAD_FRAME_PANE)

            # Seed a leftover streak from a prior lifecycle, simulating a
            # callback-dispatch external clear that didn't go through the
            # poller (which would otherwise reset the counter).
            status_polling._absent_streak[route] = 2

            # New lifecycle starts mid-publish: mode set, no msg_id yet.
            _interactive_mode[ikey] = window_id
            assert _interactive_msgs.get(ikey) is None

            await status_polling.update_status_message(
                mock_bot,
                user_id=user_id,
                window_id=window_id,
                thread_id=thread_id,
            )

            # The publish-race early return must drop the stale streak.
            assert route not in status_polling._absent_streak

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "shape,pane",
        [
            # ``full``: both prompt and Submit/Cancel options visible in the
            # tail. The exact shape from the 2026-05-21 incident.
            (
                "full",
                "  ...question content scrolled off above this line...\n"
                "\n"
                "Ready to submit your answers?\n"
                "\n"
                "❯ 1. Submit answers\n"
                "  2. Cancel\n",
            ),
            # ``options-only``: only the Submit/Cancel options remain in
            # the tail; the ``Ready to submit your answers?`` prompt has
            # scrolled off above too. Matches the 2026-05-17 12:31
            # production incident shape pinned in
            # ``TestVisiblePaneLiveness.test_submit_answers_options_only_visible_is_present``.
            # Removing the ``Submit answers`` regex from
            # ``_PICKER_ANCHOR_MARKERS`` would re-introduce the
            # destructive clear on this shape; the parametrize entry
            # pins it independently of the prompt regex.
            (
                "options-only",
                "\n❯ 1. Submit answers\n  2. Cancel\n",
            ),
            # ``prompt-only``: only the ``Ready to submit your answers?``
            # prompt remains in the tail (options have wrapped below or
            # been masked). Removing the prompt regex from
            # ``_PICKER_ANCHOR_MARKERS`` would re-introduce the
            # destructive clear on this shape; the parametrize entry
            # pins it independently of the options regex.
            (
                "prompt-only",
                "  ...prior picker content scrolled off...\n"
                "\n"
                "Ready to submit your answers?\n",
            ),
        ],
        ids=["full", "options-only", "prompt-only"],
    )
    async def test_submit_screen_anchor_visible_prevents_clear(
        self, mock_bot: AsyncMock, shape: str, pane: str
    ):
        """Regression — 2026-05-21 09:16:07 → 09:16:09 incident on @40 / msg
        34496: the multi-Q AskUserQuestion advanced to the Submit-confirmation
        screen with a long-question pane; the tab header
        (``← ☒ ... ✔ Submit →``) scrolled above the visible region.
        ``extract_interactive_content`` returned None for every UI_PATTERN
        (none of multi-tab / single-tab / plain-numbered match a Submit screen
        without ``Enter to select`` and without a visible tab header), so the
        absent streak hit ABSENT_STREAK_THRESHOLD in 3 polls and the card was
        destructively cleared while the picker was still live on the pane.

        The 2026-05-20 ``_PICKER_ANCHOR_MARKERS`` work fixed the same shape
        in ``visible_pane_liveness`` / ``handle_interactive_ui``, but didn't
        propagate to status_polling's clear gate. This parametrize pins the
        bypass on every distinct Submit-screen tail shape: full (prompt +
        options), options-only, and prompt-only. Each anchor must
        independently keep the card alive — codex P2 review 2026-05-21:
        without the per-shape coverage, a single-regex removal could go
        unnoticed because the surviving anchor still fires.
        """
        from cctelegram.handlers import status_polling
        from cctelegram.handlers.interactive_ui import (
            _interactive_mode,
            _interactive_msgs,
        )

        window_id = "@40"
        user_id = 6427984308
        thread_id = 34451
        ikey = (user_id, thread_id)
        route = (user_id, thread_id, window_id)
        _interactive_mode[ikey] = window_id
        _interactive_msgs[ikey] = 34496  # the destroyed card in the real incident

        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "clear_interactive_msg", new_callable=AsyncMock
            ) as mock_clear,
            patch.object(
                status_polling, "handle_interactive_ui", new_callable=AsyncMock
            ),
            patch.object(
                status_polling, "enqueue_status_update", new_callable=AsyncMock
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=pane)

            # Drive ABSENT_STREAK_THRESHOLD + 2 polls — well past the
            # threshold that the legacy code would have used to clear.
            # With the picker-anchor bypass, the streak resets each tick
            # and the clear must never fire.
            for _ in range(status_polling.ABSENT_STREAK_THRESHOLD + 2):
                await status_polling.update_status_message(
                    mock_bot,
                    user_id=user_id,
                    window_id=window_id,
                    thread_id=thread_id,
                )

            assert mock_clear.call_count == 0, (
                f"shape={shape}: clear must not fire while picker anchors "
                f"are visible in the tail"
            )
            # The bypass calls _absent_streak.pop on every tick where the
            # picker anchor is visible.
            assert route not in status_polling._absent_streak

    @pytest.mark.asyncio
    async def test_window_switch_clears_immediately_no_hysteresis(
        self, mock_bot: AsyncMock
    ):
        """User in interactive mode on window A; the poller ticks for window
        B (window switch). That branch must clear without hysteresis because
        the route ownership has unambiguously moved.
        """
        from cctelegram.handlers import status_polling
        from cctelegram.handlers.interactive_ui import (
            _interactive_mode,
            _interactive_msgs,
        )

        live_window = "@37"
        polling_window = "@29"
        user_id = 6427984308
        thread_id_for_poll = 3207
        ikey = (user_id, thread_id_for_poll)

        # Mode set for live_window, but we're polling polling_window.
        _interactive_mode[ikey] = live_window
        _interactive_msgs[ikey] = 32835

        mock_window = MagicMock()
        mock_window.window_id = polling_window

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "clear_interactive_msg", new_callable=AsyncMock
            ) as mock_clear,
            patch.object(
                status_polling, "enqueue_status_update", new_callable=AsyncMock
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(
                return_value="some random non-interactive pane\n❯ \n"
            )

            await status_polling.update_status_message(
                mock_bot,
                user_id=user_id,
                window_id=polling_window,
                thread_id=thread_id_for_poll,
            )

            mock_clear.assert_called_once_with(user_id, mock_bot, thread_id_for_poll)


@pytest.mark.usefixtures("_clear_interactive_state")
class TestPaneInteractivePendingWiring:
    """PR-C poller-local wiring for the pane-set WAITING bit (Bug 1):
    the _prev_run_state repaint-dedup cache lifecycle + the repaint-on-transition
    helper. The end-to-end promote/clear behaviour lives in the scenario floor
    (tests/scenarios/test_auq_waiting_indicator.py)."""

    @pytest.mark.asyncio
    async def test_on_interactive_clear_does_not_pop_prev_run_state(self):
        """v3 shared-P1 guard: the bot-less interactive-clear seam must NOT pop
        _prev_run_state — popping there masked the post-clear repaint (a route
        that flips WAITING → RUNNING after the clear would never repaint)."""
        from cctelegram.handlers import status_polling
        from cctelegram.route_runtime import RunState

        route = (111, 222, "@3")
        status_polling._prev_run_state[route] = RunState.WAITING_ON_USER
        status_polling._on_interactive_clear(111, 222, "@3")
        assert route in status_polling._prev_run_state  # NOT popped

    @pytest.mark.asyncio
    async def test_window_switch_branch_clears_old_route_bit(self, mock_bot: AsyncMock):
        """Window-switch (interactive_window != window_id, not None): the
        mode-ended reconciliation clears the polled route's pane-set WAITING bit
        before the card-delete branch — no stuck WAITING after focus moves. Same
        `interactive_window != window_id` reconciliation as the mode-popped case,
        exercised here with interactive_window pointing at a DIFFERENT window."""
        from cctelegram import route_runtime
        from cctelegram.handlers import status_polling
        from cctelegram.handlers.interactive_ui import (
            _interactive_mode,
            _interactive_msgs,
        )
        from cctelegram.route_runtime import RunState

        live_window = "@37"  # focus moved here
        polling_window = "@29"  # we poll the OLD window
        user_id, thread_id = 6427984308, 3207
        route = (user_id, thread_id, polling_window)

        # The old route carried a pane-set WAITING bit.
        await route_runtime.mark_inbound_sent(route)
        await route_runtime.mark_interactive_pending(route)
        assert route_runtime.snapshot(route).interactive_pending is True

        _interactive_mode[(user_id, thread_id)] = live_window  # != polling_window
        _interactive_msgs[(user_id, thread_id)] = 32835

        mock_window = MagicMock()
        mock_window.window_id = polling_window
        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "clear_interactive_msg", new_callable=AsyncMock
            ),
            patch.object(
                status_polling, "enqueue_status_update", new_callable=AsyncMock
            ),
            patch.object(
                status_polling,
                "refresh_activity_digest_if_present",
                new_callable=AsyncMock,
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value="non-interactive\n❯ \n")
            await status_polling.update_status_message(
                mock_bot,
                user_id=user_id,
                window_id=polling_window,
                thread_id=thread_id,
            )

        snap = route_runtime.snapshot(route)
        assert snap.interactive_pending is False  # reconciliation cleared it
        assert snap.run_state is RunState.RUNNING

    @pytest.mark.asyncio
    async def test_window_gone_path_pops_prev_run_state(self, mock_bot: AsyncMock):
        """The window-gone path is the sole _prev_run_state teardown (alongside
        _last_pane_capture / _last_published_ui_hash) — status_polling-local,
        import-safe."""
        from cctelegram.handlers import status_polling
        from cctelegram.route_runtime import RunState

        user_id, thread_id, window_id = 111, 222, "@gone"
        route = (user_id, thread_id, window_id)
        status_polling._prev_run_state[route] = RunState.RUNNING
        status_polling._last_pane_capture[route] = 1.0
        status_polling._last_published_ui_hash[route] = "h"

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "enqueue_status_update", new_callable=AsyncMock
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=None)  # window gone
            await status_polling.update_status_message(
                mock_bot, user_id=user_id, window_id=window_id, thread_id=thread_id
            )

        assert route not in status_polling._prev_run_state
        assert route not in status_polling._last_pane_capture
        assert route not in status_polling._last_published_ui_hash

    @pytest.mark.asyncio
    async def test_maybe_repaint_seeds_then_repaints_once_per_transition(
        self, mock_bot: AsyncMock
    ):
        """First observation seeds without an edit; a change repaints exactly
        once; an unchanged state does not repaint."""
        from cctelegram import route_runtime
        from cctelegram.handlers import status_polling

        user_id, thread_id, window_id = 111, 222, "@7"
        route = (user_id, thread_id, window_id)

        with patch.object(
            status_polling, "refresh_activity_digest_if_present", new_callable=AsyncMock
        ) as mock_refresh:
            # RUNNING. First observation seeds — NO edit.
            await route_runtime.mark_inbound_sent(route)
            await status_polling._maybe_repaint_digest_on_transition(
                mock_bot, user_id, thread_id, window_id
            )
            mock_refresh.assert_not_called()

            # Same state — still no edit.
            await status_polling._maybe_repaint_digest_on_transition(
                mock_bot, user_id, thread_id, window_id
            )
            mock_refresh.assert_not_called()

            # Transition RUNNING → WAITING_ON_USER (pane-set) — repaint ONCE.
            await route_runtime.mark_interactive_pending(route)
            await status_polling._maybe_repaint_digest_on_transition(
                mock_bot, user_id, thread_id, window_id
            )
            assert mock_refresh.await_count == 1

            # Same WAITING state again — no further edit.
            await status_polling._maybe_repaint_digest_on_transition(
                mock_bot, user_id, thread_id, window_id
            )
            assert mock_refresh.await_count == 1

            # Transition back WAITING → RUNNING — repaint once more (both
            # directions repaint).
            await route_runtime.mark_interactive_cleared(route)
            await status_polling._maybe_repaint_digest_on_transition(
                mock_bot, user_id, thread_id, window_id
            )
            assert mock_refresh.await_count == 2
