"""Event-driven run-state machine, sourced from ``TranscriptEvent``.

Single source of truth for "what is Claude doing on this route right now".
Surfaces (status card, native typing action, activity-digest header) read
``state(route)`` instead of re-deriving busy/idle from pane scraping or
content-task ordering.

State transitions are driven by JSONL lifecycle events (``tool_use``,
``tool_result``, ``text``, ``thinking``) plus their carried ``stop_reason``,
per the §2.2.1 transition table in the 2026-05-02 plan. The pane is still
the only signal for interactive UIs that aren't JSONL-visible until they
open — that detection lives in ``status_polling`` and bypasses this module.

Idle decay is on-read (no scheduled tasks) so we don't leak ``call_later``
handles for routes that go away mid-decay.

Public surface:
  - ``RunState``
  - ``register_state_callback(cb)`` — process-lifetime registration; deduped by identity
  - ``state(route)``
  - ``context_usage(route)`` / ``update_context_usage(route, tokens, model)``
  - ``context_pct(route)`` — derived from usage, kept for the digest gate
  - ``on_transcript_event(event, routes)``
  - ``mark_inbound_sent(route)`` — prompt successfully delivered to Claude
  - ``mark_topic_broken(route)`` / ``mark_topic_recovered(route)``
  - ``clear_route(route)``
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable

from ..session_monitor import TranscriptEvent
from .interactive_ui import INTERACTIVE_TOOL_NAMES

logger = logging.getLogger(__name__)


Route = tuple[int, int, str]


class RunState(Enum):
    RUNNING = "RUNNING"
    RUNNING_TOOL = "RUNNING_TOOL"
    WAITING_ON_USER = "WAITING_ON_USER"
    IDLE_RECENT = "IDLE_RECENT"
    IDLE_CLEARED = "IDLE_CLEARED"
    BROKEN_TOPIC = "BROKEN_TOPIC"


# Seconds an IDLE_RECENT route stays "recent" before decaying to IDLE_CLEARED.
# Mirrors handlers.status_polling.IDLE_CLEAR_DELAY_SECONDS so the visible
# status-card removal and the digest header transition stay in sync.
IDLE_CLEAR_DELAY_SECONDS = 4.0

# stop_reasons that mean "this assistant turn is over"
_TURN_END_REASONS = frozenset({"end_turn", "stop_sequence"})


# Maps tool_use_id → is_interactive. The interactivity bit must travel with
# the id: parallel turns can interleave interactive (AskUserQuestion) and
# non-interactive (Bash) tools, and closing the interactive one alone must
# step the route back to RUNNING_TOOL — not leave it stuck WAITING_ON_USER.
_open_tools: dict[Route, dict[str, bool]] = {}
_run_state: dict[Route, RunState] = {}
_last_event_at: dict[Route, float] = {}


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


# Routes whose observed tokens strictly exceed 200k must be on the 1M
# variant — a 200k session can never legitimately exceed its cap. Once
# latched, stay locked there for the rest of the session. The strict
# inequality matters: real 200k sessions can sit at 195–199k just before
# auto-compact, and we MUST keep reporting them as "97% / 200k" rather than
# silently flipping the denominator to 1M and showing "20%".
_CONTEXT_DETECT_1M_THRESHOLD = 200_001

_context_usage: dict[Route, ContextUsage] = {}
# Pre-broken state remembered so a successful event after BROKEN_TOPIC can
# restore where we were rather than guessing.
_pre_broken_state: dict[Route, RunState] = {}

StateCallback = Callable[[Route, RunState, RunState], Awaitable[None]]
_state_callbacks: list[StateCallback] = []


def register_state_callback(callback: StateCallback) -> None:
    """Register a coroutine called on every state transition.

    Callback signature: ``(route, old_state, new_state)``. Multiple callbacks
    are supported; they fire in registration order. Exceptions in one do not
    prevent the next from running.

    Registrations are process-lifetime — there is no unregister. Identity
    dedupe guards against accidental double-registration on bot reload.
    """
    if callback in _state_callbacks:
        return
    _state_callbacks.append(callback)


def _state_from_open_tools(open_tools: dict[str, bool]) -> RunState:
    """Derive the run state from the current open-tool set.

    Empty → RUNNING (turn still in flight, no tools pending).
    Any interactive id → WAITING_ON_USER (user input gates progress).
    Otherwise → RUNNING_TOOL (model is waiting on tool output).
    """
    if not open_tools:
        return RunState.RUNNING
    if any(is_interactive for is_interactive in open_tools.values()):
        return RunState.WAITING_ON_USER
    return RunState.RUNNING_TOOL


def reset_for_tests() -> None:
    """Test-only: drop all state."""
    _run_state.clear()
    _open_tools.clear()
    _last_event_at.clear()
    _context_usage.clear()
    _pre_broken_state.clear()
    _state_callbacks.clear()


def _now() -> float:
    return time.monotonic()


def _maybe_decay_idle(route: Route) -> RunState:
    """Lazily decay IDLE_RECENT → IDLE_CLEARED when read after the delay.

    On-read instead of scheduled call_later avoids leaking timer handles
    for routes that get torn down mid-decay. The cost is one comparison per
    ``state()`` call — trivial.
    """
    current = _run_state.get(route, RunState.IDLE_CLEARED)
    if current is RunState.IDLE_RECENT:
        last = _last_event_at.get(route, 0.0)
        if (_now() - last) >= IDLE_CLEAR_DELAY_SECONDS:
            _run_state[route] = RunState.IDLE_CLEARED
            return RunState.IDLE_CLEARED
    return current


def state(route: Route) -> RunState:
    """Return the route's current ``RunState``.

    Unknown routes default to ``IDLE_CLEARED`` (treat-as-idle): a surface
    asking about a route the indicator has never seen is best off rendering
    "no busy state" rather than fabricating one.
    """
    if route not in _run_state:
        return RunState.IDLE_CLEARED
    return _maybe_decay_idle(route)


def context_usage(route: Route) -> ContextUsage | None:
    """Return the cached ContextUsage for a route, or None."""
    return _context_usage.get(route)


def update_context_usage(route: Route, tokens: int | None, model: str | None) -> None:
    """Cache the latest ContextUsage for a route from a JSONL ``message.usage``.

    Auto-detects the 1M variant: once a route is observed above
    ``_CONTEXT_DETECT_1M_THRESHOLD``, it latches to a 1M cap and stays
    there. Otherwise defaults to 200k. ``model`` is accepted for future
    use (logging / explicit cap overrides) but the cap itself is derived
    from observed tokens, since JSONL doesn't carry the ``[1m]`` suffix.

    Passing ``tokens=None`` drops the cache entry — used after ``/clear``
    when there's no assistant turn yet.
    """
    if tokens is None or tokens <= 0:
        _context_usage.pop(route, None)
        return
    prior = _context_usage.get(route)
    prior_max = prior.max_tokens if prior else 200_000
    if tokens >= _CONTEXT_DETECT_1M_THRESHOLD or prior_max >= 1_000_000:
        max_tokens = 1_000_000
    else:
        max_tokens = 200_000
    _context_usage[route] = ContextUsage(tokens=tokens, max_tokens=max_tokens)
    # Bind unused arg so type-checkers are happy and future callers can
    # override the cap based on the model id.
    _ = model


def context_pct(route: Route) -> int | None:
    """Derived: integer 0–100 percent of the current context window used.

    Kept as a getter so the activity-digest threshold gate
    (``message_queue._context_pct_suffix``) keeps working unchanged. Returns
    None when no usage has been observed yet for this route.
    """
    usage = _context_usage.get(route)
    if usage is None or usage.max_tokens <= 0:
        return None
    pct = int(round(usage.tokens * 100 / usage.max_tokens))
    return max(0, min(100, pct))


def clear_route(route: Route) -> None:
    """Drop all state for a route (called from ``teardown_route``)."""
    _run_state.pop(route, None)
    _open_tools.pop(route, None)
    _last_event_at.pop(route, None)
    _context_usage.pop(route, None)
    _pre_broken_state.pop(route, None)


def parse_pending_tools_from_jsonl(jsonl_path: str) -> dict[str, bool]:
    """Scan a session's parent JSONL for tool_use entries with no tool_result.

    Returns ``{tool_use_id: is_interactive}`` for the open set, suitable for
    feeding into ``seed_open_tools``. Used at startup to recover the
    in-flight tool state lost when the bot restarts mid-turn — most acutely
    important for sub-agent ``Task`` calls, which can sit open for many
    minutes with no parent-JSONL activity to re-arm the busy indicator.

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


def seed_open_tools(route: Route, tools: dict[str, bool]) -> None:
    """Seed ``_open_tools[route]`` from a startup replay.

    Sets the route's run state from the seeded open-tool set so the typing
    indicator and digest header reflect the in-flight state immediately.

    No-op when ``tools`` is empty: the route already defaults to
    ``IDLE_CLEARED`` via ``state()``, and writing an explicit IDLE_CLEARED
    here would override any concurrent event that landed during startup.

    No-op when the route already has live ``_run_state``: a real
    ``on_transcript_event`` (or ``mark_inbound_sent`` / ``mark_topic_broken``)
    landed between callback registration and the replay walk. The live
    event is more authoritative than a JSONL-derived snapshot, especially
    for ``BROKEN_TOPIC`` which we'd otherwise silently downgrade.

    ``_last_event_at`` is set so a subsequent ``_apply_event`` doesn't
    treat the seeded route as never-seen (which would make idle decay
    behave oddly). Callbacks are NOT fired — startup seeding is bookkeeping,
    not a transition the surfaces should react to.
    """
    if not tools:
        return
    if route in _run_state:
        return
    _open_tools[route] = dict(tools)
    _run_state[route] = _state_from_open_tools(tools)
    _last_event_at[route] = _now()


async def _set_state(route: Route, new: RunState) -> None:
    """Mutate state and fire callbacks if it actually changed."""
    old = _run_state.get(route, RunState.IDLE_CLEARED)
    _last_event_at[route] = _now()
    if old is new:
        return
    _run_state[route] = new
    logger.debug(
        "busy_state route=%s old=%s new=%s open_tools=%s",
        route,
        old.value,
        new.value,
        sorted(_open_tools.get(route, {}).keys()),
    )
    for cb in list(_state_callbacks):
        try:
            await cb(route, old, new)
        except Exception as e:
            logger.error(
                "state callback error route=%s old=%s new=%s: %s",
                route,
                old.value,
                new.value,
                e,
            )


async def _apply_event(event: TranscriptEvent, route: Route) -> None:
    """Apply the §2.2.1 transition table for one route."""
    open_tools = _open_tools.setdefault(route, {})
    # _apply_event uses RUNNING as the prior because we're about to process
    # an event for this route; state() uses IDLE_CLEARED because no event
    # has happened. The two readers see different defaults intentionally.
    current = _run_state.get(route, RunState.RUNNING)

    # Recovery from BROKEN_TOPIC: any successful event restores prior state.
    # We do this BEFORE evaluating event-specific rules so subsequent rules
    # operate on the restored state (e.g. a tool_result that closes the
    # last open tool should still be able to walk to RUNNING).
    if current is RunState.BROKEN_TOPIC:
        prior = _pre_broken_state.pop(route, RunState.RUNNING)
        _run_state[route] = prior
        current = prior

    role = event.role
    block = event.block_type
    stop_reason = event.stop_reason

    # tool_use: open the tool, recording its interactivity. The interactive
    # bit must live with the id so parallel turns mixing AskUserQuestion +
    # Bash settle to the right state when each individual tool_result lands.
    if role == "assistant" and block == "tool_use" and event.tool_use_id:
        is_interactive = bool(
            event.tool_name and event.tool_name in INTERACTIVE_TOOL_NAMES
        )
        open_tools[event.tool_use_id] = is_interactive
        await _set_state(route, _state_from_open_tools(open_tools))
        return

    # tool_result: close the slot if known. Ignore stale ids (could be a
    # pre-startup tool we never saw the matching tool_use for).
    #
    # Why "stale tool_result" is correctly ignored: on bot startup,
    # _open_tools is empty for all routes. A pre-startup tool_result lands
    # here and is dropped — the next assistant event recovers state. The
    # transcript_parser._pending_tools carry-over does NOT seed _open_tools
    # (different layer).
    #
    # Role is intentionally NOT checked: transcript_parser flips tool_result
    # ParsedEntries to role="assistant" so the bubble renders on Claude's
    # side in Telegram, while the raw JSONL envelope is role="user". The
    # block_type + tool_use_id are already specific enough.
    if block == "tool_result" and event.tool_use_id:
        if event.tool_use_id not in open_tools:
            # Stale / pre-startup tool result. Don't touch _last_event_at —
            # that would falsely extend IDLE_RECENT.
            return
        open_tools.pop(event.tool_use_id, None)
        await _set_state(route, _state_from_open_tools(open_tools))
        return

    # End-of-turn signals: thinking or text with end_turn / stop_sequence
    # AND no open tools → IDLE_RECENT. With open tools we stay in
    # RUNNING_TOOL / WAITING_ON_USER until the matching tool_result lands.
    if (
        role == "assistant"
        and block in ("text", "thinking")
        and stop_reason in _TURN_END_REASONS
        and not open_tools
    ):
        await _set_state(route, RunState.IDLE_RECENT)
        return

    # Any text event from assistant: route is at least RUNNING. Don't
    # downgrade RUNNING_TOOL / WAITING_ON_USER (open tools still pending).
    if role == "assistant" and block == "text":
        if current in (RunState.RUNNING_TOOL, RunState.WAITING_ON_USER):
            _last_event_at[route] = _now()
            return
        await _set_state(route, RunState.RUNNING)
        return

    # Assistant thinking without an end-of-turn stop_reason: light up the
    # indicator if the route was idle (preliminary thinking before the
    # first text/tool_use is the most common pre-output signal). Keep
    # RUNNING_TOOL / WAITING_ON_USER unchanged — open tools still gate.
    # ``route not in _run_state`` covers the unknown-route case: ``current``
    # defaults to RUNNING for unknown routes (intentional, see line 197),
    # but visibly the surface treats them as IDLE_CLEARED, so we want to
    # write the actual RUNNING state through.
    if role == "assistant" and block == "thinking":
        if route not in _run_state or current in (
            RunState.IDLE_CLEARED,
            RunState.IDLE_RECENT,
        ):
            await _set_state(route, RunState.RUNNING)
            return
        _last_event_at[route] = _now()
        return

    # User non-tool_result message (the user prompted Claude): RUNNING.
    if role == "user" and block != "tool_result":
        await _set_state(route, RunState.RUNNING)
        return

    # Fallback: refresh activity timer without state change.
    _last_event_at[route] = _now()


async def on_transcript_event(event: TranscriptEvent, routes: list[Route]) -> None:
    """Apply the transition table for each subscribed route.

    One JSONL event can fan out to multiple routes if multiple users
    follow the same session. ``routes`` is resolved by the bot adapter
    via ``session_manager.find_users_for_session``.
    """
    for route in routes:
        await _apply_event(event, route)


async def mark_inbound_sent(route: Route) -> None:
    """Mark a route RUNNING after a Telegram-originated prompt is delivered
    to the Claude tmux window.

    Closes the visibility gap between "user message accepted" and "first
    transcript event lands": without this, the route stays IDLE_CLEARED
    until JSONL produces an assistant text/tool_use, so the V2 typing-action
    loop has nothing to refresh and the one-shot ``send_chat_action`` from
    the inbound handler expires after Telegram's ~5s TTL.

    Idempotent against already-busy routes: never downgrade RUNNING_TOOL or
    WAITING_ON_USER (open tools still gate), and don't overwrite
    BROKEN_TOPIC (recovery happens on the next real event).
    """
    current = _run_state.get(route, RunState.IDLE_CLEARED)
    if current in (
        RunState.RUNNING_TOOL,
        RunState.WAITING_ON_USER,
        RunState.BROKEN_TOPIC,
    ):
        _last_event_at[route] = _now()
        return
    await _set_state(route, RunState.RUNNING)


async def mark_topic_broken(route: Route) -> None:
    """Transition a route into BROKEN_TOPIC, remembering the prior state.

    Called by the topic-send classifier when a send lands in
    ``_TOPIC_BROKEN_OUTCOMES``. Recovery happens implicitly on the next
    ``on_transcript_event`` for the route, or explicitly via
    ``mark_topic_recovered``.

    Idempotent: repeated calls do not overwrite the original prior state
    with another BROKEN_TOPIC sentinel.
    """
    current = _run_state.get(route, RunState.RUNNING)
    if current is RunState.BROKEN_TOPIC:
        return
    _pre_broken_state[route] = current
    await _set_state(route, RunState.BROKEN_TOPIC)


async def mark_pane_idle(route: Route) -> None:
    """Reconcile the route to IDLE_CLEARED after a confirmed pane-idle stretch.

    Backstop against missed lifecycle events. Even with §Fix-2's
    lifecycle-only ParsedEntries, a malformed JSONL line, a parser bug,
    or a Claude run that dies without writing end_turn can leave a route
    pinned at RUNNING / RUNNING_TOOL forever — the V2 typing-action loop
    keeps refreshing the native indicator on a route that long ago went
    quiet. The status poller already debounces ``IDLE_CLEAR_DELAY_SECONDS``
    of confirmed pane-idle before clearing the visible status card; mirror
    that decision into the run-state machine so the indicator clears too.

    No-op while a known interactive prompt is open
    (``WAITING_ON_USER``) — those legitimately sit on the pane with no
    spinner and we don't want to spam-clear them. ``BROKEN_TOPIC`` is
    similarly preserved (recovery is gated on the next real event).
    """
    current = _run_state.get(route)
    if current in (RunState.WAITING_ON_USER, RunState.BROKEN_TOPIC):
        return
    # Drop any lingering open tools — the pane has been confirmed idle for
    # long enough that those tools are no longer in flight (the matching
    # tool_result was either lost or never written).
    open_tools = _open_tools.pop(route, None)
    new = RunState.IDLE_CLEARED
    if current is new:
        # Refresh the activity timer but skip the callback fan-out.
        _last_event_at[route] = _now()
        return
    logger.debug(
        "busy_state pane_idle route=%s old=%s new=%s dropped_tools=%s",
        route,
        current.value if current else None,
        new.value,
        sorted(open_tools.keys()) if open_tools else [],
    )
    await _set_state(route, new)


async def mark_topic_recovered(route: Route) -> None:
    """Restore a BROKEN_TOPIC route to its pre-broken state.

    Stage 4 wires this into the topic-send success path so a recovery is
    visible in the digest immediately, instead of waiting for the next
    JSONL event (which may never come if Claude already finished its turn).

    No-op if the route isn't currently BROKEN_TOPIC.
    """
    if _run_state.get(route) is not RunState.BROKEN_TOPIC:
        return
    prior = _pre_broken_state.pop(route, RunState.RUNNING)
    await _set_state(route, prior)
