"""Per-route snapshot state machine — the sole run-state / context-usage /
idle-clear authority.

Single source of truth for "what is this route doing right now":

  - ``RouteRuntimeSnapshot`` is immutable; every read returns a frozen
    point-in-time view.
  - Mutations go through ``ingest_*`` / ``mark_*`` functions, which acquire
    a per-route ``asyncio.Lock``, apply the transition, freeze a snapshot,
    and release the lock. Each mutator returns the committed snapshot.
  - ``snapshot(route)`` is the sole read seam — pull-based, fast, no
    coroutine machinery. There is no push/observer channel: surfaces read
    state by calling ``snapshot(route)``.

This module owns the run-state machine, the context-usage cache, and the
debounced pane-idle card-clear. The surfaces that consume it:

  - ``snapshot.run_state`` — run-state (RUNNING / RUNNING_TOOL /
    WAITING_ON_USER / IDLE_RECENT / IDLE_CLEARED).
  - ``snapshot.open_tools`` / ``snapshot.waiting_on_user_tools`` — the
    in-flight tool set (replay-seeded on startup via ``seed_open_tools``).
  - ``snapshot.context_usage`` — the context-window cache with the 1M latch.
  - ``snapshot.idle_clear_at`` — the run-state decay deadline (transcript
    end-of-turn → IDLE_RECENT → IDLE_CLEARED).
  - ``snapshot.pane_idle_clear_at`` plus the ``arm_pane_idle_clear`` /
    ``pane_idle_clear_due`` / ``commit_pane_idle_clear`` debounce triad —
    the visible "🟡 Busy" status-card clear, armed by ``status_polling``
    on a confirmed-idle pane observation.
  - ``snapshot.status_card_visible`` / ``snapshot.status_card_msg_id`` —
    status-card visibility (``message_queue`` mirrors its send-layer cache
    here).

The ``message_queue`` boundary:

  - ``message_queue._status_msg_info`` remains the send-layer cache.
    message_queue is the sole sender/editor of status cards. It queries
    ``snapshot.status_card_visible`` to pick edit-vs-send and calls
    ``mark_status_card_published`` after a successful send. If a change
    needs to mutate message_queue internals beyond that, **stop and
    promote Route Outbox** — that's the kill signal.

Concurrency contract:

  - One per-route ``asyncio.Lock``. Independent routes do not serialize.
  - Async mutators acquire the route's lock, mutate, and release it. Most
    (``ingest_transcript_event``, ``mark_inbound_sent``, ``mark_pane_idle``,
    ``mark_session_reset``) freeze and return the committed snapshot;
    ``commit_pane_idle_clear`` mutates under the lock and returns a ``bool``
    (whether it actually cleared). There is no push/observer channel —
    surfaces read state by calling ``snapshot(route)``.
  - **Synchronous side-band writes** (``mark_status_card_published`` /
    ``mark_status_card_cleared``, ``update_context_usage``,
    ``seed_open_tools``, ``arm_pane_idle_clear``, ``clear_route``)
    intentionally bypass the lock. They are bookkeeping for read-side
    flags — they don't change ``run_state`` (no transition table runs)
    and don't await between their initial read of ``_state`` and the
    field write. Safe under Python's single-threaded asyncio scheduling
    because no suspension point separates the read from the write.
    ``pane_idle_clear_due`` is a pure synchronous read in the same vein.
    **Do not call these from a thread** — they assume event-loop-thread
    execution.
  - Pane snapshots (``mark_pane_idle`` / ``commit_pane_idle_clear``) are
    reconciliation events with **lower authority** than transcript
    lifecycle events: they preserve ``WAITING_ON_USER``, only clearing
    ``RUNNING`` / ``RUNNING_TOOL`` to ``IDLE_CLEARED`` after the debounce
    delay has elapsed, keeping the visible "🟡 Busy" card and the
    run-state machine in sync. A pane clear that reconciled an ACTIVE
    route records ``idle_source="pane"`` and MOVES its open tools into a
    ``suspended_tools`` stash (in-memory only) instead of dropping them;
    the authoritative transcript end-of-turn records
    ``idle_source="transcript"`` and drops the stash.
  - ``mark_background_agent_activity`` is the keyed sidechain keep-alive
    (GH #44 — successor of Wave A's ``mark_subagent_activity``): it
    refreshes an active route like transcript activity, RESURRECTS an
    ``idle_source="pane"`` route (restoring the stash — sidechain
    activity is positive proof the pane clear was false), and records the
    agent's key into ``background_agents``. The stored state of a
    transcript-idle route is never mutated; instead the SNAPSHOT
    PROJECTION lifts a stored-idle route with a live background key to a
    visible ``RUNNING`` (typing + 🟡 Busy) — see ``_projected_run_state``.
    It never overrides ``WAITING_ON_USER`` and never seeds an unseen route.
  - A pane/lifecycle signal may also **PROMOTE** an *active* ``RUNNING``
    route (empty ``open_tools``) to ``WAITING_ON_USER`` via
    ``mark_interactive_pending`` — for the window where Claude Code buffers
    an interactive ``tool_use`` (AskUserQuestion / ExitPlanMode) in JSONL,
    so the transcript can't yet open the id. It is **strictly lower
    authority than the transcript**: the deriver checks ``open_tools``
    first, the promote fires only on an active ``RUNNING`` route, and the
    ``tool_use`` / known-``tool_result`` / end-of-turn / user branches zero
    the ``pane_interactive_pending`` bit (a plain-text/thinking
    continuation or an unknown ``tool_result`` preserves it). It never
    resurrects idle, seeds an unseen route, overrides a transcript
    ``RUNNING_TOOL``, or clobbers a transcript-set ``WAITING_ON_USER``.
    ``mark_interactive_cleared`` is the sole programmatic retract (no-op
    against a transcript-set ``WAITING_ON_USER``). Both run **under the
    route lock** — like every other run-state transition, no sync side-band
    transitions ``run_state``.
  - ``mark_notification_pending`` / ``mark_notification_cleared`` (Wave B)
    drive the SECOND lower-authority bit, ``notification_pending`` — set
    from a window-predicated Notification-hook side-file read by the
    poller. Unlike the pane bit it outranks ``RUNNING_TOOL`` in the deriver
    (the Workflow approval gate blocks Claude WITH its tool_use open) and
    may resurrect an ``IDLE(pane)`` route with a live ``suspended_tools``
    stash (positive live proof — the second stash-restore path). Transcript
    clears are timestamp-qualified: a ``user`` event clears unconditionally;
    tool_result / end-of-turn / assistant events clear only when strictly
    NEWER than ``notification_set_at`` (buffered pre-notification JSONL
    must not re-hide the wait). The poller also enforces the
    ``NOTIFY_TTL_SECONDS`` runtime TTL from the snapshot. The two bits
    clear INDEPENDENTLY. Both mutators run under the route lock;
    ``mark_notification_pending`` returns a ``NotificationMarkResult`` that
    drives the caller's side-file unlink ordering.

Persistence policy:

  - In-memory by default. ``open_tools`` reconstructs from JSONL replay
    on startup via ``seed_open_tools`` /
    ``parse_pending_tools_from_jsonl``. ``status_card_*`` is not
    persisted — restart-induced loss is self-healing (next status-card
    send re-publishes the msg_id). When persistence is needed it will
    land via a state.json ``schema_version`` bump in a follow-up wave.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from .utils import normalize_background_agent_key

logger = logging.getLogger(__name__)


Route = tuple[int, int, str]

# Tool names whose open tool_use means "the run-state is WAITING_ON_USER".
# Owned HERE (the run-state authority that classifies them) rather than imported
# from the heavy ``handlers.interactive_ui`` UI layer — importing UI from the
# core authority created a circular import (route_runtime → interactive_ui →
# callback_dispatcher → …) that made ``import cctelegram.route_runtime`` fail
# standalone and only work by bot.py's import order. interactive_ui and bot.py
# import this constant FROM route_runtime now (one-way dependency).
INTERACTIVE_TOOL_NAMES = frozenset({"AskUserQuestion", "ExitPlanMode"})


class RunState(Enum):
    RUNNING = "RUNNING"
    RUNNING_TOOL = "RUNNING_TOOL"
    WAITING_ON_USER = "WAITING_ON_USER"
    IDLE_RECENT = "IDLE_RECENT"
    IDLE_CLEARED = "IDLE_CLEARED"


# Wall-clock seconds an IDLE_RECENT route stays "recent" before decaying to
# IDLE_CLEARED, and the confirmed-idle debounce before the visible "🟡 Busy"
# card is cleared. Time-based rather than poll-count-based because the polling
# loop iterates all bindings sequentially — with N bound topics any single
# window is only polled every N seconds, so a poll-count threshold would make
# the perceived clear delay scale with how many topics the user has open. 4s
# of confirmed idle is comfortably longer than Claude's slowest UI transition
# while still feeling responsive.
IDLE_CLEAR_DELAY_SECONDS = 4.0


@dataclass(frozen=True)
class ContextUsage:
    """Snapshot of a route's context-window state.

    ``tokens`` is the next-turn input size (input + cache_read + cache_creation
    from the latest assistant ``message.usage``). ``max_tokens`` is the
    detected window cap — 200_000 by default, latched up to 1_000_000 once
    we observe a session exceeding the 200k threshold (the ``[1m]`` model
    variant doesn't carry a suffix in JSONL ``message.model``, so observed
    overflow is the only signal).
    """

    tokens: int
    max_tokens: int


# stop_reasons that signal "this assistant turn is over". Public: the
# session_monitor's sidechain end-of-turn scan (GH #44 done detection) must
# use the SAME definition the state machine idles on.
TURN_END_REASONS = frozenset({"end_turn", "stop_sequence"})
_TURN_END_REASONS = TURN_END_REASONS  # internal alias (historic name)

# GH #44: heartbeat-silence TTL for a background-agent key. Compared as
# ``_wall_now() - last_seen_wall`` (OUR wall clock, stamped at mark time) —
# JSONL event timestamps NEVER feed the TTL (codex r2 P1: ``_now()`` is
# monotonic; mixing clock bases never expires). Bounds how long a crashed /
# silent agent can hold the projected-Busy lift; a healthy agent refreshes on
# every sidechain write, so multi-hour agents stay lifted as long as no single
# internal tool call exceeds the TTL. Product value, mirrors NOTIFY_TTL_SECONDS.
BG_AGENT_TTL_SECONDS = 1800.0


@dataclass
class _BgAgent:
    """One background agent observed on a route (GH #44).

    ``last_seen_wall`` — ``_wall_now()`` at the latest mark; the TTL basis.
    ``last_event_ts`` — max JSONL event timestamp observed in the agent's
    sidechain batches (epoch; ``None`` until a parseable stamp arrives); the
    idle-qualification basis. The two clocks are deliberately separate.
    ``is_background`` — launch-evidence / post-turn-evidence provenance: True
    from ``mark_background_agent_launched`` (the async-launch tool_result) or
    a timestamp-qualified idle-path SET (post-turn writes are background by
    definition); False for keys first seen while the parent was active
    (foreground-presumed → pruned at the parent's end-of-turn).
    """

    last_seen_wall: float
    last_event_ts: float | None
    is_background: bool


# Cap on the per-route buffer of tool_result ids that arrived BEFORE their
# tool_use (out-of-order JSONL flush — GH #42). Oldest entry evicted past the
# cap; a post-eviction tool_use then leaks open (accepted residual — the cap
# exists only to bound memory, and 128 far exceeds any observed pair burst).
_EARLY_RESULTS_CAP = 128

# Runtime-state TTL for the notification_pending bit (Wave B). A product
# value, not an invariant: approval prompts are normally acted on within a
# working session; past the TTL the 🔔 silently degrades to 🟡 and the prompt
# remains discoverable via the pane / screenshot. The poller evaluates
# ``notification_pending and now - notification_set_at > NOTIFY_TTL_SECONDS``
# on EVERY tick from the snapshot, independent of side-file existence, so a
# consumed/unlinked file or a permanently-None-timestamp stream can never
# strand 🔔 past the TTL (v4 fix 2 strand-proofing).
NOTIFY_TTL_SECONDS = 1800.0


class NotificationMarkResult(Enum):
    """Outcome of ``mark_notification_pending`` — drives the CALLER's
    side-file unlink ordering (codex r4 P3 (b)):

      - COMMITTED_LIVE → generation-guarded unlink AFTER the commit.
      - REDUNDANT_TRANSCRIPT_WAITING / STALE_UNLINK → generation-guarded
        unlink (the file carries no information the runtime needs).
      - IGNORED_NO_UNLINK → no unlink (never seed an unseen route; the
        file may belong to a route the bot hasn't bound yet).
    """

    COMMITTED_LIVE = "committed-live"
    REDUNDANT_TRANSCRIPT_WAITING = "redundant-transcript-waiting"
    STALE_UNLINK = "stale-unlinked"
    IGNORED_NO_UNLINK = "ignored-no-unlink"


class NotificationClearReason(Enum):
    """Why the ``notification_pending`` bit last transitioned True→False.

    Surfaced on the snapshot as ``notification_clear_reason`` so the poller's
    decision-card keep/dismiss (Fix 3b) can distinguish a genuine resolution
    from the end-of-turn projected-Busy gap. Every True→False site stamps
    exactly one reason; a True→False with no reason is a bug.

      - ``USER`` — a genuine user turn (the unconditional transcript clear).
      - ``TOOL_RESULT`` — a strictly-newer tool_result reclaim (the buffered
        turn flushed) or out-of-order known-result reclaim.
      - ``END_OF_TURN`` — a strictly-newer authoritative end-of-turn.
      - ``TASK_NOTIFICATION`` — a strictly-newer ``<task-notification>`` user
        event (machine-initiated completion).
      - ``INVARIANT`` — pending-without-set_at corruption cleared as expired
        (also the narration text/thinking invariant-only clear, Fix 1).
      - ``PANE_RUNNING`` — the poller observed the pane RUNNING sufficiently
        after set_at (the user acted in the terminal).
      - ``TTL`` — the runtime-state TTL elapsed.
      - ``TEARDOWN`` — session reset / route teardown.
    """

    USER = "user"
    TOOL_RESULT = "tool_result"
    END_OF_TURN = "end_of_turn"
    TASK_NOTIFICATION = "task_notification"
    INVARIANT = "invariant"
    PANE_RUNNING = "pane_running"
    TTL = "ttl"
    TEARDOWN = "teardown"


# A route observed strictly above this token count must be on the 1M variant.
_CONTEXT_DETECT_1M_THRESHOLD = 200_001


@dataclass(frozen=True)
class TranscriptLifecycleEvent:
    """A transcript event normalized for state-machine ingestion.

    Lossless w.r.t. the fields the state machine reads from a raw
    ``session_monitor.TranscriptEvent``. The adapter (see
    ``transcript_event_adapter``) does the translation.
    """

    role: Literal["user", "assistant"]
    block_type: Literal["text", "thinking", "tool_use", "tool_result"]
    tool_use_id: str | None
    tool_name: str | None
    stop_reason: str | None
    # JSONL event wall-clock timestamp (epoch seconds), parsed by the adapter
    # from the ISO8601 ``TranscriptEvent.timestamp``; ``None`` on parse
    # failure. Read by the timestamp-qualified notification clears (an
    # older buffered event must not re-hide a fresh 🔔 — v4 fix 2) and the
    # GH #44 turn-stamp/qualification machinery. Defaults
    # ``None`` so pre-Wave-B constructors are unchanged.
    timestamp: float | None = None
    # GH #44 §3.7: True when this ``user`` event is a machine-initiated
    # ``<task-notification>`` envelope (stamped by the adapter via the public
    # ``response_builder.is_task_notification``). Such events re-derive with
    # preserved gates instead of running the genuine-user-turn unconditional
    # clears — and never reset background-agent tombstones.
    is_task_notification: bool = False


@dataclass(frozen=True)
class RouteRuntimeSnapshot:
    """Immutable point-in-time view of a route's runtime state.

    Surfaces consume the snapshot rather than the underlying dicts:

      - ``message_queue._upsert_activity_digest`` reads ``run_state`` and
        ``context_usage`` to render the header.
      - ``status_polling.typing_action_loop`` reads ``typing_eligible``
        to decide whether to re-emit the native typing indicator.
      - ``status_polling`` reads ``pane_idle_clear_at`` to drive the
        debounced "🟡 Busy" card clear: it arms the deadline on the first
        confirmed-idle pane observation and clears the card once
        ``now >= pane_idle_clear_at``.

    ``idle_clear_at`` vs ``pane_idle_clear_at`` — two *distinct* deadlines:
      - ``idle_clear_at`` is the run-state decay deadline. It is armed by a
        transcript end-of-turn (``_set_run_state(IDLE_RECENT)``) and drives
        the lazy ``IDLE_RECENT → IDLE_CLEARED`` decay read by the digest
        header. It has nothing to do with the pane.
      - ``pane_idle_clear_at`` is the *card-clear* debounce deadline. It is
        armed by ``status_polling`` on a confirmed-idle pane observation
        (``arm_pane_idle_clear``) and drives the visible status-card
        removal. Activity re-arms (cancels) it. They are kept separate
        because the card-clear trigger (confirmed-idle pane, not transcript
        end_turn) is distinct from run-state decay.

    Equality is by value — comparing two snapshots tells you whether
    *anything* the route observes has changed.
    """

    route: Route
    run_state: RunState
    open_tools: frozenset[str]
    waiting_on_user_tools: frozenset[str]
    context_usage: ContextUsage | None
    last_event_at: float
    idle_clear_at: float | None
    pane_idle_clear_at: float | None
    typing_eligible: bool
    status_card_visible: bool
    status_card_msg_id: int | None
    # Pane-confirmed "interactive prompt is live" bit. True only while the
    # poller has promoted an active RUNNING route to WAITING_ON_USER for a
    # buffered interactive tool_use (see ``mark_interactive_pending``).
    # Invariant: ``interactive_pending`` is True ⟺ ``run_state`` is a
    # *pane-set* WAITING_ON_USER (the deriver folds the bit into the
    # empty-``open_tools`` branch). Lower authority than the transcript.
    interactive_pending: bool
    # Notification-hook "waiting on you" bit (Wave B). Set only by
    # ``mark_notification_pending`` from a window-predicated side-file read;
    # outranks RUNNING_TOOL in the deriver (the Workflow approval case) but
    # stays below a transcript-interactive open id. ``notification_set_at``
    # is the hook-fire wall clock (``time.time()`` family — the side file's
    # ``ts``); ``notification_generation`` is the consumed record's
    # generation so the poller can dedup re-reads and unlink
    # generation-guarded. Invariant: pending ⟹ set_at is not None (a
    # violation is treated as TTL-expired by the poller and the clears).
    notification_pending: bool
    notification_set_at: float | None
    notification_generation: str | None
    # Wave C dashboard wall-clock turn stamps — the SAME ``time.time()``
    # clock as the delivery stamps (never mixed with the monotonic
    # ``last_event_at``). ``last_user_turn_at`` is written by
    # ``stamp_user_turn`` mirrored from the PRE-SEND delivery stamp seam
    # (``message_queue.set_route_user_turn_at``); ``last_assistant_turn_ended_at``
    # is written ONLY by the authoritative end-of-turn lifecycle branch from
    # the EVENT's JSONL timestamp, MAX-monotonic by event time (parent JSONL
    # is not strictly chronological under resume/rewind). Both are in-memory
    # only: after a restart they are None and the dashboard renders
    # state-only until fresh turns repopulate them (documented degradation).
    last_user_turn_at: float | None
    last_assistant_turn_ended_at: float | None
    # GH #44: the route's LIVE background-agent keys at freeze time (TTL
    # filter applied; tombstoned/expired keys excluded). Non-empty on a
    # stored-idle route ⟺ the snapshot's run_state was LIFTED to RUNNING by
    # the projection (unless a committed notification projects WAITING above
    # it). Empty tuple for routes with no background agents.
    background_agents: tuple[str, ...] = ()
    # Fix 3a: the reason the bit last transitioned True→False (None until the
    # first clear). The poller reads this on a transition tick to decide
    # whether to KEEP the decision card (the END_OF_TURN projected-Busy gap)
    # or dismiss it. Defaulted so pre-Fix-3a constructors stay valid.
    notification_clear_reason: NotificationClearReason | None = None


@dataclass
class _RouteState:
    """Mutable internal state — lives behind the route's lock."""

    run_state: RunState = RunState.IDLE_CLEARED
    open_tools: dict[str, bool] = field(default_factory=dict)
    context_usage: ContextUsage | None = None
    last_event_at: float = 0.0
    idle_clear_at: float | None = None
    # Card-clear debounce deadline (distinct from ``idle_clear_at``). Armed
    # by ``arm_pane_idle_clear`` on the first confirmed-idle pane
    # observation; the visible status card clears once ``now`` reaches it.
    # ``None`` means "not armed". Stores the *deadline* (first-observed-at +
    # IDLE_CLEAR_DELAY_SECONDS), not the first-observed timestamp.
    pane_idle_clear_at: float | None = None
    # "Cleared this idle stretch" sentinel: the card was cleared, so further
    # idle ticks are no-ops until activity re-arms (resets it). Without this
    # a second arm after the clear would re-fire and re-enqueue a
    # status_clear.
    pane_idle_cleared: bool = False
    status_card_msg_id: int | None = None
    # Lower-authority pane input to the deriver (NOT a parallel run_state).
    # Set True only by ``mark_interactive_pending`` (promote an active RUNNING
    # route to WAITING_ON_USER while an interactive tool_use is buffered in
    # JSONL); cleared by ``mark_interactive_cleared``, the branch-scoped
    # transcript reclaim in ``_apply_lifecycle_event``, and ``mark_session_reset``.
    pane_interactive_pending: bool = False
    # Notification-hook derivation input (Wave B) — see the snapshot field
    # docs. Set/cleared ONLY under the route lock (it transitions run_state
    # through the deriver). Cleared by: a ``user`` lifecycle event
    # (unconditional), a strictly-NEWER-timestamped tool_result / end-of-turn,
    # a ``<task-notification>`` user event (timestamp-qualified),
    # ``mark_notification_cleared`` (pane observed RUNNING sufficiently after
    # set_at, or the runtime TTL), ``mark_session_reset``, and route teardown.
    # A corrupt ``set_at=None`` is repaired as expired (reason INVARIANT) at the
    # next observation. PLAIN assistant text/thinking narration does NOT clear
    # the bit — only the set_at-None invariant repair fires on a narration block
    # — so a Workflow approval 🔔 survives the agent's own running narration.
    notification_pending: bool = False
    notification_set_at: float | None = None
    notification_generation: str | None = None
    # Fix 3a: the reason the bit last cleared (True→False). Set by every clear
    # site; RESET to None when the bit is SET True (the 3 commit sites in
    # ``mark_notification_pending``) so a stale reason never leaks across a
    # fresh notification.
    notification_clear_reason: NotificationClearReason | None = None
    # Wave C wall-clock turn stamps (see the snapshot field docs). Written by
    # ``stamp_user_turn`` (sync side-band, pre-send mirror) and the
    # authoritative end-of-turn branch (max-monotonic by event time);
    # cleared by ``mark_session_reset`` and route teardown.
    last_user_turn_at: float | None = None
    last_assistant_turn_ended_at: float | None = None
    # Why the route is currently idle (Wave A). "transcript" = the assistant's
    # authoritative end-of-turn; "pane" = a pane-idle reconciliation cleared an
    # ACTIVE route (the only resurrectable flavor — sidechain activity is
    # positive proof such a clear was false). None = not idle / no provenance
    # (reset whenever the route leaves idle, on ``mark_session_reset``, and on
    # teardown). A pane clear observed on an ALREADY-idle route preserves the
    # existing value — it reconciled nothing, so it carries no authority.
    idle_source: Literal["transcript", "pane"] | None = None
    # Tools MOVED out of ``open_tools`` by a pane-idle reconciliation (Wave A).
    # The pane has lower authority than the transcript, so the clear must not
    # destroy tool identity: ``mark_background_agent_activity`` restores the stash on
    # resurrection, and a transcript tool_result for a suspended id
    # restores+closes it through the normal pairing path. Dropped on
    # authoritative end-of-turn / user lifecycle event / ``mark_inbound_sent``
    # / ``mark_session_reset`` / route teardown. In-memory only — restart
    # recovery stays ``parse_pending_tools_from_jsonl`` + ``seed_open_tools``.
    suspended_tools: dict[str, bool] = field(default_factory=dict)
    # tool_result ids observed BEFORE their tool_use (out-of-order JSONL
    # flush — GH #42), mapped to the result event's JSONL timestamp (None on
    # parse failure). A later tool_use for a recorded id is treated as
    # already-closed: it never opens a slot (an open slot nothing closes
    # blocks the end-of-turn idle gate forever), and the STORED timestamp —
    # never the tool_use event's own — drives the ts-qualified notification
    # clear. Insertion-ordered, bounded by ``_EARLY_RESULTS_CAP`` (oldest
    # evicted). Deliberately NEVER cleared by lifecycle events: an end_turn
    # can straddle a pair, and ``toolu_*`` ids are unique so stale entries
    # can never swallow a future pair. Dropped on ``mark_session_reset`` and
    # route teardown.
    early_tool_results: dict[str, float | None] = field(default_factory=dict)
    # GH #44: background agents observed on this route (normalized key →
    # record; see ``_BgAgent``) and the done-tombstone set. The keys are a
    # PROJECTION input only — no mutator transitions ``run_state`` on their
    # account; ``_build_snapshot`` lifts a stored-idle route to a visible
    # RUNNING while a live key exists. Foreground-presumed keys
    # (``is_background=False``) are pruned at the authoritative end-of-turn;
    # background keys clear via done / TTL / teardown. Tombstones block
    # re-recording a completed key (a trailing sidechain flush must not
    # re-lift) and reset only on a GENUINE user turn (never the
    # task-notification user event that created them) or teardown.
    background_agents: dict[str, _BgAgent] = field(default_factory=dict)
    background_agents_done: set[str] = field(default_factory=set)
    seen: bool = False  # have we ever observed this route?
    # Cached frozensets — invalidated on any open_tools mutation.
    # Most snapshots happen with no open tools (idle route), so the
    # cache pays off heavily on pure read traffic.
    _open_tools_cache: frozenset[str] | None = None
    _waiting_tools_cache: frozenset[str] | None = None

    def invalidate_tool_cache(self) -> None:
        self._open_tools_cache = None
        self._waiting_tools_cache = None

    def open_tools_frozen(self) -> frozenset[str]:
        if self._open_tools_cache is None:
            self._open_tools_cache = frozenset(self.open_tools.keys())
        return self._open_tools_cache

    def waiting_tools_frozen(self) -> frozenset[str]:
        if self._waiting_tools_cache is None:
            self._waiting_tools_cache = frozenset(
                tid for tid, interactive in self.open_tools.items() if interactive
            )
        return self._waiting_tools_cache


# Per-route state + lock maps. Keys are ``Route`` tuples; the
# tuple itself is the natural identity, so we use a plain dict (not a
# defaultdict) and lazy-init under ``_lock_for_route``.
_state: dict[Route, _RouteState] = {}
_locks: dict[Route, asyncio.Lock] = {}


# ── lock helpers ────────────────────────────────────────────────────────


def _lock_for_route(route: Route) -> asyncio.Lock:
    """Return (lazy-creating) the lock that serialises ``route``'s mutations.

    ``dict.setdefault`` is atomic under the GIL, so the lock is unique per
    route even if two tasks race on first observation. The cost of the
    discarded ``asyncio.Lock()`` on the losing task is a single object
    allocation — negligible.
    """
    lock = _locks.get(route)
    if lock is None:
        lock = _locks.setdefault(route, asyncio.Lock())
    return lock


def _state_for_route(route: Route) -> _RouteState:
    """Return (lazy-creating) the mutable state for ``route``.

    Must only be called from inside the route's lock.
    """
    st = _state.get(route)
    if st is None:
        st = _RouteState()
        _state[route] = st
    return st


# ── snapshot helpers ────────────────────────────────────────────────────


def _now() -> float:
    return time.monotonic()


def _wall_now() -> float:
    """Wall clock for the GH #44 background-agent TTL (injectable in tests).

    Separate from the monotonic ``_now()`` on purpose: ``last_seen_wall`` is
    stamped with THIS clock at mark time and compared with THIS clock at
    projection time — JSONL event timestamps never enter the TTL math."""
    return time.time()


def _live_background_keys(
    st: _RouteState, now_wall: float | None = None
) -> tuple[str, ...]:
    """The route's live (non-expired) background-agent keys.

    The SINGLE TTL-filtered liveness helper — used by the snapshot projection
    AND the §3.6 notification-commit check (codex r3 P3-2: never the raw
    dict). Read-only; expiry DELETION happens in the marks' step-0
    (expire-before-classify) so an expired key must re-pass full NEW
    qualification.
    """
    if not st.background_agents:
        return ()
    now = _wall_now() if now_wall is None else now_wall
    return tuple(
        k
        for k, rec in st.background_agents.items()
        if now - rec.last_seen_wall < BG_AGENT_TTL_SECONDS
    )


def _expire_background_agents_in_place(st: _RouteState) -> None:
    """Step 0 of the background marks: DELETE expired records before
    NEW/EXISTING classification (hermes r2 P1-3 — a late ``None``-timestamp
    batch must not refresh a corpse past the idle qualification)."""
    if not st.background_agents:
        return
    now = _wall_now()
    for k in [
        k
        for k, rec in st.background_agents.items()
        if now - rec.last_seen_wall >= BG_AGENT_TTL_SECONDS
    ]:
        del st.background_agents[k]


def _projected_run_state(st: _RouteState, live_bg: tuple[str, ...]) -> RunState:
    """The GH #44 snapshot-time projection — the ONLY place the
    background-agent lift exists.

    The STORED ``run_state`` is never mutated on a background agent's
    account; every snapshot path projects through here so consumers see one
    consistent visible state (typing loop, digest repaint dedup, dashboard):

      1. stored not-idle → stored (WAITING/RUNNING/RUNNING_TOOL untouched).
      2. stored idle + committed ``notification_pending`` → WAITING_ON_USER
         (user-action-needed outranks machine-busy — §3.6).
      3. stored idle + ≥1 live background key → RUNNING (the lift).
      4. otherwise → stored idle.
    """
    if st.run_state not in (RunState.IDLE_RECENT, RunState.IDLE_CLEARED):
        return st.run_state
    if st.notification_pending:
        return RunState.WAITING_ON_USER
    if live_bg:
        return RunState.RUNNING
    return st.run_state


def _state_from_open_tools(
    open_tools: dict[str, bool],
    *,
    pane_interactive_pending: bool = False,
    notification_pending: bool = False,
) -> RunState:
    """Derive the run state from the open-tool set + the two lower bits.

    Exact precedence (plan v3 B1a — top wins):

      1. A transcript-interactive open id → WAITING_ON_USER (both bits
         ignored — the transcript is strictly above them).
      2. ``notification_pending`` → WAITING_ON_USER over ANY ``open_tools``
         (including non-interactive — the Workflow approval case) or empty.
      3. ``pane_interactive_pending`` with EMPTY ``open_tools`` →
         WAITING_ON_USER (a buffered interactive ``tool_use``).
      4. Non-interactive ``open_tools`` non-empty → RUNNING_TOOL.
      5. Empty + active → RUNNING.

    Both keywords default False so every existing positional caller is
    byte-identical; the two bits CLEAR independently (each by its own rules).
    """
    if any(open_tools.values()):
        return RunState.WAITING_ON_USER
    if notification_pending:
        return RunState.WAITING_ON_USER
    if not open_tools:
        return (
            RunState.WAITING_ON_USER if pane_interactive_pending else RunState.RUNNING
        )
    return RunState.RUNNING_TOOL


def _derived_state(st: _RouteState) -> RunState:
    """Derive the active run state from ``st``'s open tools + both bits."""
    return _state_from_open_tools(
        st.open_tools,
        pane_interactive_pending=st.pane_interactive_pending,
        notification_pending=st.notification_pending,
    )


def _clear_notification_in_place(
    st: _RouteState, *, reason: NotificationClearReason
) -> None:
    st.notification_pending = False
    st.notification_set_at = None
    st.notification_generation = None
    # Fix 3a: stamp WHY the bit cleared so the poller's decision-card
    # keep/dismiss can read it off the snapshot.
    st.notification_clear_reason = reason


def _maybe_clear_notification_by_ts(
    st: _RouteState, event_ts: float | None, *, reason: NotificationClearReason
) -> None:
    """Timestamp-qualified notification clear (v4 fix 2).

    Clears the bit ONLY when the transcript event's wall-clock timestamp is
    strictly newer than ``notification_set_at`` — an older or ``None``
    timestamp PRESERVES it (a monitor running behind must not let buffered
    pre-notification JSONL re-hide a fresh wait). A pending bit without a
    set_at violates the invariant and is treated as expired (codex r4 P3 (a))
    — the set_at-None path clears with ``INVARIANT``, NOT the caller's branch
    reason; the ts-newer path stamps the caller's ``reason`` (Fix 3a).
    """
    if not st.notification_pending:
        return
    if st.notification_set_at is None:
        _clear_notification_in_place(st, reason=NotificationClearReason.INVARIANT)
        return
    if event_ts is not None and event_ts > st.notification_set_at:
        _clear_notification_in_place(st, reason=reason)


def _clear_notification_if_setat_invalid(st: _RouteState) -> None:
    """Fix 1: narration (assistant text/thinking) must NOT causally clear 🔔.

    A Workflow blocked on an approval gate narrates WHILE blocked, and the
    buffered-flush JSONL timestamp is not a causal-order signal vs the gate —
    so a newer narration timestamp must NOT clear the wait. The ONLY clear a
    narration block still performs is the invariant repair: a pending bit
    with a corrupt ``None`` set_at is treated as expired (reason=INVARIANT),
    matching ``_maybe_clear_notification_by_ts``'s set_at-None path. Otherwise
    the bit is preserved and ``notification_clear_reason`` stays untouched.
    """
    if st.notification_pending and st.notification_set_at is None:
        _clear_notification_in_place(st, reason=NotificationClearReason.INVARIANT)


def _build_snapshot(route: Route, st: _RouteState) -> RouteRuntimeSnapshot:
    """The SINGLE snapshot constructor — every read path (``_freeze`` under
    a lock, the lock-free ``snapshot()``, mutator returns, lazy-decay reads)
    builds through here so the GH #44 projection can never drift between
    paths (hermes r2 P3-1)."""
    live_bg = _live_background_keys(st)
    projected = _projected_run_state(st, live_bg)
    return RouteRuntimeSnapshot(
        route=route,
        run_state=projected,
        open_tools=st.open_tools_frozen(),
        waiting_on_user_tools=st.waiting_tools_frozen(),
        context_usage=st.context_usage,
        last_event_at=st.last_event_at,
        idle_clear_at=st.idle_clear_at,
        pane_idle_clear_at=st.pane_idle_clear_at,
        typing_eligible=projected in (RunState.RUNNING, RunState.RUNNING_TOOL),
        status_card_visible=st.status_card_msg_id is not None,
        status_card_msg_id=st.status_card_msg_id,
        interactive_pending=st.pane_interactive_pending,
        notification_pending=st.notification_pending,
        notification_set_at=st.notification_set_at,
        notification_generation=st.notification_generation,
        notification_clear_reason=st.notification_clear_reason,
        last_user_turn_at=st.last_user_turn_at,
        last_assistant_turn_ended_at=st.last_assistant_turn_ended_at,
        background_agents=live_bg,
    )


def _freeze(route: Route, st: _RouteState) -> RouteRuntimeSnapshot:
    """Snapshot ``st`` under the route's lock. Must be called with the
    lock held."""
    return _build_snapshot(route, st)


def _default_snapshot(route: Route) -> RouteRuntimeSnapshot:
    """Snapshot of an unknown route — used by ``snapshot()`` for routes
    that have never been observed.

    Unknown routes default to ``IDLE_CLEARED`` (treat-as-idle): a surface
    asking about a route the runtime has never seen is best off rendering
    "no busy state" rather than fabricating one. Pure observation, no commit.
    """
    return RouteRuntimeSnapshot(
        route=route,
        run_state=RunState.IDLE_CLEARED,
        open_tools=frozenset(),
        waiting_on_user_tools=frozenset(),
        context_usage=None,
        last_event_at=0.0,
        idle_clear_at=None,
        pane_idle_clear_at=None,
        typing_eligible=False,
        status_card_visible=False,
        status_card_msg_id=None,
        interactive_pending=False,
        notification_pending=False,
        notification_set_at=None,
        notification_generation=None,
        notification_clear_reason=None,
        last_user_turn_at=None,
        last_assistant_turn_ended_at=None,
    )


# ── pure transitions (callers hold the lock) ────────────────────────────


def _rearm_pane_idle_in_place(st: _RouteState) -> None:
    """Cancel any pending / completed pane-idle card-clear on real activity.

    Real activity on a route MUST cancel a pending clear and reset the
    "already cleared this stretch" sentinel so the next confirmed-idle pane
    observation re-arms from scratch — this is the c313657 guard: a
    sub-agent / quick-tool turn that finishes between two pane scrapes must
    not leave the route stuck having cleared too early.

    Called unconditionally from ``ingest_transcript_event`` /
    ``mark_inbound_sent`` (even on same-state refreshes) so *every* activity
    event re-arms, not only state changes.
    """
    st.pane_idle_clear_at = None
    st.pane_idle_cleared = False


def _set_run_state(st: _RouteState, new: RunState) -> None:
    """Commit a run-state change. Updates ``idle_clear_at`` when entering
    IDLE_RECENT so the lazy decay rule has a definite deadline."""
    st.last_event_at = _now()
    if new is RunState.IDLE_RECENT:
        st.idle_clear_at = st.last_event_at + IDLE_CLEAR_DELAY_SECONDS
    elif new in (RunState.RUNNING, RunState.RUNNING_TOOL, RunState.WAITING_ON_USER):
        # Active run states clear any pending idle deadline — and the idle
        # provenance: leaving idle resets ``idle_source`` (the entering
        # transition records the new one when the route next idles).
        st.idle_clear_at = None
        st.idle_source = None
    elif new is RunState.IDLE_CLEARED:
        st.idle_clear_at = None
    st.run_state = new
    st.seen = True


def _apply_lifecycle_event(
    st: _RouteState, event: TranscriptLifecycleEvent, route: Route | None = None
) -> bool:
    """Run the §2.2.1 transition table on the route's state.

    Operates on ``_RouteState`` without async (the lock is the caller's
    responsibility). ``route`` is log context only — never mutated.

    Returns whether the event counts as ACTIVITY for the pane-idle re-arm
    (``ingest_transcript_event`` honors it). True for every event except the
    terminal matched-early ``tool_use`` (GH #42): a tool_use whose
    tool_result already passed, landing on an already-idled route, is
    historical — re-arming off it would cancel a legitimately armed
    card-clear deadline (codex r2 P2).
    """
    st.seen = True

    role = event.role
    block = event.block_type
    stop_reason = event.stop_reason

    # tool_use: open the tool. is_interactive bit travels with the id so
    # parallel turns settle correctly when each tool_result lands.
    if role == "assistant" and block == "tool_use" and event.tool_use_id:
        if event.tool_use_id in st.early_tool_results:
            # Out-of-order pair (GH #42): the tool_result for this id already
            # arrived — the pair is closed; never open the slot (an open slot
            # nothing closes blocks the end-of-turn idle gate forever).
            result_ts = st.early_tool_results.pop(event.tool_use_id)
            if (
                st.run_state in (RunState.IDLE_RECENT, RunState.IDLE_CLEARED)
                and not st.open_tools
            ):
                # An end-of-turn straddled the pair and already idled the
                # route: preserve the idle state, deadlines and provenance —
                # a re-derive here would revive the idled route (hermes r1
                # P1). Historical, not activity (no pane-idle re-arm).
                return False
            # Active route: the known-tool_result reclaim side effects,
            # driven by the STORED result timestamp — never this event's
            # own (codex r1 P1; conservative: a clear can only ride a
            # timestamp the result line actually carried).
            st.pane_interactive_pending = False
            _maybe_clear_notification_by_ts(
                st, result_ts, reason=NotificationClearReason.TOOL_RESULT
            )
            _set_run_state(st, _derived_state(st))
            return True
        is_interactive = bool(
            event.tool_name and event.tool_name in INTERACTIVE_TOOL_NAMES
        )
        st.open_tools[event.tool_use_id] = is_interactive
        st.invalidate_tool_cache()
        # Transcript reclaim: the buffered turn flushed. An interactive
        # tool_use now opens the id (→ WAITING from open_tools); a
        # non-interactive one → RUNNING_TOOL. Either way the pane bit is
        # superseded by the transcript — zero it before deriving. The
        # notification bit clears only if the event is strictly NEWER than
        # the hook fire (an older buffered Workflow tool_use must keep 🔔).
        st.pane_interactive_pending = False
        _maybe_clear_notification_by_ts(
            st, event.timestamp, reason=NotificationClearReason.TOOL_RESULT
        )
        _set_run_state(st, _derived_state(st))
        return True

    # tool_result: close the slot if known. Stale ids (e.g. pre-startup
    # tools we never saw the tool_use for) are recorded as early results
    # (out-of-order JSONL — GH #42) but otherwise ignored. Role is not
    # checked because transcript_parser flips tool_result role to
    # ``assistant`` for rendering, while the JSONL envelope is
    # role="user" — block_type + tool_use_id are already specific
    # enough.
    if block == "tool_result" and event.tool_use_id:
        if event.tool_use_id in st.suspended_tools:
            # A false pane clear stashed this id (Wave A). Restore it so the
            # normal pairing below closes it — the late parent tool_result is
            # NOT "unknown" after a pane-idle reconciliation.
            st.open_tools[event.tool_use_id] = st.suspended_tools.pop(event.tool_use_id)
            st.invalidate_tool_cache()
        if event.tool_use_id not in st.open_tools:
            # Unknown id: stale/pre-startup — or the tool_result half of an
            # out-of-order pair whose tool_use is still in flight (GH #42).
            # Record it so the late tool_use is treated as already-closed.
            # Run-state, the pane bit AND the notification bit are all
            # preserved — an unknown tool_result is not proof of resumption
            # and must not strand or re-hide a WAITING.
            if event.tool_use_id in st.early_tool_results:
                logger.debug(
                    "early_tool_result duplicate route=%s id=%s",
                    route,
                    event.tool_use_id,
                )
            st.early_tool_results[event.tool_use_id] = event.timestamp
            while len(st.early_tool_results) > _EARLY_RESULTS_CAP:
                evicted = next(iter(st.early_tool_results))
                st.early_tool_results.pop(evicted)
                logger.warning(
                    "early_tool_results cap (%d) evicted route=%s id=%s",
                    _EARLY_RESULTS_CAP,
                    route,
                    evicted,
                )
            return True
        st.open_tools.pop(event.tool_use_id, None)
        st.invalidate_tool_cache()
        # Transcript reclaim on a KNOWN id: the buffered turn flushed. Zero
        # the pane bit before deriving; the notification bit clears only on
        # a strictly newer event timestamp (v4 fix 2).
        st.pane_interactive_pending = False
        _maybe_clear_notification_by_ts(
            st, event.timestamp, reason=NotificationClearReason.TOOL_RESULT
        )
        _set_run_state(st, _derived_state(st))
        return True

    # End-of-turn: thinking or text with end_turn / stop_sequence AND no
    # open tools → IDLE_RECENT. With open tools we stay in
    # RUNNING_TOOL / WAITING_ON_USER until the matching tool_result.
    if (
        role == "assistant"
        and block in ("text", "thinking")
        and stop_reason in _TURN_END_REASONS
        and not st.open_tools
    ):
        # Wave C: the authoritative end-of-turn is the ONLY writer of
        # ``last_assistant_turn_ended_at`` — from the EVENT's JSONL wall-clock
        # timestamp, MAX-monotonic by event time (an out-of-order older
        # end-of-turn under resume/rewind must not regress the stamp). A
        # ``None`` timestamp never updates it (no ingest-time fallback — the
        # dashboard renders state-only for that route).
        if event.timestamp is not None:
            if (
                st.last_assistant_turn_ended_at is None
                or event.timestamp > st.last_assistant_turn_ended_at
            ):
                st.last_assistant_turn_ended_at = event.timestamp
        # End-of-turn reclaims authority from the pane bit (the turn is over).
        st.pane_interactive_pending = False
        # A transcript-ended turn never resurrects its tools — drop the stash
        # and record the authoritative idle provenance.
        st.suspended_tools.clear()
        # GH #44 §3.3.4: provenance-only foreground prune. A synchronous
        # agent by definition completes before its parent's turn ends, so
        # every foreground-presumed key (no launch evidence, recorded while
        # the parent was active) is dropped here UNCONDITIONALLY — no
        # timestamp comparison (hermes r2 P2-2). ``is_background`` keys are
        # NEVER pruned (a live background agent in a silent tool call
        # spanning this end-of-turn keeps its lift — codex/hermes r2 P1);
        # they clear via sidechain end-of-turn / task-notification / TTL /
        # teardown.
        if st.background_agents:
            for k in [
                k for k, rec in st.background_agents.items() if not rec.is_background
            ]:
                del st.background_agents[k]
        # The notification bit clears only on a strictly NEWER end-of-turn;
        # an older buffered one must not re-hide the wait (v4 fix 2) — when
        # the bit survives, the route stays WAITING instead of idling.
        _maybe_clear_notification_by_ts(
            st, event.timestamp, reason=NotificationClearReason.END_OF_TURN
        )
        if st.notification_pending:
            _set_run_state(st, _derived_state(st))
            return True
        _set_run_state(st, RunState.IDLE_RECENT)
        st.idle_source = "transcript"
        return True

    # Diagnosis for the GH #42 class: a genuine end-of-turn that CANNOT idle
    # the route because ``open_tools`` is non-empty (a leak, or a genuinely
    # parallel pending tool). Mutually exclusive with the branch above;
    # bounded sample, never the whole set (hermes/codex r1).
    if (
        role == "assistant"
        and block in ("text", "thinking")
        and stop_reason in _TURN_END_REASONS
        and st.open_tools
    ):
        logger.info(
            "end_of_turn blocked by open tools route=%s count=%d sample=%s "
            "stop_reason=%s ts=%s",
            route,
            len(st.open_tools),
            sorted(st.open_tools)[:8],
            stop_reason,
            event.timestamp,
        )

    # Plain assistant text: at least RUNNING. Preserve RUNNING_TOOL /
    # WAITING_ON_USER (open tools / surviving bits still gate). Fix 1:
    # narration must NOT causally clear 🔔 (a blocked Workflow narrates WHILE
    # blocked) — only the invariant repair (corrupt None set_at) clears here;
    # an end-of-turn / tool_result / user / pane-running / TTL clears it. A
    # surviving bit keeps the route WAITING through its own streaming text.
    if role == "assistant" and block == "text":
        _clear_notification_if_setat_invalid(st)
        if st.run_state in (RunState.RUNNING_TOOL, RunState.WAITING_ON_USER):
            derived = _derived_state(st)
            if derived is st.run_state:
                st.last_event_at = _now()
            else:
                _set_run_state(st, derived)
            return True
        _set_run_state(st, RunState.RUNNING)
        return True

    # Assistant thinking without end-of-turn: light up if route was idle.
    # Preserve RUNNING_TOOL / WAITING_ON_USER (same re-derive as text). Fix 1:
    # like the text branch, narration thinking must NOT causally clear 🔔 —
    # only the invariant repair fires here.
    if role == "assistant" and block == "thinking":
        _clear_notification_if_setat_invalid(st)
        if not st.seen or st.run_state in (
            RunState.IDLE_CLEARED,
            RunState.IDLE_RECENT,
        ):
            _set_run_state(st, RunState.RUNNING)
            return True
        if st.run_state in (RunState.RUNNING_TOOL, RunState.WAITING_ON_USER):
            derived = _derived_state(st)
            if derived is not st.run_state:
                _set_run_state(st, derived)
                return True
        st.last_event_at = _now()
        return True

    # User non-tool_result. Two flavors (GH #44 §3.7):
    #
    # A TASK-NOTIFICATION user event is machine-initiated — the harness
    # re-invoking the parent when a background task completes — NOT the human
    # acting. It counts as activity, but its side effects diverge from a
    # genuine user turn: the pane bit, the suspended-tools stash, and the
    # background-agent tombstones are all PRESERVED (an agent finishing
    # proves nothing about a live picker or a pending approval), the
    # notification bit clears only TIMESTAMP-QUALIFIED, and the run state is
    # RE-DERIVED through the standard deriver with those preserved gates
    # intact — never a forced RUNNING (hermes r3 P2: forcing RUNNING while
    # preserving the pane bit would break the invariant
    # ``interactive_pending ⟺ pane-set WAITING_ON_USER``).
    if role == "user" and block != "tool_result" and event.is_task_notification:
        _maybe_clear_notification_by_ts(
            st, event.timestamp, reason=NotificationClearReason.TASK_NOTIFICATION
        )
        _set_run_state(st, _derived_state(st))
        return True

    # GENUINE user turn: user prompted Claude — RUNNING. A fresh user turn
    # supersedes any pane-set WAITING (the prompt was answered or replaced),
    # any pending notification (the user acted — the ONE unconditional,
    # timestamp-free transcript clear), any suspended tools (they belong
    # to the superseded turn), and the background-agent tombstones (a new
    # user turn = a new world; agent keys are unique so a reset can never
    # unmask a completed agent's key).
    if role == "user" and block != "tool_result":
        st.pane_interactive_pending = False
        _clear_notification_in_place(st, reason=NotificationClearReason.USER)
        st.suspended_tools.clear()
        st.background_agents_done.clear()
        _set_run_state(st, RunState.RUNNING)
        return True

    # Fallback: refresh activity timer without state change.
    st.last_event_at = _now()
    return True


# ── public API: ingest + snapshot ───────────────────────────────────────


async def ingest_transcript_event(
    route: Route, event: TranscriptLifecycleEvent
) -> RouteRuntimeSnapshot:
    """Apply ``event`` to ``route``'s state and return the committed snapshot.

    Locks the route, applies the transition, freezes a snapshot, releases
    the lock, and returns the committed snapshot.
    """
    lock = _lock_for_route(route)
    async with lock:
        st = _state_for_route(route)
        is_activity = _apply_lifecycle_event(st, event, route)
        if is_activity:
            # Activity re-arms the pane-idle debounce (cancels a pending
            # clear). A terminal matched-early tool_use (GH #42) reports
            # False — historical, must not poke the net.
            _rearm_pane_idle_in_place(st)
        snap = _freeze(route, st)
    return snap


def snapshot(route: Route) -> RouteRuntimeSnapshot:
    """Return the current snapshot for ``route``.

    Pure read — no lock acquisition for the common case (the underlying
    dict reads are atomic under the GIL, and the snapshot is built from
    a single ``_RouteState`` reference which is never re-assigned). Lazy
    IDLE_RECENT decay is applied so a stale ``run_state=IDLE_RECENT``
    doesn't survive past its deadline just because nothing else hit the
    route.

    Unknown routes return ``_default_snapshot`` (``IDLE_CLEARED``).
    """
    st = _state.get(route)
    if st is None:
        return _default_snapshot(route)
    # Lazy decay is observation-only.
    if (
        st.run_state is RunState.IDLE_RECENT
        and st.idle_clear_at is not None
        and _now() >= st.idle_clear_at
    ):
        st.run_state = RunState.IDLE_CLEARED
        st.idle_clear_at = None
    return _build_snapshot(route, st)


# ── public API: mark_* mutations ────────────────────────────────────────


async def mark_inbound_sent(route: Route) -> RouteRuntimeSnapshot:
    """A Telegram-originated prompt was delivered to Claude's tmux window.

    Idempotent: never downgrades RUNNING_TOOL / WAITING_ON_USER.
    Otherwise transitions to RUNNING so the typing indicator and status
    card show activity before the first JSONL event lands.

    A **pane-set** WAITING_ON_USER (``pane_interactive_pending`` True) is
    preserved here by design — exactly like a transcript-set WAITING — and
    the bit is left set: a prompt typed while a picker is still live keeps the
    route WAITING until the transcript user-turn reclaim
    (``_apply_lifecycle_event``) and/or the poller's mode-ended reconciliation
    retract it within a tick. ``mark_inbound_sent`` does not clear the bit.
    """
    lock = _lock_for_route(route)
    async with lock:
        st = _state_for_route(route)
        if st.run_state not in (
            RunState.RUNNING_TOOL,
            RunState.WAITING_ON_USER,
        ):
            _set_run_state(st, RunState.RUNNING)
        else:
            st.last_event_at = _now()
            st.seen = True
        # A new prompt reaches the route BEFORE its transcript user event —
        # stale suspended tools must not survive that gap (plan v4, hermes
        # r3 P3-1).
        st.suspended_tools.clear()
        # Inbound delivery is real activity — re-arm the pane-idle debounce.
        _rearm_pane_idle_in_place(st)
        snap = _freeze(route, st)
    return snap


async def mark_interactive_pending(route: Route) -> RouteRuntimeSnapshot:
    """Promote an active ``RUNNING`` route to ``WAITING_ON_USER`` for a
    buffered interactive ``tool_use`` (AskUserQuestion / ExitPlanMode).

    Called by ``status_polling`` from a **pane-confirmed** live picker /
    plan-approval surface while Claude Code buffers the interactive
    ``tool_use`` in JSONL (so the transcript can't open the id yet). Lower
    authority than the transcript:

      - **Promote-from-RUNNING-with-empty-open-tools-only.** No-op on an
        unseen / idle / ``RUNNING_TOOL`` / already-``WAITING_ON_USER`` route
        — it never resurrects idle, seeds an unseen route, overrides a
        transcript ``RUNNING_TOOL``, or clobbers a transcript-set ``WAITING``.
        It ALSO requires an **empty ``open_tools``**: ``RUNNING`` does not
        imply an empty open set (a ``user`` turn mid-tool sets ``RUNNING``
        while leaving a stale ``open_tools`` entry — codex/hermes P1), and
        promoting then would set the bit while the deriver, seeing the
        non-empty set, returns ``RUNNING_TOOL`` / transcript-``WAITING`` —
        breaking the invariant "``interactive_pending`` ⟺ pane-set
        ``WAITING_ON_USER`` (empty ``open_tools``)". Empty + ``RUNNING`` is
        the only state where setting the bit derives a clean pane-set
        ``WAITING_ON_USER``.
      - **Idempotent** across the 1 Hz poll ticks (already-WAITING → no-op).

    The bit (and the ``WAITING`` it derives) is retracted by the transcript
    reclaim (``_apply_lifecycle_event``), ``mark_interactive_cleared`` (the
    poller liveness reconciliation), ``mark_session_reset``, or route
    teardown (``clear_route``).
    """
    lock = _lock_for_route(route)
    async with lock:
        st = _state_for_route(route)
        if not st.seen or st.run_state is not RunState.RUNNING or st.open_tools:
            return _freeze(route, st)
        st.pane_interactive_pending = True
        _set_run_state(
            st, _state_from_open_tools(st.open_tools, pane_interactive_pending=True)
        )
        # Promotion to WAITING is real activity — cancel any stale pane-idle
        # debounce armed during the prior RUNNING/idle-pane stretch. This keeps
        # the invariant "a pane-set WAITING never carries a live pane-idle
        # deadline" inside route_runtime, independent of the status_polling
        # control-flow contract (defensive; the deadline would otherwise be
        # dormant during the live prompt anyway).
        _rearm_pane_idle_in_place(st)
        snap = _freeze(route, st)
    return snap


async def mark_interactive_cleared(route: Route) -> RouteRuntimeSnapshot:
    """Retract a pane-set ``WAITING_ON_USER`` — the SOLE programmatic clear.

    Called by ``status_polling``'s liveness reconciliation (mode-ended) and
    the in-mode tombstone when no live interactive surface remains. It clears
    only a **pane-set** WAITING (``run_state`` WAITING with no transcript
    interactive id open). Against a **transcript-set** WAITING (an interactive
    id is open in ``open_tools``) it is a run-state NO-OP — it only drops the
    already-False bit — so it can never strip a genuine transcript ``WAITING``.
    Never arms idle. Runs under the route lock.
    """
    lock = _lock_for_route(route)
    async with lock:
        st = _state_for_route(route)
        if st.run_state is RunState.WAITING_ON_USER and not any(st.open_tools.values()):
            st.pane_interactive_pending = False
            # Re-derive from the remaining inputs: a still-set notification
            # bit keeps WAITING (the two bits clear INDEPENDENTLY); otherwise
            # empty / non-interactive open set → RUNNING / RUNNING_TOOL.
            _set_run_state(st, _derived_state(st))
            if st.run_state in (RunState.RUNNING, RunState.RUNNING_TOOL):
                # GH #42 leg 2 (W2c): real WAITING → active retract —
                # re-enable the pane-idle net for a fresh stretch (see
                # mark_notification_cleared). Status-card bookkeeping
                # untouched.
                st.pane_idle_clear_at = None
                st.pane_idle_cleared = False
        else:
            # Transcript-set WAITING (interactive id open) or not-waiting →
            # drop the (already-False) bit only; do NOT transition run_state.
            st.pane_interactive_pending = False
        snap = _freeze(route, st)
    return snap


async def mark_notification_pending(
    route: Route, *, set_at: float, generation: str
) -> NotificationMarkResult:
    """Light the Notification-hook "waiting on you" bit for ``route`` (Wave B).

    Called ONLY by ``status_polling`` after a window-predicated
    ``notify_source.notification_pending_for_window`` read. ``set_at`` is the
    side file's hook-fire wall clock (``ts``); ``generation`` its re-fire
    nonce. Returns a :class:`NotificationMarkResult` that DRIVES the caller's
    side-file unlink (codex r4 P3 (b)):

      - Unseen route → ``IGNORED_NO_UNLINK`` (never seeds — the file may
        belong to a not-yet-bound route).
      - Transcript-set WAITING (interactive id open) → already 🔔:
        ``REDUNDANT_TRANSCRIPT_WAITING``, bit NOT set (no stale re-light
        after the transcript WAITING clears).
      - Active ``RUNNING`` / ``RUNNING_TOOL`` (incl. a pane-set WAITING —
        the bits are independent) → set the bit + re-arm the pane-idle
        debounce → ``COMMITTED_LIVE`` (the deriver promotes over
        ``RUNNING_TOOL`` — the Workflow approval case).
      - Idle with ``idle_source == "pane"`` AND a non-empty
        ``suspended_tools`` stash → POSITIVE LIVE PROOF the pane clear was
        false (v4 fix 1): RESTORE the stash into ``open_tools``, set the
        bit, derive → WAITING_ON_USER → ``COMMITTED_LIVE``. The SECOND
        restore path of the stash (beside the sidechain resurrection) and
        the ONE exception to "a notification never resurrects idle".
      - Idle with ``idle_source`` "transcript"/``None``, or pane-idle with
        an EMPTY stash → ``STALE_UNLINK`` (the turn genuinely ended).
    """
    lock = _lock_for_route(route)
    async with lock:
        st = _state.get(route)
        if st is None or not st.seen:
            logger.info(
                "notification_pending ignored (unseen route) route=%s set_at=%s",
                route,
                set_at,
            )
            return NotificationMarkResult.IGNORED_NO_UNLINK
        if st.run_state is RunState.WAITING_ON_USER and any(st.open_tools.values()):
            logger.info(
                "notification_pending redundant (transcript WAITING) route=%s "
                "set_at=%s",
                route,
                set_at,
            )
            return NotificationMarkResult.REDUNDANT_TRANSCRIPT_WAITING
        if st.run_state in (
            RunState.RUNNING,
            RunState.RUNNING_TOOL,
            RunState.WAITING_ON_USER,
        ):
            prior = st.run_state
            st.notification_pending = True
            st.notification_set_at = set_at
            st.notification_generation = generation
            st.notification_clear_reason = None  # Fix 3a: fresh set clears reason
            _set_run_state(st, _derived_state(st))
            _rearm_pane_idle_in_place(st)
            logger.info(
                "notification_pending committed route=%s prior=%s set_at=%s",
                route,
                prior.name,
                set_at,
            )
            return NotificationMarkResult.COMMITTED_LIVE
        # Idle: only IDLE(pane) with a live stash is resurrectable.
        if st.idle_source == "pane" and st.suspended_tools:
            restored = sorted(st.suspended_tools)[:8]
            st.open_tools.update(st.suspended_tools)
            st.suspended_tools.clear()
            st.invalidate_tool_cache()
            st.notification_pending = True
            st.notification_set_at = set_at
            st.notification_generation = generation
            st.notification_clear_reason = None  # Fix 3a: fresh set clears reason
            # _set_run_state's active branch resets idle_source/idle_clear_at.
            _set_run_state(st, _derived_state(st))
            _rearm_pane_idle_in_place(st)
            logger.info(
                "notification_pending resurrected idle(pane) route=%s set_at=%s "
                "restored=%d sample=%s",
                route,
                set_at,
                len(st.open_tools),
                restored,
            )
            return NotificationMarkResult.COMMITTED_LIVE
        # GH #44 §3.6: stored idle + a LIVE background-agent key = positive
        # live proof (the BACKGROUND agent hit the approval gate while the
        # parent's turn was over — the route is projected-Busy, not dead).
        # Liveness via the SAME TTL-filtered helper the projection uses
        # (codex r3 P3-2 — never the raw dict). Sets the bit ONLY: the
        # stored state remains idle and the projection (rule 2) reports
        # WAITING_ON_USER above the background-RUNNING lift, so 🔔 genuinely
        # outranks projected Busy (hermes r2 P1-2).
        live_bg = _live_background_keys(st)
        if live_bg:
            st.notification_pending = True
            st.notification_set_at = set_at
            st.notification_generation = generation
            st.notification_clear_reason = None  # Fix 3a: fresh set clears reason
            logger.info(
                "notification_pending committed (projected-busy bg agent) "
                "route=%s set_at=%s bg_keys=%s",
                route,
                set_at,
                live_bg[:4],
            )
            return NotificationMarkResult.COMMITTED_LIVE
        logger.info(
            "notification_pending stale (idle route) route=%s set_at=%s idle_source=%s",
            route,
            set_at,
            st.idle_source,
        )
        return NotificationMarkResult.STALE_UNLINK


async def mark_notification_cleared(
    route: Route, *, reason: NotificationClearReason = NotificationClearReason.TEARDOWN
) -> RouteRuntimeSnapshot:
    """Retract the notification bit — the poller-side programmatic clear.

    Called by ``status_polling`` when the pane is observed RUNNING at a
    capture sufficiently after ``notification_set_at`` (the user acted in
    the terminal — level + margin, not an idle→active edge; see
    ``status_polling.NOTIFY_PANE_CLEAR_MARGIN_S``; reason=PANE_RUNNING) and on
    runtime-TTL expiry (reason=TTL). The ``reason`` (Fix 3a) is stamped on the
    snapshot so the poller's decision-card dismissal can read it; the two
    production callers always pass it explicitly and the default (TEARDOWN —
    "any teardown that holds a route") only covers the bare programmatic-clear
    callers. Re-derives the run state when the bit was holding a WAITING with
    no transcript-interactive id open (a still-set pane bit keeps WAITING —
    independent clears); a transcript-set WAITING is never stripped. Never
    seeds an unseen route.
    """
    lock = _lock_for_route(route)
    async with lock:
        st = _state.get(route)
        if st is None:
            return _default_snapshot(route)
        had = st.notification_pending
        prior = st.run_state
        _clear_notification_in_place(st, reason=reason)
        if (
            had
            and st.run_state is RunState.WAITING_ON_USER
            and not any(st.open_tools.values())
        ):
            _set_run_state(st, _derived_state(st))
        if prior is RunState.WAITING_ON_USER and st.run_state in (
            RunState.RUNNING,
            RunState.RUNNING_TOOL,
        ):
            # GH #42 leg 2 (W2c): the retract returned the route to an
            # ACTIVE state — re-enable the pane-idle net for a fresh
            # stretch (a latch left closed across this transition is what
            # kept the incident route typing for 31 minutes; the latch's
            # only other resets are pane-ACTIVE observation and transcript
            # activity, neither of which occurs on an abandoned idle pane).
            # Status-card bookkeeping is untouched.
            st.pane_idle_clear_at = None
            st.pane_idle_cleared = False
        snap = _freeze(route, st)
    return snap


async def mark_background_agent_activity(
    route: Route, agent_key: str, event_ts: float | None
) -> RouteRuntimeSnapshot:
    """Sidechain JSONL activity for one agent key on this route (GH #44).

    Replaces the Wave A ``mark_subagent_activity`` heartbeat — same callers
    (the monitor's per-tick sidechain fan-out via ``bot``), now keyed per
    agent and carrying the batch's max JSONL event timestamp. Two orthogonal
    jobs under the route lock:

    **Key recording** (projection input — independent of run_state):
      - step 0: expired records are DELETED before NEW/EXISTING
        classification (expire-before-classify, hermes r2 P1-3).
      - tombstoned key → full no-op (a completed agent's trailing flush must
        not re-lift).
      - EXISTING key → refresh ``last_seen_wall`` (OUR wall clock — a
        ``None`` ``event_ts`` cannot poison the TTL) and max-update
        ``last_event_ts``.
      - NEW key, stored state ACTIVE or WAITING → record foreground-presumed
        (``is_background=False``; the end-of-turn prune disarms it — launch
        evidence upgrades it via ``mark_background_agent_launched``).
      - NEW key, stored idle → record ONLY timestamp-qualified:
        ``event_ts > last_assistant_turn_ended_at`` (both non-None, strict,
        same JSONL clock). Post-turn writes are background by definition →
        ``is_background=True``. A buffered pre-end-of-turn flush has older
        stamps and fails closed — the guard that replaced the old blanket
        transcript-idle no-op.

    **Heartbeat duties** (the ported Wave A semantics, guard-split — the
    timestamp qualification above NEVER applies here, hermes r2 P1-3):
      - ``RUNNING`` / ``RUNNING_TOOL`` → refresh ``last_event_at`` + re-arm
        the pane-idle debounce. No ``open_tools`` mutation.
      - Idle with ``idle_source == "pane"`` → RESURRECT (verbatim Wave A):
        sidechain activity is positive proof the pane clear was false;
        restores ``suspended_tools`` and re-derives.
      - Idle with ``idle_source`` "transcript"/None and ``WAITING_ON_USER``
        → no stored-state mutation (the PROJECTION does the lifting for the
        background case; a transcript-ended turn's stored state stays idle).
      - ``last_event_at`` refreshes in EVERY state (dashboard ages track
        observed sidechain activity — hermes r2 P2-3; the field is ages-only
        by contract, never 🔔 classification).

    Never seeds an unseen route. Card semantics stay NARROWED as in Wave A:
    no send-layer authority; typing/digest recover from the snapshot.
    """
    key = normalize_background_agent_key(agent_key)
    lock = _lock_for_route(route)
    async with lock:
        st = _state.get(route)
        if st is None:
            return _default_snapshot(route)
        _expire_background_agents_in_place(st)
        if key in st.background_agents_done:
            return _freeze(route, st)
        # ── key recording ────────────────────────────────────────────
        rec = st.background_agents.get(key)
        if rec is not None:
            rec.last_seen_wall = _wall_now()
            if event_ts is not None and (
                rec.last_event_ts is None or event_ts > rec.last_event_ts
            ):
                rec.last_event_ts = event_ts
        elif st.run_state not in (RunState.IDLE_RECENT, RunState.IDLE_CLEARED):
            st.background_agents[key] = _BgAgent(
                last_seen_wall=_wall_now(),
                last_event_ts=event_ts,
                is_background=False,
            )
        elif (
            event_ts is not None
            and st.last_assistant_turn_ended_at is not None
            and event_ts > st.last_assistant_turn_ended_at
        ):
            st.background_agents[key] = _BgAgent(
                last_seen_wall=_wall_now(),
                last_event_ts=event_ts,
                is_background=True,
            )
            logger.info(
                "background_agent recorded post-turn route=%s key=%s event_ts=%s",
                route,
                key,
                event_ts,
            )
        # ── heartbeat (ported Wave A) ────────────────────────────────
        st.last_event_at = _now()
        if st.run_state is RunState.WAITING_ON_USER:
            return _freeze(route, st)
        if st.run_state in (RunState.RUNNING, RunState.RUNNING_TOOL):
            _rearm_pane_idle_in_place(st)
            return _freeze(route, st)
        if st.idle_source == "pane":
            if st.suspended_tools:
                st.open_tools.update(st.suspended_tools)
                st.suspended_tools.clear()
                st.invalidate_tool_cache()
            # _set_run_state's active branch resets idle_source+idle_clear_at.
            _set_run_state(st, _state_from_open_tools(st.open_tools))
            _rearm_pane_idle_in_place(st)
        snap = _freeze(route, st)
    return snap


async def mark_background_agent_launched(
    route: Route, agent_key: str
) -> RouteRuntimeSnapshot:
    """Async-launch evidence for ``agent_key`` on this route (GH #44 §3.2a).

    Called by the bot fan-out when the parent transcript's Agent
    ``tool_result`` carries the ``agentId:`` line (a ``run_in_background``
    launch — synchronous agents never produce one). Registers/upgrades the
    key with ``is_background=True`` so the end-of-turn prune never drops it —
    the key exists with background provenance from the moment of launch,
    independent of sidechain batching (the codex/hermes r2 silent-tool-gap
    P1). An EXISTING (foreground-presumed) key is upgraded in place with its
    ``last_event_ts`` PRESERVED (hermes r3 P3-1). Tombstoned key → no-op;
    never seeds an unseen route; no stored-state mutation.
    """
    key = normalize_background_agent_key(agent_key)
    lock = _lock_for_route(route)
    async with lock:
        st = _state.get(route)
        if st is None:
            return _default_snapshot(route)
        _expire_background_agents_in_place(st)
        if key in st.background_agents_done:
            return _freeze(route, st)
        rec = st.background_agents.get(key)
        if rec is not None:
            rec.is_background = True
            rec.last_seen_wall = _wall_now()
        else:
            st.background_agents[key] = _BgAgent(
                last_seen_wall=_wall_now(),
                last_event_ts=None,
                is_background=True,
            )
        logger.info("background_agent launched route=%s key=%s", route, key)
        snap = _freeze(route, st)
    return snap


async def mark_background_agent_done(
    route: Route, agent_key: str
) -> RouteRuntimeSnapshot:
    """Positive completion for ``agent_key`` (GH #44 §3.3 paths 1-2).

    Fired on the agent's own sidechain end-of-turn and on the parent's
    ``<task-notification>`` for the key (belt and suspenders — either alone
    clears). Removes the key and TOMBSTONES it so a trailing sidechain flush
    cannot re-record and strand a false lift until the TTL. The stored
    run_state is untouched — with the last live key gone the projection
    simply stops lifting and the snapshot reports the stored idle (no
    fall-back reconstruction). Tombstones even a never-recorded key (covers
    a completion whose activity the monitor never saw). Never seeds.
    """
    key = normalize_background_agent_key(agent_key)
    lock = _lock_for_route(route)
    async with lock:
        st = _state.get(route)
        if st is None:
            return _default_snapshot(route)
        existed = st.background_agents.pop(key, None) is not None
        st.background_agents_done.add(key)
        if existed:
            logger.info("background_agent done route=%s key=%s", route, key)
        snap = _freeze(route, st)
    return snap


def _reconcile_pane_idle_in_place(st: _RouteState) -> None:
    """Reconcile a confirmed-idle route to ``IDLE_CLEARED`` in place.

    Pane snapshots have **lower authority** than transcript lifecycle
    events:

      - ``WAITING_ON_USER`` is preserved (interactive prompt is open).
      - An ACTIVE route (``RUNNING`` / ``RUNNING_TOOL``) is reconciled to
        ``IDLE_CLEARED`` with ``idle_source="pane"``; its open tools are
        MOVED into the ``suspended_tools`` stash (not dropped) so a later
        sidechain resurrection / transcript tool_result can restore them.
      - An already-idle route stays ``IDLE_CLEARED`` but PRESERVES its
        ``idle_source`` (the pane clear reconciled nothing, so it must not
        overwrite "transcript" and open a false resurrection path).

    Shared by ``mark_pane_idle`` (immediate reconciliation seam) and
    ``commit_pane_idle_clear`` (the debounced production card-clear) so
    both apply identical reconciliation. Caller holds the route's lock.
    """
    if st.run_state is RunState.WAITING_ON_USER:
        return
    was_active = st.run_state in (RunState.RUNNING, RunState.RUNNING_TOOL)
    if was_active and st.open_tools:
        st.suspended_tools.update(st.open_tools)
    st.open_tools.clear()
    st.invalidate_tool_cache()
    _set_run_state(st, RunState.IDLE_CLEARED)
    if was_active:
        st.idle_source = "pane"


async def mark_pane_idle(route: Route) -> RouteRuntimeSnapshot:
    """Pane has been confirmed idle — reconcile immediately (no debounce).

    Reconciliation-only — pane snapshots have **lower authority** than
    transcript lifecycle events:

      - ``WAITING_ON_USER`` is preserved (interactive prompt is open).
      - Otherwise drops any lingering open tools and transitions to
        ``IDLE_CLEARED``.

    This is the *immediate* clear; the production status-card path uses the
    debounced ``arm_pane_idle_clear`` / ``pane_idle_clear_due`` /
    ``commit_pane_idle_clear`` triad instead. Retained as a direct
    reconciliation seam (and exercised by the route_runtime tests).
    """
    lock = _lock_for_route(route)
    async with lock:
        st = _state_for_route(route)
        _reconcile_pane_idle_in_place(st)
        snap = _freeze(route, st)
    return snap


# ── debounced pane-idle card-clear (route_runtime owns the timer) ────────
#
# The deadline lives here so the card-clear and the run-state reconciliation
# share a single source of truth, and so ``now`` can be injected for
# deterministic tests. ``status_polling`` arms it on a confirmed-idle pane,
# polls ``pane_idle_clear_due``, and commits via ``commit_pane_idle_clear``.
#
#   - missing arm          → ``arm_pane_idle_clear`` sets ``pane_idle_clear_at``
#   - waiting out the delay → ``pane_idle_clear_due`` returns False
#   - delay elapsed         → ``pane_idle_clear_due`` returns True; the caller
#                             then runs ``commit_pane_idle_clear``
#   - already cleared       → ``pane_idle_cleared`` sentinel; arm is a no-op
#   - activity re-arms      → ``_rearm_pane_idle_in_place`` resets both


def arm_pane_idle_clear(route: Route, *, now: float) -> RouteRuntimeSnapshot:
    """Arm the debounced card-clear deadline on the first confirmed-idle
    pane observation.

    Idempotent and synchronous (a read-side bookkeeping write — no
    run-state transition, like ``mark_status_card_published``):

      - No-op if the route was already cleared this idle stretch
        (``pane_idle_cleared``).
      - No-op if the deadline is already armed (don't push it forward — the
        arm only fires once per stretch).
      - Otherwise sets ``pane_idle_clear_at = now + IDLE_CLEAR_DELAY_SECONDS``.

    ``now`` is injected (no hidden ``time.monotonic()`` call inside the
    transition) so the deadline is deterministically testable. Pass the
    same monotonic clock ``status_polling`` reads.
    """
    st = _state.get(route)
    if st is None:
        st = _RouteState()
        _state[route] = st
    if st.pane_idle_cleared:
        return snapshot(route)
    if st.pane_idle_clear_at is None:
        st.pane_idle_clear_at = now + IDLE_CLEAR_DELAY_SECONDS
    return snapshot(route)


def pane_idle_clear_due(route: Route, *, now: float) -> bool:
    """Return True iff an armed pane-idle clear deadline has elapsed.

    Pure query (no mutation). True only when a deadline is actually armed —
    an unarmed or already-cleared route is never "due". ``now`` is injected
    so callers (``update_status_message`` and ``_process_idle_clear_only``)
    decide whether to commit using the same clock they armed with.
    """
    st = _state.get(route)
    if st is None or st.pane_idle_clear_at is None:
        return False
    return now >= st.pane_idle_clear_at


async def commit_pane_idle_clear(route: Route, *, now: float) -> bool:
    """Perform the debounced card clear once the deadline is due.

    Returns ``True`` iff the armed deadline was still due at lock time — i.e.
    the debounce *fired*: the deadline was dropped and the ``pane_idle_cleared``
    sentinel latched. ``False`` if it no-op'd (re-armed / cancelled / not yet
    due — or the route is WAITING, see below). Callers enqueue the card clear
    ONLY on ``True``. **NOTE (GH #42 leg 2, W2a):** a ``WAITING_ON_USER``
    route (transcript- or pane-set; pane has lower authority) is NOT
    reconcilable — the commit returns ``False`` WITHOUT consuming the deadline
    and WITHOUT latching, so the net still works when the WAITING is later
    retracted (TTL / mode-ended). The pre-fix consume+latch here permanently
    disarmed the 2026-06-11 incident route. Only ``RUNNING`` /
    ``RUNNING_TOOL`` are reconciled to ``IDLE_CLEARED``.

    TOCTOU re-validation (Codex 8b P1): the caller checks
    ``pane_idle_clear_due`` WITHOUT the lock, then ``await``\\s this. Between
    that check and our lock acquisition, a transcript ``ingest_transcript_event``
    or ``mark_inbound_sent`` may have re-armed — ``_rearm_pane_idle_in_place``
    sets ``pane_idle_clear_at=None`` (cancel) or a future deadline. Committing
    a now-stale clear would blank the "🟡 Busy" card mid-turn after fresh
    activity (which may land the route in ``WAITING_ON_USER`` / ``IDLE_RECENT``,
    not only ``RUNNING`` — so the run-state alone cannot tell the caller whether
    a clear happened; this explicit bool can). We re-check the SAME armed-and-due
    predicate under the lock with the caller's ``now`` and return ``False``
    (no clear) if the deadline is no longer armed or not yet due.
    This makes the card-clear strictly race-free: the deadline is
    re-validated under the lock before reconciling, so a clear can never
    blank a card that fresh activity already re-armed.
    """
    lock = _lock_for_route(route)
    async with lock:
        st = _state_for_route(route)
        if st.pane_idle_clear_at is None or now < st.pane_idle_clear_at:
            # Re-armed or cancelled since the lockless due-check — do not clear.
            return False
        if st.run_state is RunState.WAITING_ON_USER:
            # GH #42 leg 2 (W2a): a WAITING route is not reconcilable (the
            # pane has lower authority) — leave the deadline armed and the
            # latch OPEN instead of consuming them, so the net still works
            # when the WAITING is later retracted (TTL / mode-ended). The
            # pre-fix consume+latch here is what permanently disarmed the
            # incident route. The poller additionally skips arm/commit
            # while WAITING (W2b); the visible-card clear is decoupled.
            return False
        prior = st.run_state
        _reconcile_pane_idle_in_place(st)
        st.pane_idle_clear_at = None
        st.pane_idle_cleared = True
        logger.info(
            "pane_idle_clear committed route=%s prior=%s stash=%d",
            route,
            prior.name,
            len(st.suspended_tools),
        )
    return True


def reset_pane_idle_clear(route: Route) -> None:
    """Cancel a pending / completed pane-idle clear from a *pane* signal.

    Synchronous side-band write (no run-state transition — like
    ``arm_pane_idle_clear``). Called by ``status_polling`` when a pane
    scrape shows the route running again: a fresh idle stretch must re-arm
    from scratch rather than fire on a deadline left over from a previous
    stretch. Distinct from ``_rearm_pane_idle_in_place`` (the
    transcript/inbound re-arm) only in that this is the public pane-driven
    seam.
    """
    st = _state.get(route)
    if st is None:
        return
    st.pane_idle_clear_at = None
    st.pane_idle_cleared = False


async def mark_session_reset(route: Route) -> RouteRuntimeSnapshot:
    """Session_id rotated under this route (e.g. ``/clear`` mid-stream).

    Drops in-flight ``open_tools`` (they belong to the dead session),
    drops the context_usage cache, and resets to ``IDLE_CLEARED``.
    Preserves the ``status_card_msg_id`` — message_queue may still want
    to edit the same card to render the new session's first reply.
    """
    lock = _lock_for_route(route)
    async with lock:
        st = _state_for_route(route)
        st.open_tools.clear()
        st.invalidate_tool_cache()
        st.context_usage = None
        # Fresh session — drop any pending/completed pane-idle debounce so
        # the new session's first idle stretch arms from scratch.
        _rearm_pane_idle_in_place(st)
        # The old session's interactive prompt (if any) is gone with it —
        # as are any pending notification, suspended tools, early tool
        # results (GH #42 — they reference the dead session's ids), and
        # the idle provenance.
        st.pane_interactive_pending = False
        _clear_notification_in_place(st, reason=NotificationClearReason.TEARDOWN)
        st.suspended_tools.clear()
        st.early_tool_results.clear()
        # GH #44: the dead session's background agents (and their
        # tombstones) go with it.
        st.background_agents.clear()
        st.background_agents_done.clear()
        # The dead session's turn stamps go with it — the dashboard's
        # unanswered-turn derivation must not survive a /clear.
        st.last_user_turn_at = None
        st.last_assistant_turn_ended_at = None
        _set_run_state(st, RunState.IDLE_CLEARED)
        st.idle_source = None
        snap = _freeze(route, st)
    return snap


def stamp_user_turn(route: Route, ts: float) -> None:
    """Record the wall-clock instant a user turn was DELIVERED into tmux.

    Wave C: the route_runtime mirror of the pre-send delivery stamp —
    ``message_queue.set_route_user_turn_at`` calls this with the SAME
    ``time.time()`` value it stores, so the dashboard's unanswered-turn
    derivation (``last_assistant_turn_ended_at > last_user_turn_at``)
    compares two stamps on one clock. Pre-send by construction (the caller
    stamps immediately before ``send_to_window``), NOT ``mark_inbound_sent``
    (post-send — a fast transcript could land the end-of-turn between the
    delivery and the stamp).

    Synchronous side-band write (no run-state transition, like
    ``mark_status_card_published``): it never marks the route ``seen`` and
    never fabricates activity.
    """
    st = _state.get(route)
    if st is None:
        st = _RouteState()
        _state[route] = st
    st.last_user_turn_at = ts


def mark_status_card_published(route: Route, msg_id: int) -> None:
    """message_queue published / edited a status card for this route.

    Synchronous: this is bookkeeping for the read-side
    ``snapshot.status_card_visible`` flag, not a state-machine
    transition. message_queue is the authoritative writer for the status
    card; the runtime only records the id for the pull-only ``snapshot``
    read (there is no push/observer channel).
    """
    st = _state.get(route)
    if st is None:
        st = _RouteState()
        _state[route] = st
    st.status_card_msg_id = msg_id


def mark_status_card_cleared(route: Route) -> None:
    """message_queue cleared the status card for this route.

    Counterpart to ``mark_status_card_published`` — the same pull-only
    bookkeeping, not a state-machine transition.
    """
    st = _state.get(route)
    if st is not None:
        st.status_card_msg_id = None


def update_context_usage(route: Route, tokens: int | None, model: str | None) -> None:
    """Cache the latest ``ContextUsage`` for a route.

    ``None`` or non-positive tokens drops the entry (used after ``/clear``
    when there's no assistant turn yet). The 1M cap latches once observed
    (a 200k session can't legitimately exceed its cap; below threshold
    defaults to 200k). ``model`` is accepted for future use (logging /
    explicit cap overrides) but the cap is derived from observed tokens,
    since JSONL doesn't carry the ``[1m]`` suffix.
    """
    st = _state.get(route)
    if tokens is None or tokens <= 0:
        if st is not None:
            st.context_usage = None
        return
    if st is None:
        st = _RouteState()
        _state[route] = st
    prior_max = st.context_usage.max_tokens if st.context_usage else 200_000
    if tokens >= _CONTEXT_DETECT_1M_THRESHOLD or prior_max >= 1_000_000:
        max_tokens = 1_000_000
    else:
        max_tokens = 200_000
    st.context_usage = ContextUsage(tokens=tokens, max_tokens=max_tokens)
    _ = model  # accepted for future use; cap derives from observed tokens


def seed_open_tools(route: Route, tools: dict[str, bool]) -> None:
    """Replay startup-recovered open tools onto a route.

    No-op when ``tools`` is empty (default IDLE_CLEARED stands) or when
    the route already has live state (a real ingest landed between
    monitor warm-up and the replay walk, and live events have higher
    authority than a JSONL snapshot).
    """
    if not tools:
        return
    st = _state.get(route)
    if st is not None and st.seen:
        return
    if st is None:
        st = _RouteState()
        _state[route] = st
    st.open_tools = dict(tools)
    st.invalidate_tool_cache()
    st.run_state = _state_from_open_tools(st.open_tools)
    st.last_event_at = _now()
    st.seen = True


def parse_pending_tools_from_jsonl(jsonl_path: str) -> dict[str, bool]:
    """Scan a session's parent JSONL for tool_use entries with no tool_result.

    Returns ``{tool_use_id: is_interactive}`` for the open set, suitable for
    feeding into ``seed_open_tools``. Used at startup to recover the
    in-flight tool state lost when the bot restarts mid-turn — most acutely
    important for sub-agent ``Task`` calls, which can sit open for many
    minutes with no parent-JSONL activity to re-arm the run-state machine.

    Set-difference rather than running open-set: parent JSONL is NOT strictly
    chronological. Branch / rewind / ``--resume`` flows can lay a tool_result
    line down before its tool_use line, so a forward "pop on result" walk
    leaves phantom open tools in finished sessions. Collect all uses and all
    results in one pass, then return ``uses − results``.

    Sidechain entries (``isSidechain=true``) are skipped: they live in a
    separate JSONL but if any leak into the parent, they belong to a
    sub-agent's tool space, not the parent's.

    Malformed lines and unexpected shapes are tolerated — the parent JSONL
    is the source of truth so a few skipped lines just mean we miss a tool.
    A missed tool means the indicator stays dark until the next event;
    that's the pre-replay behavior, so this fails safely.
    """
    uses: dict[str, bool] = {}
    results: set[str] = set()
    try:
        with open(jsonl_path, encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if entry.get("isSidechain"):
                    continue
                message = entry.get("message")
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get("type")
                    if item_type == "tool_use":
                        tool_id = item.get("id")
                        if not isinstance(tool_id, str):
                            continue
                        tool_name = item.get("name")
                        is_interactive = bool(
                            isinstance(tool_name, str)
                            and tool_name in INTERACTIVE_TOOL_NAMES
                        )
                        # Don't downgrade an interactive id that appeared
                        # earlier (idempotent against duplicate tool_use
                        # lines after rewind).
                        uses[tool_id] = uses.get(tool_id, False) or is_interactive
                    elif item_type == "tool_result":
                        tool_id = item.get("tool_use_id")
                        if isinstance(tool_id, str):
                            results.add(tool_id)
    except OSError as e:
        logger.warning("replay: failed to read %s: %s", jsonl_path, e)
        return {}
    return {tid: interactive for tid, interactive in uses.items() if tid not in results}


# ── maintenance ─────────────────────────────────────────────────────────


def clear_route(route: Route) -> None:
    """Drop all state for ``route``. Called from topic teardown.

    Does NOT remove the route's lock — the lock is cheap and may be
    re-acquired immediately if the route is re-bound. Removing it would
    require coordination with any in-flight ``ingest_*`` task to avoid
    racing on a fresh lock object.
    """
    _state.pop(route, None)


def clear_routes_for_topic(user_id: int, thread_id_or_0: int) -> None:
    """Drop all per-route state for every route under ``(user_id, thread_id_or_0)``.

    route_runtime's OWN topic-teardown seam: route ownership must NOT be derived
    from ``message_queue._route_queues``. A route can carry run-state /
    ``pane_interactive_pending`` via ``mark_inbound_sent`` / JSONL replay /
    ``mark_interactive_pending`` *without ever having a message_queue worker*
    (a queue is created only when content is enqueued). A topic teardown that
    only walks ``routes_for_topic`` (queued routes) would therefore strand such
    a route's state — e.g. a pane-set ``WAITING_ON_USER`` left after the topic
    is closed / the window is gone (hermes round-2 P2). ``clear_topic_state``
    calls this so route_runtime is torn down for the WHOLE topic regardless of
    queue presence.

    Synchronous side-band write (drops state, no ``run_state`` transition — like
    ``clear_route``); keeps the locks (cheap, re-acquired on rebind). Safe under
    single-threaded asyncio: no ``await`` between reading ``_state`` and popping.
    """
    for key in [k for k in _state if k[0] == user_id and k[1] == thread_id_or_0]:
        _state.pop(key, None)


def reset_for_tests() -> None:
    """Test-only: drop all per-route state and locks.

    This is the single test-side reset seam for the run-state machine,
    the context-usage cache, the pane-idle debounce, and status-card
    visibility. ``message_queue`` (``_status_msg_info``) and
    ``interactive_ui`` keep their own reset seams for their send-layer
    caches.
    """
    _state.clear()
    _locks.clear()


__all__ = [
    "ContextUsage",
    "NOTIFY_TTL_SECONDS",
    "NotificationClearReason",
    "NotificationMarkResult",
    "Route",
    "RouteRuntimeSnapshot",
    "BG_AGENT_TTL_SECONDS",
    "RunState",
    "TURN_END_REASONS",
    "TranscriptLifecycleEvent",
    "arm_pane_idle_clear",
    "clear_route",
    "clear_routes_for_topic",
    "commit_pane_idle_clear",
    "ingest_transcript_event",
    "mark_inbound_sent",
    "mark_interactive_cleared",
    "mark_interactive_pending",
    "mark_notification_cleared",
    "mark_notification_pending",
    "mark_pane_idle",
    "mark_session_reset",
    "mark_status_card_cleared",
    "mark_status_card_published",
    "normalize_background_agent_key",
    "mark_background_agent_activity",
    "mark_background_agent_done",
    "mark_background_agent_launched",
    "pane_idle_clear_due",
    "parse_pending_tools_from_jsonl",
    "reset_for_tests",
    "reset_pane_idle_clear",
    "seed_open_tools",
    "snapshot",
    "stamp_user_turn",
    "update_context_usage",
]
