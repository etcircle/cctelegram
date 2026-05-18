"""Terminal status line polling for thread-bound windows.

Provides background polling of terminal status lines for all active users:
  - Detects Claude Code status (working, waiting, etc.)
  - Detects interactive UIs (permission prompts) not triggered via JSONL
  - Updates status messages in Telegram
  - Polls thread_bindings (each topic = one window)
  - Cleans up bindings whose tmux window has been killed

Topic-existence detection is reactive only: real topic_send/topic_edit failures
classify into ``_TOPIC_BROKEN_OUTCOMES`` and trigger emergency DMs from the
message queue. We deliberately do NOT poll Telegram for topic liveness; the
previously-used ``unpin_all_forum_topic_messages`` probe was destructive (it
clears pinned messages on success, not a no-op) and runs every 60s for every
bound topic, which would silently wipe legitimate user pins.

Key components:
  - STATUS_POLL_INTERVAL: Polling frequency (1 second)
  - status_poll_loop: Background polling task
  - update_status_message: Poll and enqueue status updates
"""

import asyncio
import logging
import time
from typing import Literal

from telegram import Bot
from telegram.constants import ChatAction

from ..config import config
from ..session import session_manager
from ..terminal_parser import (
    is_interactive_ui,
    is_status_active,
    parse_status_line,
)
from ..transcript_parser import read_latest_usage
from ..tmux_manager import TmuxWindow, tmux_manager
from . import busy_indicator
from .busy_indicator import RunState
from .interactive_ui import (
    clear_interactive_msg,
    get_interactive_window,
    handle_interactive_ui,
    has_interactive_surface,
)
from .cleanup import clear_topic_state
from .message_queue import enqueue_status_update, get_content_queue

logger = logging.getLogger(__name__)

# Status polling interval
STATUS_POLL_INTERVAL = 1.0  # seconds - faster response (rate limiting at send layer)

# Watchdog interval for adaptive pane capture. The 1Hz loop still ticks
# every second so stale-binding cleanup and idle-clear delay processing
# stay responsive, but the expensive ``capture_pane`` subprocess only
# fires when one of:
#   - this route is currently in interactive mode (we need to detect when
#     the user closes the open UI),
#   - WATCHDOG_INTERVAL seconds have elapsed since the last capture for
#     this route (catches RestoreCheckpoint / Settings / non-V2 status
#     transitions that don't show up in the JSONL stream),
#   - V1 indicator is in use — V1 still relies on pane-derived ``is_running``
#     to gate the typing-action send and therefore needs a fresh pane every
#     tick. V2 reads ``busy_indicator.state`` directly, so the watchdog is
#     enough to catch missed transitions.
# JSONL-driven AskUserQuestion / ExitPlanMode dispatch already happens in
# ``bot.handle_new_message`` (tool_use → ``handle_interactive_ui``), so the
# pane scrape is a redundant safety net for those — fine to skip it most
# ticks.
WATCHDOG_INTERVAL = 10.0

# Typing-action refresh interval. Telegram drops the native typing indicator
# after ~5s, so we re-emit faster than that. Decoupled from status polling
# because the per-binding tmux fan-out in ``status_poll_loop`` can push the
# full cycle past the 5s TTL (~6-8s on macOS with ~14 bindings), making the
# indicator flash on instead of staying continuous. This loop reads
# ``busy_indicator.state(route)`` directly with no tmux I/O so cadence stays
# tight regardless of binding count.
TYPING_ACTION_INTERVAL = 3.0

# Wall-clock seconds of confirmed idle (post-completion summary or no status
# line) before the stale "🟡 Busy" message is cleared. Time-based rather than
# poll-count-based because the polling loop iterates all bindings sequentially
# — with N bound topics, any single window is only polled every N seconds, so
# a poll-count threshold makes the perceived clear delay scale with how many
# topics the user has open. 4s of confirmed idle is comfortably longer than
# Claude's slowest UI transition while still feeling responsive.
IDLE_CLEAR_DELAY_SECONDS = 4.0

# Per-route idle-state machine, keyed by ``(user_id, thread_id_or_0)``:
#   - missing   → last poll saw an active status (or this route is brand new)
#   - float ts  → first poll where idle was confirmed; waiting out the delay
#   - "cleared" → idle delay elapsed and the clear has already been enqueued;
#                 further idle ticks are no-ops until ``is_running`` flips
#                 back true and the entry is dropped.
_idle_state: dict[tuple[int, int], float | Literal["cleared"]] = {}

# Per-route last-capture timestamp, keyed by ``(user_id, thread_id_or_0,
# window_id)``. Drives the WATCHDOG_INTERVAL gate in ``update_status_message``
# — entries are written each time we successfully scrape a pane and read each
# tick to decide whether to scrape again. Stale entries (route unbound) are
# harmless: a stale dict entry just costs a few bytes until the process
# restarts.
_last_pane_capture: dict[tuple[int, int, str], float] = {}


def reset_idle_counter(user_id: int, thread_id: int | None) -> None:
    """Drop the idle state for a route.

    Called by topic teardown so a re-bound topic starts with a clean slate
    instead of inheriting a stale entry from a previous binding.
    """
    _idle_state.pop((user_id, thread_id or 0), None)


async def _on_busy_activity(route: busy_indicator.Route) -> None:
    """Re-arm the idle-clear state machine when real activity hits a route.

    Wired to ``busy_indicator.register_activity_callback`` so transcript
    events and inbound prompt deliveries drop ``_idle_state[key]`` directly
    without waiting for the next ``WATCHDOG_INTERVAL`` pane scrape. Once a
    route has been "cleared" (idle delay elapsed and ``mark_pane_idle``
    fired), only ``is_running == True`` on a fresh pane scrape would
    otherwise pop the entry — and sub-agent / quick tool turns routinely
    finish between two 10s pane scrapes, leaving ``_idle_state[key] ==
    "cleared"`` while ``busy_indicator`` accumulates open tools and the
    typing indicator keeps refreshing. See
    ``busy_indicator.register_activity_callback`` for the full rationale.
    """
    user_id, thread_id, _wid = route
    _idle_state.pop((user_id, thread_id), None)


busy_indicator.register_activity_callback(_on_busy_activity)


async def update_status_message(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
    skip_status: bool = False,
    window: TmuxWindow | None = None,
) -> None:
    """Poll terminal and check for interactive UIs and status updates.

    UI detection always happens regardless of skip_status. When skip_status=True,
    only UI detection runs (used when message queue is non-empty to avoid
    flooding the queue with status updates).

    Also detects permission prompt UIs (not triggered via JSONL) and enters
    interactive mode when found.

    ``window`` lets callers (e.g. ``_poll_one_binding``) pass in an already-
    resolved TmuxWindow to avoid a redundant find_window_by_id round-trip.
    """
    if window is None:
        window = await tmux_manager.find_window_by_id(window_id)
    if not window:
        # Window gone, enqueue clear (unless skipping status)
        if not skip_status:
            await enqueue_status_update(
                bot, user_id, window_id, None, thread_id=thread_id
            )
        # Drop the watchdog entry so a re-bind starts fresh.
        _last_pane_capture.pop((user_id, thread_id or 0, window_id), None)
        return

    # Adaptive pane-capture gate. The 1Hz loop still runs (so cleanup,
    # idle-clear timing, and stale-binding sweeps remain responsive), but
    # we skip the ``capture_pane`` subprocess most ticks. See
    # WATCHDOG_INTERVAL above for the criteria.
    route = (user_id, thread_id or 0, window_id)
    interactive_window = get_interactive_window(user_id, thread_id)
    in_interactive = interactive_window == window_id
    now_mono = time.monotonic()
    last_capture = _last_pane_capture.get(route)
    watchdog_elapsed = (
        last_capture is None or (now_mono - last_capture) >= WATCHDOG_INTERVAL
    )
    should_capture = in_interactive or watchdog_elapsed or not config.busy_indicator_v2

    if not should_capture:
        # Cleanup-only path: stale-binding cleanup already ran above
        # (find_window_by_id returned a live window). Process the idle-clear
        # state machine so a previously-shown "🟡 Busy" status still gets
        # cleared on schedule, then return without scraping the pane.
        await _process_idle_clear_only(bot, user_id, window_id, thread_id, skip_status)
        return

    pane_text = await tmux_manager.capture_pane(window.window_id)
    if not pane_text:
        # Transient capture failure - keep existing status message
        return
    _last_pane_capture[route] = time.monotonic()

    # Read the next-turn context size from the session's JSONL into the
    # busy_indicator cache. The activity-digest header reads it via
    # ``context_pct`` and the per-message footer (``bot._build_context_footer``)
    # routes through the same cache so the 1M-cap latch is shared.
    # read_latest_usage is mtime+size-cached so a 1Hz poller doesn't re-scan
    # unchanged files.
    if config.busy_indicator_v2:
        session = await session_manager.resolve_session_for_window(window_id)
        if session and session.file_path:
            latest = read_latest_usage(session.file_path)
            if latest is not None:
                busy_indicator.update_context_usage(route, latest.tokens, latest.model)
            else:
                busy_indicator.update_context_usage(route, None, None)
        else:
            busy_indicator.update_context_usage(route, None, None)

    should_check_new_ui = True

    if interactive_window == window_id:
        # User is in interactive mode for THIS window
        if is_interactive_ui(pane_text):
            # Interactive UI still showing — skip status update (user is interacting)
            return
        # Interactive UI gone — but only clear if a real interactive message was
        # actually rendered. Without this guard, a 1Hz tick that lands between
        # ``set_interactive_mode`` (published in ``bot.handle_new_message`` BEFORE
        # the queue-drain + sleep + ``handle_interactive_ui`` render path) and
        # the eventual card send would clear the just-published mode with
        # ``msg_id=None`` and drop the card to plain-text fallback. Skip this
        # cycle until the render has actually published a message id.
        # PR 3: gate on ``has_interactive_surface`` (covers both
        # single-card ``_interactive_msgs`` and multi-tab
        # ``_multi_tab_sessions``). ``get_interactive_msg_id`` alone
        # missed multi-tab sessions and left their cards orphaned.
        if not has_interactive_surface(user_id, thread_id):
            return
        await clear_interactive_msg(user_id, bot, thread_id)
        should_check_new_ui = False
    elif interactive_window is not None:
        # User is in interactive mode for a DIFFERENT window (window switched).
        # Same gate as above.
        if not has_interactive_surface(user_id, thread_id):
            return
        await clear_interactive_msg(user_id, bot, thread_id)

    # Check for permission prompt (interactive UI not triggered via JSONL)
    # ALWAYS check UI, regardless of skip_status
    if should_check_new_ui and is_interactive_ui(pane_text):
        logger.debug(
            "Interactive UI detected in polling (user=%d, window=%s, thread=%s)",
            user_id,
            window_id,
            thread_id,
        )
        # Tag this as the poller call path so handle_interactive_ui can
        # apply AUQ pane-only safety rules when JSONL replay data is absent
        # or stale (render immediately, but suppress unsafe pick buttons).
        await handle_interactive_ui(
            bot, user_id, window_id, thread_id, from_poller=True
        )
        return

    # Compute active-state up front so the typing indicator fires even
    # when ``skip_status`` is True — that's when Claude is busiest and the
    # "Felix's Claude is typing…" line under the topic title is most
    # useful. Without this, the queue gets so full during active work that
    # the rest of update_status_message returns early and the typing
    # action never gets sent.
    status_line = parse_status_line(pane_text)
    key = (user_id, thread_id or 0)

    # When Claude is actively running, the spinner line sits directly above
    # the chrome separator. Post-completion summaries (e.g. "✻ Cooked for
    # 2s") get a blank line inserted above the chrome — same spinner glyph,
    # but Claude is idle. ``is_status_active`` reads that gap; we use it
    # rather than scanning the status text for keywords because Claude's
    # working statuses ("Reading file …") don't always include "esc to
    # interrupt", and past-tense summaries don't always omit the spinner.
    is_running = bool(status_line) and is_status_active(pane_text)

    # V1 path: gate typing-action on the pane-derived ``is_running``. V2
    # delegates the typing-action send to ``typing_action_loop`` (it reads
    # busy_indicator.state directly with no tmux I/O so cadence stays under
    # Telegram's 5s TTL even with many bindings). Firing it from both places
    # would just double-bill the API.
    typing_active = (not config.busy_indicator_v2) and is_running

    if typing_active:
        # Re-emit Telegram's native typing indicator on every active poll
        # so the "Felix's Claude is typing…" line under the topic title
        # stays alive while Claude works. The action expires after ~5s, and
        # we poll roughly every second per binding, so this keeps the
        # indicator continuous without burning excessive API calls.
        # Pass message_thread_id so the indicator shows in the topic, not
        # at the chat level.
        try:
            await bot.send_chat_action(
                chat_id=session_manager.resolve_chat_id(user_id, thread_id),
                action=ChatAction.TYPING,
                message_thread_id=thread_id,
            )
            logger.debug(
                "typing_action user=%d thread=%s window=%s sent",
                user_id,
                thread_id,
                window_id,
            )
        except Exception as e:
            # Best-effort: never block the status update on a transient
            # chat-action failure (rate limit, network, etc.).
            logger.debug(
                "typing_action user=%d thread=%s window=%s failed: %s",
                user_id,
                thread_id,
                window_id,
                e,
            )

    # Normal status line check — skip the rest if queue is non-empty.
    # Typing indicator already fired above so the active-work UX still
    # works during heavy queue activity.
    if skip_status:
        return

    if is_running:
        _idle_state.pop(key, None)
        await enqueue_status_update(
            bot,
            user_id,
            window_id,
            status_line,
            thread_id=thread_id,
        )
        return

    # Either no status line at all, or a static post-completion summary.
    # Wait IDLE_CLEAR_DELAY_SECONDS of confirmed idle, then clear once.
    state = _idle_state.get(key)
    if state == "cleared":
        return  # Already cleared this idle stretch.
    now = time.monotonic()
    if state is None:
        _idle_state[key] = now
        return
    # state is the timestamp of the first idle observation.
    assert isinstance(state, float)
    if (now - state) < IDLE_CLEAR_DELAY_SECONDS:
        return
    _idle_state[key] = "cleared"
    # V2 backstop: same confirmed-idle window that clears the status card
    # also reconciles the run-state machine. Without this, a missed
    # lifecycle event (lost tool_result, transcript-parser miss, crashed
    # Claude run) leaves the typing-action loop refreshing the native
    # indicator forever. ``mark_pane_idle`` is a no-op when an interactive
    # prompt is visible (WAITING_ON_USER) so we don't fight the UI in
    # ``handle_interactive_ui`` — that branch already returned early above.
    if config.busy_indicator_v2:
        await busy_indicator.mark_pane_idle((user_id, thread_id or 0, window_id))
    await enqueue_status_update(
        bot,
        user_id,
        window_id,
        None,
        thread_id=thread_id,
    )


async def _process_idle_clear_only(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None,
    skip_status: bool,
) -> None:
    """Cleanup-only path used when adaptive gating skips the pane capture.

    Without a pane scrape we can't run interactive-UI detection or update
    the busy indicator's pane-derived signals — but the IDLE_CLEAR_DELAY
    state machine still needs to advance so a previously-shown "🟡 Busy"
    status gets cleared on schedule.

    Semantics:
      - If we have no idle-state entry for this route, do nothing. The next
        watchdog-elapsed tick will scrape the pane and either confirm idle
        (start the timer) or refresh the status. Starting the idle timer
        here without confirming the pane is actually idle would falsely
        clear an active status.
      - If the route is already in idle-pending state and the delay has
        elapsed, fire the clear once. This is the only case we're solving
        for here; everything else is handled the next time we capture.
    """
    if skip_status:
        return
    key = (user_id, thread_id or 0)
    state = _idle_state.get(key)
    if state is None or state == "cleared":
        return
    assert isinstance(state, float)
    if (time.monotonic() - state) < IDLE_CLEAR_DELAY_SECONDS:
        return
    _idle_state[key] = "cleared"
    if config.busy_indicator_v2:
        await busy_indicator.mark_pane_idle((user_id, thread_id or 0, window_id))
    await enqueue_status_update(
        bot,
        user_id,
        window_id,
        None,
        thread_id=thread_id,
    )


async def status_poll_loop(bot: Bot) -> None:
    """Background task to poll terminal status for all thread-bound windows.

    Topic-existence detection is reactive: ``topic_send``/``topic_edit`` paths
    in ``message_queue`` already classify topic-shaped failures and route them
    to emergency DMs. The previous proactive ``unpin_all_forum_topic_messages``
    probe was destructive (it clears pinned messages on success, not a no-op),
    so we no longer poll Telegram for liveness. Stale bindings whose tmux
    window has been killed are still cleaned up below per-iteration.
    """
    logger.info("Status polling started (interval: %ss)", STATUS_POLL_INTERVAL)
    while True:
        try:
            # Run per-binding work concurrently. Serial iteration scaled
            # poll latency with the binding count — with ~14 topics and
            # ~1.5s per capture_pane, a full cycle took ~21s, longer than
            # Telegram's 5s typing-action TTL. As a result the in-topic
            # "Felix's Claude is typing…" indicator expired between polls
            # and never appeared continuous. Parallel iteration brings the
            # full cycle down to roughly the slowest single binding.
            bindings = list(session_manager.iter_thread_bindings())
            if bindings:
                await asyncio.gather(
                    *(
                        _poll_one_binding(bot, user_id, thread_id, wid)
                        for user_id, thread_id, wid in bindings
                    ),
                    return_exceptions=True,
                )
        except Exception as e:
            logger.error(f"Status poll loop error: {e}")

        await asyncio.sleep(STATUS_POLL_INTERVAL)


async def _poll_one_binding(bot: Bot, user_id: int, thread_id: int, wid: str) -> None:
    """Single-binding poll body extracted from ``status_poll_loop`` so the
    outer loop can run all bindings concurrently via ``asyncio.gather``.
    """
    try:
        # Clean up stale bindings (window no longer exists)
        w = await tmux_manager.find_window_by_id(wid)
        if not w:
            session_manager.unbind_thread(user_id, thread_id)
            await clear_topic_state(user_id, thread_id, bot)
            logger.info(
                "Cleaned up stale binding: user=%d thread=%d window_id=%s",
                user_id,
                thread_id,
                wid,
            )
            return

        # UI detection happens unconditionally in update_status_message.
        # Status enqueue is skipped inside update_status_message when
        # interactive UI is detected (returns early) or when this route's
        # content queue has pending tasks (unrelated routes do not throttle).
        queue = get_content_queue((user_id, thread_id, wid))
        skip_status = queue is not None and queue.qsize() > 0

        await update_status_message(
            bot,
            user_id,
            wid,
            thread_id=thread_id,
            skip_status=skip_status,
            window=w,
        )
    except Exception as e:
        logger.debug(
            "Status update error for user %d thread %d: %s",
            user_id,
            thread_id,
            e,
        )


async def typing_action_loop(bot: Bot) -> None:
    """Re-emit Telegram's native typing indicator for every actively-running
    route on a fixed cadence, independent of pane polling.

    Reads ``busy_indicator.state(route)`` directly: no tmux subprocess fan-out,
    no per-binding capture_pane. With ~14 bindings the status poller's full
    cycle empirically lands at 6-8s on macOS, longer than Telegram's ~5s
    typing-action TTL, so the indicator was flashing rather than holding
    steady. This loop fires every ``TYPING_ACTION_INTERVAL`` seconds so the
    cadence stays well under the TTL regardless of binding count.

    V1 (``busy_indicator_v2`` off) keeps the legacy pane-derived path in
    ``update_status_message``. The two paths are mutually exclusive — when V2
    is on, ``update_status_message`` skips the typing-action send.
    """
    if not config.busy_indicator_v2:
        logger.info(
            "Typing-action loop: V2 indicator disabled, deferring to status poller"
        )
        return
    logger.info("Typing-action loop started (interval: %ss)", TYPING_ACTION_INTERVAL)
    while True:
        try:
            bindings = list(session_manager.iter_thread_bindings())
            sends: list = []
            for user_id, thread_id, wid in bindings:
                run = busy_indicator.state((user_id, thread_id or 0, wid))
                if run not in (RunState.RUNNING, RunState.RUNNING_TOOL):
                    continue
                sends.append(_send_typing_action(bot, user_id, thread_id, wid))
            if sends:
                await asyncio.gather(*sends, return_exceptions=True)
        except Exception as e:
            logger.error("Typing-action loop error: %s", e)
        await asyncio.sleep(TYPING_ACTION_INTERVAL)


async def _send_typing_action(bot: Bot, user_id: int, thread_id: int, wid: str) -> None:
    """Best-effort typing-action send. Failures (rate limit, network) are
    logged at debug and swallowed — never let one route's failure abort the
    gather over all routes.
    """
    try:
        await bot.send_chat_action(
            chat_id=session_manager.resolve_chat_id(user_id, thread_id or None),
            action=ChatAction.TYPING,
            message_thread_id=thread_id or None,
        )
        logger.debug(
            "typing_action user=%d thread=%s window=%s sent",
            user_id,
            thread_id,
            wid,
        )
    except Exception as e:
        logger.debug(
            "typing_action user=%d thread=%s window=%s failed: %s",
            user_id,
            thread_id,
            wid,
            e,
        )
