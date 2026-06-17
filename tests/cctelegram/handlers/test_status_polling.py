"""Tests for status_polling — Settings UI detection via the poller path.

Simulates the user workflow: /model is sent to Claude Code, the Settings
model picker renders in the terminal, and the status poller detects it
on its next 1s tick.
"""

import asyncio
import time
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
        arms one (no pane confirmation). Post-fix-5 the commit additionally
        requires a confirmed-idle RE-CAPTURE, so the tmux seam is patched to
        return an idle pane."""
        from cctelegram import route_runtime
        from cctelegram.handlers import status_polling

        window_id = "@5"
        route = (1, 42, window_id)
        fake_now = [1000.0]

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "enqueue_status_update", new_callable=AsyncMock
            ) as mock_enqueue,
            patch.object(
                status_polling.time, "monotonic", side_effect=lambda: fake_now[0]
            ),
        ):
            mock_tmux.capture_pane = AsyncMock(return_value=_IDLE_PANE)
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

    @pytest.mark.asyncio
    async def test_idle_clear_only_busy_recapture_does_not_commit_and_cancels(
        self, mock_bot: AsyncMock
    ):
        """Fix 5 RED gate: WATCHDOG_INTERVAL (10s) > IDLE_CLEAR_DELAY_SECONDS
        (4s) means the cleanup-only path's commit was previously made off a
        SINGLE armed frame with no second pane observation — one mid-redraw
        misparse on a RUNNING_TOOL route would wipe ``open_tools`` and drop
        the Task's eventual tool_result as an unknown id. The fix re-captures
        the pane when the deadline is due and commits ONLY on a second
        confirmed-idle frame; a positively-BUSY re-capture must NOT commit —
        it CANCELS the deadline (2026-06-11: matching the full path's running
        branch; an unknown frame still re-arms), preserving the run-state and
        open_tools.

        Pre-fix this is RED: ``_process_idle_clear_only`` never captures, so
        the due deadline commits unconditionally (enqueue fires, RUNNING_TOOL
        is forced to IDLE_CLEARED)."""
        from cctelegram import route_runtime, transcript_event_adapter
        from cctelegram.handlers import status_polling
        from cctelegram.session_monitor import TranscriptEvent

        window_id = "@5"
        route = (1, 42, window_id)
        fake_now = [1000.0]

        # Put the route into RUNNING_TOOL with an open tool — the user-visible
        # stake: a single-frame misparse commit would wipe this.
        await transcript_event_adapter.dispatch_transcript_event(
            TranscriptEvent(
                session_id="sess-1",
                role="assistant",
                block_type="tool_use",
                tool_use_id="task-1",
                tool_name="Task",
                stop_reason="tool_use",
                timestamp=None,
                text="",
                image_data=None,
            ),
            [route],
        )
        assert route_runtime.snapshot(route).run_state is RunState.RUNNING_TOOL
        assert route_runtime.snapshot(route).open_tools

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "enqueue_status_update", new_callable=AsyncMock
            ) as mock_enqueue,
            patch.object(
                status_polling.time, "monotonic", side_effect=lambda: fake_now[0]
            ),
        ):
            # A (misparsed) confirmed-idle frame armed the deadline...
            route_runtime.arm_pane_idle_clear(route, now=1000.0)
            fake_now[0] += status_polling.IDLE_CLEAR_DELAY_SECONDS + 0.1

            # ...but the due-tick re-capture shows Claude actively running.
            mock_tmux.capture_pane = AsyncMock(return_value=_BUSY_PANE)
            await status_polling._process_idle_clear_only(
                mock_bot, 1, window_id, 42, skip_status=False
            )

            # NO commit: card untouched, run-state + open_tools preserved.
            assert mock_enqueue.await_count == 0
            snap = route_runtime.snapshot(route)
            assert snap.run_state is RunState.RUNNING_TOOL
            assert snap.open_tools
            # Deadline CANCELLED outright: a positively-ACTIVE re-capture
            # ("esc to interrupt" visible) is the same evidence as the full
            # path's running branch — a fresh idle stretch must re-arm from a
            # new confirmed-idle scrape. (Pre-2026-06-11 this re-armed instead,
            # keeping a rolling deadline alive for the whole run — the fuel for
            # the cross-path false commit on the @4 stuck route.)
            assert snap.pane_idle_clear_at is None

    @pytest.mark.asyncio
    async def test_idle_clear_only_unparseable_recapture_does_not_commit(
        self, mock_bot: AsyncMock
    ):
        """Fix 5: an EMPTY/failed re-capture cannot confirm idle — the due
        deadline must not commit; it re-arms for the next due tick."""
        from cctelegram import route_runtime
        from cctelegram.handlers import status_polling

        window_id = "@5"
        route = (1, 42, window_id)
        fake_now = [1000.0]

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "enqueue_status_update", new_callable=AsyncMock
            ) as mock_enqueue,
            patch.object(
                status_polling.time, "monotonic", side_effect=lambda: fake_now[0]
            ),
        ):
            route_runtime.arm_pane_idle_clear(route, now=1000.0)
            fake_now[0] += status_polling.IDLE_CLEAR_DELAY_SECONDS + 0.1
            mock_tmux.capture_pane = AsyncMock(return_value="")
            await status_polling._process_idle_clear_only(
                mock_bot, 1, window_id, 42, skip_status=False
            )
            assert mock_enqueue.await_count == 0
            assert (
                route_runtime.snapshot(route).pane_idle_clear_at
                == fake_now[0] + status_polling.IDLE_CLEAR_DELAY_SECONDS
            )

    @pytest.mark.asyncio
    async def test_idle_clear_only_malformed_nonempty_recapture_does_not_commit(
        self, mock_bot: AsyncMock
    ):
        """Fix 5 (hermes round-2 RED gate): a NON-EMPTY malformed/truncated/
        mid-redraw re-capture is NOT positive idle evidence. "Confirmed idle"
        requires the frame to look like a live Claude Code pane at rest —
        the chrome separator anchor present (``has_pane_chrome``, the same
        ``─``-line anchor ``parse_status_line``/``strip_pane_chrome`` trust)
        AND no active-run marker. A garbage frame with no parseable status
        previously slipped the absence-of-active-status predicate and
        committed, wiping transcript-set ``open_tools`` on a RUNNING_TOOL
        route. Post-fix: NO commit, run-state + open_tools preserved, the
        deadline re-arms for the next due tick."""
        from cctelegram import route_runtime, transcript_event_adapter
        from cctelegram.handlers import status_polling
        from cctelegram.session_monitor import TranscriptEvent

        window_id = "@5"
        route = (1, 42, window_id)
        fake_now = [1000.0]

        # RUNNING_TOOL with an open Task — the stake a misread commit wipes.
        await transcript_event_adapter.dispatch_transcript_event(
            TranscriptEvent(
                session_id="sess-1",
                role="assistant",
                block_type="tool_use",
                tool_use_id="task-1",
                tool_name="Task",
                stop_reason="tool_use",
                timestamp=None,
                text="",
                image_data=None,
            ),
            [route],
        )
        assert route_runtime.snapshot(route).run_state is RunState.RUNNING_TOOL

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "enqueue_status_update", new_callable=AsyncMock
            ) as mock_enqueue,
            patch.object(
                status_polling.time, "monotonic", side_effect=lambda: fake_now[0]
            ),
        ):
            route_runtime.arm_pane_idle_clear(route, now=1000.0)
            fake_now[0] += status_polling.IDLE_CLEAR_DELAY_SECONDS + 0.1
            # Non-empty, but no chrome separator: a truncated/mid-redraw frame.
            mock_tmux.capture_pane = AsyncMock(
                return_value="some partial garbage\noutput with no chrome\n"
            )
            await status_polling._process_idle_clear_only(
                mock_bot, 1, window_id, 42, skip_status=False
            )
            assert mock_enqueue.await_count == 0
            snap = route_runtime.snapshot(route)
            assert snap.run_state is RunState.RUNNING_TOOL
            assert snap.open_tools
            assert (
                snap.pane_idle_clear_at
                == fake_now[0] + status_polling.IDLE_CLEAR_DELAY_SECONDS
            )

    @pytest.mark.asyncio
    async def test_idle_clear_only_idle_recapture_commits(self, mock_bot: AsyncMock):
        """Fix 5 GREEN side: a due deadline whose re-capture ALSO parses
        confirmed-idle commits exactly once (the 4s clear UX is preserved —
        re-capture-at-commit, not two-observation arming)."""
        from cctelegram import route_runtime
        from cctelegram.handlers import status_polling

        window_id = "@5"
        route = (1, 42, window_id)
        fake_now = [1000.0]

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "enqueue_status_update", new_callable=AsyncMock
            ) as mock_enqueue,
            patch.object(
                status_polling.time, "monotonic", side_effect=lambda: fake_now[0]
            ),
        ):
            route_runtime.arm_pane_idle_clear(route, now=1000.0)
            fake_now[0] += status_polling.IDLE_CLEAR_DELAY_SECONDS + 0.1
            mock_tmux.capture_pane = AsyncMock(return_value=_IDLE_PANE)
            await status_polling._process_idle_clear_only(
                mock_bot, 1, window_id, 42, skip_status=False
            )
            assert mock_enqueue.await_count == 1
            assert mock_enqueue.await_args[0][3] is None
            assert route_runtime.snapshot(route).run_state is RunState.IDLE_CLEARED

            # Latched: a later due-shaped tick is a no-op (sentinel).
            fake_now[0] += status_polling.IDLE_CLEAR_DELAY_SECONDS + 1.0
            await status_polling._process_idle_clear_only(
                mock_bot, 1, window_id, 42, skip_status=False
            )
            assert mock_enqueue.await_count == 1


# Genuinely ACTIVE v2.1.168 pane during a long foreground Bash run: the
# spinner's attached task-progress block sits BETWEEN the spinner line and the
# chrome separator, and the agent task-list footer renders BELOW the bottom
# chrome line. Mirrors the live ccbot:@4 capture from the 2026-06-11 stuck-route
# incident (anonymized).
_ACTIVE_TASK_BLOCK_PANE = (
    "✻ Building wave 5… (2h 4m 6s · ↓ 198.9k tokens)\n"
    "  ⎿ \xa0✔ W3: first wave done\n"
    "     ✔ W4: second wave done\n"
    "     ◼ W5: third wave running\n"
    "\n"
    + "─" * 40
    + "\n"
    + "❯ \n"
    + "─" * 40
    + "\n"
    + "  ⏵⏵ bypass permissions on · 2 shells · esc to interrupt · ctrl+t to hide\n"
    "\n"
    "  ⏺ main                                ↑/↓ to select · Enter to view\n"
    "  ◯ general-purpose  Implement wave 5                          10m 59s\n"
)

# Active-run marker visible but NO parseable spinner status line at all — a
# parser-hostile-but-unambiguously-active frame (future chrome variants). The
# full-path commit must treat this as running, never as confirmed idle.
_ACTIVE_NO_STATUS_PANE = (
    "⏺ Bash(long foreground run)\n"
    "  ⎿  Running…\n"
    "\n"
    + "─" * 40
    + "\n"
    + "❯ \n"
    + "─" * 40
    + "\n"
    + "  ⏵⏵ bypass permissions on · esc to interrupt\n"
)


@pytest.mark.usefixtures("_clear_interactive_state")
class TestActivePaneNeverCommitsIdleClear:
    """2026-06-11 stuck-route RCA (route (6427984308, 378, '@4')): during a
    ~4-minute quiet foreground Bash, every watchdog capture computed
    ``is_running=False`` because v2.1.168's task-progress block broke
    ``parse_status_line`` — the full capture path armed the pane-idle
    deadline on an ACTIVE pane and, unlike ``_process_idle_clear_only``
    (which re-validates with positive idle evidence), committed the clear
    with NO gate as soon as a watchdog tick landed past the rolling
    deadline. RUNNING_TOOL was falsely reconciled to IDLE_CLEARED(pane) and
    typing/digest went dark until the next transcript event (~3.5 min).

    Two invariants pinned here:
      1. the real v2.1.168 active frame parses as running (no arm at all);
      2. even when the status line is unparseable, a frame whose active-run
         marker is visible must never arm/commit on the FULL path (the same
         positive-evidence rule the cleanup-only path already enforces).
    """

    def _open_bash(self):
        from cctelegram.session_monitor import TranscriptEvent

        return TranscriptEvent(
            session_id="sess-1",
            role="assistant",
            block_type="tool_use",
            tool_use_id="bash-1",
            tool_name="Bash",
            stop_reason="tool_use",
            timestamp=None,
            text="",
            image_data=None,
        )

    @pytest.mark.asyncio
    async def test_task_block_frame_keeps_route_running_tool(self, mock_bot: AsyncMock):
        """The live incident frame: RUNNING_TOOL with an open Bash, watchdog
        captures returning the ACTIVE task-block pane across ticks spanning
        the debounce delay — the route must stay RUNNING_TOOL (typing
        eligible) and no card clear may be enqueued."""
        from cctelegram import route_runtime, transcript_event_adapter
        from cctelegram.handlers import status_polling

        window_id = "@5"
        route = (1, 42, window_id)
        mock_window = MagicMock()
        mock_window.window_id = window_id
        fake_now = [1000.0]

        await transcript_event_adapter.dispatch_transcript_event(
            self._open_bash(), [route]
        )
        assert route_runtime.snapshot(route).run_state is RunState.RUNNING_TOOL

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
            mock_tmux.capture_pane = AsyncMock(return_value=_ACTIVE_TASK_BLOCK_PANE)

            # Several watchdog-spaced capture ticks, each > the debounce delay.
            for _ in range(3):
                await status_polling.update_status_message(
                    mock_bot, user_id=1, window_id=window_id, thread_id=42
                )
                fake_now[0] += status_polling.WATCHDOG_INTERVAL + 0.1

            snap = route_runtime.snapshot(route)
            assert snap.run_state is RunState.RUNNING_TOOL
            assert snap.typing_eligible is True
            assert snap.open_tools == frozenset({"bash-1"})
            # No deadline may be left armed on an active pane, and no clear
            # (text=None) may ever have been enqueued.
            assert snap.pane_idle_clear_at is None
            for call in mock_enqueue.await_args_list:
                assert call[0][3] is not None

    @pytest.mark.asyncio
    async def test_unparseable_status_but_active_marker_never_commits(
        self, mock_bot: AsyncMock
    ):
        """Defense in depth for the NEXT parser-hostile chrome variant: with
        a deadline already due, a full-path capture whose status line is
        unparseable but whose active-run marker is visible must NOT commit
        the pane-idle clear — mirror of the cleanup-only path's positive
        idle evidence rule."""
        from cctelegram import route_runtime, transcript_event_adapter
        from cctelegram.handlers import status_polling

        window_id = "@5"
        route = (1, 42, window_id)
        mock_window = MagicMock()
        mock_window.window_id = window_id
        fake_now = [1000.0]

        await transcript_event_adapter.dispatch_transcript_event(
            self._open_bash(), [route]
        )

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
            mock_tmux.capture_pane = AsyncMock(return_value=_ACTIVE_NO_STATUS_PANE)

            # A deadline armed by a prior (misread) tick, now overdue.
            route_runtime.arm_pane_idle_clear(route, now=1000.0)
            fake_now[0] += status_polling.WATCHDOG_INTERVAL + 0.1

            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            snap = route_runtime.snapshot(route)
            assert snap.run_state is RunState.RUNNING_TOOL
            assert snap.typing_eligible is True
            assert snap.open_tools == frozenset({"bash-1"})
            # Active marker ⇒ the stale deadline is cancelled, not committed.
            assert snap.pane_idle_clear_at is None
            assert mock_enqueue.await_count == 0


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


@pytest.mark.usefixtures("_clear_interactive_state")
class TestPollerSourceDriftRemint:
    """Item 1 RED gate: a live AUQ card whose minted source has DRIFTED must be
    re-minted by the poller on the same-hash idle tick.

    When the PreToolUse side file ages past the read-TTL under a static idle
    picker, ``resolve_auq_source`` flips ``side_file`` → ``pane`` while the
    displayed card's tokens were minted from ``side_file``. The poller's
    same-hash idle branch currently only ``refresh_route_deadlines`` and returns
    WITHOUT re-rendering, so the card keeps stale ``side_file`` tokens and the
    user's first tap ``source_drift``s (swallowed + a misleading "Form changed,
    refreshing."). The fix: the poller detects the source drift (re-resolve +
    ``peek_route_source`` vs the live source) and re-mints via
    ``handle_interactive_ui`` so the tokens track the current source.

    RED pre-fix / GREEN post-fix: on current main the poller never re-mints on
    the same-hash branch, so ``handle_interactive_ui`` is NOT awaited.
    """

    async def test_same_hash_source_drift_remints_card(self, mock_bot: AsyncMock):
        # FAITHFUL RED (exposes the key-mismatch P1): production mints a side_file
        # card at the SIDE-FILE form's fingerprint — the side-file dict carries
        # the question title — but after the side file ages out the poller can
        # only see the PANE form, whose title is None on single-select panes, so
        # its fingerprint DIFFERS. The row is therefore seeded at the side-file-
        # form key, NOT the pane-form key the poller computes. The drift detector
        # must find the displayed card by ROUTE (regardless of fingerprint) and
        # re-mint; a fingerprint-keyed lookup misses it and the bug is unfixed.
        import hashlib
        import json
        from pathlib import Path

        from cctelegram.handlers import pick_token, status_polling
        from cctelegram.handlers.interactive_ui import _interactive_mode
        from cctelegram.handlers.pick_token import _CacheRow, _pick_token_cache
        from cctelegram.handlers.status_polling import extract_interactive_content
        from cctelegram.terminal_parser import resolve_ask_form

        pick_token.reset_for_tests()
        try:
            fx = Path(__file__).parents[1] / "fixtures"
            pane = (fx / "auq_single_select_with_affordances_pane.txt").read_text()
            tool_input = json.loads(
                (fx / "auq_single_select_with_affordances_sidefile.json").read_text()
            )["tool_input"]
            window_id = "@5"
            route = (1, 42, window_id)
            mock_window = MagicMock()
            mock_window.window_id = window_id
            _interactive_mode[(1, 42)] = window_id

            # Pin the published hash to the LIVE pane's hash → same-hash branch.
            ui_content = extract_interactive_content(pane)
            assert ui_content is not None
            ui_hash = hashlib.sha256(ui_content.content.encode("utf-8")).hexdigest()
            status_polling._last_published_ui_hash[route] = ui_hash

            # Production mints at the SIDE-FILE form fingerprint (title present),
            # which differs from the poller's pane-form fingerprint (title=None).
            side_file_form = resolve_ask_form(tool_input, pane)
            pane_form = resolve_ask_form(None, pane)
            assert side_file_form is not None and pane_form is not None
            assert side_file_form.fingerprint() != pane_form.fingerprint(), (
                "fixture must exercise the side-file-vs-pane title mismatch"
            )
            _pick_token_cache[(1, 42, window_id, side_file_form.fingerprint())] = (
                _CacheRow(
                    tokens=["redtok"],
                    row_generation=1,
                    source_kind="side_file",
                    source_fingerprint="sf-fp",
                    consumed_generation=None,
                )
            )

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
                mock_tmux.capture_pane = AsyncMock(return_value=pane)

                await status_polling.update_status_message(
                    mock_bot, user_id=1, window_id=window_id, thread_id=42
                )

            # No side file on disk → resolve_auq_source → pane, so the card's
            # minted side_file source has DRIFTED. The poller MUST re-mint (found
            # by ROUTE despite the fingerprint mismatch) and NOT refresh.
            mock_handle_ui.assert_awaited()
            mock_refresh.assert_not_awaited()
        finally:
            pick_token.reset_for_tests()

    async def test_drift_remint_terminates_next_tick_does_not_remint(
        self, mock_bot: AsyncMock
    ):
        """Loop-termination: after the drift re-mint, the NEXT tick must NOT
        re-mint. Tick 1 detects the drift (side_file row, live pane). We then
        REPLACE the seeded row with the ``pane``-sourced row at the live pane
        fingerprint — exactly what the real ``mint_row`` leaves after fresh-
        minting the pane source and hygiene-dropping the old side_file-fp row.
        Tick 2 now sees live pane == minted pane → no drift → exactly ONE
        re-mint over the two ticks."""
        import hashlib
        import json
        from pathlib import Path

        from cctelegram.handlers import auq_source, pick_token, status_polling
        from cctelegram.handlers.interactive_ui import _interactive_mode
        from cctelegram.handlers.pick_token import _CacheRow, _pick_token_cache
        from cctelegram.handlers.status_polling import extract_interactive_content
        from cctelegram.terminal_parser import resolve_ask_form

        pick_token.reset_for_tests()
        try:
            fx = Path(__file__).parents[1] / "fixtures"
            pane = (fx / "auq_single_select_with_affordances_pane.txt").read_text()
            tool_input = json.loads(
                (fx / "auq_single_select_with_affordances_sidefile.json").read_text()
            )["tool_input"]
            window_id = "@5"
            route = (1, 42, window_id)
            mock_window = MagicMock()
            mock_window.window_id = window_id
            _interactive_mode[(1, 42)] = window_id

            ui_content = extract_interactive_content(pane)
            assert ui_content is not None
            ui_hash = hashlib.sha256(ui_content.content.encode("utf-8")).hexdigest()
            status_polling._last_published_ui_hash[route] = ui_hash

            # Tick-1 seed: side_file-fp row (production mint shape), drifted.
            side_file_form = resolve_ask_form(tool_input, pane)
            assert side_file_form is not None
            _pick_token_cache[(1, 42, window_id, side_file_form.fingerprint())] = (
                _CacheRow(
                    tokens=["redtok"],
                    row_generation=1,
                    source_kind="side_file",
                    source_fingerprint="sf-fp",
                    consumed_generation=None,
                )
            )

            with (
                patch.object(status_polling, "tmux_manager") as mock_tmux,
                patch.object(
                    status_polling.pick_token,
                    "refresh_route_deadlines",
                    new_callable=AsyncMock,
                ),
                patch.object(
                    status_polling, "handle_interactive_ui", new_callable=AsyncMock
                ) as mock_handle_ui,
            ):
                mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
                mock_tmux.capture_pane = AsyncMock(return_value=pane)

                # Tick 1: drift detected → re-mint.
                await status_polling.update_status_message(
                    mock_bot, user_id=1, window_id=window_id, thread_id=42
                )
                assert mock_handle_ui.await_count == 1

                # Simulate the real mint_row outcome: the fresh pane mint + the
                # stale-row hygiene drop leave a SINGLE pane-sourced row at the
                # live pane fingerprint.
                _pick_token_cache.clear()
                live = auq_source.resolve_auq_source(window_id, None, pane)
                pane_form = resolve_ask_form(live.payload, pane)
                assert pane_form is not None
                _pick_token_cache[(1, 42, window_id, pane_form.fingerprint())] = (
                    _CacheRow(
                        tokens=["panetok"],
                        row_generation=2,
                        source_kind=live.kind,
                        source_fingerprint=live.source_fingerprint,
                        consumed_generation=None,
                    )
                )

                # Tick 2: live pane == minted pane → no drift → NO re-mint.
                await status_polling.update_status_message(
                    mock_bot, user_id=1, window_id=window_id, thread_id=42
                )
                assert mock_handle_ui.await_count == 1
        finally:
            pick_token.reset_for_tests()

    async def test_same_hash_no_drift_does_not_remint(self, mock_bot: AsyncMock):
        """Source-MATCH guard: when the displayed card's minted source equals the
        live re-resolved source, the same-hash idle tick must NOT re-render — it
        only refreshes deadlines. This is the loop-termination condition: after a
        re-mint to ``pane`` the next tick sees pane==pane → no re-render storm."""
        import hashlib
        from pathlib import Path

        from cctelegram.handlers import auq_source, pick_token, status_polling
        from cctelegram.handlers.interactive_ui import _interactive_mode
        from cctelegram.handlers.pick_token import _CacheRow, _pick_token_cache
        from cctelegram.handlers.status_polling import extract_interactive_content
        from cctelegram.terminal_parser import resolve_ask_form

        pick_token.reset_for_tests()
        try:
            auq_pane = (
                Path(__file__).parents[1] / "fixtures" / "auq-baseline-pane.txt"
            ).read_text()
            window_id = "@5"
            route = (1, 42, window_id)
            mock_window = MagicMock()
            mock_window.window_id = window_id
            _interactive_mode[(1, 42)] = window_id

            ui_content = extract_interactive_content(auq_pane)
            assert ui_content is not None
            ui_hash = hashlib.sha256(ui_content.content.encode("utf-8")).hexdigest()
            status_polling._last_published_ui_hash[route] = ui_hash

            # Seed the row minted from the REAL live `pane` source — no drift, so
            # the poller must NOT re-render (only refresh deadlines).
            live = auq_source.resolve_auq_source(window_id, None, auq_pane)
            assert live.kind == "pane"
            form = resolve_ask_form(live.payload, auq_pane)
            assert form is not None
            fp = form.fingerprint()
            _pick_token_cache[(1, 42, window_id, fp)] = _CacheRow(
                tokens=["livetok"],
                row_generation=1,
                source_kind=live.kind,
                source_fingerprint=live.source_fingerprint,
                consumed_generation=None,
            )

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
                mock_tmux.capture_pane = AsyncMock(return_value=auq_pane)

                await status_polling.update_status_message(
                    mock_bot, user_id=1, window_id=window_id, thread_id=42
                )

            # Source matches → no re-render storm; deadlines refreshed once.
            mock_handle_ui.assert_not_awaited()
            mock_refresh.assert_awaited_once()
        finally:
            pick_token.reset_for_tests()

    async def test_non_auq_pane_bails_to_refresh(self, mock_bot: AsyncMock):
        """Non-AUQ interactive panes (e.g. the /model Settings picker) parse to
        no AskUserQuestionForm, so the drift check must bail (``resolve_ask_form``
        is None) — no re-mint, just the deadline refresh. Guards against a
        spurious re-mint / crash on a non-AUQ same-hash idle tick."""
        import hashlib

        from cctelegram.handlers import pick_token, status_polling
        from cctelegram.handlers.interactive_ui import _interactive_mode
        from cctelegram.handlers.status_polling import extract_interactive_content
        from cctelegram.terminal_parser import resolve_ask_form

        pick_token.reset_for_tests()
        try:
            settings_pane = (
                "Select model\n\n❯ 1. Default\n  2. Opus\n  3. Sonnet\n\n"
                "Enter to confirm · Esc to cancel\n"
            )
            assert resolve_ask_form(None, settings_pane) is None  # not an AUQ
            window_id = "@5"
            route = (1, 42, window_id)
            mock_window = MagicMock()
            mock_window.window_id = window_id
            _interactive_mode[(1, 42)] = window_id

            ui_content = extract_interactive_content(settings_pane)
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
                mock_tmux.capture_pane = AsyncMock(return_value=settings_pane)

                await status_polling.update_status_message(
                    mock_bot, user_id=1, window_id=window_id, thread_id=42
                )

            mock_handle_ui.assert_not_awaited()
            mock_refresh.assert_awaited_once()
        finally:
            pick_token.reset_for_tests()


@pytest.mark.usefixtures("_clear_interactive_state")
class TestStaleBindingPollerCacheTeardown:
    """Fix 14 RED gate: ``_poll_one_binding`` returns on the stale-binding
    branch (``not w``) BEFORE ever reaching ``update_status_message``'s
    window-gone teardown, so the poller-local route-keyed caches
    (``_last_pane_capture`` / ``_last_published_ui_hash`` / ``_absent_streak``
    / ``_prev_run_state``) survived teardown. A rebound topic reusing the same
    route key then inherited stale entries — defeating the first-picker
    content-drain ordering barrier (``route not in _last_published_ui_hash``)
    and the seed-without-edit semantics of ``_prev_run_state``.

    Pre-fix this is RED: all four dicts retain their entries after the
    stale-binding sweep."""

    @pytest.mark.asyncio
    async def test_stale_binding_pops_all_four_poller_caches(self, mock_bot: AsyncMock):
        from cctelegram.handlers import status_polling

        user_id = 1
        thread_id = 42
        window_id = "@5"
        route = (user_id, thread_id, window_id)

        # Seed every poller-local route-keyed cache as a live binding would.
        status_polling._last_pane_capture[route] = 1234.5
        status_polling._last_published_ui_hash[route] = "deadbeef"
        status_polling._absent_streak[route] = 2
        status_polling._prev_run_state[route] = RunState.RUNNING

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "clear_topic_state", new_callable=AsyncMock
            ) as mock_clear_topic,
            patch.object(
                status_polling.session_manager, "unbind_thread"
            ) as mock_unbind,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=None)
            await status_polling._poll_one_binding(
                mock_bot, user_id, thread_id, window_id
            )

        mock_unbind.assert_called_once_with(user_id, thread_id)
        mock_clear_topic.assert_awaited_once()
        # All four poller-local caches must start clean for a rebound route.
        assert route not in status_polling._last_pane_capture
        assert route not in status_polling._last_published_ui_hash
        assert route not in status_polling._absent_streak
        assert route not in status_polling._prev_run_state


@pytest.mark.usefixtures("_clear_interactive_state")
class TestSiteBSourceDriftRemint:
    """Fix 15 RED gate: the item-1 source-drift re-mint covered only the
    same-hash idle branch; preserve-site (b) (``is_picker_anchor_visible`` on a
    scrolled/compressed Submit screen, ``ui_content`` is None) still refreshed
    deadlines while PRESERVING the minted source tags. A card preserved there
    past the side file's read-TTL keeps stale ``side_file`` tokens, so the
    user's first tap is swallowed as ``source_drift``. The fix applies the SAME
    drift comparison (re-resolve + ``peek_route_source`` by route vs the live
    source) at site (b), via one shared helper, with the same loop-safety
    invariant (exactly ONE re-mint; the next tick sees pane==pane → converges).
    """

    @staticmethod
    def _scrolled_submit_pane() -> str:
        """The site-(b) shape: the tab header has scrolled off, so
        ``extract_interactive_content`` is None while the Submit tail anchors
        are visible AND the pane still parses to a review-screen form."""
        from pathlib import Path

        pane = (
            Path(__file__).parents[1] / "fixtures" / "auq_multiq_submit_pane.txt"
        ).read_text()
        lines = pane.splitlines(keepends=True)
        return "".join(lines[2:])  # drop the chrome + tab-header lines

    def _setup_route(self, window_id: str = "@5"):
        from cctelegram.handlers.interactive_ui import (
            _interactive_mode,
            _interactive_msgs,
        )

        ikey = (1, 42)
        _interactive_mode[ikey] = window_id
        _interactive_msgs[ikey] = 777  # site (b) requires has_interactive_surface
        mock_window = MagicMock()
        mock_window.window_id = window_id
        return mock_window

    @pytest.mark.asyncio
    async def test_site_b_pane_shape_assumptions(self):
        """Pin the fixture shape this class depends on."""
        from cctelegram.terminal_parser import (
            extract_interactive_content,
            is_picker_anchor_visible,
            resolve_ask_form,
        )

        pane = self._scrolled_submit_pane()
        assert extract_interactive_content(pane) is None
        assert is_picker_anchor_visible(pane)
        assert resolve_ask_form(None, pane) is not None

    @pytest.mark.asyncio
    async def test_site_b_source_drift_remints_card(self, mock_bot: AsyncMock):
        from cctelegram.handlers import pick_token, status_polling
        from cctelegram.handlers.pick_token import _CacheRow, _pick_token_cache
        from cctelegram.terminal_parser import resolve_ask_form

        pick_token.reset_for_tests()
        try:
            pane = self._scrolled_submit_pane()
            window_id = "@5"
            mock_window = self._setup_route(window_id)

            # The displayed card's row was minted from the (now read-TTL-aged /
            # absent) side file — drifted vs the live pane source. The
            # fingerprint deliberately differs from the pane form's (the
            # side-file form carries the question title); the route-based
            # lookup must still find it.
            side_fp = "0123456789abcdef"
            pane_form = resolve_ask_form(None, pane)
            assert pane_form is not None
            assert side_fp != pane_form.fingerprint()
            _pick_token_cache[(1, 42, window_id, side_fp)] = _CacheRow(
                tokens=["redtok"],
                row_generation=1,
                source_kind="side_file",
                source_fingerprint="sf-fp",
                consumed_generation=None,
            )

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
                mock_tmux.capture_pane = AsyncMock(return_value=pane)

                await status_polling.update_status_message(
                    mock_bot, user_id=1, window_id=window_id, thread_id=42
                )

            # Site (b) must detect the drift and re-mint, NOT refresh stale
            # side_file tokens.
            mock_handle_ui.assert_awaited()
            mock_refresh.assert_not_awaited()
        finally:
            pick_token.reset_for_tests()

    @pytest.mark.asyncio
    async def test_site_b_no_drift_refreshes_deadlines_no_rerender(
        self, mock_bot: AsyncMock
    ):
        """Parity pin: no drift at site (b) → existing behavior byte-for-byte —
        deadlines refreshed, no re-render, card preserved."""
        from cctelegram.handlers import auq_source, pick_token, status_polling
        from cctelegram.handlers.pick_token import _CacheRow, _pick_token_cache
        from cctelegram.terminal_parser import resolve_ask_form

        pick_token.reset_for_tests()
        try:
            pane = self._scrolled_submit_pane()
            window_id = "@5"
            mock_window = self._setup_route(window_id)

            # Row minted from the REAL live source for this pane → no drift.
            live = auq_source.resolve_auq_source(window_id, None, pane)
            form = resolve_ask_form(live.payload, pane)
            assert form is not None
            _pick_token_cache[(1, 42, window_id, form.fingerprint())] = _CacheRow(
                tokens=["livetok"],
                row_generation=1,
                source_kind=live.kind,
                source_fingerprint=live.source_fingerprint,
                consumed_generation=None,
            )

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
                patch.object(
                    status_polling, "clear_interactive_msg", new_callable=AsyncMock
                ) as mock_clear,
            ):
                mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
                mock_tmux.capture_pane = AsyncMock(return_value=pane)

                await status_polling.update_status_message(
                    mock_bot, user_id=1, window_id=window_id, thread_id=42
                )

            mock_handle_ui.assert_not_awaited()
            mock_refresh.assert_awaited_once()
            mock_clear.assert_not_awaited()
        finally:
            pick_token.reset_for_tests()

    @pytest.mark.asyncio
    async def test_site_b_drift_remint_terminates_next_tick(self, mock_bot: AsyncMock):
        """Loop-safety at site (b): after the drift re-mint, the next tick sees
        live pane == minted pane → no further re-render (exactly ONE re-mint)."""
        from cctelegram.handlers import auq_source, pick_token, status_polling
        from cctelegram.handlers.pick_token import _CacheRow, _pick_token_cache
        from cctelegram.terminal_parser import resolve_ask_form

        pick_token.reset_for_tests()
        try:
            pane = self._scrolled_submit_pane()
            window_id = "@5"
            mock_window = self._setup_route(window_id)

            # Tick-1 seed: drifted side_file row.
            _pick_token_cache[(1, 42, window_id, "0123456789abcdef")] = _CacheRow(
                tokens=["redtok"],
                row_generation=1,
                source_kind="side_file",
                source_fingerprint="sf-fp",
                consumed_generation=None,
            )

            with (
                patch.object(status_polling, "tmux_manager") as mock_tmux,
                patch.object(
                    status_polling.pick_token,
                    "refresh_route_deadlines",
                    new_callable=AsyncMock,
                ),
                patch.object(
                    status_polling, "handle_interactive_ui", new_callable=AsyncMock
                ) as mock_handle_ui,
            ):
                mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
                mock_tmux.capture_pane = AsyncMock(return_value=pane)

                # Tick 1: drift detected → re-mint.
                await status_polling.update_status_message(
                    mock_bot, user_id=1, window_id=window_id, thread_id=42
                )
                assert mock_handle_ui.await_count == 1

                # Simulate the real mint_row outcome: one pane-sourced row at
                # the live pane fingerprint (hygiene dropped the side_file row).
                _pick_token_cache.clear()
                live = auq_source.resolve_auq_source(window_id, None, pane)
                pane_form = resolve_ask_form(live.payload, pane)
                assert pane_form is not None
                _pick_token_cache[(1, 42, window_id, pane_form.fingerprint())] = (
                    _CacheRow(
                        tokens=["panetok"],
                        row_generation=2,
                        source_kind=live.kind,
                        source_fingerprint=live.source_fingerprint,
                        consumed_generation=None,
                    )
                )

                # Tick 2: live pane == minted pane → no drift → NO re-mint.
                await status_polling.update_status_message(
                    mock_bot, user_id=1, window_id=window_id, thread_id=42
                )
                assert mock_handle_ui.await_count == 1
        finally:
            pick_token.reset_for_tests()

    @pytest.mark.asyncio
    async def test_site_b_drift_tick_still_promotes_waiting(self, mock_bot: AsyncMock):
        """Hermes round-2 P3: SET (b) runs BEFORE the drift re-mint early
        return. Site (b) is pane-confirmed (``is_picker_anchor_visible``)
        regardless of token-source drift, so a drift tick on an active
        RUNNING route must still promote it to WAITING_ON_USER — not leave
        the digest/typing on RUNNING for an extra poll cycle."""
        from cctelegram import route_runtime
        from cctelegram.handlers import pick_token, status_polling
        from cctelegram.handlers.pick_token import _CacheRow, _pick_token_cache
        from cctelegram.route_runtime import RunState

        pick_token.reset_for_tests()
        route_runtime.reset_for_tests()
        try:
            pane = self._scrolled_submit_pane()
            window_id = "@5"
            route = (1, 42, window_id)
            mock_window = self._setup_route(window_id)

            # Active RUNNING route with empty open_tools — the promotable state.
            await route_runtime.mark_inbound_sent(route)
            assert route_runtime.snapshot(route).run_state is RunState.RUNNING

            # Drifted side_file row → the drift re-mint fires this tick.
            _pick_token_cache[(1, 42, window_id, "0123456789abcdef")] = _CacheRow(
                tokens=["redtok"],
                row_generation=1,
                source_kind="side_file",
                source_fingerprint="sf-fp",
                consumed_generation=None,
            )

            with (
                patch.object(status_polling, "tmux_manager") as mock_tmux,
                patch.object(
                    status_polling.pick_token,
                    "refresh_route_deadlines",
                    new_callable=AsyncMock,
                ),
                patch.object(
                    status_polling, "handle_interactive_ui", new_callable=AsyncMock
                ) as mock_handle_ui,
            ):
                mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
                mock_tmux.capture_pane = AsyncMock(return_value=pane)

                await status_polling.update_status_message(
                    mock_bot, user_id=1, window_id=window_id, thread_id=42
                )

            # Drift re-mint fired AND the promotion landed on the same tick.
            mock_handle_ui.assert_awaited()
            snap = route_runtime.snapshot(route)
            assert snap.interactive_pending is True
            assert snap.run_state is RunState.WAITING_ON_USER
        finally:
            pick_token.reset_for_tests()
            route_runtime.reset_for_tests()


# ── EPM prose-ordering anchor (PR-1) ─────────────────────────────────────────
#
# ExitPlanMode has no PreToolUse side file, so its prose-ordering anchor is the
# poller's FIRST-DETECTION stamp: `_epm_surface_first_seen_at[route] = now` (via
# `setdefault`, so it's the first detection, never a sliding window). The stamp
# is read by `_maybe_post_live_prose` (peek_epm_surface_emitted_at) and cleared
# at every EPM lifecycle end.


@pytest.mark.usefixtures("_clear_interactive_state")
class TestEpmSurfaceAnchor:
    _WID = "@epm"
    _ROUTE = (1, 42, "@epm")

    @pytest.fixture(autouse=True)
    def _clear_epm(self):
        from cctelegram.handlers import status_polling

        getattr(status_polling, "_epm_surface_first_seen_at", {}).clear()
        from cctelegram.handlers.interactive_ui import _interactive_mode

        _interactive_mode.clear()
        yield
        getattr(status_polling, "_epm_surface_first_seen_at", {}).clear()
        _interactive_mode.clear()

    def test_peek_none_when_unstamped(self):
        from cctelegram.handlers.status_polling import peek_epm_surface_emitted_at

        assert peek_epm_surface_emitted_at(1, 42, self._WID) is None

    @pytest.mark.asyncio
    async def test_exit_plan_pane_stamps_first_detect(
        self, mock_bot: AsyncMock, sample_pane_exit_plan: str
    ):
        from cctelegram.handlers.status_polling import peek_epm_surface_emitted_at

        mock_window = MagicMock()
        mock_window.window_id = self._WID
        with (
            patch("cctelegram.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "cctelegram.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_exit_plan)
            # hermes P3: the stamp must be set BEFORE handle_interactive_ui runs
            # (that's where the real _maybe_post_live_prose reads it). Assert it
            # is already present at the moment the render is invoked — a stamp set
            # AFTER the render would leave emitted_at=None on the first detection.
            stamp_at_render: list[float | None] = []

            async def _capture(*args, **kwargs):
                stamp_at_render.append(peek_epm_surface_emitted_at(1, 42, self._WID))
                return True

            mock_handle_ui.side_effect = _capture
            before = time.time()
            await update_status_message(
                mock_bot, user_id=1, window_id=self._WID, thread_id=42
            )
        assert mock_handle_ui.called
        assert stamp_at_render and stamp_at_render[0] is not None, (
            "EPM anchor was not stamped BEFORE handle_interactive_ui read it"
        )
        stamp = peek_epm_surface_emitted_at(1, 42, self._WID)
        assert stamp is not None and stamp >= before

    @pytest.mark.asyncio
    async def test_non_epm_pane_does_not_stamp(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        from cctelegram.handlers.status_polling import peek_epm_surface_emitted_at

        mock_window = MagicMock()
        mock_window.window_id = self._WID
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
                mock_bot, user_id=1, window_id=self._WID, thread_id=42
            )
        # Settings picker is NOT ExitPlanMode → no stamp.
        assert peek_epm_surface_emitted_at(1, 42, self._WID) is None

    @pytest.mark.asyncio
    async def test_setdefault_keeps_first_detect_in_mode(
        self, mock_bot: AsyncMock, sample_pane_exit_plan: str
    ):
        """A second EPM observation in the SAME lifecycle (in-mode path) keeps the
        FIRST stamp (setdefault), so the anchor is the emission instant."""
        from cctelegram.handlers import status_polling
        from cctelegram.handlers.status_polling import peek_epm_surface_emitted_at
        from cctelegram.handlers.interactive_ui import _interactive_mode

        # Force "in interactive mode for THIS window" so the in-mode block runs.
        _interactive_mode[(1, 42)] = self._WID
        status_polling._epm_surface_first_seen_at[self._ROUTE] = 100.0
        mock_window = MagicMock()
        mock_window.window_id = self._WID
        with (
            patch("cctelegram.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "cctelegram.handlers.status_polling._maybe_repaint_digest_on_transition",
                new_callable=AsyncMock,
            ),
            patch(
                "cctelegram.handlers.status_polling._remint_on_source_drift",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "cctelegram.handlers.status_polling.pick_token.refresh_route_deadlines",
                new_callable=AsyncMock,
            ),
            patch(
                "cctelegram.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
            patch("cctelegram.handlers.status_polling.time.time", return_value=9999.0),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_exit_plan)
            mock_handle_ui.return_value = True
            await update_status_message(
                mock_bot, user_id=1, window_id=self._WID, thread_id=42
            )
        assert peek_epm_surface_emitted_at(1, 42, self._WID) == 100.0

    def test_on_interactive_clear_pops_stamp(self):
        from cctelegram.handlers import status_polling
        from cctelegram.handlers.status_polling import (
            _on_interactive_clear,
            peek_epm_surface_emitted_at,
        )

        status_polling._epm_surface_first_seen_at[self._ROUTE] = 123.0
        _on_interactive_clear(1, 42, self._WID)
        assert peek_epm_surface_emitted_at(1, 42, self._WID) is None

    def test_clear_route_caches_pops_stamp(self):
        from cctelegram.handlers import status_polling
        from cctelegram.handlers.status_polling import (
            clear_route_caches_for_topic,
            peek_epm_surface_emitted_at,
        )

        status_polling._epm_surface_first_seen_at[self._ROUTE] = 123.0
        clear_route_caches_for_topic(1, 42)
        assert peek_epm_surface_emitted_at(1, 42, self._WID) is None


@pytest.mark.usefixtures("_clear_interactive_state")
class TestEpmSurfaceAnchorModeEndClear:
    """hermes P3: pin the gap-free A3-FIX backstop — the poller mode-end
    reconciliation pops the EPM anchor when the route is observed NOT in
    interactive mode (an EPM that resolved without firing the clear callback), so
    a SECOND EPM in the topic can't reuse the first's stale stamp."""

    _WID = "@epm2"
    _ROUTE = (1, 42, "@epm2")

    @pytest.fixture(autouse=True)
    def _clear_epm(self):
        from cctelegram.handlers import status_polling
        from cctelegram.handlers.interactive_ui import _interactive_mode

        status_polling._epm_surface_first_seen_at.clear()
        _interactive_mode.clear()
        yield
        status_polling._epm_surface_first_seen_at.clear()
        _interactive_mode.clear()

    @pytest.mark.asyncio
    async def test_mode_end_reconciliation_pops_stamp(self, mock_bot: AsyncMock):
        from cctelegram.handlers import status_polling
        from cctelegram.handlers.status_polling import peek_epm_surface_emitted_at

        # A stamp left by a now-resolved EPM; the route is NOT in interactive mode
        # (the autouse fixture cleared _interactive_mode), so the poller's
        # interactive_window != window_id mode-end block runs.
        status_polling._epm_surface_first_seen_at[self._ROUTE] = 111.0
        bar = "─" * 40
        idle_pane = f"idle output\n{bar}\n❯ \n{bar}\n  [Opus] Context: 10%\n"
        mock_window = MagicMock()
        mock_window.window_id = self._WID
        with (
            patch("cctelegram.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "cctelegram.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=idle_pane)
            await update_status_message(
                mock_bot, user_id=1, window_id=self._WID, thread_id=42
            )
        assert peek_epm_surface_emitted_at(1, 42, self._WID) is None
