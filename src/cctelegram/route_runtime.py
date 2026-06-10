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
  - ``mark_subagent_activity`` is the Wave A sidechain keep-alive: it
    refreshes an active route like transcript activity and RESURRECTS an
    ``idle_source="pane"`` route (restoring the stash — sidechain
    activity is positive proof the pane clear was false). It never
    resurrects a transcript-idle route, never overrides
    ``WAITING_ON_USER``, and never seeds an unseen route.
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


# stop_reasons that signal "this assistant turn is over".
_TURN_END_REASONS = frozenset({"end_turn", "stop_sequence"})

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
    # destroy tool identity: ``mark_subagent_activity`` restores the stash on
    # resurrection, and a transcript tool_result for a suspended id
    # restores+closes it through the normal pairing path. Dropped on
    # authoritative end-of-turn / user lifecycle event / ``mark_inbound_sent``
    # / ``mark_session_reset`` / route teardown. In-memory only — restart
    # recovery stays ``parse_pending_tools_from_jsonl`` + ``seed_open_tools``.
    suspended_tools: dict[str, bool] = field(default_factory=dict)
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


def _state_from_open_tools(
    open_tools: dict[str, bool], *, pane_interactive_pending: bool = False
) -> RunState:
    """Derive the run state from the current open-tool set.

    Transcript is strictly above the pane bit:

      - Empty ``open_tools`` → WAITING_ON_USER iff ``pane_interactive_pending``
        (a buffered interactive ``tool_use`` the transcript can't open yet),
        else RUNNING (turn still in flight, no tools pending).
      - Any interactive id open → WAITING_ON_USER (transcript-set; **the pane
        bit is ignored** — it is only reachable on an empty ``open_tools``).
      - Otherwise → RUNNING_TOOL (a non-interactive tool is open; pane bit
        ignored).

    ``pane_interactive_pending`` defaults False so every existing positional
    caller (``seed_open_tools`` / the transcript reclaim branches) is
    byte-identical.
    """
    if not open_tools:
        return (
            RunState.WAITING_ON_USER if pane_interactive_pending else RunState.RUNNING
        )
    if any(open_tools.values()):
        return RunState.WAITING_ON_USER
    return RunState.RUNNING_TOOL


def _freeze(route: Route, st: _RouteState) -> RouteRuntimeSnapshot:
    """Snapshot ``st`` under the route's lock. Must be called with the
    lock held."""
    return RouteRuntimeSnapshot(
        route=route,
        run_state=st.run_state,
        open_tools=st.open_tools_frozen(),
        waiting_on_user_tools=st.waiting_tools_frozen(),
        context_usage=st.context_usage,
        last_event_at=st.last_event_at,
        idle_clear_at=st.idle_clear_at,
        pane_idle_clear_at=st.pane_idle_clear_at,
        typing_eligible=st.run_state in (RunState.RUNNING, RunState.RUNNING_TOOL),
        status_card_visible=st.status_card_msg_id is not None,
        status_card_msg_id=st.status_card_msg_id,
        interactive_pending=st.pane_interactive_pending,
    )


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


def _apply_lifecycle_event(st: _RouteState, event: TranscriptLifecycleEvent) -> None:
    """Run the §2.2.1 transition table on the route's state.

    Operates on ``_RouteState`` without async (the lock is the caller's
    responsibility).
    """
    st.seen = True

    role = event.role
    block = event.block_type
    stop_reason = event.stop_reason

    # tool_use: open the tool. is_interactive bit travels with the id so
    # parallel turns settle correctly when each tool_result lands.
    if role == "assistant" and block == "tool_use" and event.tool_use_id:
        is_interactive = bool(
            event.tool_name and event.tool_name in INTERACTIVE_TOOL_NAMES
        )
        st.open_tools[event.tool_use_id] = is_interactive
        st.invalidate_tool_cache()
        # Transcript reclaim: the buffered turn flushed. An interactive
        # tool_use now opens the id (→ WAITING from open_tools); a
        # non-interactive one → RUNNING_TOOL. Either way the pane bit is
        # superseded by the transcript — zero it before deriving so the
        # derived state comes purely from open_tools.
        st.pane_interactive_pending = False
        _set_run_state(st, _state_from_open_tools(st.open_tools))
        return

    # tool_result: close the slot if known. Stale ids (e.g. pre-startup
    # tools we never saw the tool_use for) are ignored. Role is not
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
            # Unknown id (stale / pre-startup). Preserve run_state AND the pane
            # bit — an unknown tool_result must not strand a pane-set WAITING.
            return
        st.open_tools.pop(event.tool_use_id, None)
        st.invalidate_tool_cache()
        # Transcript reclaim on a KNOWN id: the buffered turn flushed. Zero
        # the pane bit before deriving from the remaining open set.
        st.pane_interactive_pending = False
        _set_run_state(st, _state_from_open_tools(st.open_tools))
        return

    # End-of-turn: thinking or text with end_turn / stop_sequence AND no
    # open tools → IDLE_RECENT. With open tools we stay in
    # RUNNING_TOOL / WAITING_ON_USER until the matching tool_result.
    if (
        role == "assistant"
        and block in ("text", "thinking")
        and stop_reason in _TURN_END_REASONS
        and not st.open_tools
    ):
        # End-of-turn reclaims authority from the pane bit (the turn is over).
        st.pane_interactive_pending = False
        # A transcript-ended turn never resurrects its tools — drop the stash
        # and record the authoritative idle provenance.
        st.suspended_tools.clear()
        _set_run_state(st, RunState.IDLE_RECENT)
        st.idle_source = "transcript"
        return

    # Plain assistant text: at least RUNNING. Preserve RUNNING_TOOL /
    # WAITING_ON_USER (open tools still gate).
    if role == "assistant" and block == "text":
        if st.run_state in (RunState.RUNNING_TOOL, RunState.WAITING_ON_USER):
            st.last_event_at = _now()
            return
        _set_run_state(st, RunState.RUNNING)
        return

    # Assistant thinking without end-of-turn: light up if route was idle.
    # Preserve RUNNING_TOOL / WAITING_ON_USER.
    if role == "assistant" and block == "thinking":
        if not st.seen or st.run_state in (
            RunState.IDLE_CLEARED,
            RunState.IDLE_RECENT,
        ):
            _set_run_state(st, RunState.RUNNING)
            return
        st.last_event_at = _now()
        return

    # User non-tool_result: user prompted Claude — RUNNING. A fresh user turn
    # supersedes any pane-set WAITING (the prompt was answered or replaced)
    # and any suspended tools (they belong to the superseded turn).
    if role == "user" and block != "tool_result":
        st.pane_interactive_pending = False
        st.suspended_tools.clear()
        _set_run_state(st, RunState.RUNNING)
        return

    # Fallback: refresh activity timer without state change.
    st.last_event_at = _now()


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
        _apply_lifecycle_event(st, event)
        # Activity re-arms the pane-idle debounce (cancels a pending clear).
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
        return _freeze(route, st)
    return RouteRuntimeSnapshot(
        route=route,
        run_state=st.run_state,
        open_tools=st.open_tools_frozen(),
        waiting_on_user_tools=st.waiting_tools_frozen(),
        context_usage=st.context_usage,
        last_event_at=st.last_event_at,
        idle_clear_at=st.idle_clear_at,
        pane_idle_clear_at=st.pane_idle_clear_at,
        typing_eligible=st.run_state in (RunState.RUNNING, RunState.RUNNING_TOOL),
        status_card_visible=st.status_card_msg_id is not None,
        status_card_msg_id=st.status_card_msg_id,
        interactive_pending=st.pane_interactive_pending,
    )


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
            # Empty / non-interactive open set → RUNNING / RUNNING_TOOL.
            _set_run_state(st, _state_from_open_tools(st.open_tools))
        else:
            # Transcript-set WAITING (interactive id open) or not-waiting →
            # drop the (already-False) bit only; do NOT transition run_state.
            st.pane_interactive_pending = False
        snap = _freeze(route, st)
    return snap


async def mark_subagent_activity(route: Route) -> RouteRuntimeSnapshot:
    """Sidechain (sub-agent) JSONL activity observed for this route (Wave A).

    A running sub-agent writes only to its own sidechain file, so the parent
    transcript is silent and a transient confirmed-idle pane frame can falsely
    clear the route. This mutator is the keep-alive + bounded self-heal:

      - ``RUNNING`` / ``RUNNING_TOOL`` → refresh ``last_event_at`` and
        re-arm/cancel the pane-idle debounce (the same treatment as transcript
        activity). NO ``open_tools`` mutation — this is a heartbeat, not a
        lifecycle ingestion.
      - Idle with ``idle_source == "pane"`` → RESURRECT: sidechain activity is
        positive proof the pane clear was false. Restores ``suspended_tools``
        into ``open_tools`` and re-derives (``RUNNING_TOOL`` when the parent
        Agent tool was stashed, ``RUNNING`` on an empty stash), clearing the
        idle deadlines. The ONLY resurrection path for this mutator.
      - Idle with ``idle_source == "transcript"`` / ``None`` → no-op (the
        transcript has spoken; a stray late sidechain write must not resurrect
        a genuinely ended turn).
      - ``WAITING_ON_USER`` (transcript- or pane-bit-set) → never overridden.
      - Unseen route (no ``_RouteState``) → bail; never seeds.

    Card semantics are explicitly NARROWED: a status clear already enqueued
    before resurrection MAY still delete the visible Busy card —
    ``mark_subagent_activity`` has no send-layer authority and the queue is
    not generation-guarded. The card re-publishes on the next active status
    tick; ``typing_eligible`` and the digest header recover immediately from
    the snapshot.
    """
    lock = _lock_for_route(route)
    async with lock:
        st = _state.get(route)
        if st is None:
            return _default_snapshot(route)
        if st.run_state is RunState.WAITING_ON_USER:
            return _freeze(route, st)
        if st.run_state in (RunState.RUNNING, RunState.RUNNING_TOOL):
            st.last_event_at = _now()
            _rearm_pane_idle_in_place(st)
            return _freeze(route, st)
        # Idle: only a pane-sourced idle is resurrectable.
        if st.idle_source != "pane":
            return _freeze(route, st)
        if st.suspended_tools:
            st.open_tools.update(st.suspended_tools)
            st.suspended_tools.clear()
            st.invalidate_tool_cache()
        # _set_run_state's active branch resets idle_source + idle_clear_at.
        _set_run_state(st, _state_from_open_tools(st.open_tools))
        _rearm_pane_idle_in_place(st)
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
    due). Callers enqueue the card clear ONLY on ``True``. **NOTE:** ``True``
    does NOT imply ``run_state`` changed — ``_reconcile_pane_idle_in_place``
    *preserves* a ``WAITING_ON_USER`` route (transcript- or pane-set; pane has
    lower authority), so for WAITING the deadline is consumed and ``True`` is
    returned while the run-state is left untouched. Only ``RUNNING`` /
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
        _reconcile_pane_idle_in_place(st)
        st.pane_idle_clear_at = None
        st.pane_idle_cleared = True
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
        # as are any suspended tools and the idle provenance.
        st.pane_interactive_pending = False
        st.suspended_tools.clear()
        _set_run_state(st, RunState.IDLE_CLEARED)
        st.idle_source = None
        snap = _freeze(route, st)
    return snap


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
    "Route",
    "RouteRuntimeSnapshot",
    "RunState",
    "TranscriptLifecycleEvent",
    "arm_pane_idle_clear",
    "clear_route",
    "clear_routes_for_topic",
    "commit_pane_idle_clear",
    "ingest_transcript_event",
    "mark_inbound_sent",
    "mark_interactive_cleared",
    "mark_interactive_pending",
    "mark_pane_idle",
    "mark_session_reset",
    "mark_status_card_cleared",
    "mark_status_card_published",
    "mark_subagent_activity",
    "pane_idle_clear_due",
    "parse_pending_tools_from_jsonl",
    "reset_for_tests",
    "reset_pane_idle_clear",
    "seed_open_tools",
    "snapshot",
    "update_context_usage",
]
