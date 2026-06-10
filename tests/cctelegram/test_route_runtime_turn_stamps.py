"""Wave C unit tests — the two wall-clock turn stamps on the snapshot.

Covers spec section A of the busy-signal dashboard plan (v2 C3 + v3 C3a +
v4 pre-C fix 2):

  - ``stamp_user_turn(route, ts)`` is a synchronous mirror of the pre-send
    delivery stamp: it lands ``ts`` in ``snapshot.last_user_turn_at`` without
    seeding run-state.
  - ``last_assistant_turn_ended_at`` is set ONLY by the authoritative
    end-of-turn lifecycle branch, from the EVENT's JSONL timestamp,
    MAX-MONOTONIC (out-of-order older events never regress it); a ``None``
    event timestamp never updates it (no ingest-time fallback).
  - Both fields clear on ``mark_session_reset`` / ``clear_route`` /
    ``clear_routes_for_topic``.
  - Fast-transcript race: an end-of-turn whose event timestamp predates the
    pre-send user stamp yields ``ended <= user_turn`` (never classified
    unanswered).
"""

from __future__ import annotations

import pytest

from cctelegram import route_runtime
from cctelegram.route_runtime import RunState, TranscriptLifecycleEvent

ROUTE: route_runtime.Route = (1, 42, "@7")


@pytest.fixture(autouse=True)
def _reset():
    route_runtime.reset_for_tests()
    yield
    route_runtime.reset_for_tests()


def _evt(
    role: str = "assistant",
    block: str = "text",
    *,
    tool_use_id: str | None = None,
    tool_name: str | None = None,
    stop_reason: str | None = None,
    timestamp: float | None = None,
) -> TranscriptLifecycleEvent:
    return TranscriptLifecycleEvent(
        role=role,  # type: ignore[arg-type]
        block_type=block,  # type: ignore[arg-type]
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        stop_reason=stop_reason,
        timestamp=timestamp,
    )


def _end_of_turn(ts: float | None) -> TranscriptLifecycleEvent:
    return _evt("assistant", "text", stop_reason="end_turn", timestamp=ts)


async def test_default_snapshot_has_none_stamps():
    snap = route_runtime.snapshot(ROUTE)
    assert snap.last_user_turn_at is None
    assert snap.last_assistant_turn_ended_at is None


async def test_stamp_user_turn_lands_exact_ts():
    route_runtime.stamp_user_turn(ROUTE, 1234.5)
    snap = route_runtime.snapshot(ROUTE)
    assert snap.last_user_turn_at == 1234.5


async def test_stamp_user_turn_does_not_seed_run_state():
    """The stamp is bookkeeping — an unseen route stays IDLE_CLEARED and is
    not marked seen (no fabricated activity)."""
    route_runtime.stamp_user_turn(ROUTE, 100.0)
    snap = route_runtime.snapshot(ROUTE)
    assert snap.run_state is RunState.IDLE_CLEARED
    assert snap.typing_eligible is False


async def test_end_of_turn_sets_assistant_ended_from_event_timestamp():
    await route_runtime.ingest_transcript_event(ROUTE, _evt("user", "text"))
    await route_runtime.ingest_transcript_event(ROUTE, _end_of_turn(2000.0))
    snap = route_runtime.snapshot(ROUTE)
    assert snap.last_assistant_turn_ended_at == 2000.0


async def test_end_of_turn_none_timestamp_never_falls_back_to_ingest_time():
    await route_runtime.ingest_transcript_event(ROUTE, _evt("user", "text"))
    await route_runtime.ingest_transcript_event(ROUTE, _end_of_turn(None))
    snap = route_runtime.snapshot(ROUTE)
    assert snap.last_assistant_turn_ended_at is None


async def test_end_of_turn_none_timestamp_preserves_prior_value():
    await route_runtime.ingest_transcript_event(ROUTE, _end_of_turn(2000.0))
    await route_runtime.ingest_transcript_event(ROUTE, _end_of_turn(None))
    snap = route_runtime.snapshot(ROUTE)
    assert snap.last_assistant_turn_ended_at == 2000.0


async def test_max_monotonic_out_of_order_event_does_not_regress():
    """Parent JSONL is not strictly chronological under resume/rewind — an
    older end-of-turn ingested later must not move the stamp backwards."""
    await route_runtime.ingest_transcript_event(ROUTE, _end_of_turn(2000.0))
    await route_runtime.ingest_transcript_event(ROUTE, _end_of_turn(1500.0))
    snap = route_runtime.snapshot(ROUTE)
    assert snap.last_assistant_turn_ended_at == 2000.0


async def test_newer_end_of_turn_advances_the_stamp():
    await route_runtime.ingest_transcript_event(ROUTE, _end_of_turn(2000.0))
    await route_runtime.ingest_transcript_event(ROUTE, _end_of_turn(2500.0))
    snap = route_runtime.snapshot(ROUTE)
    assert snap.last_assistant_turn_ended_at == 2500.0


async def test_non_end_of_turn_events_do_not_stamp_assistant_ended():
    """Only the authoritative end-of-turn branch stamps: a tool_use-blocked
    'end_turn' text (open tools) and plain text/thinking never write it."""
    await route_runtime.ingest_transcript_event(ROUTE, _evt("user", "text"))
    await route_runtime.ingest_transcript_event(
        ROUTE,
        _evt(
            "assistant",
            "tool_use",
            tool_use_id="t1",
            tool_name="Bash",
            timestamp=1000.0,
        ),
    )
    # end_turn text while a tool is open — NOT the authoritative branch.
    await route_runtime.ingest_transcript_event(ROUTE, _end_of_turn(2000.0))
    assert route_runtime.snapshot(ROUTE).last_assistant_turn_ended_at is None
    # Plain assistant text without a stop reason — also not.
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "text", timestamp=3000.0)
    )
    assert route_runtime.snapshot(ROUTE).last_assistant_turn_ended_at is None


async def test_mark_session_reset_clears_both_stamps():
    route_runtime.stamp_user_turn(ROUTE, 100.0)
    await route_runtime.ingest_transcript_event(ROUTE, _end_of_turn(200.0))
    await route_runtime.mark_session_reset(ROUTE)
    snap = route_runtime.snapshot(ROUTE)
    assert snap.last_user_turn_at is None
    assert snap.last_assistant_turn_ended_at is None


async def test_clear_route_drops_stamps():
    route_runtime.stamp_user_turn(ROUTE, 100.0)
    await route_runtime.ingest_transcript_event(ROUTE, _end_of_turn(200.0))
    route_runtime.clear_route(ROUTE)
    snap = route_runtime.snapshot(ROUTE)
    assert snap.last_user_turn_at is None
    assert snap.last_assistant_turn_ended_at is None


async def test_clear_routes_for_topic_drops_stamps():
    route_runtime.stamp_user_turn(ROUTE, 100.0)
    await route_runtime.ingest_transcript_event(ROUTE, _end_of_turn(200.0))
    route_runtime.clear_routes_for_topic(ROUTE[0], ROUTE[1])
    snap = route_runtime.snapshot(ROUTE)
    assert snap.last_user_turn_at is None
    assert snap.last_assistant_turn_ended_at is None


async def test_fast_transcript_race_old_end_of_turn_stays_below_user_stamp():
    """The pre-send stamp seam regression: a delayed end-of-turn whose JSONL
    timestamp predates the user-turn delivery instant lands BELOW the user
    stamp — the route is never classified unanswered for the new turn."""
    route_runtime.stamp_user_turn(ROUTE, 1000.0)
    # The prior turn's end-of-turn flushes late (ingested after the stamp)
    # but its event time is from before the delivery.
    await route_runtime.ingest_transcript_event(ROUTE, _end_of_turn(999.5))
    snap = route_runtime.snapshot(ROUTE)
    assert snap.last_user_turn_at == 1000.0
    assert snap.last_assistant_turn_ended_at == 999.5
    assert snap.last_assistant_turn_ended_at <= snap.last_user_turn_at
