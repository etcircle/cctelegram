"""Translate ``session_monitor.TranscriptEvent`` into ``route_runtime``
lifecycle events.

Callers emit the higher-level ``TranscriptEvent`` (with full provenance)
while ``route_runtime`` consumes the smaller normalized
``TranscriptLifecycleEvent`` shape needed by the state machine; this
adapter does the translation.

Two responsibilities:

  1. ``to_lifecycle_event(event)`` — pure translation. Returns ``None``
     when the event is ignorable (sidechain leak, unknown block_type,
     malformed shape). Drops the heavy ``text`` / ``image_data`` /
     ``tool_input`` fields the state machine doesn't read.
  2. ``dispatch_transcript_event(event, routes)`` — fan-out over a list
     of routes. For each route, calls
     ``route_runtime.ingest_transcript_event`` and returns the
     resulting snapshots. Also routes ``message.usage`` through
     ``route_runtime.update_context_usage`` when the session_monitor's
     parser propagated usage data on the event.

LoC budget: 150-250 lines (kill signal at 250 — beyond that this is
Transcript Stream pretending to be an adapter, and the campaign should
pause and re-evaluate). Current size is well under the floor; the
helpers stay terse because the underlying ``TranscriptEvent`` already
carries the lifecycle fields cleanly. If a new ``TranscriptEvent`` shape
lands that requires non-trivial normalisation here, file it against the
kill signal.

Error contract: parse failures are logged once per session, the event
is dropped, no snapshot is mutated.
"""

from __future__ import annotations

import asyncio
import logging

from . import route_runtime
from .route_runtime import Route, RouteRuntimeSnapshot, TranscriptLifecycleEvent
from .session_monitor import TranscriptEvent
from .utils import parse_iso_timestamp

logger = logging.getLogger(__name__)

# Once-per-session warning suppression — repeated parse failures on the
# same session would otherwise flood the log. Keys are session_ids.
_warned_sessions: set[str] = set()


def _parse_event_timestamp(raw: str | None) -> float | None:
    """Parse the JSONL ISO8601 ``timestamp`` to epoch seconds; ``None`` on
    any failure (the timestamp-qualified notification clears then PRESERVE).
    Delegates to the shared ``utils.parse_iso_timestamp`` so the monitor's
    GH #44 sidechain-timestamp aggregation uses the SAME parse semantics."""
    return parse_iso_timestamp(raw)


def _is_task_notification_user_event(event: TranscriptEvent) -> bool:
    """GH #44 §3.7: stamp machine-initiated ``<task-notification>`` user
    events so ``route_runtime`` can suppress the genuine-user-turn side
    effects. Deferred import: the predicate lives in
    ``handlers.response_builder`` (the single envelope-regex owner)."""
    if event.role != "user" or event.block_type != "text":
        return False
    from .handlers.response_builder import is_task_notification

    return is_task_notification(event.text)


def to_lifecycle_event(event: TranscriptEvent) -> TranscriptLifecycleEvent | None:
    """Translate a raw ``TranscriptEvent`` into the normalized lifecycle
    shape that ``route_runtime`` consumes.

    Returns ``None`` when:
      - ``role`` is not one of the expected literals (defensive — the
        upstream parser already constrains this, but a future
        TranscriptEvent revision shouldn't crash Wave B's adapter).
      - ``block_type`` is not one of ``text`` / ``thinking`` / ``tool_use``
        / ``tool_result``.
      - ``tool_use`` or ``tool_result`` arrives without a ``tool_use_id``
        (the state machine has no slot key to update).

    Sidechain entries (``isSidechain=true`` in the source JSONL) are
    filtered upstream by ``session_monitor`` so they never reach this
    adapter; if one ever does, the role/block_type fall-through here
    drops it harmlessly.
    """
    role = event.role
    block = event.block_type
    if role not in ("user", "assistant"):
        return None
    if block not in ("text", "thinking", "tool_use", "tool_result"):
        return None
    if block in ("tool_use", "tool_result") and not event.tool_use_id:
        return None
    return TranscriptLifecycleEvent(
        role=role,
        block_type=block,
        tool_use_id=event.tool_use_id,
        tool_name=event.tool_name,
        stop_reason=event.stop_reason,
        timestamp=_parse_event_timestamp(event.timestamp),
        is_task_notification=_is_task_notification_user_event(event),
    )


async def dispatch_transcript_event(
    event: TranscriptEvent,
    routes: list[Route],
) -> list[RouteRuntimeSnapshot]:
    """Ingest ``event`` into ``route_runtime`` for each route in ``routes``.

    Concurrency: per-route ingests run **concurrently** under
    ``asyncio.gather`` so independent route locks don't serialise on the
    adapter side. Within a route, ``route_runtime.ingest_transcript_event``
    holds the per-route lock across mutation + freeze and returns the
    committed snapshot (there is no observer/push channel). The
    "independent routes do not serialise" invariant holds at the adapter
    level — work on route A does NOT delay route B seeing the same event.

    Returns the per-route committed snapshots in input order — useful
    for callers that want to chain immediate side-effects off the new
    state (e.g. a status-card refresh that needs the post-commit
    ``run_state``).

    Robustness:
      - Per-route ingest failures are logged at warning level once per
        session; other routes still get their snapshot.
      - ``to_lifecycle_event`` returning ``None`` causes a no-op dispatch
        with no snapshot mutation. Callers can ignore the empty result.
    """
    lifecycle = to_lifecycle_event(event)
    if lifecycle is None:
        _warn_once(
            event.session_id,
            "dropped_transcript_event_unrecognised role=%s block=%s",
            event.role,
            event.block_type,
        )
        return []

    async def _ingest_one(route: Route) -> RouteRuntimeSnapshot | None:
        try:
            return await route_runtime.ingest_transcript_event(route, lifecycle)
        except Exception as e:
            _warn_once(
                event.session_id,
                "ingest_transcript_event failed route=%s err=%s",
                route,
                e,
            )
            return None

    results = await asyncio.gather(*(_ingest_one(r) for r in routes))
    return [snap for snap in results if snap is not None]


def dispatch_context_usage(
    routes: list[Route], tokens: int | None, model: str | None
) -> None:
    """Fan ``update_context_usage`` out to every route observing this session.

    Synchronous because the underlying update is synchronous.
    """
    for route in routes:
        try:
            route_runtime.update_context_usage(route, tokens, model)
        except Exception as e:
            logger.warning(
                "route_runtime.update_context_usage failed route=%s err=%s",
                route,
                e,
            )


def dispatch_seed_open_tools(route: Route, tools: dict[str, bool]) -> None:
    """Replay startup-recovered open tools into ``route_runtime``.

    Thin wrapper so the bot's startup replay loop can stay clean.
    """
    try:
        route_runtime.seed_open_tools(route, tools)
    except Exception as e:
        logger.warning("route_runtime.seed_open_tools failed route=%s err=%s", route, e)


def _warn_once(session_id: str, fmt: str, *args: object) -> None:
    """Log a warning at most once per session.

    Per-session because a malformed line in one user's transcript
    shouldn't be silenced for everyone, but a parser bug that affects
    every event in one session would otherwise spam the log forever.
    """
    if session_id in _warned_sessions:
        return
    _warned_sessions.add(session_id)
    logger.warning(fmt, *args)


def reset_for_tests() -> None:
    """Test-only: drop the once-per-session warning suppression."""
    _warned_sessions.clear()


__all__ = [
    "dispatch_context_usage",
    "dispatch_seed_open_tools",
    "dispatch_transcript_event",
    "reset_for_tests",
    "to_lifecycle_event",
]
