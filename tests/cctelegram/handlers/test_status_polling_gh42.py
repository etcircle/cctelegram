"""GH #42 leg 2 (W2b) — the poller freezes the pane-idle net while WAITING.

route_runtime guarantees commit-on-WAITING is a no-op (W2a), but the poller
must not keep arming deadlines / re-capturing panes against a WAITING route —
and a 🟡 Busy status card published BEFORE the WAITING transition must still
clear, decoupled from the net and ahead of every ``skip_status`` early return
(a backed-up content queue must not strand the card — hermes r2 P2-1).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cctelegram import route_runtime
from cctelegram.route_runtime import RunState, TranscriptLifecycleEvent

IDLE_PANE = (
    "$ echo done\n"
    "done\n"
    "──────────────────────────────────────\n"
    "❯ \n"
    "──────────────────────────────────────\n"
    "  [Opus 4.6] Context: 50%\n"
)


@pytest.fixture(autouse=True)
def _reset():
    route_runtime.reset_for_tests()
    yield
    route_runtime.reset_for_tests()


@pytest.fixture
def mock_bot() -> AsyncMock:
    return AsyncMock()


async def _make_waiting(route: route_runtime.Route) -> None:
    """Drive ``route`` to a notification-set WAITING over an open tool —
    the incident's exact flavor. ``set_at`` is NOW on the wall clock: the
    poller's consume path enforces the runtime TTL against ``time.time()``,
    so a synthetic old set_at would be cleared as ttl-expired mid-test."""
    import time as _time

    await route_runtime.ingest_transcript_event(
        route,
        TranscriptLifecycleEvent(
            role="assistant",
            block_type="tool_use",
            tool_use_id="t1",
            tool_name="Bash",
            stop_reason=None,
        ),
    )
    await route_runtime.mark_notification_pending(
        route, set_at=_time.time(), generation="g1"
    )
    assert route_runtime.snapshot(route).run_state is RunState.WAITING_ON_USER


@pytest.mark.asyncio
async def test_waiting_route_never_arms_pane_idle_deadline(mock_bot: AsyncMock):
    """Idle frames against a WAITING route must not arm the net (W2b) —
    arming + the due commit were the latch fuel in the incident."""
    from cctelegram.handlers import status_polling

    window_id = "@4"
    route: route_runtime.Route = (1, 42, window_id)
    await _make_waiting(route)

    mock_window = MagicMock()
    mock_window.window_id = window_id
    fake_now = [1000.0]

    with (
        patch.object(status_polling, "tmux_manager") as mock_tmux,
        patch.object(
            status_polling, "enqueue_status_update", new_callable=AsyncMock
        ) as mock_enqueue,
        patch.object(status_polling, "handle_interactive_ui", new_callable=AsyncMock),
        patch.object(status_polling.time, "monotonic", side_effect=lambda: fake_now[0]),
    ):
        mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
        mock_tmux.capture_pane = AsyncMock(return_value=IDLE_PANE)

        for _ in range(3):
            await status_polling.update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )
            fake_now[0] += status_polling.IDLE_CLEAR_DELAY_SECONDS + 1.0

    snap = route_runtime.snapshot(route)
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.pane_idle_clear_at is None  # never armed while WAITING
    assert route_runtime._state[route].pane_idle_cleared is False
    # No card on screen → nothing to clear either.
    assert mock_enqueue.await_count == 0


@pytest.mark.asyncio
async def test_waiting_route_with_visible_card_clears_even_with_skip_status(
    mock_bot: AsyncMock,
):
    """hermes r2 P2-1: a Busy card published before the WAITING transition
    must clear even when ``skip_status=True`` (backed-up queue), and the
    pane-idle fields must stay untouched."""
    from cctelegram.handlers import status_polling

    window_id = "@4"
    route: route_runtime.Route = (1, 42, window_id)
    await _make_waiting(route)
    route_runtime.mark_status_card_published(route, 777)

    mock_window = MagicMock()
    mock_window.window_id = window_id

    with (
        patch.object(status_polling, "tmux_manager") as mock_tmux,
        patch.object(
            status_polling, "enqueue_status_update", new_callable=AsyncMock
        ) as mock_enqueue,
        patch.object(status_polling, "handle_interactive_ui", new_callable=AsyncMock),
    ):
        mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
        mock_tmux.capture_pane = AsyncMock(return_value=IDLE_PANE)

        await status_polling.update_status_message(
            mock_bot, user_id=1, window_id=window_id, thread_id=42, skip_status=True
        )

    assert mock_enqueue.await_count == 1
    args, kwargs = mock_enqueue.await_args
    assert args[3] is None
    assert kwargs.get("thread_id") == 42
    snap = route_runtime.snapshot(route)
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.pane_idle_clear_at is None
    assert route_runtime._state[route].pane_idle_cleared is False


@pytest.mark.asyncio
async def test_process_idle_clear_only_skips_capture_and_commit_on_waiting(
    mock_bot: AsyncMock,
):
    """The capture-skipped cleanup path must not re-capture or commit
    against a WAITING route, even with a stale armed-and-due deadline from
    the pre-WAITING stretch."""
    from cctelegram.handlers import status_polling

    window_id = "@4"
    route: route_runtime.Route = (1, 42, window_id)
    # Deadline armed while the route was still active...
    await route_runtime.ingest_transcript_event(
        route,
        TranscriptLifecycleEvent(
            role="assistant",
            block_type="tool_use",
            tool_use_id="t1",
            tool_name="Bash",
            stop_reason=None,
        ),
    )
    route_runtime.arm_pane_idle_clear(route, now=100.0)
    # ...then the WAITING transition lands (notification commit re-arms /
    # cancels, so re-arm a fresh deadline to simulate the stale-armed shape).
    await route_runtime.mark_notification_pending(route, set_at=500.0, generation="g1")
    route_runtime.arm_pane_idle_clear(route, now=200.0)
    assert route_runtime.snapshot(route).run_state is RunState.WAITING_ON_USER

    with (
        patch.object(status_polling, "tmux_manager") as mock_tmux,
        patch.object(
            status_polling, "enqueue_status_update", new_callable=AsyncMock
        ) as mock_enqueue,
        patch.object(
            status_polling.time,
            "monotonic",
            side_effect=lambda: 200.0 + status_polling.IDLE_CLEAR_DELAY_SECONDS + 1.0,
        ),
    ):
        mock_tmux.capture_pane = AsyncMock(return_value=IDLE_PANE)
        await status_polling._process_idle_clear_only(
            mock_bot, user_id=1, window_id=window_id, thread_id=42, skip_status=False
        )

    # No second-observation capture, no commit, no card clear while WAITING.
    assert mock_tmux.capture_pane.await_count == 0
    assert mock_enqueue.await_count == 0
    snap = route_runtime.snapshot(route)
    assert snap.run_state is RunState.WAITING_ON_USER
    assert route_runtime._state[route].pane_idle_cleared is False
