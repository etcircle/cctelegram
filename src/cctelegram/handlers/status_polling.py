"""Terminal status line polling for thread-bound windows.

Provides background polling of terminal status lines for all active users:
  - Detects Claude Code status (working, waiting, etc.)
  - Detects interactive UIs (permission prompts) not triggered via JSONL
  - Updates status messages in Telegram
  - Polls thread_bindings (each topic = one window)
  - Cleans up bindings whose tmux window has been killed

Topic-existence detection is reactive in the polling loop: real
topic_send/topic_edit failures classify into ``_TOPIC_BROKEN_OUTCOMES`` and
trigger emergency DMs from the message queue. The status poller itself does NOT
poll Telegram for topic liveness; the previously-used
``unpin_all_forum_topic_messages`` probe was destructive (it clears pinned
messages on success, not a no-op) and ran every 60s for every bound topic,
which would silently wipe legitimate user pins. (A non-destructive
``sendChatAction`` probe for *dormant* deleted topics does run, but only once
per day from the GC loop in ``bot.py`` via ``message_queue.probe_topic_liveness``
— that is separate from this 1s status loop.)

Key components:
  - STATUS_POLL_INTERVAL: Polling frequency (1 second)
  - status_poll_loop: Background polling task
  - update_status_message: Poll and enqueue status updates
  - _consume_notification_signal: Wave B Notification side-file consumption
    + the runtime notification TTL, at the TOP of the per-binding path so a
    capture-skipped tick still consumes and a 🔔 transition repaints the
    digest the same tick; the pane running-after-set_at clear (level +
    NOTIFY_PANE_CLEAR_MARGIN_S, not an edge) lives further down beside
    ``is_running``.
  - clear_route_caches_for_topic: the topic-teardown seam for the
    poller-local route-keyed caches, called by cleanup.clear_topic_state.
"""

import asyncio
import hashlib
import logging
import time

from telegram import Bot
from telegram.constants import ChatAction

from .. import route_runtime

# Re-exported from route_runtime (the idle-clear authority) so callers and
# tests that read ``status_polling.IDLE_CLEAR_DELAY_SECONDS`` keep resolving
# to the single source of truth.
from ..route_runtime import IDLE_CLEAR_DELAY_SECONDS as IDLE_CLEAR_DELAY_SECONDS
from ..session import session_manager
from ..terminal_parser import (
    extract_interactive_content,
    has_pane_chrome,
    is_picker_anchor_visible,
    is_status_active,
    parse_status_line,
    resolve_ask_form,
)
from ..transcript_parser import read_latest_usage
from ..tmux_manager import TmuxWindow, tmux_manager
from . import auq_source, notify_source, pick_token
from .interactive_ui import (
    clear_interactive_msg,
    get_interactive_window,
    handle_interactive_ui,
    has_interactive_surface,
    register_clear_callback,
)
from .cleanup import clear_topic_state
from .message_queue import (
    enqueue_status_update,
    get_content_queue,
    refresh_activity_digest_if_present,
)

logger = logging.getLogger(__name__)

# Status polling interval
STATUS_POLL_INTERVAL = 1.0  # seconds - faster response (rate limiting at send layer)

# Best-effort ordering barrier before the poller publishes the FIRST picker for
# a route. Keep this small: the poller runs at 1Hz, and a stuck content worker
# must not freeze interactive detection globally. If the route queue is badly
# backlogged, we prefer rendering late-ish context before the picker when it can
# finish quickly, then proceed anyway on timeout.
FIRST_PICKER_CONTENT_DRAIN_TIMEOUT = 2.0

# Watchdog interval for adaptive pane capture. The 1Hz loop still ticks
# every second so stale-binding cleanup and idle-clear delay processing
# stay responsive, but the expensive ``capture_pane`` subprocess only
# fires when one of:
#   - this route is currently in interactive mode (we need to detect when
#     the user closes the open UI),
#   - WATCHDOG_INTERVAL seconds have elapsed since the last capture for
#     this route (catches RestoreCheckpoint / Settings / status transitions
#     that don't show up in the JSONL stream).
# The run-state machine reads ``route_runtime.snapshot(route)`` directly, so
# the watchdog cadence is enough to catch missed pane-only transitions.
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
# ``route_runtime.snapshot(route).typing_eligible`` directly with no tmux I/O
# so cadence stays tight regardless of binding count.
TYPING_ACTION_INTERVAL = 3.0

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

# D3-β: re-stamp a live card's pick-token deadlines when a token is within this
# many seconds of its 300s TTL. At STATUS_POLL_INTERVAL=1.0 a live card is
# re-stamped at most ~once per (300 - margin)s, so the keyboard stays
# byte-identical (no MESSAGE_NOT_MODIFIED churn) while never letting a visible
# button's token expire.
_DEADLINE_REFRESH_MARGIN_S = 60.0
_absent_streak: dict[tuple[int, int, str], int] = {}

# Per-route last-rendered run_state, keyed by ``(user_id, thread_id_or_0,
# window_id)``. Drives ``_maybe_repaint_digest_on_transition``: a poller-local,
# self-healing repaint-dedup cache so the activity-digest header is re-rendered
# exactly once per run-state transition (e.g. the pane-confirmed RUNNING →
# WAITING_ON_USER promotion, or its retract). Read ONLY inside that helper
# (which is only ever called from the poller), so a leaked entry is harmless: at
# worst one spurious ``refresh_activity_digest_if_present`` that no-ops when no
# matching digest is on screen. Torn down best-effort in the window-gone path;
# NOT popped on external interactive-clear / window-switch (popping there masked
# the post-clear repaint — the v3 shared P1). Pull-only; no observer channel.
_UNSEEN = object()
_prev_run_state: dict[tuple[int, int, str], object] = {}

# Margin between the notification's hook-fire wall clock (``set_at``) and a
# pane capture allowed to clear it. The Wave B pane-activity clear is LEVEL +
# time-qualified — "pane observed RUNNING at a capture taken strictly after
# set_at + margin" — NOT an idle→active edge. Rationale (gate P2-1): the
# adaptive watchdog capture can skip the blocked approval frame entirely, so
# an edge requirement (prev=False → True) strands the 🔔 bit whenever the
# last capture before the notification was already running (prev stays True,
# post-approval frame is True→True, no edge ever fires). A blocked approval
# prompt REPLACES the run chrome — ``is_status_active`` keys on the literal
# ``esc to interrupt`` hint that Claude Code renders only while a run is in
# flight (the prompt's footer shows its own dialog text instead) — so a
# status-active capture strictly after the hook fired is positive proof the
# user approved and execution resumed, independent of whether the blocked
# frame was ever observed. The margin (> one 1s poll interval) guards the
# one race: a capture landing the same tick the hook fired could still show
# the pre-prompt running frame before the TUI repaints. Fail-safe: even if a
# future Claude Code version rendered the run hint UNDER a live prompt, a
# wrong clear only degrades 🔔 → 🟡 (the same degradation as the 30-min
# runtime TTL) and the prompt stays discoverable on the pane — never a wrong
# dispatch. A restart cannot fabricate a clear either: the predicate is
# stateless across ticks (no seeded prev), and a running pane after restart
# satisfying it means the prompt is genuinely gone.
NOTIFY_PANE_CLEAR_MARGIN_S = 1.5


def clear_route_caches_for_topic(user_id: int, thread_id_or_0: int) -> None:
    """Pop every poller-local route-keyed cache entry for ``(user, thread)``.

    The topic-teardown seam (gate P3-1): ``cleanup.clear_topic_state`` —
    topic close / delete / stale-binding GC / ``/unbind`` — tears down
    message_queue, side-file, interactive, and route_runtime state, and
    calls this so a rebound topic reusing the same route key never inherits
    stale poller entries (a leftover ``_last_published_ui_hash`` skips the
    first-picker content-drain barrier, a leftover ``_prev_run_state``
    defeats the seed-without-edit repaint semantics, and a stale
    ``_last_pane_capture`` delays the rebound's first watchdog scrape).
    Window-scoped within the topic is unnecessary: 1 topic = 1 window, and
    any historical window's entries under this (user, thread) are equally
    stale. Called by ``cleanup`` via a lazy import (this module imports
    ``cleanup`` at the top, so the reverse edge must stay function-local).
    """
    caches = (
        _last_pane_capture,
        _last_published_ui_hash,
        _absent_streak,
        _prev_run_state,
    )
    for cache in caches:
        for key in [k for k in cache if k[0] == user_id and k[1] == thread_id_or_0]:
            cache.pop(key, None)


async def _consume_notification_signal(
    user_id: int, thread_id: int | None, window_id: str
) -> None:
    """Wave B: runtime-TTL check + Notification side-file consumption.

    Runs at the TOP of the per-binding poll path — BEFORE
    ``_maybe_repaint_digest_on_transition`` (so a 🔔 transition repaints the
    digest on the SAME tick via the ordinary transition-repaint seed) and
    BEFORE the adaptive capture gating / early returns (a capture-skipped
    tick still consumes; plan v3 B1d).

    Two phases, both pull-only:

      1. Runtime-state TTL (v4 fix 2 strand-proofing): evaluated from the
         SNAPSHOT every tick, independent of side-file existence — a
         consumed/unlinked file or a permanently-None-timestamp transcript
         cannot strand 🔔 past ``NOTIFY_TTL_SECONDS``. A pending bit without
         a set_at violates the invariant and is treated as expired.
      2. Window-predicated side-file read → ``mark_notification_pending``.
         The returned ``NotificationMarkResult`` DRIVES the unlink (codex
         r4 P3 (b)): committed-live → generation-guarded unlink AFTER the
         commit; redundant/stale → generation-guarded unlink; ignored → NO
         unlink (never seed; the file may belong to a not-yet-bound route).
         An on-disk record older than the TTL is treated as absent and
         unlinked without ever lighting the bit.
    """
    route = (user_id, thread_id or 0, window_id)
    now = time.time()
    snap = route_runtime.snapshot(route)
    if snap.notification_pending and (
        snap.notification_set_at is None
        or now - snap.notification_set_at > route_runtime.NOTIFY_TTL_SECONDS
    ):
        await route_runtime.mark_notification_cleared(route)
        snap = route_runtime.snapshot(route)

    rec = notify_source.notification_pending_for_window(window_id)
    if rec is None:
        return
    if snap.notification_pending and snap.notification_generation == rec.generation:
        return  # already reflected — nothing to do this tick
    if now - rec.ts > route_runtime.NOTIFY_TTL_SECONDS:
        # Expired on disk (e.g. written while the bot was down) — treated
        # absent; unlink so it doesn't re-surface every tick.
        notify_source.unlink_if_generation_matches(rec.session_id, rec.generation)
        return
    result = await route_runtime.mark_notification_pending(
        route, set_at=rec.ts, generation=rec.generation
    )
    if result is not route_runtime.NotificationMarkResult.IGNORED_NO_UNLINK:
        notify_source.unlink_if_generation_matches(rec.session_id, rec.generation)


async def _maybe_repaint_digest_on_transition(
    bot: Bot, user_id: int, thread_id: int | None, window_id: str
) -> None:
    """Repaint the activity-digest header iff this route's ``run_state`` changed
    since the last poller observation.

    First observation seeds ``_prev_run_state`` WITHOUT a spurious edit (a route
    absent from the map records its state and does not repaint). Only a change
    from a recorded prior repaints — and ``refresh_activity_digest_if_present``
    itself no-ops when no digest is on screen, so this is cheap. Pull-only: the
    poller reads the committed snapshot, decides, and edits — no observer/push
    channel (the c313657 pattern stays forbidden)."""
    route = (user_id, thread_id or 0, window_id)
    snap = route_runtime.snapshot(route)
    prev = _prev_run_state.get(route, _UNSEEN)
    if prev is _UNSEEN:
        _prev_run_state[route] = snap.run_state  # seed only — no edit
        return
    if prev is not snap.run_state:
        _prev_run_state[route] = snap.run_state
        await refresh_activity_digest_if_present(bot, user_id, thread_id, window_id)


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


async def _remint_on_source_drift(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    pane_text: str,
    *,
    ui_hash: str | None = None,
) -> bool:
    """Item-1 source-drift re-mint, shared by the same-hash idle branch and
    preserve-site (b) (review finding 15).

    A live card's TOKENS must track its OBSERVED SOURCE: when the PreToolUse
    side file ages past the read-TTL under a still-displayed card,
    ``resolve_auq_source`` flips ``side_file`` → ``pane`` while the card's
    tokens were minted from ``side_file``, so the user's first tap
    ``source_drift``s (swallowed + a misleading "Form changed, refreshing.").
    Re-resolve the live source, parse the live form (``resolve_ask_form``
    gates out non-AUQ panes — Settings / EPM — so we never spuriously
    re-mint there), look the displayed card up by ROUTE via the pure,
    tombstone-aware ``pick_token.peek_route_source`` (fingerprint-agnostic —
    the side-file-form and pane-form fingerprints differ), and on a mismatch
    re-render via ``handle_interactive_ui`` (re-mint to the CURRENT source).

    Returns True iff the drift re-mint fired — the caller returns without
    refreshing deadlines. Loop-safe (exactly ONE re-mint): the re-mint
    fresh-mints the live source and ``mint_row``'s hygiene drops the old
    row, so the next tick sees live == minted → no further re-render.

    ``ui_hash`` (same-hash branch only): stored in ``_last_published_ui_hash``
    before the await for parity with the new-UI branch so a concurrent tick
    doesn't double-publish. Site (b) has no ``ui_content`` and passes None.
    """
    live = auq_source.resolve_auq_source(window_id, None, pane_text)
    if resolve_ask_form(live.payload, pane_text) is None:
        return False
    minted = pick_token.peek_route_source(user_id, thread_id, window_id)
    if minted is None or (live.kind, live.source_fingerprint) == minted:
        return False
    if ui_hash is not None:
        _last_published_ui_hash[(user_id, thread_id or 0, window_id)] = ui_hash
    await handle_interactive_ui(bot, user_id, window_id, thread_id, from_poller=True)
    return True


async def _drain_content_queue_before_first_picker_publish(
    route: tuple[int, int, str],
) -> None:
    """Best-effort route-local queue barrier before the first poller picker."""
    content_queue = get_content_queue(route)
    if content_queue is None:
        return

    try:
        await asyncio.wait_for(
            content_queue.join(), timeout=FIRST_PICKER_CONTENT_DRAIN_TIMEOUT
        )
    except asyncio.TimeoutError:
        logger.debug(
            "Timed out waiting %.1fs for content queue before first picker publish "
            "(user=%d, thread=%d, window=%s); rendering picker anyway",
            FIRST_PICKER_CONTENT_DRAIN_TIMEOUT,
            route[0],
            route[1],
            route[2],
        )


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
    # Wave B: consume the Notification side file + enforce the runtime TTL
    # BEFORE the transition repaint below — the repaint then observes the
    # post-consume run_state, so a 🔔 set/clear repaints the digest on the
    # SAME tick (plan v3 B1d) — and before every capture-gating early return
    # (a capture-skipped tick still consumes).
    await _consume_notification_signal(user_id, thread_id, window_id)
    # Repaint the activity-digest header on any run-state transition since the
    # last tick (e.g. a transcript reclaim flushed and flipped WAITING → Done,
    # or the reconciliation below cleared a stale bit). Placed FIRST (after the
    # notification consume) so it runs before every early-return, both
    # directions; pull-only and a no-op when no digest is on screen, so it is
    # cheap on the common idle tick.
    await _maybe_repaint_digest_on_transition(bot, user_id, thread_id, window_id)
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
        _absent_streak.pop((user_id, thread_id or 0, window_id), None)
        # Best-effort teardown of the poller-local repaint-dedup cache (the only
        # _prev_run_state pop; status_polling-local, so import-safe — NOT from
        # message_queue, which would invert the status_polling → message_queue
        # edge). A re-bound window seeds fresh (first observation, no edit).
        _prev_run_state.pop((user_id, thread_id or 0, window_id), None)
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
        # Mode-ended liveness reconciliation (the UNIFIED pane-set WAITING
        # clear). Reaching this block ⟺ the route is NOT in interactive mode
        # for this window (mode popped, window-switched, or never-this-window).
        # The bit is only ever SET while this window IS in interactive mode
        # (sites a/b require interactive_window == window_id; site d is a
        # first-render that immediately set_interactive_mode), so a set bit
        # here means the interactive lifecycle ended without a transcript flush
        # → clear it. Gap-free (covers mode-popped, window-switch, and
        # ExitPlanMode-no-flush — no side file needed), no flush dependency, no
        # flap (a genuinely-live picker keeps interactive_window == window_id so
        # this branch isn't taken). Runs every tick before the capture gate.
        if route_runtime.snapshot(route).interactive_pending:
            await route_runtime.mark_interactive_cleared(route)
            await _maybe_repaint_digest_on_transition(
                bot, user_id, thread_id, window_id
            )
    in_interactive = interactive_window == window_id
    now_mono = time.monotonic()
    last_capture = _last_pane_capture.get(route)
    watchdog_elapsed = (
        last_capture is None or (now_mono - last_capture) >= WATCHDOG_INTERVAL
    )
    should_capture = in_interactive or watchdog_elapsed
    # Nudge: force a pane capture while the pane-set WAITING bit is live so the
    # SET sites (a/b/d) re-assert promptly. Belt-and-suspenders — a set bit
    # implies this window is in interactive mode, so ``in_interactive`` is
    # normally already True (the mode-ended reconciliation above already cleared
    # the bit on any window-mismatch tick).
    should_capture = should_capture or route_runtime.snapshot(route).interactive_pending

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
    # Wall-clock stamp of THIS capture, recorded immediately so the Wave B
    # notification clear below compares the capture instant (not the later
    # check instant, which would overstate how long after ``set_at`` the
    # frame was observed) against ``notification_set_at`` — both on the
    # ``time.time()`` clock the Notification hook stamps ``ts`` with.
    capture_wall = time.time()

    # Read the next-turn context size from the session's JSONL into the
    # route_runtime context-usage cache. The activity-digest header reads it
    # via ``snapshot.context_usage`` and the per-message footer
    # (``bot._build_context_footer``) routes through the same cache so the
    # 1M-cap latch is shared. read_latest_usage is mtime+size-cached so a 1Hz
    # poller doesn't re-scan unchanged files.
    session = await session_manager.resolve_session_for_window(window_id)
    if session and session.file_path:
        latest = read_latest_usage(session.file_path)
        if latest is not None:
            route_runtime.update_context_usage(route, latest.tokens, latest.model)
        else:
            route_runtime.update_context_usage(route, None, None)
    else:
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
            # SET (a): pane-confirmed live picker (AUQ or ExitPlanMode plan
            # approval) while THIS window is in interactive mode → promote an
            # active RUNNING route to WAITING_ON_USER. Before the same-hash
            # early-return so it re-asserts on same-UI ticks (idempotent; the
            # RUNNING-with-empty-open_tools guard no-ops otherwise). Repaint on
            # transition.
            await route_runtime.mark_interactive_pending(route)
            await _maybe_repaint_digest_on_transition(
                bot, user_id, thread_id, window_id
            )
            ui_hash = hashlib.sha256(ui_content.content.encode("utf-8")).hexdigest()
            if ui_hash == _last_published_ui_hash.get(route):
                # Same UI as last publish — user is mid-interaction, skip the
                # re-render. D3-β: BUT re-stamp this live card's pick-token
                # deadlines so an idle tap never finds a TTL-pruned token (the
                # reported dead-first-tap). Inside the same-hash branch (NOT at
                # the streak reset above, which also runs the re-render path,
                # which fresh-mints anyway). Same token + generation → no churn.
                #
                # Item 1: BEFORE the deadline refresh, detect a SOURCE drift
                # under the still-displayed card. A single-select picker left
                # open >300s ages its PreToolUse side file past the read-TTL, so
                # ``resolve_auq_source`` flips side_file → pane while the card's
                # tokens were minted from side_file. The same-hash branch keeps
                # the stale tokens, so the user's first tap ``source_drift``s
                # (swallowed + a misleading "Form changed, refreshing."). Re-mint
                # the live card to the current source so the tokens track it.
                # mint_row's source-aware reuse prevents a re-render loop (after
                # the re-mint to pane, the next tick sees pane==pane → no drift).
                # Shared with preserve-site (b) via _remint_on_source_drift
                # (review finding 15) — same comparison, same loop-safety.
                if await _remint_on_source_drift(
                    bot, user_id, thread_id, window_id, pane_text, ui_hash=ui_hash
                ):
                    return
                await pick_token.refresh_route_deadlines(
                    user_id,
                    thread_id,
                    window_id,
                    min_remaining_s=_DEADLINE_REFRESH_MARGIN_S,
                )
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
        # Gate on ``has_interactive_surface`` rather than
        # ``get_interactive_msg_id`` so callers reason in terms of
        # route-owns-a-card semantics.
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
            # SET (b): scrolled/compressed Submit screen — picker tail anchors
            # visible. Pane-confirmed → promote + repaint. BEFORE the drift
            # re-mint early-return below (hermes round 2), mirroring SET (a):
            # the promotion is pane-confirmed by ``is_picker_anchor_visible``
            # regardless of token-source drift, and the re-mint path
            # (``handle_interactive_ui``) never touches the bit — without
            # this order a drift tick would leave the route RUNNING (wrong
            # digest + typing) for one extra poll cycle.
            await route_runtime.mark_interactive_pending(route)
            await _maybe_repaint_digest_on_transition(
                bot, user_id, thread_id, window_id
            )
            # Item 1 at site (b) (review finding 15): this preserve branch also
            # keeps a card whose side file may have aged past the read-TTL —
            # refreshing its deadlines would PRESERVE stale side_file source
            # tags and the first tap would be swallowed as source_drift. Run
            # the SAME drift comparison as the same-hash branch BEFORE the
            # deadline refresh; on a re-mint, return (the next tick converges:
            # live == minted → no further re-render, then SET (b) re-asserts).
            if await _remint_on_source_drift(
                bot, user_id, thread_id, window_id, pane_text
            ):
                return
            # D3-β: live (scrolled/compressed) Submit card — keep its tokens
            # alive so a tap after a long idle on this screen still dispatches.
            await pick_token.refresh_route_deadlines(
                user_id,
                thread_id,
                window_id,
                min_remaining_s=_DEADLINE_REFRESH_MARGIN_S,
            )
            return
        # The visible pane lacks picker anchors — but the pane is only a
        # DISPLAY, not the lifecycle authority. The PreToolUse side file
        # (auq_pending/<session>.json) is written before the picker renders
        # and unlinked ONLY when the question truly resolves (tool_result),
        # on /clear, on window delete, or by the 1h GC. While it is live, an
        # obstructing overlay (Claude task-list, a scrolled/compressed Submit
        # screen, tool-output spam) must NOT tear down a still-pending
        # question's card. RouteRuntime contract: pane signals are LOWER
        # authority than the resolution lifecycle. (2026-05-31 @4/msg48427:
        # the task-list overlay made both pane predicates read "absent" for
        # 3 polls and a LIVE multi-select AUQ card was tombstoned.) NB: this
        # uses ``side_file_live_for_window`` and NOT ``resolve_record`` —
        # the latter needs a pane-parsed form which is None under exactly
        # the overlay this must survive.
        if auq_source.side_file_live_for_window(window_id):
            _absent_streak.pop(route, None)
            # D3-β: card preserved on side-file liveness (pane obscured by a
            # task-list overlay / scrolled Submit / tool spam) — keep its tokens
            # alive so a tap after a long obscured idle still dispatches.
            await pick_token.refresh_route_deadlines(
                user_id,
                thread_id,
                window_id,
                min_remaining_s=_DEADLINE_REFRESH_MARGIN_S,
            )
            logger.debug(
                "Interactive UI absent on pane but PreToolUse side file live "
                "for window_id %s — preserving card (route=%s)",
                window_id,
                route,
            )
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
        # 2026-05-25: pane-absent clear means the AUQ vanished from
        # Claude's TUI without a Telegram callback (the user never
        # picked). Could be the user typing the answer in tmux directly
        # OR Claude auto-resolving (bypassPermissions). Either way, the
        # user's chat history would otherwise lose all trace of the
        # picker. Tombstone instead of delete: edits the card body to a
        # "resolved without Telegram input" notice and strips the
        # keyboard. The other clear_interactive_msg call sites (topic
        # close, window switch, callback-handled picks) keep delete.
        await clear_interactive_msg(user_id, bot, thread_id, tombstone=True)
        # CLEAR (ii): genuine in-mode absence (mode still set for this window,
        # side file not live, pane absent ≥ ABSENT_STREAK_THRESHOLD). Retract
        # the pane-set WAITING bit alongside the card tombstone + repaint. The
        # hysteresis above prevents a transient redraw from clearing early.
        await route_runtime.mark_interactive_cleared(route)
        await _maybe_repaint_digest_on_transition(bot, user_id, thread_id, window_id)
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
        # Store the hash before any await so a concurrent tick on the same
        # route sees it and skips duplicate publish. Only the first publish for
        # the route waits for same-route content to drain; refreshes must stay
        # fast so Q2→Q3 transitions do not stall every poll.
        is_first_publish_for_route = route not in _last_published_ui_hash
        _last_published_ui_hash[route] = hashlib.sha256(
            ui_content.content.encode("utf-8")
        ).hexdigest()
        if is_first_publish_for_route:
            await _drain_content_queue_before_first_picker_publish(route)
        published = await handle_interactive_ui(
            bot, user_id, window_id, thread_id, from_poller=True
        )
        # SET (d): first-render dispatch. Promote RUNNING → WAITING_ON_USER +
        # repaint only on a real surface publish. `published` gates on
        # handle_interactive_ui's return — which is True only when a card was
        # actually published; it can set _interactive_mode yet return False on a
        # topic-send failure, so `published` (not mode-set) is the right gate.
        # The no-surface case is handled next tick by site (a) (if the picker is
        # still on the pane) or the has_interactive_surface publish-race return.
        if published:
            await route_runtime.mark_interactive_pending(route)
            await _maybe_repaint_digest_on_transition(
                bot, user_id, thread_id, window_id
            )
        return

    # Compute active-state up front so the typing indicator fires even
    # when ``skip_status`` is True — that's when Claude is busiest and the
    # "Felix's Claude is typing…" line under the topic title is most
    # useful. Without this, the queue gets so full during active work that
    # the rest of update_status_message returns early and the typing
    # action never gets sent.
    status_line = parse_status_line(pane_text)

    # When Claude is actively running, the spinner line sits directly above
    # the chrome separator. Post-completion summaries (e.g. "✻ Cooked for
    # 2s") get a blank line inserted above the chrome — same spinner glyph,
    # but Claude is idle. ``is_status_active`` reads that gap; we use it
    # rather than scanning the status text for keywords because Claude's
    # working statuses ("Reading file …") don't always include "esc to
    # interrupt", and past-tense summaries don't always omit the spinner.
    is_running = bool(status_line) and is_status_active(pane_text)

    # Wave B: a pane observed RUNNING at a capture taken strictly after
    # ``set_at + NOTIFY_PANE_CLEAR_MARGIN_S`` means the user acted in the
    # terminal (the blocked prompt replaces the run chrome, so a
    # status-active frame after the hook fired is positive proof execution
    # resumed) — retract 🔔 + unlink the (possibly still-present) side file
    # generation-guarded. Level + margin, NOT an idle→active edge: the
    # adaptive capture can skip the blocked frame, so an edge requirement
    # strands the bit when the last pre-notification capture was already
    # running (gate P2-1); the margin keeps a same-tick capture of the
    # pre-prompt frame from clearing early (see NOTIFY_PANE_CLEAR_MARGIN_S).
    # Placed before the ``skip_status`` return so a busy queue can't defer
    # the clear.
    if is_running:
        snap = route_runtime.snapshot(route)
        if (
            snap.notification_pending
            and snap.notification_set_at is not None
            and capture_wall > snap.notification_set_at + NOTIFY_PANE_CLEAR_MARGIN_S
        ):
            cleared_gen = snap.notification_generation
            await route_runtime.mark_notification_cleared(route)
            await _maybe_repaint_digest_on_transition(
                bot, user_id, thread_id, window_id
            )
            if cleared_gen:
                rec = notify_source.notification_pending_for_window(window_id)
                if rec is not None and rec.generation == cleared_gen:
                    notify_source.unlink_if_generation_matches(
                        rec.session_id, cleared_gen
                    )

    # Typing-action delivery is owned by ``typing_action_loop`` (it reads
    # ``route_runtime.snapshot(route).typing_eligible`` directly with no tmux
    # I/O so cadence stays under Telegram's 5s TTL even with many bindings).
    # The status poller does not fire the indicator itself.

    # Normal status line check — skip the rest if queue is non-empty.
    if skip_status:
        return

    # The debounced card clear is owned by route_runtime
    # (``arm_pane_idle_clear`` / ``pane_idle_clear_due`` /
    # ``commit_pane_idle_clear``). The card is not cleared until
    # IDLE_CLEAR_DELAY_SECONDS of *continued* confirmed idle, and any
    # transcript / inbound activity re-arms (cancels) the pending clear
    # inside route_runtime.
    await _drive_pane_idle_clear(
        bot, user_id, window_id, thread_id, is_running, status_line
    )


async def _drive_pane_idle_clear(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None,
    is_running: bool,
    status_line: str | None,
) -> None:
    """Pane-idle card-clear driver.

    The debounce deadline lives in ``route_runtime`` (so it shares a source
    of truth with the run-state machine and ``now`` is injectable for tests):

      - **Running pane** → cancel any pending pane-idle clear so a fresh idle
        stretch re-arms from scratch, then enqueue the live status line.
      - **Idle pane, not yet due** → ``arm_pane_idle_clear`` (idempotent;
        sets the deadline on the first confirmed-idle observation, then
        waits it out).
      - **Idle pane, deadline due** → ``commit_pane_idle_clear`` (reconciles
        run-state to IDLE_CLEARED, latches the cleared sentinel) + enqueue
        the card clear.

    A single ``now`` is read once and threaded through arm/due so the
    decision is consistent within the tick.
    """
    route = (user_id, thread_id or 0, window_id)
    if is_running:
        # Pane shows active work. Cancel any pending pane-idle clear so a
        # fresh idle stretch re-arms from scratch; without this a deadline
        # armed in a prior idle stretch could fire immediately when the pane
        # next goes quiet, skipping the debounce. Transcript / inbound events
        # also re-arm inside route_runtime, but the pane can show running
        # before the next transcript event lands.
        route_runtime.reset_pane_idle_clear(route)
        await enqueue_status_update(
            bot, user_id, window_id, status_line, thread_id=thread_id
        )
        return

    now = time.monotonic()
    if route_runtime.pane_idle_clear_due(route, now=now):
        # Debounce elapsed — clear once. ``commit_pane_idle_clear``
        # reconciles run-state (no-op for WAITING_ON_USER, the same guard
        # the interactive branch above already enforces) and
        # latches the cleared sentinel so repeat idle ticks are no-ops.
        # ``now`` lets commit re-validate armed-and-due under the lock (TOCTOU
        # vs a concurrent activity re-arm) — it no-ops if re-armed/cancelled.
        if not await route_runtime.commit_pane_idle_clear(route, now=now):
            # commit no-op'd: activity re-armed between the lockless due-check
            # and the lock. Do NOT clear the card this tick (TOCTOU guard —
            # see commit_pane_idle_clear; run-state alone is ambiguous, so the
            # explicit bool is authoritative).
            return
        await enqueue_status_update(bot, user_id, window_id, None, thread_id=thread_id)
        return

    # Idle but not yet due — arm (idempotent) and wait out the delay.
    route_runtime.arm_pane_idle_clear(route, now=now)


async def _process_idle_clear_only(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None,
    skip_status: bool,
) -> None:
    """Cleanup-only path used when adaptive gating skips the pane capture.

    Without a pane scrape we can't run interactive-UI detection or arm a new
    idle deadline — but the route_runtime-owned debounce still needs to
    advance so a previously-shown "🟡 Busy" status gets cleared on schedule.

    Semantics: NEVER arm a FRESH deadline here — without a confirmed-idle
    pane scrape we can't start a debounce. Only act on a deadline a prior
    confirmed-idle tick already armed and that is now due — and even then
    (review finding 5) commit ONLY after a SECOND confirmed-idle pane
    observation: ``WATCHDOG_INTERVAL`` (10s) exceeds
    ``IDLE_CLEAR_DELAY_SECONDS`` (4s), so the deadline armed by ONE capture
    would otherwise commit with no further pane evidence, and a single
    mid-redraw misparse on a RUNNING_TOOL route would wipe transcript-set
    ``open_tools`` (the Task's eventual tool_result then arrives as an
    unknown id and is dropped). When the deadline is due we RE-CAPTURE the
    pane and commit ONLY on POSITIVE idle evidence: the frame must look
    like a live Claude Code pane at rest — ``has_pane_chrome`` (the chrome
    separator anchor that ``parse_status_line`` / ``strip_pane_chrome``
    already trust; absent on an empty/truncated/mid-redraw frame) AND not
    ``is_status_active`` (no ``esc to interrupt`` run marker). Mere
    absence-of-active-status is NOT enough (hermes round 2): a non-empty
    malformed frame has no parseable status either and would commit,
    wiping transcript-set ``open_tools``. Anything short of positive idle
    (busy / empty / chrome-less) RE-ARMS via the existing arm path (reset
    first — ``arm_pane_idle_clear`` is a no-op while a deadline is armed)
    so the next due tick re-validates against a fresh frame. A permanently
    chrome-less pane therefore never clears its Busy card via THIS path —
    the full-path watchdog capture still owns that lifecycle. The
    confirmed-idle commit keeps the 4s clear UX (re-capture-at-commit,
    not two-observation arming).
    """
    if skip_status:
        return

    route = (user_id, thread_id or 0, window_id)
    now = time.monotonic()
    if not route_runtime.pane_idle_clear_due(route, now=now):
        return
    # Second-observation gate (finding 5): never commit off the single frame
    # that armed the deadline. One extra capture per due deadline only.
    pane_text = await tmux_manager.capture_pane(window_id)
    # POSITIVE idle evidence required (see docstring): a rendered Claude
    # pane (chrome separator anchor) with no active-run marker. Fail closed
    # on anything else — never treat "couldn't parse a status" as idle.
    second_frame_idle = (
        pane_text is not None
        and has_pane_chrome(pane_text)
        and not is_status_active(pane_text)
    )
    if not second_frame_idle:
        # Busy, empty, or chrome-less (malformed/truncated/mid-redraw)
        # re-capture — re-arm instead of committing.
        route_runtime.reset_pane_idle_clear(route)
        route_runtime.arm_pane_idle_clear(route, now=now)
        return
    if not await route_runtime.commit_pane_idle_clear(route, now=now):
        # commit no-op'd (activity re-armed) — do not clear the card.
        return
    await enqueue_status_update(bot, user_id, window_id, None, thread_id=thread_id)


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
            # Pop ALL poller-local route-keyed caches (review finding 14).
            # This branch returns before update_status_message, so its
            # window-gone teardown never runs for a stale binding — without
            # this a rebound topic reusing the same route key inherits stale
            # entries (see clear_route_caches_for_topic). clear_topic_state
            # above also runs the helper (gate P3-1 — it covers topic close /
            # delete / /unbind too); calling it here keeps this sweep
            # self-sufficient and is idempotent.
            clear_route_caches_for_topic(user_id, thread_id or 0)
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

    Reads ``route_runtime.snapshot(route).typing_eligible`` directly: no tmux
    subprocess fan-out, no per-binding capture_pane. With ~14 bindings the
    status poller's full cycle empirically lands at 6-8s on macOS, longer than
    Telegram's ~5s typing-action TTL, so the indicator was flashing rather
    than holding steady. This loop fires every ``TYPING_ACTION_INTERVAL``
    seconds so the cadence stays well under the TTL regardless of binding
    count. ``update_status_message`` never fires the typing indicator itself.
    """
    logger.info("Typing-action loop started (interval: %ss)", TYPING_ACTION_INTERVAL)
    while True:
        try:
            bindings = list(session_manager.iter_thread_bindings())
            sends: list = []
            for user_id, thread_id, wid in bindings:
                route = (user_id, thread_id or 0, wid)
                # ``typing_eligible`` already covers the
                # RUNNING / RUNNING_TOOL discrimination.
                if not route_runtime.snapshot(route).typing_eligible:
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
