"""Per-route message queue management for ordered message delivery.

Provides a queue-based message processing system that ensures:
  - Content messages are sent in receive order (FIFO) per route
  - Status updates coalesce into a per-route ephemeral slot drained after
    every content tick (status-after-content invariant)
  - Consecutive content messages can be merged for efficiency
  - Thread-aware sending: each MessageTask carries an optional thread_id
    for Telegram topic support

Routes are ``(user_id, thread_id_or_0, window_id)`` so a backlog in one
topic cannot block status / interactive prompts in another.

Rate limiting is handled globally by AIORateLimiter on the Application.

Key components:
  - MessageTask: Dataclass representing a queued message task
  - Route: Per-route key for queues, workers, locks, ephemeral slots
  - get_content_queue: Lookup the per-route content queue
  - Message queue worker: One per route, processes content + drains ephemeral
  - Content task processing with tool_use/tool_result handling
  - Status message tracking and conversion (keyed by (user_id, thread_id))
"""

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Coroutine
from dataclasses import dataclass, field
from collections.abc import Iterable
from typing import Any, Literal

from telegram import Bot, ReplyParameters
from telegram.error import RetryAfter

from ..config import config
from .. import route_runtime
from ..session import session_id_for_window, session_manager
from ..terminal_parser import is_status_active, parse_status_line
from ..tmux_manager import tmux_manager
from . import attention
from . import output_prefs
from . import pane_signals
from ..route_runtime import RunState
from .message_sender import (
    TopicSendOutcome,
    _classify_bad_request,
    send_photo,
    send_with_fallback,
    strip_sentinels,
    topic_delete,
    topic_edit,
    topic_send,
)

Route = tuple[int, int, str]

logger = logging.getLogger(__name__)


# Merge limit for content messages
MERGE_MAX_LENGTH = 3800  # Leave room for markdown conversion overhead


@dataclass
class MessageTask:
    """Message task for queue processing."""

    task_type: Literal["content", "status_update", "status_clear"]
    text: str | None = None
    window_id: str | None = None
    # content type fields
    parts: list[str] = field(default_factory=list)
    tool_use_id: str | None = None
    content_type: str = "text"
    thread_id: int | None = None  # Telegram topic thread_id for targeted send
    image_data: list[tuple[str, bytes]] | None = None  # From tool_result images
    # JSONL ``message.stop_reason`` (W2, plan v4 §3): a sidechain text task
    # carrying an end-of-turn stop_reason is the sub-agent's own "I'm done"
    # signal — the primary collapse trigger for its digest card.
    stop_reason: str | None = None
    # §2.7: surfaced for tool_use tasks so the dispatcher can promote
    # Agent / Task subagent invocations to the top-level message surface.
    tool_name: str | None = None
    # §2.7: raw tool input dict, only carried for tool_use tasks. Used by
    # _render_agent_tool_use to extract description / subagent_type / prompt.
    tool_input: dict[str, object] | None = None
    transcript_uuid: str | None = None
    # Retry-resume cursor (finding 10): number of leading ``parts`` already
    # delivered. ``_run_with_retry`` re-invokes the processor on the SAME
    # task object after a RetryAfter, and ``_process_content_task`` skips
    # parts below this cursor — so a flood-control retry on part 2/3 never
    # re-sends part 1 as a duplicate. Mutated in place by
    # ``_process_content_task`` only.
    parts_sent: int = 0
    # Sub-agent (sidechain) run identifier. When non-None, the task
    # represents an event from one sub-agent JSONL — text/thinking/tool_use/
    # tool_result blocks emitted by the sub-agent itself. The dispatcher
    # routes these through ``_process_subagent_activity_task`` so each run
    # collapses into a single editable digest message instead of one bubble
    # per block. The to-do-list digest also gates on this field so a
    # sub-agent's TodoWrites don't paint the parent topic's task card.
    subagent_key: str | None = None
    # Hermes P2-1: the promoted top-level MessageTask minted by
    # ``_process_agent_task`` for an Agent tool_use / tool_result. Cached on
    # the ORIGINAL Agent task (whose object identity survives
    # ``_run_with_retry`` retries) so a RetryAfter raised AFTER successful
    # delivery re-enters with the SAME promoted task — preserving its
    # advanced ``parts_sent`` cursor / saturation instead of minting a fresh
    # one at ``parts_sent = 0`` (which replayed the tool_use bubble / sent a
    # duplicate tool_result bubble). Lifecycle: set once on the first
    # ``_process_agent_task`` attempt and never cleared — it dies with the
    # task object, which is dropped after ``_run_with_retry`` returns or
    # exhausts its attempts.
    agent_promoted: "MessageTask | None" = None


@dataclass
class ActivityDigestState:
    """Single editable per-topic activity digest for noisy tool/thinking events."""

    message_id: int
    window_id: str
    lines: list[str] = field(default_factory=list)
    tool_count: int = 0
    completed_count: int = 0
    last_text: str = ""
    done: bool = False
    # W1 collapse-on-done (plan v4 §2): wall-clock stamps frozen at first
    # event / finalize so the collapsed summary's duration is stable across
    # repaints, plus the Agent-run counter for the "N sub-agents" part.
    started_at: float = 0.0
    finalized_at: float = 0.0
    subagent_count: int = 0
    # Set under the per-key lock by the delete path; the upsert re-checks it
    # (with slot identity) before any send so a straggler flush can neither
    # repaint nor re-send a deleted card (codex r2 P1-2 protocol).
    tombstoned: bool = False


@dataclass
class SubagentDigestState:
    """Single editable digest for one sub-agent run.

    Mirrors ``ActivityDigestState`` but lives at per-sidechain granularity:
    one digest per ``(user_id, thread_id, subagent_key)``. Each text /
    thinking / tool_use / tool_result block emitted by the sub-agent
    appends or edits a line in this digest, so a long sub-agent run
    surfaces as a single editable message in the parent topic instead of
    one bubble per block.
    """

    message_id: int
    window_id: str
    subagent_key: str
    lines: list[str] = field(default_factory=list)
    tool_count: int = 0
    completed_count: int = 0
    last_text: str = ""
    # W2 (plan v4 §3): True once the card collapsed to its one-line summary
    # (the sidechain's own end-of-turn, or the parent-finalize backstop).
    # The slot is KEPT as a tombstone — a late re-detected block must not
    # re-inflate the play-by-play; a genuinely new run has a new key.
    collapsed: bool = False
    tombstoned: bool = False


@dataclass
class TodoListDigestState:
    """Single editable to-do-list digest per ``(user_id, thread_id_or_0)``.

    Each ``TodoWrite`` call carries the *complete* todo snapshot, so the
    digest is a snapshot replace, not a delta accumulator. We keep only
    ``last_text`` for dedup; the todo content is always re-rendered from
    the most recent ``TodoWrite.tool_input`` before edit/send.
    """

    message_id: int
    window_id: str
    last_text: str = ""


# Per-route message queues, workers, locks, and ephemeral slots
_route_queues: dict[Route, asyncio.Queue[MessageTask]] = {}
_route_workers: dict[Route, asyncio.Task[None]] = {}
_route_locks: dict[Route, asyncio.Lock] = {}  # Merge drain/refill + ephemeral slot
_route_pending_ephemeral: dict[Route, MessageTask | None] = {}
_route_ephemeral_kick: dict[Route, asyncio.Event] = {}
# Set while the worker has a task in flight; teardown waits on this so it
# never hard-cancels mid-`await topic_send` and leak _tool_msg_ids.
_route_inflight: dict[Route, asyncio.Event] = {}
# Routes currently in teardown. Enqueue paths consult this set BEFORE
# touching any route maps; if a route is tearing down, new tasks are
# dropped rather than racing with the teardown's worker-cancel step
# (which would otherwise land mid-`await topic_send` and leak
# `_tool_msg_ids` entries).
_route_tearing_down: set[Route] = set()

# Map (tool_use_id, user_id, thread_id_or_0) -> telegram message_id
# for editing tool_use messages with results
_tool_msg_ids: dict[tuple[str, int, int], int] = {}

# §2.7 Agent (subagent) prominence: tool_use_ids dispatched as Agent tools
# are tracked here so the matching tool_result can be routed back to the
# top-level promotion path instead of falling into the activity digest.
# Keyed by (tool_use_id, user_id, thread_id_or_0) — same shape as
# _tool_msg_ids so the tool_result edit machinery works identically.
AGENT_TOOL_NAMES: frozenset[str] = frozenset({"Agent", "Task"})
# Maps (tool_use_id, user_id, thread_id_or_0) -> the original tool_use
# ``input_data`` dict (subagent_type / description / prompt). Stashing the
# input here lets the matching tool_result render the same description and
# subagent_type even though tool_result blocks don't carry the original
# input themselves.
_agent_tool_ids: dict[tuple[str, int, int], dict[str, object]] = {}

# Status message tracking: (user_id, thread_id_or_0) -> (message_id, window_id, last_text)
_status_msg_info: dict[tuple[int, int], tuple[int, str, str]] = {}

# §2.5.2: route → most recent inbound user message_id. Set at the inbound
# handler offer site (text/photo/voice handlers in bot.py); consumed by the
# first-part-only outbound anchor below and by interactive_ui's card sends.
# Popped after use so an unrelated later assistant turn cannot anchor to a
# stale message. Tearing down a route also drops the entry.
_route_last_user_message: dict[Route, int] = {}

# Per-route wall-clock instant the bot DELIVERED the user's turn into tmux (Item
# 3 / P2-1). Stamped (``time.time()``) PRE-SEND at the delivery seam so a fast
# prose→AUQ turn cannot finalize its prose before the stamp lands; read
# non-consumingly by ``interactive_ui._maybe_post_live_prose`` as the
# ``not_before`` turn boundary for ``md_capture.select_fresh_prose`` so a PRIOR
# turn's leftover prose (still within the freshness TTL) is not posted above a
# picker whose own turn produced no prose. Same clock as the appender's
# ``captured_at``, so directly comparable. Torn down with the route; degrades to
# ``None`` (TTL-only) across a restart.
_route_user_turn_at: dict[Route, float] = {}

# Activity digest tracking: one editable message per user/topic that collapses
# tool calls, tool results, and thinking into a Hermes-style activity card.
_activity_msg_info: dict[tuple[int, int], ActivityDigestState] = {}
_tool_activity_indices: dict[tuple[str, int, int], int] = {}
# Per-(user, thread) debounce timer for activity-digest flushes. Heavy tool
# work emits 5-10 events / sec / topic — flushing each one inline blew through
# Telegram's 20 msg/min/group flood limit when several topics were active at
# once and starved text replies behind a backlog of activity edits. The
# debounce coalesces a burst of state mutations into a single edit, dropping
# typical activity-card traffic by 5-10x. Critical paths (terminal "done"
# state before assistant text, attention-state changes) still flush
# synchronously via _flush_activity_digest_now.
_activity_flush_tasks: dict[tuple[int, int], asyncio.Task[None]] = {}
# Per-(user, thread) lock that serializes calls into ``_upsert_activity_digest``.
# Without this, a debounced flush already inside the awaited Telegram API call
# could race a synchronous ``_flush_activity_digest_now`` triggered by the next
# content task — both reading ``state.message_id == 0`` and both ``topic_send``-ing,
# producing two activity messages that fight for the slot. The lock closes that
# window: the second caller waits, sees the freshly-updated ``state.message_id``,
# and either edits or no-ops via the ``text == last_text`` check.
_activity_locks: dict[tuple[int, int], asyncio.Lock] = {}
ACTIVITY_FLUSH_DEBOUNCE_SECONDS = 10.0

# Sub-agent (sidechain) digest tracking: one editable message per sub-agent
# run, keyed by (user_id, thread_id_or_0, subagent_key). The parent topic's
# regular activity digest stays untouched — a sub-agent's blocks live in
# their own card so a multi-step run is one bubble, not N.
_subagent_msg_info: dict[tuple[int, int, str], SubagentDigestState] = {}
# (tool_use_id, user_id, thread_id_or_0, subagent_key) → index into
# state.lines for tool_use → tool_result pairing inside the digest.
_subagent_tool_indices: dict[tuple[str, int, int, str], int] = {}
# Per-(user, thread, subagent_key) lock that serializes upsert calls so a
# debounced flush can't race a synchronous flush. Mirrors _activity_locks.
_subagent_locks: dict[tuple[int, int, str], asyncio.Lock] = {}
# Per-(user, thread, subagent_key) debounce timer. Same rationale as the
# parent activity digest: bursts of sub-agent events coalesce into one edit.
_subagent_flush_tasks: dict[tuple[int, int, str], asyncio.Task[None]] = {}
SUBAGENT_DIGEST_MAX_LINES = 12
SUBAGENT_DIGEST_MAX_LINE_LENGTH = 400
SUBAGENT_DIGEST_TEXT_SNIPPET_LENGTH = 240

# To-do-list digest tracking: one editable card per ``(user_id, thread_id_or_0)``
# rendered from the most recent parent ``TodoWrite`` snapshot. The Telegram
# message_id is held in ``_todo_msg_info``; the lock + debounce mirror the
# activity / subagent digest patterns so a burst of TodoWrites coalesces into
# one edit.
_todo_msg_info: dict[tuple[int, int], TodoListDigestState] = {}
_todo_locks: dict[tuple[int, int], asyncio.Lock] = {}
_todo_flush_tasks: dict[tuple[int, int], asyncio.Task[None]] = {}
# Latest snapshot of todos, keyed by route. Cached so the debounced flush
# can render after the original task is gone, and so back-to-back TodoWrites
# within the debounce window collapse to "render the most recent only".
_todo_pending_snapshot: dict[tuple[int, int], list[dict[str, object]]] = {}
# tool_use_ids that came from a parent ``TodoWrite``. Tracked per-route so
# the matching ``tool_result`` can be dropped from the activity digest
# path; otherwise the result line leaks into the activity card as
# "**TodoWrite** — applied" noise duplicating the digest. (Historic note:
# this set was added when ``tool_result`` ParsedEntries carried no
# ``tool_name``; transcript_parser now propagates it on tool_result too,
# but the id-set lookup remains the authoritative identity for
# routed-through-digest results.)
#
# Bounded LRU because the entry is only removed on (a) the matching
# tool_result landing or (b) ``teardown_route``. A TodoWrite whose
# tool_result never arrives (transcript truncation, session killed
# mid-tool, hook drop) would otherwise leak forever in this
# process-global structure.
_todo_tool_ids: OrderedDict[tuple[str, int, int], None] = OrderedDict()
TODO_TOOL_IDS_MAX = 1024
TODO_DIGEST_MAX_VISIBLE = 8
TODO_DIGEST_CONTENT_SNIPPET = 120


def _todo_tool_ids_record(key: tuple[str, int, int]) -> None:
    """Record a TodoWrite tool_use id; evict the oldest if over cap.

    Wrapping the add path centralizes the bounded-size invariant — call
    sites stay one-liners, and the eviction policy can be tuned in one
    place if we ever switch to TTL or per-route bounds. ``move_to_end``
    on a re-recorded key is defensive: in normal flow each TodoWrite has
    a fresh tool_use_id so the branch is dead, but if a future code path
    ever re-records (e.g. JSONL replay on startup), it keeps still-open
    ids from being prematurely evicted by a flood of new ones.
    """
    if key in _todo_tool_ids:
        _todo_tool_ids.move_to_end(key)
        return
    _todo_tool_ids[key] = None
    if len(_todo_tool_ids) > TODO_TOOL_IDS_MAX:
        evicted, _ = _todo_tool_ids.popitem(last=False)
        # Hitting the cap shouldn't happen at typical TodoWrite rates;
        # if we do, it's likely a leak path (a tool_result code path
        # that doesn't pop) rather than legitimate volume. Surface it.
        logger.debug(
            "todo_tool_ids LRU evicted oldest entry %s (cap=%d)",
            evicted,
            TODO_TOOL_IDS_MAX,
        )


# Mirrors bot._TURN_END_STOP_REASONS (bot imports this module, so the tiny
# constant is duplicated here rather than imported back).
_TURN_END_STOP_REASONS = frozenset({"end_turn", "stop_sequence"})

ACTIVITY_DIGEST_CONTENT_TYPES = {"tool_use", "tool_result", "thinking"}
ACTIVITY_DIGEST_MAX_LINES = 10
# Per-line cap inside the activity digest. The previous 180 was tight
# enough that most tool_use bash invocations got truncated mid-command and
# tool_result outputs collapsed to "Output N lines" with no actual content
# visible — leaving the user staring at "🟡 Busy" without knowing what
# Claude was doing. 400 fits a typical bash command plus a sentence of
# output without overflowing Telegram's compact-card visual budget.
ACTIVITY_DIGEST_MAX_LINE_LENGTH = 400
# Length cap for the first-line-of-output snippet appended after a tool
# call's "  ⎿  " marker. Short enough that long log dumps don't blow up
# the digest, long enough to surface useful messages (errors, paths,
# stat lines) instead of just the word count.
ACTIVITY_DIGEST_RESULT_SNIPPET_LENGTH = 240

# Flood control: user_id -> monotonic time when ban expires.
# Intentionally per-user (NOT per-route): Telegram's flood-control limit is
# global to the bot token, so a 429 on one route should pause that user's
# other routes too. Per-route tracking would not buy real isolation.
_flood_until: dict[int, float] = {}

# Cross-module emergency DM cooldown is owned by ``handlers.attention``
# (``attention.should_emit_emergency_dm``). Both this module and
# ``handlers.interactive_ui`` route through that fence so the same waiting
# episode cannot fire two DMs from two surfaces.

# Topic ids that Telegram rejected during this process lifetime. Keep the binding
# for routing inbound replies to the tmux window, but deliver outbound updates by DM.
#
# KNOWN LIMITATION (intentionally not fixed in this pass): the set is sticky
# for the life of the process. A topic that fails once is treated as broken
# until the bot restarts even if the topic is later reopened/recreated by the
# user. There is no proactive probe — the previous
# ``unpin_all_forum_topic_messages`` polling probe was removed because it
# silently cleared user-pinned messages on success, not a no-op. The current
# lean behavior is intentionally simple: noisy DMs beat silent data loss when
# a topic is genuinely unreachable. Do NOT add automatic create/reopen here
# without a real Telegram supergroup smoke path.
_bad_topic_threads: set[tuple[int, int]] = set()

# Strong refs to detached background tasks (finding 23). A bare
# ``asyncio.create_task`` with the only reference dropped can be GC'd mid-run
# (cpython#91887 — the loop holds a weak ref) and silently swallows
# exceptions. Local copy of ``inbound_aggregator._spawn_background`` (importing
# it here would create a cycle: the aggregator imports from this module), plus
# exception logging in the done-callback.
_background_tasks: set[asyncio.Task[object]] = set()


def _on_background_task_done(task: asyncio.Task[object]) -> None:
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Detached background task failed: %s", exc, exc_info=exc)


def _spawn_background(coro: Coroutine[object, object, object]) -> None:
    """Spawn a detached task retained in a module-level strong-ref set."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_on_background_task_done)


# Topic outcomes that should trigger emergency DM fallback. These are the cases
# where retrying the same topic is futile within the current process.
_TOPIC_BROKEN_OUTCOMES: frozenset[TopicSendOutcome] = frozenset(
    {
        TopicSendOutcome.TOPIC_NOT_FOUND,
        TopicSendOutcome.TOPIC_CLOSED,
        TopicSendOutcome.FORBIDDEN,
    }
)

# Max seconds to wait for flood control before dropping tasks
FLOOD_CONTROL_MAX_WAIT = 10


def _route_for(user_id: int, thread_id: int | None, window_id: str) -> Route:
    return (user_id, thread_id or 0, window_id)


# ``_session_id_for_window`` lived here in Stage 5.c; it is now the canonical
# ``session.session_id_for_window`` so attention/interactive-UI/message-queue
# all share one implementation. Local alias kept so internal callers below
# don't churn.
_session_id_for_window = session_id_for_window


def set_route_last_user_message(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    message_id: int,
) -> None:
    """Stash the latest inbound user ``message_id`` for outbound anchoring.

    The first part of the next assistant-text response will set
    ``reply_parameters=ReplyParameters(message_id=...)`` so the response
    visually replies to the user's prompt. Per §2.5.2, only the first part
    anchors; tool / activity / status sends never anchor.
    """
    route = _route_for(user_id, thread_id, window_id)
    _route_last_user_message[route] = message_id


def peek_route_last_user_message(
    user_id: int,
    thread_id: int | None,
    window_id: str,
) -> int | None:
    """Read-only lookup for ``interactive_ui`` card anchoring.

    The interactive-UI card surface anchors to the user's prompt that
    triggered the interactive tool — same first-only rule as content sends.
    Reading without popping lets the same anchor still apply when the
    assistant's text response follows the interactive UI.
    """
    route = _route_for(user_id, thread_id, window_id)
    return _route_last_user_message.get(route)


def consume_route_last_user_message(
    user_id: int,
    thread_id: int | None,
    window_id: str,
) -> int | None:
    """Pop helper for callers that own the anchor lifecycle (interactive UI)."""
    route = _route_for(user_id, thread_id, window_id)
    return _route_last_user_message.pop(route, None)


def set_route_user_turn_at(
    user_id: int,
    thread_id: int | None,
    window_id: str,
) -> None:
    """Stamp the wall-clock instant the bot delivers the current user turn into
    tmux (Item 3 / P2-1 turn boundary).

    Called PRE-SEND at the delivery seam (immediately before
    ``send_to_window``) so the boundary precedes any prose the turn streams. The
    same ``time.time()`` clock the MessageDisplay appender stamps as
    ``captured_at``, so ``select_fresh_prose(not_before=...)`` can compare them
    directly: the current turn's prose finalizes AFTER this stamp, a prior
    turn's leftover prose BEFORE it.

    Wave C: the SAME ``ts`` is mirrored into
    ``route_runtime.stamp_user_turn`` so the dashboard's unanswered-turn
    derivation (``last_assistant_turn_ended_at > last_user_turn_at``)
    compares two stamps on one clock. The mirror lives HERE — the single
    writer — rather than at the three delivery seams (aggregator
    ``_send_bundle``, ``bot.forward_command_handler``, the ``/effort``
    callback) so same-ts is guaranteed by construction and a future seam
    can't forget it.
    """
    route = _route_for(user_id, thread_id, window_id)
    ts = time.time()
    _route_user_turn_at[route] = ts
    route_runtime.stamp_user_turn(route, ts)


def peek_route_user_turn_at(
    user_id: int,
    thread_id: int | None,
    window_id: str,
) -> float | None:
    """Non-consuming read of the route's last user-turn delivery instant, or
    ``None`` when none was stamped (e.g. after a restart — degrades the live
    prose freshness to TTL-only)."""
    route = _route_for(user_id, thread_id, window_id)
    return _route_user_turn_at.get(route)


def get_content_queue(route: Route) -> asyncio.Queue[MessageTask] | None:
    """Get the content queue for a route (if exists)."""
    return _route_queues.get(route)


def _get_or_create_route(bot: Bot, route: Route) -> asyncio.Queue[MessageTask]:
    """Ensure per-route queue/worker/lock/ephemeral state exists.

    Race note: this function MUST NOT contain any ``await`` between the
    ``route not in _route_queues`` check and the inserts below. asyncio's
    cooperative scheduling guarantees that a single coroutine runs to its
    next ``await`` without interleaving — so today, two concurrent
    ``enqueue_*`` calls cannot both pass the check. Adding an ``await``
    here would break that invariant and let two workers spawn for the
    same route. If you need to await on something during route creation,
    introduce an ``asyncio.Lock`` keyed on the route and double-check
    membership after acquiring it.
    """
    if route not in _route_queues:
        _route_queues[route] = asyncio.Queue()
        _route_locks[route] = asyncio.Lock()
        _route_pending_ephemeral[route] = None
        _route_ephemeral_kick[route] = asyncio.Event()
        idle = asyncio.Event()
        idle.set()
        _route_inflight[route] = idle
        _route_workers[route] = asyncio.create_task(_message_queue_worker(bot, route))
    return _route_queues[route]


def _inspect_queue(queue: asyncio.Queue[MessageTask]) -> list[MessageTask]:
    """Non-destructively inspect all items in queue.

    Drains the queue and returns all items. Caller must refill.
    """
    items: list[MessageTask] = []
    while not queue.empty():
        try:
            item = queue.get_nowait()
            items.append(item)
        except asyncio.QueueEmpty:
            break
    return items


def _can_merge_tasks(base: MessageTask, candidate: MessageTask) -> bool:
    """Check if two content tasks can be merged."""
    if base.window_id != candidate.window_id:
        return False
    if candidate.task_type != "content":
        return False
    # Activity events are handled by the editable activity digest, not merged
    # into final assistant text.
    if base.content_type in ACTIVITY_DIGEST_CONTENT_TYPES:
        return False
    if candidate.content_type in ACTIVITY_DIGEST_CONTENT_TYPES:
        return False
    # Sub-agent events go to the per-sidechain editable digest, never merged
    # into ad-hoc text bubbles. (The base check covers the case where a
    # sub-agent task is at the head of the queue.)
    if base.subagent_key is not None or candidate.subagent_key is not None:
        return False
    return True


async def _merge_content_tasks(
    queue: asyncio.Queue[MessageTask],
    first: MessageTask,
    lock: asyncio.Lock,
) -> tuple[MessageTask, int]:
    """Merge consecutive content tasks from queue.

    Returns: (merged_task, merge_count) where merge_count is the number of
    additional tasks merged (0 if no merging occurred).

    Note on queue counter management:
        When we put items back, we call task_done() to compensate for the
        internal counter increment caused by put_nowait(). This is necessary
        because the items were already counted when originally enqueued.
        Without this compensation, queue.join() would wait indefinitely.
    """
    merged_parts = list(first.parts)
    current_length = sum(len(p) for p in merged_parts)
    merge_count = 0

    async with lock:
        items = _inspect_queue(queue)
        remaining: list[MessageTask] = []

        for i, task in enumerate(items):
            if not _can_merge_tasks(first, task):
                # Can't merge, keep this and all remaining items
                remaining = items[i:]
                break

            # Check length before merging
            task_length = sum(len(p) for p in task.parts)
            if current_length + task_length > MERGE_MAX_LENGTH:
                # Too long, stop merging
                remaining = items[i:]
                break

            merged_parts.extend(task.parts)
            current_length += task_length
            merge_count += 1

        # Put remaining items back into the queue
        for item in remaining:
            queue.put_nowait(item)
            # Compensate: this item was already counted when first enqueued,
            # put_nowait adds a duplicate count that must be removed
            queue.task_done()

    if merge_count == 0:
        return first, 0

    return (
        MessageTask(
            task_type="content",
            window_id=first.window_id,
            parts=merged_parts,
            tool_use_id=first.tool_use_id,
            content_type=first.content_type,
            thread_id=first.thread_id,
        ),
        merge_count,
    )


CONTENT_RETRY_MAX_ATTEMPTS = 3


def _retry_after_seconds(exc: RetryAfter) -> int:
    """Coerce ``RetryAfter.retry_after`` (int or timedelta) into seconds."""
    return (
        exc.retry_after
        if isinstance(exc.retry_after, int)
        else int(exc.retry_after.total_seconds())
    )


def _is_agent_tool_use(task: MessageTask) -> bool:
    return (
        task.task_type == "content"
        and task.content_type == "tool_use"
        and task.tool_name in AGENT_TOOL_NAMES
    )


def _is_agent_tool_result(task: MessageTask, user_id: int) -> bool:
    if (
        task.task_type != "content"
        or task.content_type != "tool_result"
        or not task.tool_use_id
    ):
        return False
    tid = task.thread_id or 0
    return (task.tool_use_id, user_id, tid) in _agent_tool_ids


async def _dispatch_task(
    bot: Bot,
    user_id: int,
    queue: asyncio.Queue[MessageTask],
    lock: asyncio.Lock,
    task: MessageTask,
    merged_holder: list[MessageTask] | None = None,
) -> MessageTask:
    """Run a single task. May raise RetryAfter; the caller decides whether to
    retry (content) or drop (status).

    Merge-side bookkeeping (drain queue, ``task_done`` for merged items)
    runs exactly once here. The returned ``MessageTask`` is the actually
    dispatched form (merged for content, original for everything else),
    and ``_run_with_retry`` reuses it on subsequent attempts via
    ``_dispatch_already_merged`` so retries never re-drain the queue.

    ``merged_holder`` (finding 10): the merged task is recorded there
    BEFORE processing, because a RetryAfter raises out of this function —
    a return value alone would lose the merged form, the retry would
    re-enter ``_merge_content_tasks`` (dropping the first merge's drained
    tasks and double-counting ``task_done``), and the per-task
    ``parts_sent`` resume cursor would reset.
    """
    if task.task_type == "content":
        # Sub-agent events route to the per-sidechain editable digest BEFORE
        # any other branching. They never reach the agent / activity / merge
        # paths because each sub-agent run owns its own editable card.
        if task.subagent_key is not None:
            await _process_subagent_activity_task(bot, user_id, task)
            return task
        # §2.7: Agent (subagent) tool_use / tool_result get promoted to
        # top-level messages BEFORE the digest short-circuit. The activity
        # counter still tracks them so the digest header stays accurate
        # (`_process_agent_task` updates the counter without appending to
        # `lines`).
        if _is_agent_tool_use(task) or _is_agent_tool_result(task, user_id):
            await _process_agent_task(bot, user_id, task)
            return task
        if _is_todo_tool_use(task):
            await _process_todo_task(bot, user_id, task)
            return task
        if _is_todo_tool_result(task, user_id):
            # Drop the matching tool_result entirely so it doesn't paint a
            # duplicate "**TodoWrite** — applied" line on the activity card.
            tid = task.thread_id or 0
            if task.tool_use_id:
                _todo_tool_ids.pop((task.tool_use_id, user_id, tid), None)
            return task
        if task.content_type in ACTIVITY_DIGEST_CONTENT_TYPES:
            await _process_activity_task(bot, user_id, task)
            return task
        merged_task, merge_count = await _merge_content_tasks(queue, task, lock)
        if merge_count > 0:
            logger.debug("Merged %d tasks for user %d", merge_count, user_id)
            for _ in range(merge_count):
                queue.task_done()
        if merged_holder is not None:
            merged_holder.append(merged_task)
        await _process_content_task(bot, user_id, merged_task)
        return merged_task
    if task.task_type == "status_update":
        await _process_status_update_task(bot, user_id, task)
        return task
    if task.task_type == "status_clear":
        await _do_clear_status_message(bot, user_id, task.thread_id or 0)
        return task
    return task


async def _dispatch_already_merged(
    bot: Bot,
    user_id: int,
    task: MessageTask,
) -> None:
    """Dispatch a task whose merge bookkeeping already happened.

    Used by ``_run_with_retry`` for second-and-later attempts so retries
    do NOT re-enter ``_merge_content_tasks`` (which would re-drain the
    live queue and double-count ``task_done``).
    """
    if task.task_type == "content":
        if task.subagent_key is not None:
            await _process_subagent_activity_task(bot, user_id, task)
            return
        if _is_agent_tool_use(task) or _is_agent_tool_result(task, user_id):
            await _process_agent_task(bot, user_id, task)
            return
        if _is_todo_tool_use(task):
            await _process_todo_task(bot, user_id, task)
            return
        if _is_todo_tool_result(task, user_id):
            tid = task.thread_id or 0
            if task.tool_use_id:
                _todo_tool_ids.pop((task.tool_use_id, user_id, tid), None)
            return
        if task.content_type in ACTIVITY_DIGEST_CONTENT_TYPES:
            await _process_activity_task(bot, user_id, task)
            return
        await _process_content_task(bot, user_id, task)
        return
    if task.task_type == "status_update":
        await _process_status_update_task(bot, user_id, task)
        return
    if task.task_type == "status_clear":
        await _do_clear_status_message(bot, user_id, task.thread_id or 0)


async def _run_with_retry(
    bot: Bot,
    user_id: int,
    queue: asyncio.Queue[MessageTask],
    lock: asyncio.Lock,
    task: MessageTask,
) -> None:
    """Dispatch a task with the documented RetryAfter / flood-control policy."""
    flood_end = _flood_until.get(user_id, 0)
    if flood_end > 0:
        remaining = flood_end - time.monotonic()
        if remaining > 0:
            if task.task_type != "content":
                return
            logger.debug(
                "Flood controlled: waiting %.0fs for content (user %d)",
                remaining,
                user_id,
            )
            await asyncio.sleep(remaining)
        _flood_until.pop(user_id, None)
        logger.info("Flood control lifted for user %d", user_id)

    attempts_remaining = (
        CONTENT_RETRY_MAX_ATTEMPTS if task.task_type == "content" else 1
    )
    dispatched: MessageTask | None = None
    while attempts_remaining > 0:
        attempts_remaining -= 1
        try:
            if dispatched is None:
                # First attempt: drain/merge bookkeeping happens here. The
                # holder captures the merged task even when the dispatch
                # RAISES (finding 10) — without it, ``dispatched`` stayed
                # None on a RetryAfter and the retry re-entered the merge,
                # losing the first merge's drained tasks and resetting the
                # ``parts_sent`` resume cursor.
                holder: list[MessageTask] = []
                try:
                    dispatched = await _dispatch_task(
                        bot, user_id, queue, lock, task, holder
                    )
                except RetryAfter:
                    if holder:
                        dispatched = holder[0]
                    raise
            else:
                # Retry: reuse the already-merged task; do NOT re-drain.
                await _dispatch_already_merged(bot, user_id, dispatched)
            return
        except RetryAfter as e:
            retry_secs = _retry_after_seconds(e)
            if retry_secs > FLOOD_CONTROL_MAX_WAIT:
                _flood_until[user_id] = time.monotonic() + retry_secs
                logger.warning(
                    "Flood control for user %d: retry_after=%ds, "
                    "pausing queue until ban expires (task_type=%s)",
                    user_id,
                    retry_secs,
                    task.task_type,
                )
            else:
                logger.warning(
                    "Flood control for user %d: waiting %ds (task_type=%s, attempts_left=%d)",
                    user_id,
                    retry_secs,
                    task.task_type,
                    attempts_remaining,
                )

            if task.task_type != "content":
                return

            if attempts_remaining <= 0:
                logger.error(
                    "Content task dropped for user %d after %d RetryAfter retries (window=%s)",
                    user_id,
                    CONTENT_RETRY_MAX_ATTEMPTS,
                    task.window_id,
                )
                return

            await asyncio.sleep(retry_secs)


async def _drain_pending_ephemeral(
    bot: Bot,
    route: Route,
    queue: asyncio.Queue[MessageTask],
    lock: asyncio.Lock,
) -> None:
    """Send the latest coalesced ephemeral, if any. Always runs after a content
    tick so the status-after-content invariant holds.

    The slot snapshot AND the kick clear happen under the same lock so a
    concurrent ``enqueue_status_update`` cannot land between drain-snapshot
    and kick.clear() — that race would set the slot but then the worker
    would clear the kick and park indefinitely until the next content
    arrival.
    """
    user_id = route[0]
    kick = _route_ephemeral_kick.get(route)
    async with lock:
        pending = _route_pending_ephemeral.get(route)
        _route_pending_ephemeral[route] = None
        # Clear kick UNDER the lock — paired with enqueue_status_update,
        # which sets the slot and then sets the kick under the same lock.
        if kick is not None:
            kick.clear()
    if pending is None:
        return
    try:
        await _run_with_retry(bot, user_id, queue, lock, pending)
    except Exception as e:
        logger.error("Error processing ephemeral task for route %s: %s", route, e)


async def _drain_pending(tasks: Iterable[asyncio.Task[Any]]) -> None:
    """Cancel + collect the losing ``asyncio.wait`` branches.

    Finding 2: the previous ``for p in pending: await p`` under
    ``except BaseException: pass`` ate the WORKER's own CancelledError when
    ``teardown_route``'s ``worker.cancel()`` landed in the window between
    the racing ``asyncio.wait`` returning and ``inflight.clear()`` (the
    inflight Event is still SET there, so teardown's ``inflight.wait()``
    passes immediately). Cancellation is one-shot — eating it resumed the
    worker, hung ``await worker`` forever, and silently dropped every
    future message for the route via the tearing-down guard.

    ``asyncio.wait`` never raises the collected tasks' exceptions (or their
    cancellations) into the waiter, so a CancelledError escaping THIS
    coroutine can only be the worker's own cancellation — it must
    propagate. No broad except here, and ``.result()`` is never called on
    cancelled tasks; real exceptions are retrieved only to keep the event
    loop from warning about them.
    """
    if not tasks:
        return
    for t in tasks:
        t.cancel()
    done, _ = await asyncio.wait(tasks)
    for t in done:
        if not t.cancelled() and t.exception() is not None:
            logger.warning("queue worker wait-branch task failed: %r", t.exception())


async def _message_queue_worker(bot: Bot, route: Route) -> None:
    """Process content + ephemeral tasks for a single route."""
    user_id = route[0]
    queue = _route_queues[route]
    lock = _route_locks[route]
    kick = _route_ephemeral_kick[route]
    inflight = _route_inflight[route]
    logger.info(f"Message queue worker started for route {route}")

    while True:
        try:
            content_get: asyncio.Task[MessageTask] = asyncio.create_task(queue.get())
            kick_wait: asyncio.Task[bool] = asyncio.create_task(kick.wait())
            try:
                done, pending = await asyncio.wait(
                    {content_get, kick_wait},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                content_get.cancel()
                kick_wait.cancel()
                raise

            await _drain_pending(pending)

            task: MessageTask | None = None
            if content_get in done:
                task = content_get.result()

            inflight.clear()
            try:
                if task is not None:
                    logger.info(
                        "worker_dequeue route=%s task_type=%s ctype=%s wid=%s qsize=%d",
                        route,
                        task.task_type,
                        task.content_type,
                        task.window_id,
                        queue.qsize(),
                    )
                    try:
                        await _run_with_retry(bot, user_id, queue, lock, task)
                    except Exception as e:
                        logger.error(
                            "Error processing message task for route %s: %s",
                            route,
                            e,
                        )
                    finally:
                        queue.task_done()

                await _drain_pending_ephemeral(bot, route, queue, lock)
            finally:
                inflight.set()
        except asyncio.CancelledError:
            logger.info(f"Message queue worker cancelled for route {route}")
            break
        except Exception as e:
            logger.error(f"Unexpected error in queue worker for route {route}: {e}")
            # Avoid a tight error loop if the outer try keeps raising on
            # the same condition. A small backoff lets transient issues
            # (e.g. asyncio scheduling glitches) settle without us
            # spinning the event loop.
            await asyncio.sleep(0.1)


def _send_kwargs(thread_id: int | None) -> dict[str, int]:
    """Build message_thread_id kwargs for bot.send_message()."""
    if thread_id is not None:
        return {"message_thread_id": thread_id}
    return {}


def _delivery_target(user_id: int, thread_id: int | None) -> tuple[int, int | None]:
    """Return (chat_id, effective_thread_id), falling back to DM for known-bad topics."""
    if thread_id is not None and (user_id, thread_id) in _bad_topic_threads:
        return user_id, None
    return session_manager.resolve_chat_id(user_id, thread_id), thread_id


def _mark_bad_topic(user_id: int, thread_id: int | None) -> None:
    if thread_id is not None:
        _bad_topic_threads.add((user_id, thread_id))


# Outcomes that prove the topic itself is gone (deleted) or unreachable
# (closed). Telegram does not emit a ``forum_topic_deleted`` service message,
# so a failed send is the only signal we ever get for deletion. ``TOPIC_CLOSED``
# is included as a fallback in case the dedicated ``FORUM_TOPIC_CLOSED``
# handler missed the event (bot down at close time, privacy-mode quirk, etc).
_TOPIC_GONE_OUTCOMES: frozenset[TopicSendOutcome] = frozenset(
    {TopicSendOutcome.TOPIC_NOT_FOUND, TopicSendOutcome.TOPIC_CLOSED}
)


async def _orphan_window_for_dead_topic(
    bot: Bot,
    user_id: int,
    thread_id: int,
    window_id: str,
    outcome: TopicSendOutcome,
) -> None:
    """Mirror ``topic_closed_handler`` cleanup when a send proves the topic is gone.

    Invoked once per topic transition from ``_emergency_dm``. Spawned as a
    detached task because ``clear_topic_state`` calls ``teardown_route`` on
    the worker route currently executing this code path — awaiting it inline
    would deadlock on ``inflight.wait()``.
    """
    display = session_manager.get_display_name(window_id) or window_id
    window = await tmux_manager.find_window_by_id(window_id)
    if window is not None:
        await tmux_manager.kill_window(window.window_id)
    session_manager.unbind_thread(user_id, thread_id)
    # Lazy import: ``handlers.cleanup`` imports from this module.
    from . import cleanup as _cleanup

    await _cleanup.clear_topic_state(
        user_id, thread_id, bot, user_data=None, drop_pending=True
    )
    logger.info(
        "Reactive topic cleanup: outcome=%s killed window %s "
        "(user=%d, thread=%d) — no service message; topic deleted "
        "or close was missed",
        outcome.value,
        display,
        user_id,
        thread_id,
    )


# Tiny inter-probe pause so a user with many bindings doesn't get a thundering
# herd of typing actions on the daily tick.
_PROBE_INTER_DELAY_SECONDS = 0.1


async def probe_topic_liveness(bot: Bot) -> None:
    """Detect silently-deleted topics on a daily tick.

    Telegram does not emit ``forum_topic_deleted`` to bots, so a topic the
    user deleted while its session was idle would never surface — reactive
    cleanup in ``_emergency_dm`` only runs when something tries to send. This
    probe walks each bound topic and treats ``TOPIC_NOT_FOUND`` /
    ``TOPIC_CLOSED`` as evidence the topic is gone, then runs the same
    cleanup ``topic_closed_handler`` does.

    Probe call: ``sendChatAction(typing, message_thread_id=...)``. Cheapest
    side-effect-free RPC that reports the topic-shaped failures we care
    about. The 5-second typing flicker on a dormant topic is essentially
    invisible — no service message, no notification, no persistent UI.

    Safe to ``await _orphan_window_for_dead_topic`` inline here: the probe
    runs from the daily GC task, NOT from inside any per-route worker, so
    ``teardown_route``'s ``inflight.wait()`` will not deadlock.
    """
    snapshot: list[tuple[int, int, str]] = []
    for user_id, bindings in list(session_manager.thread_bindings.items()):
        for thread_id, window_id in list(bindings.items()):
            if (user_id, thread_id) in _bad_topic_threads:
                continue
            snapshot.append((user_id, thread_id, window_id))

    if not snapshot:
        return

    cleaned = 0
    for user_id, thread_id, window_id in snapshot:
        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
        try:
            await bot.send_chat_action(
                chat_id=chat_id,
                action="typing",
                message_thread_id=thread_id,
            )
        except RetryAfter:
            # Don't fight the global limiter on a best-effort probe; the
            # remaining bindings get checked on the next daily tick.
            logger.info(
                "topic liveness probe deferred by RetryAfter at user=%d thread=%d",
                user_id,
                thread_id,
            )
            return
        except Exception as exc:
            outcome = _classify_bad_request(exc)
            if outcome in _TOPIC_GONE_OUTCOMES:
                _mark_bad_topic(user_id, thread_id)
                await _orphan_window_for_dead_topic(
                    bot, user_id, thread_id, window_id, outcome
                )
                cleaned += 1
            else:
                logger.debug(
                    "topic_liveness probe non-gone failure user=%d thread=%d "
                    "outcome=%s err=%r",
                    user_id,
                    thread_id,
                    outcome.value,
                    exc,
                )
        await asyncio.sleep(_PROBE_INTER_DELAY_SECONDS)

    logger.info(
        "topic liveness probe checked %d binding(s), cleaned %d dead topic(s)",
        len(snapshot),
        cleaned,
    )


async def _emergency_dm(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
    kind: str = "content",
    outcome: TopicSendOutcome | None = None,
) -> None:
    """STRICT EMERGENCY ONLY: DM when the topic itself is unreachable.

    Reached after a topic_send/topic_edit returned a topic-shaped failure
    (TOPIC_NOT_FOUND/TOPIC_CLOSED/FORBIDDEN). The topic-first attention card
    and content/status sends are the normal surface; this exists so that
    Claude output and "needs your input" cues do not silently vanish when the
    topic is truly broken.

    On the first occurrence of TOPIC_NOT_FOUND/TOPIC_CLOSED for a given
    ``(user_id, thread_id)`` we also fire reactive cleanup of the orphaned
    tmux window — Telegram does not notify bots of topic deletion, so this
    failed send is the only deletion signal we get.
    """
    # Sample membership before _mark_bad_topic mutates the set so the cleanup
    # fires exactly once per topic transition. Check + mark are both sync, no
    # await between them, so concurrent _emergency_dm calls cannot both observe
    # ``already_marked=False``.
    already_marked = (
        thread_id is not None and (user_id, thread_id) in _bad_topic_threads
    )
    _mark_bad_topic(user_id, thread_id)
    if (
        not already_marked
        and thread_id is not None
        and window_id
        and outcome in _TOPIC_GONE_OUTCOMES
    ):
        # Detached task: clear_topic_state tears down the current worker's
        # route; awaiting it from inside the worker would deadlock on
        # teardown_route's inflight.wait(). Strong-ref retained (finding 23).
        _spawn_background(
            _orphan_window_for_dead_topic(bot, user_id, thread_id, window_id, outcome)
        )
    display = session_manager.get_display_name(window_id) if window_id else "unknown"
    if not attention.should_emit_emergency_dm(user_id, thread_id, window_id):
        logger.debug(
            "Skipping duplicate emergency DM for user=%d thread=%s window=%s kind=%s",
            user_id,
            thread_id,
            window_id,
            kind,
        )
        return

    reason = outcome.value if outcome is not None else "topic delivery failed"
    prefix = (
        f"⚠️ CC Telegram could not post this {kind} in topic {thread_id} ({display}) "
        f"[{reason}]; emergency DM:\n\n"
    )
    body = text
    # Telegram hard limit is 4096; keep room for markdown escaping/fallback.
    if len(prefix) + len(body) > 3800:
        body = body[: 3800 - len(prefix) - 20] + "\n… [truncated]"
    try:
        sent = await send_with_fallback(bot, user_id, prefix + body)
        if sent:
            logger.info(
                "emergency_dm op=%s user=%d thread=%s window=%s outcome=%s sent",
                kind,
                user_id,
                thread_id,
                window_id,
                reason,
            )
        else:
            logger.error(
                "emergency_dm op=%s user=%d thread=%s outcome=%s send returned None",
                kind,
                user_id,
                thread_id,
                reason,
            )
    except Exception as e:
        logger.error(
            "emergency_dm op=%s user=%d thread=%s outcome=%s failed: %s",
            kind,
            user_id,
            thread_id,
            reason,
            e,
        )


def _display_name(window_id: str) -> str:
    """Best-effort human name for a tmux window/topic."""
    return session_manager.get_display_name(window_id) or window_id or "Claude"


def _status_display_text(window_id: str, text: str) -> str:
    """Format busy status with the window/topic identity visible."""
    display = _display_name(window_id)
    return f"🟡 Busy — {display}\n{text}"


def _compact_activity_line(
    task: MessageTask,
    *,
    line_chars: int = ACTIVITY_DIGEST_MAX_LINE_LENGTH,
    snippet_chars: int = ACTIVITY_DIGEST_RESULT_SNIPPET_LENGTH,
) -> str:
    """Render a compact single-line activity entry.

    ``line_chars`` / ``snippet_chars`` come from the recipient's resolved
    ``OutputPrefs`` (plan v4 §4); the module constants stay as the
    ``verbose``-preset defaults.
    """
    if task.content_type == "thinking":
        return "💭 Thinking"

    raw = " ".join(part.strip() for part in task.parts if part and part.strip())
    raw = strip_sentinels(raw).replace("\n", " ")
    raw = " ".join(raw.split())
    if not raw:
        raw = task.content_type

    if "  ⎿  " in raw:
        left, rest = raw.split("  ⎿  ", 1)
        # Surface the first line of output (where stats / first error /
        # first useful sentence usually lives) rather than truncating to a
        # fixed word count — that used to swallow long error messages and
        # path lists. Subsequent lines are dropped to keep the digest
        # compact; the user can still expand the topic for the full reply.
        first_line = rest.split("\n", 1)[0].strip()
        if len(first_line) > snippet_chars:
            first_line = first_line[: snippet_chars - 1].rstrip() + "…"
        raw = f"{left} — {first_line}" if first_line else left

    if len(raw) > line_chars:
        raw = raw[: line_chars - 1].rstrip() + "…"

    if task.content_type == "tool_result":
        if "error" in raw.lower():
            return f"❌ {raw}"
        if "interrupted" in raw.lower():
            return f"⏹ {raw}"
        return f"✅ {raw}"
    return f"⚙️ {raw}"


_RUN_STATE_HEADER: dict[RunState, str] = {
    RunState.RUNNING: "🟡 Busy",
    RunState.RUNNING_TOOL: "🟡 Busy",
    # IDLE_RECENT and IDLE_CLEARED both render as "Done" because the digest
    # is finalized exactly once when the assistant's final text lands; nothing
    # re-renders the digest on the IDLE_RECENT → IDLE_CLEARED decay 4s later.
    # The grace window matters for the typing-action lifecycle and the visible
    # Busy card removal (status_polling continues to read state() each tick),
    # not for this header. A turn ending in stop_reason=end_turn means Claude
    # is done with this exchange — surface that immediately.
    RunState.IDLE_RECENT: "✅ Done",
    RunState.WAITING_ON_USER: "🔔 Waiting on you",
    RunState.IDLE_CLEARED: "✅ Done",
}


def _context_pct_suffix(pct: int | None) -> str:
    """Render the threshold-gated context-window suffix.

    Below ``CC_TELEGRAM_CONTEXT_PCT_THRESHOLD`` (or unknown): empty string.
    At/above threshold: ``" · ctx NN%"``. At ≥95: ``" · ⚠️ ctx NN%"``.
    """
    if pct is None:
        return ""
    if pct < config.context_pct_threshold:
        return ""
    if pct >= 95:
        return f" · ⚠️ ctx {pct}%"
    return f" · ctx {pct}%"


def _format_duration(seconds: float) -> str:
    """Compact duration for the collapsed digest summary: "41s" / "3m 41s"."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    return f"{total // 60}m {total % 60:02d}s"


def _render_collapsed_activity_summary(
    state: ActivityDigestState,
    status: str,
    display: str,
    suffix: str,
    bg_jobs: int | None = None,
) -> str:
    """One-line W1 summary: header + frozen counts + frozen duration.

    ``bg_jobs`` (GH #43) appends a live ``⏳ N background job(s)`` decoration
    — the one non-frozen part: it appears while the pane reports running
    background shells on an idle route and disappears when they finish (the
    poller repaints on count change; staleness hides it otherwise).
    """
    parts = [f"{status} — {display}{suffix}"]
    if state.tool_count:
        parts.append(f"{state.tool_count} tool{'s' if state.tool_count != 1 else ''}")
    if state.subagent_count:
        parts.append(
            f"{state.subagent_count} sub-agent"
            f"{'s' if state.subagent_count != 1 else ''}"
        )
    if state.started_at and state.finalized_at:
        parts.append(_format_duration(state.finalized_at - state.started_at))
    if bg_jobs:
        parts.append(f"⏳ {bg_jobs} background job{'s' if bg_jobs != 1 else ''}")
    return " · ".join(parts)


def _render_activity_digest(
    state: ActivityDigestState,
    *,
    waiting: bool = False,
    route: Route | None = None,
    live_lines: int = ACTIVITY_DIGEST_MAX_LINES,
    collapse_done: bool = False,
) -> str:
    """Render the editable activity digest card.

    With a ``route``, the header is driven by ``RunState`` (read from
    ``route_runtime.snapshot``) rather than ``state.done`` + ``waiting``.
    Without a route (e.g. the rare digest with no resolvable route), it
    falls back to the ``waiting`` / ``state.done`` flags.

    ``live_lines`` is the recipient's body budget (plan v4 §5): 0 renders
    header + counts ONLY — no body lines and no hidden-events line (the
    ``compact`` preset's header-only card).

    ``collapse_done`` (W1, plan v4 §2): when True and the turn has
    finalized, the card collapses to the one-line summary. The header part
    STILL comes from the run-state snapshot, so a post-turn 🔔
    (notification / unanswered turn) survives the collapse — only the body
    is dropped. Counts and duration are frozen state, so the collapsed text
    is stable across later refresh repaints.
    """
    display = _display_name(state.window_id)
    if route is not None:
        # route_runtime is the authority. Read the snapshot once so
        # run_state + context_usage come from the same committed transition.
        snap = route_runtime.snapshot(route)
        run = snap.run_state
        pct: int | None = None
        if snap.context_usage and snap.context_usage.max_tokens > 0:
            raw = int(
                round(snap.context_usage.tokens * 100 / snap.context_usage.max_tokens)
            )
            pct = max(0, min(100, raw))
        status = _RUN_STATE_HEADER.get(run, "🟡 Busy")
        suffix = _context_pct_suffix(pct)
    else:
        if waiting:
            status = "🔔 Waiting on you"
        elif state.done:
            status = "✅ Done"
        else:
            status = "🟡 Busy"
        suffix = ""
    if collapse_done and state.done:
        # GH #43: idle routes decorate the collapsed card with the pane's
        # background-shell count (pull-read from the pane_signals leaf;
        # active routes never render it — Busy/typing already say "work in
        # flight").
        bg_jobs: int | None = None
        if route is not None and route_runtime.snapshot(route).run_state in (
            RunState.IDLE_RECENT,
            RunState.IDLE_CLEARED,
        ):
            bg_jobs = pane_signals.peek_background_jobs(route, now=time.time())
        return _render_collapsed_activity_summary(
            state, status, display, suffix, bg_jobs=bg_jobs
        )
    lines = [f"{status} — {display}{suffix}"]
    if state.tool_count or state.completed_count:
        lines.append(
            f"Activity: {state.completed_count}/{state.tool_count} tool calls complete"
        )
    else:
        lines.append("Activity: thinking")

    shown = state.lines[-live_lines:] if live_lines > 0 else []
    if shown:
        hidden = max(0, len(state.lines) - len(shown))
        if hidden:
            lines.append(f"• … {hidden} earlier event(s)")
        lines.extend(f"• {line}" for line in shown)
    return "\n".join(lines)


async def _upsert_activity_digest(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    state: ActivityDigestState,
) -> None:
    """Send or edit the per-topic activity digest.

    Precondition: the caller has already bound ``_activity_msg_info[(user_id,
    thread_id_or_0)]`` to ``state``. ``_process_activity_task`` and
    ``_bump_agent_activity_counter`` both do this before scheduling the
    flush. We mutate ``state`` in place and rely on that pre-bind; we do
    NOT re-assign the dict slot after a successful send/edit. Re-binding
    would clobber a fresh state that a concurrent ``_process_activity_task``
    may have written during the in-flight ``topic_send`` (window rebind),
    leaving the next flush editing a message in the now-stale topic.
    """
    # Cancellation-safe collapse protocol (codex r2 P1-2): a tombstoned or
    # superseded slot must neither send nor edit — the delete path set the
    # flag under the same per-key lock the caller holds, so a straggler
    # flush that raced the finalize lands here and no-ops.
    if (
        state.tombstoned
        or _activity_msg_info.get((user_id, thread_id or 0)) is not state
    ):
        return
    chat_id, effective_thread_id = _delivery_target(user_id, thread_id)
    route = _route_for(user_id, thread_id, state.window_id)
    prefs = output_prefs.resolve(user_id)
    text = _render_activity_digest(
        state,
        waiting=attention.is_waiting(user_id, thread_id),
        route=route,
        live_lines=prefs.digest_live_lines,
        collapse_done=prefs.digest_on_done == output_prefs.DIGEST_ON_DONE_SUMMARY,
    )
    if text == state.last_text:
        return

    if state.message_id:
        outcome = await topic_edit(
            bot,
            op="activity",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=effective_thread_id,
            window_id=state.window_id,
            message_id=state.message_id,
            text=text,
        )
        if outcome is TopicSendOutcome.OK:
            state.last_text = text
            return
        # Edit failed (message gone, topic gone, etc). Drop the id and retry as
        # a fresh send below; topic-shaped failures cascade into emergency DM.
        state.message_id = 0

    sent, outcome = await topic_send(
        bot,
        op="activity",
        user_id=user_id,
        chat_id=chat_id,
        thread_id=effective_thread_id,
        window_id=state.window_id,
        text=text,
        disable_notification=True,
        role="activity",
        content_type="activity",
        session_id=_session_id_for_window(state.window_id),
    )
    if sent is not None:
        state.message_id = sent.message_id
        state.last_text = text
        return
    if thread_id is not None and outcome in _TOPIC_BROKEN_OUTCOMES:
        await _emergency_dm(
            bot,
            user_id,
            thread_id,
            state.window_id,
            text,
            kind="activity",
            outcome=outcome,
        )


def _get_activity_lock(user_id: int, tid: int) -> asyncio.Lock:
    """Return (creating if needed) the per-(user, thread) upsert lock."""
    key = (user_id, tid)
    lock = _activity_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _activity_locks[key] = lock
    return lock


def _schedule_activity_flush(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
) -> None:
    """Schedule a debounced flush of the activity digest for this (user, thread).

    Cancels any existing pending flush so a burst of activity events within
    ``ACTIVITY_FLUSH_DEBOUNCE_SECONDS`` collapses to a single edit. The state
    itself is mutated synchronously by the caller; only the API call is
    debounced. If the topic gets ``_finalize_activity_digest`` (or
    ``_flush_activity_digest_now``) before the timer fires, the pending flush
    is cancelled and the digest is sent immediately.
    """
    tid = thread_id or 0
    key = (user_id, tid)
    pending = _activity_flush_tasks.get(key)
    if pending is not None and not pending.done():
        pending.cancel()

    async def _locked_flush() -> None:
        # Re-read the slot INSIDE the shielded section: a teardown/collapse
        # that won the race already popped or tombstoned it.
        state = _activity_msg_info.get(key)
        if state is None:
            return
        async with _get_activity_lock(user_id, tid):
            await _upsert_activity_digest(bot, user_id, thread_id, state)

    async def _delayed() -> None:
        try:
            await asyncio.sleep(ACTIVITY_FLUSH_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        # Pop BEFORE the upsert so a state change during the in-flight edit
        # starts a fresh debounce window rather than coalescing into the one
        # currently being sent.
        _activity_flush_tasks.pop(key, None)
        try:
            # Shield wraps the LOCK HOLDER (codex r3 P2-2, the todo-path
            # precedent): a cancel() can only ever interrupt the sleep
            # above — never mid-`topic_send` with the lock released, which
            # could create a Telegram message that is never recorded into
            # ``state.message_id`` and orphan the delete protocol.
            await asyncio.shield(_locked_flush())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - background task safety
            logger.warning(
                "debounced activity flush failed user=%d thread=%s: %s",
                user_id,
                thread_id,
                e,
            )

    _activity_flush_tasks[key] = asyncio.create_task(_delayed())


async def _flush_activity_digest_now(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
) -> None:
    """Cancel any pending debounce and flush the digest synchronously.

    The lock is the real serialization primitive — cancellation just speeds
    things up by stopping a pending debounce that's still sleeping. If a
    debounced upsert is already past its sleep and inside ``topic_edit``,
    the lock makes us wait for it to finish before our synchronous upsert
    runs (which then sees the freshly-updated ``state.message_id`` /
    ``state.last_text`` and either edits or no-ops).
    """
    tid = thread_id or 0
    key = (user_id, tid)
    # Drop the synchronous flush if the route is being torn down — firing
    # an edit at a route mid-cleanup races with route teardown's state
    # invalidation.
    route_user, route_tid = key
    if any(r[0] == route_user and r[1] == route_tid for r in _route_tearing_down):
        return
    pending = _activity_flush_tasks.pop(key, None)
    if pending is not None and not pending.done():
        pending.cancel()
    state = _activity_msg_info.get(key)
    if state is None:
        return
    async with _get_activity_lock(user_id, tid):
        await _upsert_activity_digest(bot, user_id, thread_id, state)


async def _process_activity_task(bot: Bot, user_id: int, task: MessageTask) -> None:
    """Collapse noisy thinking/tool events into one editable activity message."""
    wid = task.window_id or ""
    tid = task.thread_id or 0
    prefs = output_prefs.resolve(user_id)

    # quiet (digest_card=False): no digest card AT ALL for this recipient —
    # no state slot is ever created, so the flush/refresh/repaint paths have
    # nothing to send (plan v4 §2). The two non-display side effects stay:
    # images are real output, and a tool_use still dismisses the attention
    # card (interaction semantics, not display).
    if not prefs.digest_card:
        if task.content_type == "tool_use":
            await attention.dismiss(bot, user_id=user_id, thread_id=task.thread_id)
        if task.image_data:
            chat_id, effective_thread_id = _delivery_target(user_id, task.thread_id)
            await _send_task_images(bot, chat_id, task, effective_thread_id)
        return

    # thinking_line=False (compact/quiet): the 💭 keep-alive line is noise
    # for this recipient — no state mutation, no flush.
    if task.content_type == "thinking" and not prefs.thinking_line:
        return

    state = _activity_msg_info.get((user_id, tid))
    if state is None or state.window_id != wid or state.done:
        state = ActivityDigestState(message_id=0, window_id=wid, started_at=time.time())

    line = _compact_activity_line(
        task,
        line_chars=prefs.digest_line_chars,
        snippet_chars=prefs.result_snippet_chars,
    )
    if task.content_type == "tool_use":
        state.tool_count += 1
        state.lines.append(line)
        if task.tool_use_id:
            _tool_activity_indices[(task.tool_use_id, user_id, tid)] = (
                len(state.lines) - 1
            )
    elif task.content_type == "tool_result":
        state.completed_count += 1
        if (
            task.tool_use_id
            and (task.tool_use_id, user_id, tid) in _tool_activity_indices
        ):
            idx = _tool_activity_indices.pop((task.tool_use_id, user_id, tid))
            if 0 <= idx < len(state.lines):
                state.lines[idx] = line
            else:
                state.lines.append(line)
        else:
            state.lines.append(line)
    else:
        # Thinking should show that the topic is alive without sending the full
        # reasoning blob to Telegram.
        if not state.lines or state.lines[-1] != line:
            state.lines.append(line)

    state.done = False
    _activity_msg_info[(user_id, tid)] = state
    # New tool work means Claude is no longer waiting on the user. Dismiss the
    # attention card so the topic can flip back to "in progress" cleanly.
    if task.content_type == "tool_use":
        await attention.dismiss(bot, user_id=user_id, thread_id=task.thread_id)
    # Debounce the API call: state is already updated, the eventual flush
    # renders whatever the latest state is. Critical paths (assistant text
    # arriving, attention state changes) flush immediately via
    # _flush_activity_digest_now / _finalize_activity_digest.
    _schedule_activity_flush(bot, user_id, task.thread_id)

    # Images are real output, not noise. Keep delivering them.
    if task.image_data:
        chat_id, effective_thread_id = _delivery_target(user_id, task.thread_id)
        await _send_task_images(bot, chat_id, task, effective_thread_id)


# ── Sub-agent (sidechain) digest ──────────────────────────────────────────


def _short_subagent_id(key: str) -> str:
    """Compact display id from a sidechain tracking key.

    Tracking keys look like ``sub:<parent_session>:agent-<id>``; the
    user-visible suffix is the trailing 6 chars of the agent id.
    """
    suffix = key.rsplit(":", 1)[-1]
    if suffix.startswith("agent-"):
        suffix = suffix[len("agent-") :]
    return suffix[-6:] if len(suffix) > 6 else suffix


def _compact_subagent_line(
    task: MessageTask,
    *,
    line_chars: int = SUBAGENT_DIGEST_MAX_LINE_LENGTH,
    snippet_chars: int = SUBAGENT_DIGEST_TEXT_SNIPPET_LENGTH,
) -> str:
    """Render one line for the sub-agent digest from a sub-agent task.

    Mirrors ``_compact_activity_line`` for tool_use / tool_result / thinking
    so the per-sub-agent digest reads like the parent activity card. Text
    blocks (assistant prose) are truncated to a snippet — full prose still
    lives in the parent transcript / file system; the digest is a peek.
    ``line_chars`` / ``snippet_chars`` come from the recipient's resolved
    ``OutputPrefs``; the module constants stay as the ``verbose`` defaults.
    """
    if task.content_type == "thinking":
        return "💭 Thinking"

    raw = " ".join(part.strip() for part in task.parts if part and part.strip())
    raw = strip_sentinels(raw).replace("\n", " ")
    raw = " ".join(raw.split())
    if not raw:
        raw = task.content_type

    if task.content_type == "text":
        snippet = raw
        if len(snippet) > snippet_chars:
            snippet = snippet[: snippet_chars - 1].rstrip() + "…"
        return f"📝 {snippet}"

    # tool_use / tool_result share the activity-card formatting: the parent's
    # parser produces "**Bash**(cmd)  ⎿  output" so split off the result
    # snippet and trim aggressively for the card.
    if "  ⎿  " in raw:
        left, rest = raw.split("  ⎿  ", 1)
        first_line = rest.split("\n", 1)[0].strip()
        if len(first_line) > snippet_chars:
            first_line = first_line[: snippet_chars - 1].rstrip() + "…"
        raw = f"{left} — {first_line}" if first_line else left

    if len(raw) > line_chars:
        raw = raw[: line_chars - 1].rstrip() + "…"

    if task.content_type == "tool_result":
        if "error" in raw.lower():
            return f"❌ {raw}"
        if "interrupted" in raw.lower():
            return f"⏹ {raw}"
        return f"✅ {raw}"
    return f"⚙️ {raw}"


def _render_subagent_digest(
    state: SubagentDigestState,
    *,
    live_lines: int = SUBAGENT_DIGEST_MAX_LINES,
) -> str:
    """Render the editable per-sub-agent digest card.

    ``live_lines`` mirrors the activity digest's recipient budget; 0 renders
    header + counts only (no body lines, no hidden-events line). A
    ``collapsed`` state (W2) renders the one-line summary regardless — the
    🤖✅ report message is the run's artifact; this card's play-by-play is
    only valuable live.
    """
    sid = _short_subagent_id(state.subagent_key)
    if state.collapsed:
        tools = f"{state.tool_count} tool{'s' if state.tool_count != 1 else ''}"
        return f"↳ Sub-agent · {sid} ✅ {tools}"
    header = f"↳ Sub-agent · {sid}"
    if state.tool_count or state.completed_count:
        progress = (
            f"Activity: {state.completed_count}/{state.tool_count} tool calls complete"
        )
    else:
        progress = "Activity: thinking"
    lines = [header, progress]

    shown = state.lines[-live_lines:] if live_lines > 0 else []
    if shown:
        hidden = max(0, len(state.lines) - len(shown))
        if hidden:
            lines.append(f"• … {hidden} earlier event(s)")
        lines.extend(f"• {line}" for line in shown)
    return "\n".join(lines)


def _get_subagent_lock(user_id: int, tid: int, subagent_key: str) -> asyncio.Lock:
    """Return (creating if needed) the per-(user, thread, subagent_key) upsert lock."""
    key = (user_id, tid, subagent_key)
    lock = _subagent_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _subagent_locks[key] = lock
    return lock


async def _upsert_subagent_digest(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    state: SubagentDigestState,
) -> None:
    """Send or edit the per-sub-agent digest card.

    Precondition: the caller has already bound ``_subagent_msg_info[(user_id,
    thread_id_or_0, subagent_key)]`` to ``state``
    (``_process_subagent_activity_task`` does this before scheduling the
    flush). We mutate ``state`` in place and rely on that pre-bind; we do NOT
    re-assign the dict slot after a successful send/edit. Re-binding would
    clobber a fresh state that a concurrent ``_process_subagent_activity_task``
    may have written during the in-flight Telegram call (window rebind under
    the same sub-agent key), leaving the next flush editing a message in the
    now-stale topic.
    """
    # Same tombstone + slot-identity guard as the activity upsert (codex r2
    # P1-2): a popped/tombstoned slot must neither send nor edit.
    skey = (user_id, thread_id or 0, state.subagent_key)
    if state.tombstoned or _subagent_msg_info.get(skey) is not state:
        return
    chat_id, effective_thread_id = _delivery_target(user_id, thread_id)
    text = _render_subagent_digest(
        state,
        live_lines=output_prefs.resolve(user_id).subagent_live_lines,
    )
    if text == state.last_text:
        return

    if state.message_id:
        outcome = await topic_edit(
            bot,
            op="subagent_activity",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=effective_thread_id,
            window_id=state.window_id,
            message_id=state.message_id,
            text=text,
        )
        if outcome is TopicSendOutcome.OK:
            state.last_text = text
            return
        # Edit failed (message gone, topic gone, etc). Drop the id and retry as
        # a fresh send below; topic-shaped failures cascade into emergency DM.
        state.message_id = 0

    sent, outcome = await topic_send(
        bot,
        op="subagent_activity",
        user_id=user_id,
        chat_id=chat_id,
        thread_id=effective_thread_id,
        window_id=state.window_id,
        text=text,
        disable_notification=True,
        role="activity",
        content_type="subagent_activity",
        session_id=_session_id_for_window(state.window_id),
    )
    if sent is not None:
        state.message_id = sent.message_id
        state.last_text = text
        return
    if thread_id is not None and outcome in _TOPIC_BROKEN_OUTCOMES:
        await _emergency_dm(
            bot,
            user_id,
            thread_id,
            state.window_id,
            text,
            kind="subagent_activity",
            outcome=outcome,
        )


def _schedule_subagent_flush(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    subagent_key: str,
) -> None:
    """Debounced flush of the sub-agent digest for this (user, thread, key)."""
    tid = thread_id or 0
    key = (user_id, tid, subagent_key)
    pending = _subagent_flush_tasks.get(key)
    if pending is not None and not pending.done():
        pending.cancel()

    async def _locked_flush() -> None:
        state = _subagent_msg_info.get(key)
        if state is None:
            return
        async with _get_subagent_lock(user_id, tid, subagent_key):
            await _upsert_subagent_digest(bot, user_id, thread_id, state)

    async def _delayed() -> None:
        try:
            await asyncio.sleep(ACTIVITY_FLUSH_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        _subagent_flush_tasks.pop(key, None)
        try:
            # Same shield-wraps-the-lock-holder shape as the activity path
            # (codex r3 P2-2 / hermes r2 P1-3): cancel only ever interrupts
            # the sleep, never a mid-send upsert.
            await asyncio.shield(_locked_flush())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - background task safety
            logger.warning(
                "debounced subagent flush failed user=%d thread=%s key=%s: %s",
                user_id,
                thread_id,
                subagent_key,
                e,
            )

    _subagent_flush_tasks[key] = asyncio.create_task(_delayed())


async def _process_subagent_activity_task(
    bot: Bot, user_id: int, task: MessageTask
) -> None:
    """Collapse one sub-agent run's blocks into a single editable digest."""
    if not task.subagent_key:
        return
    prefs = output_prefs.resolve(user_id)
    # subagent_cards == "off" (compact/quiet, or the legacy env-false
    # mapping): no play-by-play card for this recipient — no state slot is
    # ever created, so the debounced flush path has nothing to send. The
    # sidechain keep-alive is unaffected (it fires from session_monitor, not
    # from this display path — Wave A contract).
    if prefs.subagent_cards == output_prefs.SUBAGENT_CARDS_OFF:
        return
    wid = task.window_id or ""
    tid = task.thread_id or 0
    subagent_key = task.subagent_key
    state_key = (user_id, tid, subagent_key)
    state = _subagent_msg_info.get(state_key)
    # W2 tombstone: a block re-detected AFTER the card collapsed must not
    # re-inflate the play-by-play (plan v4 §3) — a genuinely new run has a
    # new subagent_key, so the kept slot only ever blocks stragglers. The
    # RE-FLUSH (not a pure return) makes the collapse retry-safe (codex
    # PR-2 P1-2): a RetryAfter raised out of the collapse upsert re-enters
    # this task via ``_run_with_retry`` and must re-attempt the collapsed
    # delivery; a delivered collapse dedups on ``last_text`` (no API call).
    if state is not None and state.window_id == wid and state.collapsed:
        await _flush_subagent_digest_now(bot, user_id, task.thread_id, subagent_key)
        return
    if state is None or state.window_id != wid:
        state = SubagentDigestState(
            message_id=0,
            window_id=wid,
            subagent_key=subagent_key,
        )

    line = _compact_subagent_line(
        task,
        line_chars=prefs.digest_line_chars,
        snippet_chars=prefs.result_snippet_chars,
    )
    if task.content_type == "tool_use":
        state.tool_count += 1
        state.lines.append(line)
        if task.tool_use_id:
            _subagent_tool_indices[(task.tool_use_id, user_id, tid, subagent_key)] = (
                len(state.lines) - 1
            )
    elif task.content_type == "tool_result":
        state.completed_count += 1
        idx_key = (
            (task.tool_use_id, user_id, tid, subagent_key) if task.tool_use_id else None
        )
        if idx_key is not None and idx_key in _subagent_tool_indices:
            idx = _subagent_tool_indices.pop(idx_key)
            if 0 <= idx < len(state.lines):
                state.lines[idx] = line
            else:
                state.lines.append(line)
        else:
            state.lines.append(line)
    else:
        # text / thinking — append, but coalesce identical thinking placeholders
        # so a sub-agent's repeated "(thinking)" markers don't pile up.
        if not state.lines or state.lines[-1] != line:
            state.lines.append(line)

    _subagent_msg_info[state_key] = state

    # W2 primary trigger (plan v4 §3): the sidechain's own end-of-turn — a
    # final visible text with an end-turn stop_reason. Collapse instead of
    # scheduling another play-by-play paint. Only the ``summary`` policy
    # collapses; ``keep`` (verbose) leaves the card as-is.
    if (
        task.content_type == "text"
        and task.stop_reason in _TURN_END_STOP_REASONS
        and prefs.subagent_cards == output_prefs.SUBAGENT_CARDS_SUMMARY
    ):
        await _collapse_subagent_digest(bot, user_id, task.thread_id, subagent_key)
        return

    _schedule_subagent_flush(bot, user_id, task.thread_id, subagent_key)


async def _collapse_subagent_digest(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    subagent_key: str,
) -> None:
    """Synchronously collapse one sub-agent card to its one-line summary.

    The synchronous collapse path hermes r1 P2-7 asked for: cancel the
    pending debounce FIRST (it can only be interrupted in its sleep — the
    flush phase is shielded), then render exactly once under the per-key
    lock. The collapsed render becomes ``last_text`` so any straggler
    dedups instead of repainting the play-by-play.
    """
    tid = thread_id or 0
    key = (user_id, tid, subagent_key)
    pending = _subagent_flush_tasks.pop(key, None)
    if pending is not None and not pending.done():
        pending.cancel()
    state = _subagent_msg_info.get(key)
    if state is None or state.collapsed:
        return
    state.collapsed = True
    async with _get_subagent_lock(user_id, tid, subagent_key):
        await _upsert_subagent_digest(bot, user_id, thread_id, state)


async def _flush_subagent_digest_now(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    subagent_key: str,
) -> None:
    """Cancel any pending debounce and flush one sub-agent card synchronously.

    Mirror of ``_flush_activity_digest_now`` at per-sidechain granularity —
    the lock is the real serializer; cancelling just skips a still-sleeping
    debounce.
    """
    tid = thread_id or 0
    key = (user_id, tid, subagent_key)
    pending = _subagent_flush_tasks.pop(key, None)
    if pending is not None and not pending.done():
        pending.cancel()
    state = _subagent_msg_info.get(key)
    if state is None:
        return
    async with _get_subagent_lock(user_id, tid, subagent_key):
        await _upsert_subagent_digest(bot, user_id, thread_id, state)


# ── To-do list digest ────────────────────────────────────────────────────


def _is_todo_tool_use(task: MessageTask) -> bool:
    """Parent (non-sidechain) TodoWrite tool_use that gates the digest path.

    Sub-agent TodoWrites stay on the sub-agent digest path — they describe
    the sub-agent's plan, not the parent's task list, and surfacing them
    on the parent's todo card would conflate two different agendas.
    """
    return (
        task.task_type == "content"
        and task.content_type == "tool_use"
        and task.tool_name == "TodoWrite"
        and task.subagent_key is None
    )


def _is_todo_tool_result(task: MessageTask, user_id: int) -> bool:
    """tool_result for an id we've already routed through the todo digest.

    transcript_parser now propagates ``tool_name`` to tool_result entries
    when ``pending_tools`` has recovered it, so a name-based check on
    tool_result is theoretically possible. The per-route id set stays
    authoritative here because (a) it pins the routing decision made at
    tool_use time, not what the parser happened to recover, and (b) the
    pre-existing identification path is the contract this digest relies
    on for replay/restart correctness.
    """
    if (
        task.task_type != "content"
        or task.content_type != "tool_result"
        or not task.tool_use_id
        or task.subagent_key is not None
    ):
        return False
    tid = task.thread_id or 0
    return (task.tool_use_id, user_id, tid) in _todo_tool_ids


def _render_todo_digest(todos: list[dict[str, object]]) -> str:
    """Render the editable to-do-list card from a TodoWrite snapshot.

    Status emoji mirror Claude Code's pane: ✅ completed, 🔄 in_progress,
    ⬜ pending. ``activeForm`` is preferred for the in_progress label
    because it reads as "what's happening right now"; ``content`` is the
    canonical task description and is preferred for everything else.

    Long todo lists are truncated to ``TODO_DIGEST_MAX_VISIBLE`` rows to
    fit Telegram's compact-card visual budget; remaining items get a
    "… +N more" tail line so the user knows the list isn't fully shown.
    """
    total = len(todos)
    completed = sum(
        1 for t in todos if isinstance(t, dict) and t.get("status") == "completed"
    )
    in_progress = sum(
        1 for t in todos if isinstance(t, dict) and t.get("status") == "in_progress"
    )
    header = f"📋 Tasks ({completed}/{total} done"
    if in_progress:
        header += f" · {in_progress} active"
    header += ")"

    visible = todos[:TODO_DIGEST_MAX_VISIBLE]
    hidden = max(0, total - len(visible))

    lines = [header]
    for item in visible:
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        if status == "completed":
            emoji = "✅"
            label = item.get("content", "")
        elif status == "in_progress":
            emoji = "🔄"
            # activeForm is the present-continuous label Claude writes for
            # the current task; falls back to content when missing.
            label = item.get("activeForm") or item.get("content", "")
        else:
            emoji = "⬜"
            label = item.get("content", "")
        if not isinstance(label, str):
            label = str(label)
        label = label.strip().replace("\n", " ")
        if len(label) > TODO_DIGEST_CONTENT_SNIPPET:
            label = label[: TODO_DIGEST_CONTENT_SNIPPET - 1].rstrip() + "…"
        lines.append(f"{emoji} {label}")
    if hidden:
        lines.append(f"… +{hidden} more")
    return "\n".join(lines)


def _get_todo_lock(user_id: int, tid: int) -> asyncio.Lock:
    """Return (creating if needed) the per-route todo upsert lock."""
    key = (user_id, tid)
    lock = _todo_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _todo_locks[key] = lock
    return lock


async def _upsert_todo_digest(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    state: TodoListDigestState,
    todos: list[dict[str, object]],
) -> None:
    """Send or edit the to-do-list digest card.

    Precondition: the caller has already bound ``_todo_msg_info[(user_id,
    thread_id_or_0)]`` to ``state`` (``_process_todo_task`` does this before
    scheduling the flush). We mutate ``state`` in place and rely on that
    pre-bind; we do NOT re-assign the dict slot after a successful
    send/edit. Re-binding would clobber a fresh state that a concurrent
    ``_process_todo_task`` may have written during the in-flight
    ``topic_send`` (window rebind under the same topic), leaving the next
    flush editing a message in the now-stale topic.
    """
    chat_id, effective_thread_id = _delivery_target(user_id, thread_id)
    text = _render_todo_digest(todos)
    if text == state.last_text:
        return

    if state.message_id:
        outcome = await topic_edit(
            bot,
            op="todo_digest",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=effective_thread_id,
            window_id=state.window_id,
            message_id=state.message_id,
            text=text,
        )
        if outcome is TopicSendOutcome.OK:
            # Mutate state in place — do NOT re-bind ``_todo_msg_info[key]``
            # to ``state``. ``_process_todo_task`` already put this state in
            # the dict before scheduling us, and may have *replaced* the slot
            # with a fresh state during a window_id rebind that ran while
            # ``topic_edit`` was in flight. Re-binding to our captured
            # reference would clobber the fresh state and leave the next
            # flush editing a message in the now-stale window.
            state.last_text = text
            return
        # Edit failed (message gone, etc). Drop the id and retry as a fresh send.
        state.message_id = 0

    sent, outcome = await topic_send(
        bot,
        op="todo_digest",
        user_id=user_id,
        chat_id=chat_id,
        thread_id=effective_thread_id,
        window_id=state.window_id,
        text=text,
        disable_notification=True,
        role="activity",
        content_type="todo_digest",
        session_id=_session_id_for_window(state.window_id),
    )
    if sent is not None:
        # Same in-place-mutation rule as the edit branch above. The dict
        # reference was set by ``_process_todo_task``; if it has since been
        # replaced (window rebind), our captured ``state`` is an orphan and
        # the next flush should pick up the fresh slot, not our message_id.
        state.message_id = sent.message_id
        state.last_text = text
        return
    if thread_id is not None and outcome in _TOPIC_BROKEN_OUTCOMES:
        await _emergency_dm(
            bot,
            user_id,
            thread_id,
            state.window_id,
            text,
            kind="todo_digest",
            outcome=outcome,
        )


async def _run_locked_todo_upsert(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
) -> None:
    """Acquire the per-route lock and run one todo-digest upsert.

    Lifted out of ``_schedule_todo_flush._delayed`` so the flush task can
    wrap *this* awaitable in ``asyncio.shield`` — protecting the
    network-call window and the immediately-following state assignment
    (``state.message_id = sent.message_id``) from outer cancellation.

    Without the shield, a TodoWrite arriving while ``await topic_send``
    is in flight would cancel the in-flight task; if Telegram had
    already created the message but our local state was never updated,
    the next flush would issue a *second* card instead of editing the
    first. With shield, the inner upsert runs to completion even if the
    outer ``_delayed`` task was cancelled — and any subsequent flush
    correctly waits on this lock before reading state.

    Reading snapshot + state INSIDE the lock keeps the
    "render the latest known snapshot" boundary at the lock; an
    interleaved ``_process_todo_task`` cannot mutate state between our
    read and the upsert.
    """
    tid = thread_id or 0
    key = (user_id, tid)
    async with _get_todo_lock(user_id, tid):
        snapshot = _todo_pending_snapshot.get(key)
        if snapshot is None:
            return
        state = _todo_msg_info.get(key)
        if state is None:
            # _process_todo_task creates the state record before scheduling
            # the flush, so missing state here means teardown raced us.
            return
        await _upsert_todo_digest(bot, user_id, thread_id, state, snapshot)


def _schedule_todo_flush(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
) -> None:
    """Debounced flush of the to-do digest for this (user, thread).

    Reuses ``ACTIVITY_FLUSH_DEBOUNCE_SECONDS`` to share the same flood
    budget logic. The pending snapshot is read at flush time, not capture
    time, so two TodoWrites within the debounce window correctly collapse
    to "edit once with the latest snapshot".

    Cancellation safety: the upsert runs under ``asyncio.shield`` so
    cancelling this debounced task while a network call is in flight
    cannot orphan a Telegram message. See ``_run_locked_todo_upsert``.
    """
    tid = thread_id or 0
    key = (user_id, tid)
    pending = _todo_flush_tasks.get(key)
    if pending is not None and not pending.done():
        pending.cancel()

    async def _delayed() -> None:
        try:
            await asyncio.sleep(ACTIVITY_FLUSH_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        _todo_flush_tasks.pop(key, None)
        try:
            await asyncio.shield(_run_locked_todo_upsert(bot, user_id, thread_id))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - background task safety
            logger.warning(
                "debounced todo flush failed user=%d thread=%s: %s",
                user_id,
                thread_id,
                e,
            )

    _todo_flush_tasks[key] = asyncio.create_task(_delayed())


async def _process_todo_task(bot: Bot, user_id: int, task: MessageTask) -> None:
    """Route a parent TodoWrite tool_use to the per-route todo digest."""
    # quiet: the 📋 task-list card is display-only — skip it entirely for
    # this recipient (no state slot, nothing for the flush to send).
    if not output_prefs.resolve(user_id).todo_card:
        return
    if not task.tool_input:
        return
    todos_raw = task.tool_input.get("todos")
    if not isinstance(todos_raw, list):
        return
    todos: list[dict[str, object]] = [t for t in todos_raw if isinstance(t, dict)]
    # Empty todos render as "📋 Tasks (0/0 done)" — visual noise on session
    # start where Claude sometimes opens with TodoWrite([]) to clear the
    # prior list. Skip the card entirely; the next non-empty TodoWrite will
    # send a fresh one.
    if not todos:
        return

    wid = task.window_id or ""
    tid = task.thread_id or 0
    key = (user_id, tid)
    state = _todo_msg_info.get(key)
    if state is None or state.window_id != wid:
        # New route or window changed under us (re-bind): start a fresh card.
        # Stale ``message_id`` from a different window would point into the
        # wrong topic; safer to send anew than try to edit it.
        state = TodoListDigestState(message_id=0, window_id=wid)
        _todo_msg_info[key] = state

    _todo_pending_snapshot[key] = todos
    if task.tool_use_id:
        _todo_tool_ids_record((task.tool_use_id, user_id, tid))
    _schedule_todo_flush(bot, user_id, task.thread_id)


# ── §2.7 Agent (subagent) prominence ──────────────────────────────────────


def _render_agent_tool_use(
    input_data: dict[str, object] | None,
    tool_name: str,
    preview_chars: int | None = None,
) -> str:
    """Render the top-level "Subagent dispatched" message body.

    Returns a single rendered string. The caller wraps it in a list when the
    surrounding ``MessageTask`` expects ``parts``; the send layer's
    ``split_message`` already handles Telegram's 4096-char limit.
    ``preview_chars`` comes from the recipient's resolved ``OutputPrefs``;
    None falls back to the global env default.
    """
    inp = input_data if isinstance(input_data, dict) else {}
    subagent_type = str(inp.get("subagent_type") or "general-purpose")
    description = str(inp.get("description") or "")
    prompt_text = str(inp.get("prompt") or "")

    cap = (
        preview_chars
        if preview_chars is not None
        else config.agent_prompt_preview_chars
    )
    excerpt = prompt_text.strip()
    if len(excerpt) > cap:
        excerpt = excerpt[: cap - 1].rstrip() + "…"

    header = f"🤖 Subagent dispatched — {subagent_type}"
    body_lines = [header]
    if description:
        body_lines.append(f"Description: {description}")
    if excerpt:
        body_lines.append("")
        body_lines.append(f"▶ {excerpt}")
    elif tool_name == "Task" and not description:
        body_lines.append(f"(legacy {tool_name} invocation)")
    return "\n".join(body_lines)


def _agent_result_status(text: str) -> str:
    """Mirror ``_compact_activity_line`` heuristics for Agent tool_result."""
    lowered = (text or "").lower()
    if "interrupted" in lowered:
        return "interrupted"
    if "error" in lowered:
        return "error"
    return "done"


def _render_agent_tool_result(
    text: str,
    input_data: dict[str, object] | None,
    status: str,
) -> str:
    """Render the edited "Subagent done / error / interrupted" body.

    Edits the original tool_use message in place so the user sees the
    dispatch and the result on the same Telegram message — the existing
    ``_tool_msg_ids`` machinery owns the edit; this helper just shapes the
    body. Long results are kept whole; the caller's split layer respects
    Telegram's 4096-char limit.
    """
    inp = input_data if isinstance(input_data, dict) else {}
    subagent_type = str(inp.get("subagent_type") or "general-purpose")
    description = str(inp.get("description") or "")
    glyph_map = {"done": "🤖✅", "error": "🤖❌", "interrupted": "🤖⏹"}
    label_map = {
        "done": "Subagent done",
        "error": "Subagent error",
        "interrupted": "Subagent interrupted",
    }
    glyph = glyph_map.get(status, "🤖✅")
    label = label_map.get(status, "Subagent done")
    body_lines = [f"{glyph} {label} — {subagent_type}"]
    if description:
        body_lines.append(f"Description: {description}")
    body = (text or "").strip()
    if body:
        body_lines.append("")
        body_lines.append(body)
    return "\n".join(body_lines)


async def _bump_agent_activity_counter(
    bot: Bot,
    user_id: int,
    task: MessageTask,
) -> None:
    """Increment the activity digest's tool counter without appending a line.

    The digest header reads ``Activity: N/M tool calls complete`` — for the
    user that line must include Agent runs alongside short Read/Write/Bash,
    or they'll see "0/1 complete" while a subagent is clearly running. The
    body of the digest, however, would be misleading if Agent showed up as
    a generic "⚙️ Agent(...)" line — Stage 4.c routes the body to the
    top-level message instead.

    Prefs-aware (hermes r3 P1-1): under a no-digest policy (quiet) this MUST
    NOT create ``ActivityDigestState`` or schedule a flush — a pure Agent
    turn would otherwise paint a digest card off the counter path alone.
    """
    if not output_prefs.resolve(user_id).digest_card:
        return
    wid = task.window_id or ""
    tid = task.thread_id or 0
    state = _activity_msg_info.get((user_id, tid))
    if state is None or state.window_id != wid or state.done:
        state = ActivityDigestState(message_id=0, window_id=wid, started_at=time.time())
    if task.content_type == "tool_use":
        state.tool_count += 1
        # W1 summary's "N sub-agents" part — Agent/Task runs counted here
        # (this is the only digest seam Agent tool_use passes through).
        state.subagent_count += 1
    elif task.content_type == "tool_result":
        state.completed_count += 1
    state.done = False
    # Persist the slot up-front so the matching tool_result lands in the
    # same digest state (otherwise the upsert path would only store after
    # a successful send, and a stubbed upsert in tests would lose the
    # tool_count carry-over).
    _activity_msg_info[(user_id, tid)] = state
    # Same debounce as ``_process_activity_task``: subagent dispatches
    # generate paired tool_use/tool_result events and were the largest
    # source of inline edits leaking past the debounce.
    _schedule_activity_flush(bot, user_id, task.thread_id)


async def _process_agent_task(bot: Bot, user_id: int, task: MessageTask) -> None:
    """Render the Agent tool_use / tool_result as a top-level message.

    Top-level path uses ``_process_content_task`` so the existing
    ``_tool_msg_ids`` edit machinery + multipart split + status handoff all
    Just Work. After the top-level send, the activity counter is bumped so
    the digest header reflects "M tool calls" including the subagent.

    Hermes P2-1: the promoted task is cached on ``task.agent_promoted`` so
    a ``_run_with_retry`` retry (which re-invokes with the SAME original
    Agent task) reuses the SAME promoted task — its ``parts_sent`` cursor
    and the eager ``_tool_msg_ids`` saturation survive, so a RetryAfter
    raised AFTER successful delivery (e.g. in ``_check_and_send_status``)
    cannot replay the tool_use bubble or send a duplicate tool_result
    bubble. The render + ``_agent_tool_ids`` stash are first-attempt-only
    (the recorded input is already stashed; the rendered text already lives
    in the cached promoted task's ``parts``).
    """
    tid = task.thread_id or 0
    promoted = task.agent_promoted
    if promoted is not None:
        await _process_content_task(bot, user_id, promoted)
        if task.content_type != "tool_use" and task.tool_use_id:
            _agent_tool_ids.pop((task.tool_use_id, user_id, tid), None)
        await _bump_agent_activity_counter(bot, user_id, task)
        return
    prefs = output_prefs.resolve(user_id)
    rendered: str
    if task.content_type == "tool_use":
        if task.tool_use_id:
            # Stash the input dict so the matching tool_result can render
            # the same description / subagent_type (tool_result blocks
            # don't carry the original input). Stashed even when the
            # dispatch bubble is suppressed below — the 🤖✅ result render
            # and ``_is_agent_tool_result`` routing both depend on it
            # (codex r2 P1-1).
            _agent_tool_ids[(task.tool_use_id, user_id, tid)] = (
                task.tool_input if isinstance(task.tool_input, dict) else {}
            )
        if not prefs.agent_dispatch_msg:
            # quiet: no "🤖 Subagent dispatched" bubble. The later
            # tool_result renders the 🤖✅ report as a fresh message (or via
            # the status→content conversion — both shapes are the contract,
            # hermes r3 P2-2). Counter bump keeps header math right for
            # recipients that do show a digest (no-op under quiet's
            # digest_card=False).
            await _bump_agent_activity_counter(bot, user_id, task)
            return
        rendered = _render_agent_tool_use(
            task.tool_input,
            task.tool_name or "Agent",
            preview_chars=prefs.agent_prompt_preview_chars,
        )
    else:
        status = _agent_result_status(task.text or "\n\n".join(task.parts))
        # Look up the original tool_use input first; fall back to whatever
        # the tool_result task carries (defense-in-depth) and finally to
        # an empty dict so render shows generic "general-purpose".
        recorded_input: dict[str, object] | None = None
        if task.tool_use_id:
            recorded_input = _agent_tool_ids.get((task.tool_use_id, user_id, tid))
        effective_input = recorded_input
        if effective_input is None and isinstance(task.tool_input, dict):
            effective_input = task.tool_input
        rendered = _render_agent_tool_result(
            task.text or "\n\n".join(task.parts),
            effective_input,
            status,
        )
        # NOTE: the ``_agent_tool_ids`` pop is deferred until the promoted
        # content task below succeeds (finding 10 / Hermes P2-3). Popping
        # here meant a RetryAfter raised by ``_process_content_task`` lost
        # the routing key — the retry's ``_is_agent_tool_result`` then
        # missed and the Agent tool_result re-routed to the generic
        # activity digest with the wrong rendering.

    promoted = MessageTask(
        task_type="content",
        text=task.text,
        window_id=task.window_id,
        parts=[rendered],
        tool_use_id=task.tool_use_id,
        content_type=task.content_type,
        thread_id=task.thread_id,
        image_data=task.image_data,
        tool_name=task.tool_name,
        tool_input=task.tool_input,
    )
    # Cache BEFORE processing — a RetryAfter raises out of the await below,
    # and the retry must find the promoted task with its mutated state.
    task.agent_promoted = promoted
    await _process_content_task(bot, user_id, promoted)
    if task.content_type != "tool_use" and task.tool_use_id:
        # Promotion delivered — NOW consume the tool_use's recorded input.
        _agent_tool_ids.pop((task.tool_use_id, user_id, tid), None)
    await _bump_agent_activity_counter(bot, user_id, task)


async def _delete_activity_digest(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
) -> None:
    """W1 ``delete`` policy — remove the finished card, cancellation-safe.

    Protocol (codex r2 P1-2): cancel the pending debounce FIRST (its flush
    phase is shielded, so a cancel only ever lands in the sleep), then take
    the per-key lock — any shielded in-flight flush has completed and
    recorded its ``message_id`` by the time we hold it — tombstone the
    state, delete the message best-effort, and pop the slot. With the slot
    gone, the refresh/repaint paths are no-ops; the upsert's tombstone +
    slot-identity guard stops any straggler still holding the state object.
    A delete failure (RetryAfter / transient) never wedges content delivery
    — the slot is popped regardless and the orphan ages out as a normal
    chat message.
    """
    tid = thread_id or 0
    key = (user_id, tid)
    pending = _activity_flush_tasks.pop(key, None)
    if pending is not None and not pending.done():
        pending.cancel()
    async with _get_activity_lock(user_id, tid):
        state = _activity_msg_info.get(key)
        if state is None:
            return
        state.tombstoned = True
        msg_id = state.message_id
        _activity_msg_info.pop(key, None)
        if msg_id:
            chat_id, effective_thread_id = _delivery_target(user_id, thread_id)
            try:
                await topic_delete(
                    bot,
                    op="activity_done_delete",
                    user_id=user_id,
                    chat_id=chat_id,
                    thread_id=effective_thread_id,
                    window_id=state.window_id,
                    message_id=msg_id,
                )
            except Exception as e:
                logger.warning(
                    "activity digest delete failed user=%d thread=%s msg=%d: %s",
                    user_id,
                    thread_id,
                    msg_id,
                    e,
                )


async def _finalize_activity_digest(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    window_id: str,
) -> None:
    """Mark activity digest done before final assistant text.

    Stage 4 / Option A: assistant text never raises an attention card, so the
    digest is finalized to its terminal value driven by ``RunState`` (Done /
    Waiting on you). The previous attention-heuristic short-circuit is gone —
    it left the digest stuck on Busy whenever a card never raised.

    W1 (plan v4 §2): the recipient's ``digest_on_done`` policy decides the
    terminal shape — ``keep`` (today's full card), ``summary`` (the one-line
    collapse, rendered by the same upsert path), or ``delete`` (the
    cancellation-safe removal). The finalize is also the W2 BACKSTOP: any
    sub-agent card of this (user, thread) not yet collapsed (an empty-final
    sidechain whose end-of-turn never reached the display path) collapses
    here under the ``summary`` sub-agent policy.
    """
    tid = thread_id or 0
    prefs = output_prefs.resolve(user_id)

    # W2 backstop sweep — before the parent card finalizes, so the topic
    # settles in one pass. Only the summary policy collapses; ``keep``
    # leaves play-by-play cards as today, ``off`` never created any.
    # Already-collapsed cards are RE-FLUSHED, not skipped (codex PR-2 P1-2):
    # a RetryAfter may have aborted the collapse delivery after the
    # ``collapsed`` mark; the flush dedups on ``last_text`` so a delivered
    # collapse is a no-op.
    if prefs.subagent_cards == output_prefs.SUBAGENT_CARDS_SUMMARY:
        for (s_uid, s_tid, s_key), s_state in list(_subagent_msg_info.items()):
            if s_uid != user_id or s_tid != tid:
                continue
            if s_state.collapsed:
                await _flush_subagent_digest_now(bot, user_id, thread_id, s_key)
            else:
                await _collapse_subagent_digest(bot, user_id, thread_id, s_key)

    state = _activity_msg_info.get((user_id, tid))
    if not state or state.window_id != window_id:
        return
    # Retry-safe (codex PR-2 P1-1): the stamps are first-call-only, but the
    # flush below runs on EVERY call — a RetryAfter raised out of the flush
    # makes ``_run_with_retry`` re-enter this finalize, and an early-return
    # on ``state.done`` would permanently skip the terminal collapse while
    # the assistant text still delivered. Repeat flushes are free: the
    # upsert dedups on ``last_text``.
    if not state.done:
        state.done = True
        state.finalized_at = time.time()
    if prefs.digest_on_done == output_prefs.DIGEST_ON_DONE_DELETE:
        await _delete_activity_digest(bot, user_id, thread_id)
        return
    # ``keep`` and ``summary`` share the synchronous flush — the render
    # picks the shape from the policy. Assistant text is about to land and
    # the user must see the terminal state before the reply, not 5s after.
    await _flush_activity_digest_now(bot, user_id, thread_id)


async def refresh_activity_digest_if_present(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    window_id: str,
) -> None:
    """Re-render an existing activity digest after attention/run-state changes.

    Public seam: ``status_polling`` calls this on a run-state transition
    (e.g. the pane-confirmed WAITING_ON_USER promotion / its retract) to
    repaint the digest header immediately. No-ops when no digest for
    ``(user_id, thread_id)`` is on screen, or when its window_id differs."""
    tid = thread_id or 0
    state = _activity_msg_info.get((user_id, tid))
    if not state or state.window_id != window_id:
        return
    # Attention state flips (raised / dismissed) must reflect immediately —
    # the user is being asked to look at this topic. Don't sit on it for the
    # debounce window.
    await _flush_activity_digest_now(bot, user_id, thread_id)


def _looks_like_attention_request(text: str) -> bool:
    """Backwards-compatible alias for ``attention.is_attention_request``."""
    return attention.is_attention_request(text)


async def _send_task_images(
    bot: Bot, chat_id: int, task: MessageTask, effective_thread_id: int | None = None
) -> None:
    """Send images attached to a task, if any."""
    if not task.image_data:
        return
    logger.info(
        "Sending %d image(s) in thread %s",
        len(task.image_data),
        task.thread_id,
    )
    await send_photo(
        bot,
        chat_id,
        task.image_data,
        **_send_kwargs(effective_thread_id),  # type: ignore[arg-type]
    )


async def _process_content_task(bot: Bot, user_id: int, task: MessageTask) -> None:
    """Process a content message task."""
    wid = task.window_id or ""
    tid = task.thread_id or 0
    chat_id, effective_thread_id = _delivery_target(user_id, task.thread_id)

    logical_text = "\n\n".join(task.parts)
    # Diagnostic: track text content delivery — silent text-loss bug seen
    # 2026-05-02 where topic_send op=content never fired for enqueued text
    # tasks while activity-digest edits continued. Remove once the next
    # firing has been root-caused.
    skey = (user_id, tid)
    logger.info(
        "content_task entry: user=%d tid=%d wid=%s ctype=%s parts=%d "
        "logical_len=%d status_present=%s digest_present=%s",
        user_id,
        tid,
        wid,
        task.content_type,
        len(task.parts),
        len(logical_text),
        skey in _status_msg_info,
        skey in _activity_msg_info,
    )
    if task.content_type == "text":
        await _finalize_activity_digest(
            bot,
            user_id,
            task.thread_id,
            wid,
        )

    # 1. Handle tool_result editing (merged parts are edited together)
    if task.content_type == "tool_result" and task.tool_use_id:
        _tkey = (task.tool_use_id, user_id, tid)
        edit_msg_id = _tool_msg_ids.get(_tkey)
        if edit_msg_id is not None:
            # Clear status message first
            await _do_clear_status_message(bot, user_id, tid)
            # Join all parts for editing (merged content goes together)
            full_text = "\n\n".join(task.parts)
            outcome = await topic_edit(
                bot,
                op="tool_result",
                user_id=user_id,
                chat_id=chat_id,
                thread_id=effective_thread_id,
                window_id=wid,
                message_id=edit_msg_id,
                text=full_text,
                role="tool",
                content_type="tool_result",
            )
            # Pop only after the edit attempt RETURNED (finding 10): a
            # raised RetryAfter (from the status clear above or the edit
            # itself) leaves the entry in place so the retry edits the
            # SAME message instead of posting a new bubble. A non-OK
            # outcome (returned, not raised) is a terminal edit failure —
            # consume the entry and fall through to a fresh send, exactly
            # as before.
            _tool_msg_ids.pop(_tkey, None)
            if outcome is TopicSendOutcome.OK:
                # The edit delivered the full text — mark every part as
                # sent so a RetryAfter raised by the follow-ups below
                # can't re-send the body as fresh bubbles on the retry
                # (the entry above is already consumed by then).
                task.parts_sent = len(task.parts)
                # Tool work resumed → user no longer "needed".
                await attention.dismiss(bot, user_id=user_id, thread_id=task.thread_id)
                await _send_task_images(bot, chat_id, task, effective_thread_id)
                await _check_and_send_status(bot, user_id, wid, task.thread_id)
                return
            logger.debug(
                "tool_result edit non-OK msg=%s outcome=%s — sending new",
                edit_msg_id,
                outcome.value,
            )
            # Fall through to send as new message

    # 2. Send content messages, converting status message to first content part
    # ``parts_sent`` is the retry-resume cursor (finding 10): parts below it
    # were already delivered by a previous attempt that then raised
    # RetryAfter — skip them so the retry never re-sends a delivered part.
    first_part = task.parts_sent == 0
    first_topic_send_done = False
    last_msg_id: int | None = None
    # Role for the message_refs row: tool_use/tool_result → "tool";
    # everything else (text, thinking) → "assistant". This mirrors the
    # taxonomy in §2.5.3 / §2.5.5: ``role`` is a coarse routing key, and the
    # finer distinction lives in ``content_type``.
    ref_role = (
        "tool" if task.content_type in ("tool_use", "tool_result") else "assistant"
    )
    ref_session_id = _session_id_for_window(wid)
    for part_idx, part in enumerate(task.parts):
        if part_idx < task.parts_sent:
            continue
        sent = None

        # For first part, try to convert status message to content (edit instead of delete)
        if first_part:
            first_part = False
            converted_msg_id = await _convert_status_to_content(
                bot,
                user_id,
                tid,
                wid,
                part,
                ref_role=ref_role,
                ref_content_type=task.content_type,
            )
            logger.info(
                "content_task convert: user=%d tid=%d wid=%s ctype=%s "
                "part_idx=%d converted_msg_id=%s",
                user_id,
                tid,
                wid,
                task.content_type,
                part_idx,
                converted_msg_id,
            )
            if converted_msg_id is not None:
                last_msg_id = converted_msg_id
                # Part delivered via the conversion edit — advance the
                # retry-resume cursor and record the tool_use edit target
                # eagerly (a later RetryAfter must not lose it).
                task.parts_sent = part_idx + 1
                if task.tool_use_id and task.content_type == "tool_use":
                    _tool_msg_ids[(task.tool_use_id, user_id, tid)] = last_msg_id
                # Status-conversion edits the existing status message — no
                # fresh send happens here, so ``reply_parameters`` cannot
                # attach (Telegram has no edit-with-reply primitive). The
                # next iteration will be a real ``topic_send`` and that one
                # is treated as the first anchor candidate.
                continue

        # Tool-use top-level messages (including the §2.7 Agent promotion)
        # are content, not attention. They should not ping the user every
        # time a subagent dispatches or a long-running tool starts.
        # Assistant text uses the default (notify) — that's the
        # conversational surface the user actually wants pinged.
        silent = task.content_type in ("tool_use", "tool_result")

        # §2.5.2 outbound anchor: first part of assistant final text only.
        # Tool / activity / status sends are deliberately excluded — those
        # are UI state, not conversation. The anchor is consumed here
        # BEFORE the send attempt; if the send fails into the emergency-DM
        # fallback below (topic-broken outcome), the DM is intentionally
        # unanchored — Telegram replies cannot cross chat boundaries from
        # the topic to the user's DM, so there is no anchor to carry.
        anchor: ReplyParameters | None = None
        if (
            not first_topic_send_done
            and task.content_type == "text"
            and config.reply_context_enabled
        ):
            anchor_id = consume_route_last_user_message(user_id, task.thread_id, wid)
            if anchor_id is not None:
                anchor = ReplyParameters(message_id=anchor_id)
        first_topic_send_done = True

        logger.info(
            "content_task send_pre: user=%d tid=%d wid=%s ctype=%s "
            "part_idx=%d part_len=%d anchor=%s",
            user_id,
            tid,
            wid,
            task.content_type,
            part_idx,
            len(part),
            anchor is not None,
        )
        if anchor is not None:
            sent, outcome = await topic_send(
                bot,
                op="content",
                user_id=user_id,
                chat_id=chat_id,
                thread_id=effective_thread_id,
                window_id=wid,
                text=part,
                disable_notification=silent,
                reply_parameters=anchor,
                role=ref_role,
                content_type=task.content_type,
                part_index=part_idx,
                transcript_uuid=task.transcript_uuid,
                session_id=ref_session_id,
            )
        else:
            sent, outcome = await topic_send(
                bot,
                op="content",
                user_id=user_id,
                chat_id=chat_id,
                thread_id=effective_thread_id,
                window_id=wid,
                text=part,
                disable_notification=silent,
                role=ref_role,
                content_type=task.content_type,
                part_index=part_idx,
                transcript_uuid=task.transcript_uuid,
                session_id=ref_session_id,
            )
        logger.info(
            "content_task send_post: user=%d tid=%d wid=%s ctype=%s "
            "part_idx=%d sent=%s outcome=%s",
            user_id,
            tid,
            wid,
            task.content_type,
            part_idx,
            sent.message_id if sent is not None else None,
            outcome.value if outcome is not None else None,
        )

        if sent is not None:
            last_msg_id = sent.message_id
            # Record the tool_use edit target eagerly (not only at step 3):
            # a RetryAfter raised AFTER this loop (attention / images /
            # status) would re-enter with all parts skipped and
            # ``last_msg_id`` None, so step 3 alone would never record it
            # and the later tool_result would post as a new bubble.
            if task.tool_use_id and task.content_type == "tool_use":
                _tool_msg_ids[(task.tool_use_id, user_id, tid)] = last_msg_id
        elif task.thread_id is not None and outcome in _TOPIC_BROKEN_OUTCOMES:
            await _emergency_dm(
                bot, user_id, task.thread_id, wid, part, kind="content", outcome=outcome
            )
        # The part was handled (sent, or terminally failed into the
        # emergency-DM path — which is not retried either way): advance the
        # retry-resume cursor. A RetryAfter raised by topic_send above
        # skips this, so the retry resumes AT this part.
        task.parts_sent = part_idx + 1

    # The attention/dismiss heuristic must run on the logical final text once,
    # not per multipart segment. Otherwise an early part containing a question
    # cue gets dismissed by a later neutral part (or vice versa), and the user
    # sees an attention card flap to acknowledged within the same logical
    # message.
    if logical_text:
        await _maybe_attention_or_dismiss(
            bot, user_id, task.thread_id, wid, logical_text, task.content_type
        )
        if task.content_type == "text" and attention.is_attention_request(logical_text):
            await refresh_activity_digest_if_present(bot, user_id, task.thread_id, wid)

    # 3. Record tool_use message ID for later editing
    if last_msg_id and task.tool_use_id and task.content_type == "tool_use":
        _tool_msg_ids[(task.tool_use_id, user_id, tid)] = last_msg_id

    # 4. Send images if present (from tool_result with base64 image blocks)
    await _send_task_images(bot, chat_id, task, effective_thread_id)

    # 5. After content, check and send status
    await _check_and_send_status(bot, user_id, wid, task.thread_id)


async def _maybe_attention_or_dismiss(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
    content_type: str,
) -> None:
    """Dismiss any standing attention card after assistant text lands.

    We deliberately do NOT raise a fresh attention card for assistant text:
    the user already sees Claude's message in the topic, so a "Claude needs
    a decision" card right next to it is pure noise. Interactive-UI cards
    (permission prompts, ExitPlanMode, etc.) are still emitted from
    ``handlers.interactive_ui`` because those genuinely require the user to
    open the topic — you can't dismiss a permission prompt with a text reply.

    Any prior card is dismissed regardless of attention-cue heuristic so
    routes don't get stuck in a "waiting" state once Claude resumes talking.
    """
    await attention.dismiss(bot, user_id=user_id, thread_id=thread_id)


async def _convert_status_to_content(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
    window_id: str,
    content_text: str,
    *,
    ref_role: str = "assistant",
    ref_content_type: str = "text",
) -> int | None:
    """Convert status message to content message by editing it.

    Returns the message_id if converted successfully, None otherwise.
    The ``ref_role`` / ``ref_content_type`` are forwarded to
    ``message_refs.update_role_and_content_type`` so the provenance row
    flips from ``status`` to whatever first-content-part landed here.
    """
    skey = (user_id, thread_id_or_0)
    info = _status_msg_info.pop(skey, None)
    if not info:
        return None

    msg_id, stored_wid, _ = info
    # The card is being repurposed as a content message — from
    # route_runtime's perspective the status card is no longer visible (mq
    # has no record of an editable status surface).
    route_runtime.mark_status_card_cleared((user_id, thread_id_or_0, stored_wid))
    try:
        return await _convert_status_to_content_inner(
            bot,
            user_id,
            thread_id_or_0,
            window_id,
            content_text,
            msg_id=msg_id,
            stored_wid=stored_wid,
            ref_role=ref_role,
            ref_content_type=ref_content_type,
        )
    except RetryAfter:
        # Wave-4 P3-1: the entry was popped (and the card marked cleared)
        # BEFORE the awaited edit/delete, so a RetryAfter raise would leave
        # the retry seeing no status entry — it would send fresh and strand
        # the visible card. Restore the popped tracking (and re-mirror the
        # route_runtime flag) before re-raising so the retry re-converts the
        # SAME card.
        _status_msg_info[skey] = info
        route_runtime.mark_status_card_published(
            (user_id, thread_id_or_0, stored_wid), msg_id
        )
        raise


async def _convert_status_to_content_inner(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
    window_id: str,
    content_text: str,
    *,
    msg_id: int,
    stored_wid: str,
    ref_role: str,
    ref_content_type: str,
) -> int | None:
    """Body of ``_convert_status_to_content`` after the pop (see wrapper)."""
    skey = (user_id, thread_id_or_0)
    thread_id = thread_id_or_0 if thread_id_or_0 != 0 else None
    chat_id, effective_thread_id = _delivery_target(user_id, thread_id)
    if stored_wid != window_id:
        # Different window, just delete the old status
        await topic_delete(
            bot,
            op="status",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=effective_thread_id,
            window_id=stored_wid,
            message_id=msg_id,
        )
        return None

    # If an activity digest exists for this route at a HIGHER message_id than
    # the status, repurposing the status into content would put the final text
    # ABOVE the digest in chat order — wrong, because the digest covers
    # tool_use that ran BEFORE the final text. Delete the status and let the
    # caller send content fresh (it'll get a message_id higher than the
    # digest, landing chronologically after it). Single-turn / no-tool cases
    # keep the in-place edit optimization.
    digest_state = _activity_msg_info.get(skey)
    if (
        digest_state is not None
        and digest_state.message_id
        and digest_state.message_id > msg_id
    ):
        await topic_delete(
            bot,
            op="status",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=effective_thread_id,
            window_id=stored_wid,
            message_id=msg_id,
        )
        return None

    # Edit status message to show content (op=content because the message is
    # being repurposed as the first content message).
    outcome = await topic_edit(
        bot,
        op="content",
        user_id=user_id,
        chat_id=chat_id,
        thread_id=effective_thread_id,
        window_id=window_id,
        message_id=msg_id,
        text=content_text,
        role=ref_role,
        content_type=ref_content_type,
    )
    if outcome in (TopicSendOutcome.OK, TopicSendOutcome.MESSAGE_NOT_MODIFIED):
        # MESSAGE_NOT_MODIFIED is caller-success (see message_sender):
        # Telegram refused the edit because the body ALREADY renders this
        # exact content — that IS the converted message. Returning None here
        # would make the caller fresh-send the same part (duplicate content
        # next to the still-visible card). Pre-existing on main too — main's
        # version also only returned msg_id on OK. topic_edit flips the
        # provenance row on this outcome as well, so the post-state matches
        # OK exactly (entry popped, card cleared, row repurposed).
        return msg_id
    # Finding 17: the failed repurposing edit left the old status card
    # visible but untracked (popped above) — a permanent stale "🟡 Busy"
    # bubble with no self-heal. Best-effort delete it, mirroring the two
    # earlier delete branches.
    await topic_delete(
        bot,
        op="status",
        user_id=user_id,
        chat_id=chat_id,
        thread_id=effective_thread_id,
        window_id=stored_wid,
        message_id=msg_id,
    )
    return None


async def _process_status_update_task(
    bot: Bot, user_id: int, task: MessageTask
) -> None:
    """Process a status update task."""
    wid = task.window_id or ""
    tid = task.thread_id or 0
    chat_id, effective_thread_id = _delivery_target(user_id, task.thread_id)
    skey = (user_id, tid)
    status_text = task.text or ""

    if not status_text:
        # No status text means clear status
        await _do_clear_status_message(bot, user_id, tid)
        return

    current_info = _status_msg_info.get(skey)

    if current_info:
        msg_id, stored_wid, last_text = current_info

        if stored_wid != wid:
            # Window changed - delete old and send new
            await _do_clear_status_message(bot, user_id, tid)
            await _do_send_status_message(bot, user_id, tid, wid, status_text)
        elif status_text == last_text:
            # Same content, skip edit
            return
        else:
            # Same window, text changed - edit in place.
            # (Topic-level "typing" indicator is fired by status_polling on
            # every active poll — that's the right cadence for keeping the
            # "Claude is typing…" line under the topic title alive.)
            outcome = await topic_edit(
                bot,
                op="status",
                user_id=user_id,
                chat_id=chat_id,
                thread_id=effective_thread_id,
                window_id=wid,
                message_id=msg_id,
                text=_status_display_text(wid, status_text),
            )
            if outcome is TopicSendOutcome.OK:
                _status_msg_info[skey] = (msg_id, wid, status_text)
                route_runtime.mark_status_card_published((user_id, tid, wid), msg_id)
            else:
                _status_msg_info.pop(skey, None)
                route_runtime.mark_status_card_cleared((user_id, tid, wid))
                await _do_send_status_message(bot, user_id, tid, wid, status_text)
    else:
        # No existing status message, send new
        await _do_send_status_message(bot, user_id, tid, wid, status_text)


async def _do_send_status_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
    window_id: str,
    text: str,
) -> None:
    """Send a new status message and track it (internal, called from worker)."""
    skey = (user_id, thread_id_or_0)
    thread_id: int | None = thread_id_or_0 if thread_id_or_0 != 0 else None
    chat_id, effective_thread_id = _delivery_target(user_id, thread_id)
    # Safety net: delete any orphaned status message before sending a new one.
    # This catches edge cases where tracking was cleared without deleting the message.
    old = _status_msg_info.pop(skey, None)
    if old:
        route_runtime.mark_status_card_cleared((user_id, thread_id_or_0, old[1]))
        await topic_delete(
            bot,
            op="status",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=effective_thread_id,
            window_id=old[1],
            message_id=old[0],
        )
    # (Topic-level "typing" indicator is fired by the typing_action_loop.)
    rendered = _status_display_text(window_id, text)
    sent, outcome = await topic_send(
        bot,
        op="status",
        user_id=user_id,
        chat_id=chat_id,
        thread_id=effective_thread_id,
        window_id=window_id,
        text=rendered,
        disable_notification=True,
        role="status",
        content_type="status",
        session_id=_session_id_for_window(window_id),
    )
    if sent is not None:
        _status_msg_info[skey] = (sent.message_id, window_id, text)
        route_runtime.mark_status_card_published(
            (user_id, thread_id_or_0, window_id), sent.message_id
        )
        return
    if thread_id is not None and outcome in _TOPIC_BROKEN_OUTCOMES:
        await _emergency_dm(
            bot,
            user_id,
            thread_id,
            window_id,
            rendered,
            kind="status",
            outcome=outcome,
        )


async def _do_clear_status_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int = 0,
) -> None:
    """Delete the status message for a user (internal, called from worker)."""
    skey = (user_id, thread_id_or_0)
    info = _status_msg_info.pop(skey, None)
    if info:
        msg_id, stored_wid, _ = info
        route_runtime.mark_status_card_cleared((user_id, thread_id_or_0, stored_wid))
        thread_id = thread_id_or_0 if thread_id_or_0 != 0 else None
        chat_id, effective_thread_id = _delivery_target(user_id, thread_id)
        await topic_delete(
            bot,
            op="status",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=effective_thread_id,
            window_id=stored_wid,
            message_id=msg_id,
        )


async def _check_and_send_status(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
) -> None:
    """Check terminal for status line and send status message if present."""
    # Per-route gate is now mostly cosmetic; rare to have queued content right
    # after a content tick on the same route. Kept for symmetry with the
    # polling-side _poll_one_binding skip_status check.
    route = _route_for(user_id, thread_id, window_id)
    queue = _route_queues.get(route)
    if queue and not queue.empty():
        return
    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        return

    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text:
        return

    tid = thread_id or 0
    status_line = parse_status_line(pane_text)
    # Mirror the gate used by status_polling.update_status_message: a
    # post-completion summary like "✻ Worked for 2s" still parses as a
    # status_line, but is_status_active() returns False because the spinner
    # sits above a blank-line gap rather than directly above chrome. Without
    # this check, the post-content status path would resurrect a "🟡 Busy"
    # card right after Claude finishes — exactly the stale-Busy regression
    # the polling path already guards against.
    if status_line and is_status_active(pane_text):
        await _do_send_status_message(bot, user_id, tid, window_id, status_line)
    else:
        await _do_clear_status_message(bot, user_id, tid)


async def enqueue_content_message(
    bot: Bot,
    user_id: int,
    window_id: str,
    parts: list[str],
    tool_use_id: str | None = None,
    content_type: str = "text",
    text: str | None = None,
    thread_id: int | None = None,
    image_data: list[tuple[str, bytes]] | None = None,
    tool_name: str | None = None,
    tool_input: dict[str, object] | None = None,
    transcript_uuid: str | None = None,
    subagent_key: str | None = None,
    stop_reason: str | None = None,
) -> None:
    """Enqueue a content message task."""
    logger.debug(
        "Enqueue content: user=%d, window_id=%s, content_type=%s",
        user_id,
        window_id,
        content_type,
    )
    route = _route_for(user_id, thread_id, window_id)
    if route in _route_tearing_down:
        # Bug 1 guard: a teardown is in flight. Dropping content here is
        # noisy but correct — re-creating the route now would race with
        # the teardown's worker-cancel and leak _tool_msg_ids slots.
        logger.warning(
            "Dropping content for route %s: route is tearing down "
            "(content_type=%s, parts=%d)",
            route,
            content_type,
            len(parts),
        )
        return
    queue = _get_or_create_route(bot, route)

    task = MessageTask(
        task_type="content",
        text=text,
        window_id=window_id,
        parts=parts,
        tool_use_id=tool_use_id,
        content_type=content_type,
        thread_id=thread_id,
        image_data=image_data,
        tool_name=tool_name,
        tool_input=tool_input,
        transcript_uuid=transcript_uuid,
        subagent_key=subagent_key,
        stop_reason=stop_reason,
    )
    queue.put_nowait(task)


async def enqueue_status_update(
    bot: Bot,
    user_id: int,
    window_id: str,
    status_text: str | None,
    thread_id: int | None = None,
) -> None:
    """Enqueue status update. Skipped if text unchanged or during flood control."""
    # Don't enqueue during flood control — they'd just be dropped
    flood_end = _flood_until.get(user_id, 0)
    if flood_end > time.monotonic():
        return

    tid = thread_id or 0

    # Deduplicate: skip if text matches what's already displayed
    if status_text:
        skey = (user_id, tid)
        info = _status_msg_info.get(skey)
        if info and info[1] == window_id and info[2] == status_text:
            return

    route = _route_for(user_id, thread_id, window_id)
    if route in _route_tearing_down:
        # Bug 1 guard: silently drop ephemerals during teardown — they're
        # latest-wins, and the route is going away anyway.
        return
    _get_or_create_route(bot, route)
    lock = _route_locks[route]
    kick = _route_ephemeral_kick[route]

    if status_text:
        task = MessageTask(
            task_type="status_update",
            text=status_text,
            window_id=window_id,
            thread_id=thread_id,
        )
    else:
        task = MessageTask(task_type="status_clear", thread_id=thread_id)

    # Set the slot AND the kick under the same lock that
    # ``_drain_pending_ephemeral`` uses. Otherwise kick.set() can land
    # after the worker observes "slot empty" and clears the kick under
    # its own lock, leaving the new task parked indefinitely.
    async with lock:
        _route_pending_ephemeral[route] = task
        kick.set()


def clear_status_msg_info(user_id: int, thread_id: int | None = None) -> None:
    """Clear status message tracking for a user (and optionally a specific thread)."""
    skey = (user_id, thread_id or 0)
    info = _status_msg_info.pop(skey, None)
    if info is not None:
        _msg_id, stored_wid, _last_text = info
        route_runtime.mark_status_card_cleared((user_id, thread_id or 0, stored_wid))


def clear_tool_msg_ids_for_topic(user_id: int, thread_id: int | None = None) -> None:
    """Clear tool message ID tracking for a specific topic.

    Removes all entries in _tool_msg_ids and _agent_tool_ids that match the
    given user and thread, preventing per-topic teardown from leaking
    long-lived (tool_use_id, user, thread) keys.
    """
    tid = thread_id or 0
    # Find and remove all matching keys
    keys_to_remove = [
        key for key in _tool_msg_ids if key[1] == user_id and key[2] == tid
    ]
    for key in keys_to_remove:
        _tool_msg_ids.pop(key, None)
    agent_keys_to_remove = [
        key for key in _agent_tool_ids if key[1] == user_id and key[2] == tid
    ]
    for key in agent_keys_to_remove:
        _agent_tool_ids.pop(key, None)


async def teardown_route(route: Route, *, drop_pending: bool) -> None:
    """Tear down a route's queue + worker.

    drop_pending=False → wait for queued tasks to drain naturally.
    drop_pending=True  → discard queued tasks; in-flight task is still allowed
    to finish so ``_tool_msg_ids`` slots are not leaked.

    Bug 1 fix: marks the route in ``_route_tearing_down`` BEFORE awaiting
    inflight, so ``enqueue_content_message`` / ``enqueue_status_update``
    drop new work for this route instead of resurrecting the queue
    between ``inflight.wait()`` and ``worker.cancel()``.
    """
    queue = _route_queues.get(route)
    worker = _route_workers.get(route)
    inflight = _route_inflight.get(route)
    if queue is None and worker is None:
        return

    _route_tearing_down.add(route)
    try:
        if inflight is not None:
            await inflight.wait()

        if queue is not None:
            if drop_pending:
                while not queue.empty():
                    try:
                        queue.get_nowait()
                        queue.task_done()
                    except asyncio.QueueEmpty:
                        break
            else:
                await queue.join()

        if worker is not None:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass

        _route_queues.pop(route, None)
        _route_workers.pop(route, None)
        _route_pending_ephemeral.pop(route, None)
        _route_locks.pop(route, None)
        _route_ephemeral_kick.pop(route, None)
        _route_inflight.pop(route, None)
        # Drop per-route agent_tool_ids so a session crash mid-Agent or a
        # /clear-driven teardown can't leak (tool_use_id, user, thread)
        # entries past the route's lifetime.
        route_user_id, route_tid, _ = route
        agent_keys_to_remove = [
            key
            for key in _agent_tool_ids
            if key[1] == route_user_id and key[2] == route_tid
        ]
        for key in agent_keys_to_remove:
            _agent_tool_ids.pop(key, None)
        # §2.5.2: drop any per-route anchor candidate so a freshly re-bound
        # route can't reply-to a Telegram message_id from the old session.
        _route_last_user_message.pop(route, None)
        # Item 3 / P2-1: drop the route's user-turn delivery stamp alongside the
        # anchor so a re-bound route can't carry a stale turn boundary.
        _route_user_turn_at.pop(route, None)
        route_runtime.clear_route(route)
        pane_signals.clear_route(route)  # GH #43
        # Cancel any pending activity-digest debounce so we don't fire an
        # edit against a torn-down route. Also drop the upsert lock — a
        # fresh route gets a fresh lock.
        flush = _activity_flush_tasks.pop((route_user_id, route_tid), None)
        if flush is not None and not flush.done():
            flush.cancel()
        _activity_locks.pop((route_user_id, route_tid), None)
        # Sub-agent digests are scoped per (user, thread, subagent_key) — drop
        # all entries that belong to this route. Same rationale as the parent
        # activity digest above; a torn-down route must not own ghost state.
        sub_keys_to_remove = [
            k for k in _subagent_msg_info if k[0] == route_user_id and k[1] == route_tid
        ]
        for k in sub_keys_to_remove:
            _subagent_msg_info.pop(k, None)
            sub_flush = _subagent_flush_tasks.pop(k, None)
            if sub_flush is not None and not sub_flush.done():
                sub_flush.cancel()
            _subagent_locks.pop(k, None)
        sub_tool_keys_to_remove = [
            k
            for k in _subagent_tool_indices
            if k[1] == route_user_id and k[2] == route_tid
        ]
        for k in sub_tool_keys_to_remove:
            _subagent_tool_indices.pop(k, None)
        # Drop the to-do digest state and any pending flush for this route.
        # Mirrors the activity digest cleanup above; a fresh route should
        # start with a fresh card, not inherit an old message_id.
        _todo_msg_info.pop((route_user_id, route_tid), None)
        _todo_pending_snapshot.pop((route_user_id, route_tid), None)
        todo_flush = _todo_flush_tasks.pop((route_user_id, route_tid), None)
        if todo_flush is not None and not todo_flush.done():
            todo_flush.cancel()
        _todo_locks.pop((route_user_id, route_tid), None)
        # _todo_tool_ids is OrderedDict-as-set keyed by tool_use_id; we don't
        # know which ids belong to this route without scanning. Materialize
        # the list of matches first so the pop loop doesn't mutate during
        # iteration.
        todo_ids_to_remove = [
            k for k in _todo_tool_ids if k[1] == route_user_id and k[2] == route_tid
        ]
        for k in todo_ids_to_remove:
            _todo_tool_ids.pop(k, None)
    finally:
        _route_tearing_down.discard(route)


def routes_for_topic(user_id: int, thread_id: int | None) -> list[Route]:
    """Return all live routes matching ``(user_id, thread_id_or_0)``."""
    tid = thread_id or 0
    # Snapshot the keys so concurrent mutation (teardown) cannot break
    # iteration.
    return [r for r in list(_route_queues) if r[0] == user_id and r[1] == tid]


async def shutdown_workers() -> None:
    """Stop all queue workers (called during bot shutdown)."""
    for _, worker in list(_route_workers.items()):
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
    # Cancel any pending activity-digest debounces; the bot is going away,
    # there is no value in firing one last edit during shutdown.
    for _, flush in list(_activity_flush_tasks.items()):
        if not flush.done():
            flush.cancel()
    _activity_flush_tasks.clear()
    _activity_locks.clear()
    for _, flush in list(_subagent_flush_tasks.items()):
        if not flush.done():
            flush.cancel()
    _subagent_flush_tasks.clear()
    _subagent_locks.clear()
    _subagent_msg_info.clear()
    _subagent_tool_indices.clear()
    for _, flush in list(_todo_flush_tasks.items()):
        if not flush.done():
            flush.cancel()
    _todo_flush_tasks.clear()
    _todo_locks.clear()
    _todo_msg_info.clear()
    _todo_pending_snapshot.clear()
    _todo_tool_ids.clear()
    _route_workers.clear()
    _route_queues.clear()
    _route_locks.clear()
    _route_pending_ephemeral.clear()
    _route_ephemeral_kick.clear()
    _route_inflight.clear()
    logger.info("Message queue workers stopped")


def reset_for_tests() -> None:
    """Test-only: drop ALL module-level send-layer state and cancel any
    scheduled asyncio task.

    Co-located with the state it resets (the R3 reset-seam contract): every
    map is resolved by direct module reference, never ``getattr(name)`` string
    indirection — that string lookup is the "reset fixture by stale name =
    silent no-op" footgun this seam removes. Adding a module-level MUTABLE
    per-test dict/set/task map WITHOUT adding it here will leave state leaking
    into the next test; the pinning test ``test_message_queue_reset.py`` guards
    the invariant: after this call no module-level state and no scheduled
    asyncio task survives into the next test. (Immutable module-level constant
    lookup tables — e.g. ``_RUN_STATE_HEADER`` — are NOT per-test state and are
    intentionally excluded.)

    The task maps are cancel-then-clear: ``_route_workers`` (the per-route
    queue workers) and the three flush-task maps each have every pending (not
    ``.done()``) task ``.cancel()``-ed BEFORE the map is cleared, so a live
    worker or a debounce scheduled by a prior test cannot survive — or fire its
    edit — into the next one.
    """
    # Plain dicts.
    _route_queues.clear()
    _route_locks.clear()
    _route_pending_ephemeral.clear()
    _route_ephemeral_kick.clear()
    _route_inflight.clear()
    _status_msg_info.clear()
    _route_last_user_message.clear()
    _route_user_turn_at.clear()
    _tool_msg_ids.clear()
    _agent_tool_ids.clear()
    _activity_msg_info.clear()
    _tool_activity_indices.clear()
    _activity_locks.clear()
    _subagent_msg_info.clear()
    _subagent_tool_indices.clear()
    _subagent_locks.clear()
    _todo_locks.clear()
    _todo_msg_info.clear()
    _todo_pending_snapshot.clear()
    _flood_until.clear()
    # OrderedDict (LRU-as-set of seen TodoWrite tool_use ids).
    _todo_tool_ids.clear()
    # Set-typed state.
    _route_tearing_down.clear()
    _bad_topic_threads.clear()
    # Task maps: cancel-then-clear, preserving that exact ordering, so no
    # scheduled asyncio task survives into the next test. ``_route_workers``
    # holds live per-route queue workers (created via ``asyncio.create_task``
    # in ``_get_or_create_route``); the other three are debounce-flush tasks.
    for task_map in (
        _route_workers,
        _activity_flush_tasks,
        _subagent_flush_tasks,
        _todo_flush_tasks,
    ):
        for task in list(task_map.values()):
            if not task.done():
                task.cancel()
        task_map.clear()
    # Task SET: the strong-ref detached background tasks (finding 23) —
    # same cancel-then-clear ordering.
    for task in list(_background_tasks):
        if not task.done():
            task.cancel()
    _background_tasks.clear()
