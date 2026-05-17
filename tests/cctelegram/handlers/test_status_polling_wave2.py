"""Tests for Wave 2 adaptive pane-capture gating in ``status_polling``.

Wave 2 stops the 1Hz pane scrape from running on every binding every cycle.
The 1Hz loop still ticks (so idle-clear and stale-binding cleanup remain
responsive), but ``capture_pane`` only fires when one of:
  - this route is currently in interactive mode,
  - WATCHDOG_INTERVAL seconds have elapsed since the last capture,
  - V1 busy indicator is in use (V1 needs the pane every tick).

These tests pin those criteria, plus the regression that stale-binding
cleanup still runs even on capture-skipped ticks.

A separate suite at the bottom verifies the dead-code permission /
bash-approval patterns are gone from ``terminal_parser.UI_PATTERNS`` and
that AskUserQuestion / ExitPlanMode detection still works.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cctelegram.handlers import status_polling
from cctelegram.terminal_parser import is_interactive_ui


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 999
    bot.send_message.return_value = sent_msg
    return bot


@pytest.fixture
def _clear_state():
    """Reset all per-route state between tests."""
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


# Active pane (Claude is running) — used to assert capture / no-capture
# without triggering interactive-UI detection.
_ACTIVE_PANE = (
    "✻ Cooking for 2s\n"
    "──────────────────────────────────────\n"
    "❯ \n"
    "──────────────────────────────────────\n"
    "  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt\n"
)


@pytest.mark.usefixtures("_clear_state")
class TestAdaptivePaneCapture:
    """Adaptive ``capture_pane`` gating: only scrape when needed."""

    @pytest.mark.asyncio
    async def test_skip_capture_under_watchdog_no_interactive(
        self, mock_bot: AsyncMock
    ):
        """Non-interactive route with a recent capture → capture_pane NOT called."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        route = (1, 42, window_id)

        fake_now = [1000.0]

        def fake_monotonic() -> float:
            return fake_now[0]

        # Pretend we captured 1s ago — well within the 10s watchdog.
        status_polling._last_pane_capture[route] = fake_now[0] - 1.0

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(status_polling.time, "monotonic", side_effect=fake_monotonic),
            patch.object(status_polling.config, "busy_indicator_v2", True),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=_ACTIVE_PANE)

            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_tmux.capture_pane.assert_not_called()

    @pytest.mark.asyncio
    async def test_capture_when_route_is_interactive(self, mock_bot: AsyncMock):
        """Route in interactive mode → capture_pane IS called (need to detect close)."""
        from cctelegram.handlers.interactive_ui import _interactive_mode

        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        route = (1, 42, window_id)

        # Mark route as interactive for THIS window.
        _interactive_mode[(1, 42)] = window_id

        fake_now = [1000.0]

        def fake_monotonic() -> float:
            return fake_now[0]

        # Watchdog NOT elapsed.
        status_polling._last_pane_capture[route] = fake_now[0] - 1.0

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "clear_interactive_msg", new_callable=AsyncMock
            ),
            patch.object(status_polling.time, "monotonic", side_effect=fake_monotonic),
            patch.object(status_polling.config, "busy_indicator_v2", True),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=_ACTIVE_PANE)

            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_tmux.capture_pane.assert_called_once()

    @pytest.mark.asyncio
    async def test_capture_when_watchdog_elapsed(self, mock_bot: AsyncMock):
        """Last capture > WATCHDOG_INTERVAL ago → capture_pane IS called."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        route = (1, 42, window_id)

        fake_now = [1000.0]

        def fake_monotonic() -> float:
            return fake_now[0]

        # Last capture 11s ago — past the 10s watchdog.
        status_polling._last_pane_capture[route] = (
            fake_now[0] - status_polling.WATCHDOG_INTERVAL - 1.0
        )

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "enqueue_status_update", new_callable=AsyncMock
            ),
            patch.object(status_polling.time, "monotonic", side_effect=fake_monotonic),
            patch.object(status_polling.config, "busy_indicator_v2", True),
            patch.object(
                status_polling.session_manager,
                "resolve_session_for_window",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=_ACTIVE_PANE)

            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_tmux.capture_pane.assert_called_once()
            # Watchdog timestamp updated for the next cycle.
            assert status_polling._last_pane_capture[route] == fake_now[0]

    @pytest.mark.asyncio
    async def test_stale_binding_cleanup_runs_when_capture_skipped(
        self, mock_bot: AsyncMock
    ):
        """find_window_by_id returns None → unbind_thread fires regardless of
        whether capture_pane would have been skipped. Stale-binding cleanup
        is the one path that MUST stay responsive on every tick.
        """
        window_id = "@5"
        route = (1, 42, window_id)

        fake_now = [1000.0]

        def fake_monotonic() -> float:
            return fake_now[0]

        # Recent capture so capture would be skipped, but the window is gone
        # so we never even reach the gate — the stale-binding path triggers
        # in ``_poll_one_binding``.
        status_polling._last_pane_capture[route] = fake_now[0] - 1.0

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(status_polling, "session_manager") as mock_sm,
            patch.object(status_polling, "clear_topic_state", new_callable=AsyncMock),
            patch.object(status_polling.time, "monotonic", side_effect=fake_monotonic),
            patch.object(status_polling.config, "busy_indicator_v2", True),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=None)
            mock_tmux.capture_pane = AsyncMock(return_value=_ACTIVE_PANE)
            mock_sm.unbind_thread = MagicMock()

            await status_polling._poll_one_binding(
                mock_bot, user_id=1, thread_id=42, wid=window_id
            )

            mock_sm.unbind_thread.assert_called_once_with(1, 42)
            # The watchdog entry for the dead route is dropped by the
            # window-gone branch in update_status_message OR by
            # _poll_one_binding's early return — either way capture_pane
            # is never called for a dead window.
            mock_tmux.capture_pane.assert_not_called()

    @pytest.mark.asyncio
    async def test_v1_indicator_captures_every_tick(self, mock_bot: AsyncMock):
        """V1 indicator (busy_indicator_v2 = False) bypasses the watchdog gate
        entirely — V1 needs a fresh pane every tick to derive ``is_running``
        for the typing-action send.
        """
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        route = (1, 42, window_id)

        fake_now = [1000.0]

        def fake_monotonic() -> float:
            return fake_now[0]

        # Watchdog NOT elapsed — V1 should capture anyway.
        status_polling._last_pane_capture[route] = fake_now[0] - 0.5

        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "enqueue_status_update", new_callable=AsyncMock
            ),
            patch.object(status_polling.time, "monotonic", side_effect=fake_monotonic),
            patch.object(status_polling.config, "busy_indicator_v2", False),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=_ACTIVE_PANE)

            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_tmux.capture_pane.assert_called_once()


# ── terminal_parser: dead-pattern removal ─────────────────────────────────


class TestPermissionPatternsRemoved:
    """The permission / bash-approval patterns are intentionally gone — the
    deployment runs Claude with ``--dangerously-skip-permissions`` so they
    never render. Verify both that the old pane shapes no longer match AND
    the kept patterns still do.
    """

    def test_old_permission_proceed_no_longer_matches(self):
        pane = "  Do you want to proceed?\n  Some permission details\n  Esc to cancel\n"
        assert is_interactive_ui(pane) is False

    def test_old_permission_make_edit_no_longer_matches(self):
        pane = (
            "  Do you want to make this edit to file.py?\n"
            "  Some details\n"
            "  Esc to cancel\n"
        )
        assert is_interactive_ui(pane) is False

    def test_old_permission_numbered_menu_no_longer_matches(self):
        pane = "  ❯  1. Yes\n     2. No\n     3. Cancel\n"
        assert is_interactive_ui(pane) is False

    def test_old_bash_approval_no_longer_matches(self):
        pane = (
            "  Bash command\n"
            "  ls -la\n"
            "  This command requires approval\n"
            "  Esc to cancel\n"
        )
        assert is_interactive_ui(pane) is False

    def test_ask_user_question_still_detected_single_tab(self):
        pane = "  ☐ Option A\n  ☐ Option B\n  Enter to select\n"
        assert is_interactive_ui(pane) is True

    def test_ask_user_question_still_detected_multi_tab(self):
        pane = "  ←  ☐ Option A\n     ☐ Option B\n     ☐ Option C\n  Enter to select\n"
        assert is_interactive_ui(pane) is True

    def test_exit_plan_mode_still_detected(self):
        pane = (
            "  Would you like to proceed?\n"
            "  ─────────────────────────────────\n"
            "  Yes     No\n"
            "  ─────────────────────────────────\n"
            "  ctrl-g to edit in vim\n"
        )
        assert is_interactive_ui(pane) is True
