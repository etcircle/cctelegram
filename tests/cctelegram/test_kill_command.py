"""Tests for /kill — kill tmux window + clear bot state, leave Telegram topic open."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_update(user_id: int = 1, thread_id: int | None = 42) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.message_thread_id = thread_id
    update.effective_chat = MagicMock()
    update.effective_chat.type = "supergroup"
    update.effective_chat.id = 100
    return update


def _make_context() -> MagicMock:
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {}
    return context


class TestKillCommand:
    @pytest.mark.asyncio
    async def test_outside_topic_replies_with_error(self):
        """/kill in DM / general → error reply, no cleanup."""
        update = _make_update(thread_id=None)
        context = _make_context()

        safe_reply_mock = AsyncMock()
        with (
            patch("cctelegram.bot.is_user_allowed", return_value=True),
            patch("cctelegram.bot._get_thread_id", return_value=None),
            patch("cctelegram.bot.session_manager") as mock_sm,
            patch("cctelegram.bot.tmux_manager") as mock_tmux,
            patch(
                "cctelegram.bot.clear_topic_state", new_callable=AsyncMock
            ) as mock_clear,
            patch("cctelegram.bot.safe_reply", safe_reply_mock),
        ):
            from cctelegram.bot import kill_command

            await kill_command(update, context)

            safe_reply_mock.assert_awaited_once()
            args, _ = safe_reply_mock.call_args
            assert "only works in a topic" in args[1]
            mock_sm.unbind_thread.assert_not_called()
            mock_tmux.find_window_by_id.assert_not_called()
            mock_clear.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_binding_replies_with_error(self):
        """/kill on an unbound topic → error reply, no cleanup."""
        update = _make_update()
        context = _make_context()

        safe_reply_mock = AsyncMock()
        with (
            patch("cctelegram.bot.is_user_allowed", return_value=True),
            patch("cctelegram.bot._get_thread_id", return_value=42),
            patch("cctelegram.bot.session_manager") as mock_sm,
            patch("cctelegram.bot.tmux_manager") as mock_tmux,
            patch(
                "cctelegram.bot.clear_topic_state", new_callable=AsyncMock
            ) as mock_clear,
            patch("cctelegram.bot.safe_reply", safe_reply_mock),
        ):
            mock_sm.get_window_for_thread.return_value = None

            from cctelegram.bot import kill_command

            await kill_command(update, context)

            safe_reply_mock.assert_awaited_once()
            args, _ = safe_reply_mock.call_args
            assert "No session bound" in args[1]
            mock_sm.unbind_thread.assert_not_called()
            mock_tmux.find_window_by_id.assert_not_called()
            mock_clear.assert_not_called()

    @pytest.mark.asyncio
    async def test_alive_window_killed_and_state_cleared(self):
        """Happy path: window alive → kill + unbind + clear_topic_state(drop_pending=True)."""
        update = _make_update()
        context = _make_context()

        safe_reply_mock = AsyncMock()
        with (
            patch("cctelegram.bot.is_user_allowed", return_value=True),
            patch("cctelegram.bot._get_thread_id", return_value=42),
            patch("cctelegram.bot.session_manager") as mock_sm,
            patch("cctelegram.bot.tmux_manager") as mock_tmux,
            patch(
                "cctelegram.bot.clear_topic_state", new_callable=AsyncMock
            ) as mock_clear,
            patch("cctelegram.bot.safe_reply", safe_reply_mock),
        ):
            mock_sm.get_window_for_thread.return_value = "@5"
            mock_sm.get_display_name.return_value = "project"
            fake_window = MagicMock()
            fake_window.window_id = "@5"
            mock_tmux.find_window_by_id = AsyncMock(return_value=fake_window)
            mock_tmux.kill_window = AsyncMock(return_value=True)

            from cctelegram.bot import kill_command

            await kill_command(update, context)

            mock_tmux.kill_window.assert_awaited_once_with("@5")
            mock_sm.unbind_thread.assert_called_once_with(1, 42)
            mock_clear.assert_awaited_once_with(
                1, 42, context.bot, context.user_data, drop_pending=True
            )
            # Confirmation reply mentions the killed display name.
            safe_reply_mock.assert_awaited_once()
            args, _ = safe_reply_mock.call_args
            assert "project" in args[1]
            assert "Topic remains open" in args[1]

    @pytest.mark.asyncio
    async def test_window_already_gone_still_cleans_up(self):
        """Window externally killed before /kill → no kill, but unbind + clear still run."""
        update = _make_update()
        context = _make_context()

        safe_reply_mock = AsyncMock()
        with (
            patch("cctelegram.bot.is_user_allowed", return_value=True),
            patch("cctelegram.bot._get_thread_id", return_value=42),
            patch("cctelegram.bot.session_manager") as mock_sm,
            patch("cctelegram.bot.tmux_manager") as mock_tmux,
            patch(
                "cctelegram.bot.clear_topic_state", new_callable=AsyncMock
            ) as mock_clear,
            patch("cctelegram.bot.safe_reply", safe_reply_mock),
        ):
            mock_sm.get_window_for_thread.return_value = "@5"
            mock_sm.get_display_name.return_value = "project"
            mock_tmux.find_window_by_id = AsyncMock(return_value=None)
            mock_tmux.kill_window = AsyncMock()

            from cctelegram.bot import kill_command

            await kill_command(update, context)

            mock_tmux.kill_window.assert_not_awaited()
            mock_sm.unbind_thread.assert_called_once_with(1, 42)
            mock_clear.assert_awaited_once()
            safe_reply_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disallowed_user_silently_ignored(self):
        """/kill from a user not in allowlist → no-op, no reply."""
        update = _make_update()
        context = _make_context()

        safe_reply_mock = AsyncMock()
        with (
            patch("cctelegram.bot.is_user_allowed", return_value=False),
            patch("cctelegram.bot.session_manager") as mock_sm,
            patch("cctelegram.bot.tmux_manager") as mock_tmux,
            patch(
                "cctelegram.bot.clear_topic_state", new_callable=AsyncMock
            ) as mock_clear,
            patch("cctelegram.bot.safe_reply", safe_reply_mock),
        ):
            from cctelegram.bot import kill_command

            await kill_command(update, context)

            safe_reply_mock.assert_not_awaited()
            mock_sm.unbind_thread.assert_not_called()
            mock_tmux.find_window_by_id.assert_not_called()
            mock_clear.assert_not_awaited()
