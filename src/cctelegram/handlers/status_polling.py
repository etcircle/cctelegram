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
import hashlib
import logging
import time
from typing import Literal

from telegram import Bot
from telegram.constants import ChatAction

from ..config import config
from .. import route_runtime
from ..session import session_manager
from ..terminal_parser import (
    extract_interactive_content,
    is_picker_anchor_visible,
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
    register_clear_callback,
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

# Per-route content-hash of the last interactive UI the poller refreshed to
# Telegram, keyed by ``(user_id, thread_id_or_0, window_id)``. Used to detect
# back-to-back AskUserQuestion transitions (Q2 → Q3) where the pane never
# leaves the interactive-UI state but the question and options change. Without
# this hash, the in-interactive-mode early-return below would keep the stale
# Q2 keyboard live in Telegram while the user is staring at Q3 in the pane —
# and Claude Code buffers the new tool_use JSONL line until the user answers,
# so the JSONL-driven dispatch can't recover until after the fact.
_last_published_ui_hash: dict[tuple[int, int, str], str] = {}

# Hysteresis on the clear path for in-interactive routes. A single 1Hz poll
# that lands during a Claude Code redraw frame can come up empty even while
# the picker is genuinely live — long multi-Q AskUserQuestion forms on the
# Submit-confirmation step are the canonical case: when ``extract_interactive_content``
# runs over a visible-only capture and the top tab anchor + the bottom picker
# footer are both off-screen for one frame, the predicate returns None and the
# legacy single-tick clear would destroy the still-live card. Observed once on
# 2026-05-19 22:30 → 2026-05-20 00:15:23 (cgc-fork @37 / msg 32835): the AUQ
# stayed live for ~1h45m, the JSONL tool_result didn't flush until 00:18:46,
# but the card was deleted at 00:15:23 because one poll saw the visible-bottom
# as TaskList rows instead of the picker footer. Require ABSENT_STREAK_THRESHOLD
# consecutive absent polls before clearing so transient single-frame redraws
# can't kill a live picker; reset to 0 on any non-None UI observation.
ABSENT_STREAK_THRESHOLD = 3  # ~3s at STATUS_POLL_INTERVAL=1.0
_absent_streak: dict[tuple[int, int, str], int] = {}


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


def _on_interactive_clear(
    user_id: int, thread_id_or_0: int, window_id: str | None
) -> None:
    """Drop ``_absent_streak[(user, thread, window)]`` synchronously when an
    interactive lifecycle ends (codex P2, 2026-05-20). Without this hook the
    streak survives external clears (callback dispatcher / JSONL tool_result
    routing) and is inherited by the next lifecycle on the same route+window,
    so a single bad-frame poll on the new card could reach
    ``ABSENT_STREAK_THRESHOLD`` instantly and delete a freshly-published
    picker — exactly the failure mode the hysteresis was added to prevent.
    Lazy reset on the next poll's ``interactive_window != window_id`` branch
    is not sufficient: the external-clear → new-lifecycle transition can
    happen entirely between polls, so the cleanup window collapses.
    """
    if window_id is None:
        return
    _absent_streak.pop((user_id, thread_id_or_0, window_id), None)


register_clear_callback(_on_interactive_clear)


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
        _last_published_ui_hash.pop((user_id, thread_id or 0, window_id), None)
        return

    # Adaptive pane-capture gate. The 1Hz loop still runs (so cleanup,
    # idle-clear timing, and stale-binding sweeps remain responsive), but
    # we skip the ``capture_pane`` subprocess most ticks. See
    # WATCHDOG_INTERVAL above for the criteria.
    route = (user_id, thread_id or 0, window_id)
    interactive_window = get_interactive_window(user_id, thread_id)
    if interactive_window != window_id:
        # ``_absent_streak`` is meaningful only while THIS route+window owns
        # the active interactive lifecycle. When the route's interactive has
        # been retired (external clear via callback dispatcher / JSONL
        # tool_result routing) or moved to a different window, drop any
        # accumulated streak so the next lifecycle doesn't inherit a stale
        # counter (codex P2, 2026-05-20 review). Done BEFORE the watchdog
        # gate so the cleanup runs even on ticks where we skip the pane
        # capture — ``_absent_streak`` doesn't depend on pane content, only
        # on interactive lifecycle ownership.
        _absent_streak.pop(route, None)
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
                if config.route_runtime_v2:
                    route_runtime.update_context_usage(
                        route, latest.tokens, latest.model
                    )
            else:
                busy_indicator.update_context_usage(route, None, None)
                if config.route_runtime_v2:
                    route_runtime.update_context_usage(route, None, None)
        else:
            busy_indicator.update_context_usage(route, None, None)
            if config.route_runtime_v2:
                route_runtime.update_context_usage(route, None, None)

    should_check_new_ui = True

    # Extract the interactive UI content once so both the in-interactive-mode
    # dedup hash (below) and the new-UI dispatch path (further below) share a
    # single regex pass over the pane.
    ui_content = extract_interactive_content(pane_text)

    if interactive_window == window_id:
        # User is in interactive mode for THIS window
        if ui_content is not None:
            # Interactive UI still on the pane. Compare the content hash to
            # the last published one to detect Q2 → Q3 transitions: both pass
            # ``is_interactive_ui`` so a naive early-return here would leave
            # Telegram pinned to the stale Q2 keyboard. JSONL recovery is also
            # blocked because Claude Code buffers the new AUQ ``tool_use``
            # line until the user answers, so the poller is the only chance
            # to refresh in real time.
            _absent_streak.pop(route, None)
            ui_hash = hashlib.sha256(ui_content.content.encode("utf-8")).hexdigest()
            if ui_hash == _last_published_ui_hash.get(route):
                # Same UI as last publish — user is mid-interaction, skip.
                return
            # New UI content. Store the new hash BEFORE the await so a
            # concurrent tick on the same route doesn't fire a duplicate
            # publish, then refresh via ``handle_interactive_ui`` (which
            # edits the existing Telegram card in place).
            _last_published_ui_hash[route] = ui_hash
            logger.debug(
                "Interactive UI content changed (user=%d, window=%s, thread=%s) — "
                "refreshing keyboard",
                user_id,
                window_id,
                thread_id,
            )
            await handle_interactive_ui(
                bot, user_id, window_id, thread_id, from_poller=True
            )
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
            # Publish race: mode set but no card yet. Drop any leftover
            # streak so the new lifecycle starts from zero once published.
            _absent_streak.pop(route, None)
            return
        # 2026-05-21: ``extract_interactive_content`` does not match the
        # multi-Q AskUserQuestion Submit-confirmation screen when the tab
        # header has scrolled above the visible region (long-question
        # case). The picker is still live — its tail anchors
        # (``Ready to submit your answers?`` / ``❯ 1. Submit answers``)
        # are visible and ``visible_pane_liveness`` would correctly
        # return ``present`` — but ``ui_content`` is None and the
        # hysteresis below would clear the card mid-Submit, leaving the
        # user staring at a live picker on the pane with no Telegram
        # card to dispatch from. Observed 2026-05-21 09:16:07 → 09:16:09
        # on @40 / msg 34496 (multi-Q D3-D6 form). The 2026-05-20
        # ``_PICKER_ANCHOR_MARKERS`` work fixed the same shape in
        # ``visible_pane_liveness`` (used by ``handle_interactive_ui``)
        # but didn't propagate to status_polling's clear gate. Check
        # ``is_picker_anchor_visible`` here as the same fallback: tail
        # anchors present → reset the streak, keep the card.
        if is_picker_anchor_visible(pane_text):
            _absent_streak.pop(route, None)
            return
        # Hysteresis: a single absent poll can be a transient redraw frame on
        # a still-live picker (see ``ABSENT_STREAK_THRESHOLD`` docstring).
        # Require N consecutive absent polls before destroying the card.
        streak = _absent_streak.get(route, 0) + 1
        if streak < ABSENT_STREAK_THRESHOLD:
            _absent_streak[route] = streak
            logger.debug(
                "Interactive UI absent (streak=%d/%d) for window_id %s — "
                "deferring clear",
                streak,
                ABSENT_STREAK_THRESHOLD,
                window_id,
            )
            return
        await clear_interactive_msg(user_id, bot, thread_id)
        _last_published_ui_hash.pop(route, None)
        _absent_streak.pop(route, None)
        should_check_new_ui = False
    elif interactive_window is not None:
        # User is in interactive mode for a DIFFERENT window (window switched).
        # Same gate as above. No hysteresis — the user has unambiguously moved
        # focus, so the old card is dead by definition.
        if not has_interactive_surface(user_id, thread_id):
            return
        await clear_interactive_msg(user_id, bot, thread_id)
        _last_published_ui_hash.pop(route, None)
        _absent_streak.pop(route, None)

    # Check for permission prompt (interactive UI not triggered via JSONL)
    # ALWAYS check UI, regardless of skip_status
    if should_check_new_ui and ui_content is not None:
        logger.debug(
            "Interactive UI detected in polling (user=%d, window=%s, thread=%s)",
            user_id,
            window_id,
            thread_id,
        )
        # Tag this as the poller call path so handle_interactive_ui can
        # apply AUQ pane-only safety rules when JSONL replay data is absent
        # or stale (render immediately, but suppress unsafe pick buttons).
        # Store the hash before the await for the same reason as above.
        _last_published_ui_hash[route] = hashlib.sha256(
            ui_content.content.encode("utf-8")
        ).hexdigest()
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
    if config.route_runtime_v2:
        await route_runtime.mark_pane_idle((user_id, thread_id or 0, window_id))
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
    if config.route_runtime_v2:
        await route_runtime.mark_pane_idle((user_id, thread_id or 0, window_id))
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
                route = (user_id, thread_id or 0, wid)
                if config.route_runtime_v2:
                    # Wave B: route_runtime is authoritative under v2.
                    # ``typing_eligible`` already covers the
                    # RUNNING / RUNNING_TOOL discrimination.
                    if not route_runtime.snapshot(route).typing_eligible:
                        continue
                else:
                    run = busy_indicator.state(route)
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
