"""Pane-derived per-route decoration signals — the background-jobs store (GH #43).

A tiny in-memory leaf holding the latest pane-parsed background-shell count
per route, written by ``status_polling`` on every full pane capture and
PULLED by the renderers (the collapsed done-card in ``message_queue`` and the
``/dashboard`` row) — a decoration, NEVER a run-state input: ``route_runtime``
stays the sole run-state authority, nothing here observes or pushes (the
c313657 pattern stays forbidden), and typing never fires off this store
(user decision recorded on GH #43: typing promises imminent output; a
background shell does not).

Core responsibilities:
  - ``record_background_jobs(route, count, *, now)`` — poller write; returns
    whether the rendered value CHANGED so the caller can trigger a digest
    repaint (a pull-side refresh, not an observer).
  - ``peek_background_jobs(route, *, now)`` — renderer read with staleness:
    a count older than ``BG_JOBS_MAX_AGE_S`` (3× the poller's 10s capture
    watchdog) reads as ``None`` so a dead window can't advertise a phantom
    job forever.
  - ``clear_route`` / ``clear_routes_for_topic`` — teardown seams, wired
    beside every ``route_runtime`` route-clearing callsite.

True leaf: imports nothing from the application (keeps the import graph
acyclic by construction; the subprocess import-cycle gate covers it).
"""

from __future__ import annotations

from dataclasses import dataclass

# (user_id, thread_id_or_0, window_id) — structurally route_runtime.Route,
# re-declared locally so this module stays a leaf.
Route = tuple[int, int, str]

# Staleness horizon for a recorded count: 3× status_polling's 10s capture
# watchdog — a live window refreshes the record well inside this; past it
# the decoration silently hides rather than showing a stale ⏳.
BG_JOBS_MAX_AGE_S = 30.0


@dataclass(frozen=True)
class BackgroundJobs:
    """One pane observation: shell count + capture wall-clock."""

    count: int
    captured_at: float


_signals: dict[Route, BackgroundJobs] = {}


def record_background_jobs(route: Route, count: int, *, now: float) -> bool:
    """Record the pane-parsed background-shell ``count`` for ``route``.

    ``count`` is the parser's non-``None`` result — 0 means "chrome present,
    positively no shells" and is recorded (it HIDES the decoration; recording
    it is what lets a finished shell's ⏳ disappear). ``None`` results (no
    chrome / failed capture) must not reach here — the caller skips them so
    a bad frame can't erase a fresh count.

    Returns True iff the RENDERED value changed (hermes GH #43 diff P2):
    what renders is "fresh count>0 → suffix" vs "stale / 0 / absent → no
    suffix", so the comparison is between rendered states, not raw counts —
    a record that went STALE and is now re-observed at the same count must
    repaint (the card dropped nothing while stale only because nothing
    re-rendered; the next render after this record must be triggered).
    The caller uses True to fire a digest repaint; a same-rendered-value
    refresh only re-stamps freshness.
    """
    prev = _signals.get(route)
    prev_shown = (
        prev is not None
        and prev.count > 0
        and (now - prev.captured_at) <= BG_JOBS_MAX_AGE_S
    )
    new_shown = count > 0
    _signals[route] = BackgroundJobs(count=count, captured_at=now)
    if prev is None:
        return True
    if prev_shown != new_shown:
        return True
    return new_shown and prev.count != count


def peek_background_jobs(
    route: Route, *, now: float, max_age: float = BG_JOBS_MAX_AGE_S
) -> int | None:
    """Return the fresh count for ``route``; ``None`` when absent or stale."""
    rec = _signals.get(route)
    if rec is None or now - rec.captured_at > max_age:
        return None
    return rec.count


def clear_route(route: Route) -> None:
    """Drop the record for one route (window-gone / session-reset seams)."""
    _signals.pop(route, None)


def clear_routes_for_topic(user_id: int, thread_id_or_0: int) -> None:
    """Drop every route under ``(user_id, thread_id_or_0)`` (topic teardown)."""
    for key in [k for k in _signals if k[0] == user_id and k[1] == thread_id_or_0]:
        _signals.pop(key, None)


def reset_for_tests() -> None:
    """Test-only: drop all records."""
    _signals.clear()


__all__ = [
    "BG_JOBS_MAX_AGE_S",
    "BackgroundJobs",
    "Route",
    "clear_route",
    "clear_routes_for_topic",
    "peek_background_jobs",
    "record_background_jobs",
    "reset_for_tests",
]
