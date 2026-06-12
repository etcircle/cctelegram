"""Wave B unit tests — the ``notification_pending`` derivation input in
``route_runtime``.

Covers the busy-signal Wave B route_runtime contract (plan v2 B1 + v3
B1a/B1b/B1c + v4 fixes 1/2):

  - ``mark_notification_pending`` authority matrix: Workflow-open
    ``RUNNING_TOOL`` promotion (THE target case), empty ``RUNNING``,
    transcript-WAITING redundancy, idle(transcript) staleness,
    idle(pane)+stash positive-live-proof resurrection (v4 fix 1),
    idle(pane)+empty-stash staleness, unseen-route ignore.
  - Deriver precedence: transcript-interactive > notification bit >
    pane bit > RUNNING_TOOL > RUNNING; the two lower-authority bits
    clear INDEPENDENTLY.
  - Timestamp-qualified clears (v4 fix 2): ``user`` clears
    unconditionally; ``tool_result`` / end-of-turn / assistant
    ``tool_use``/``text``/``thinking`` clear ONLY on a strictly newer
    event timestamp; ``None`` or older preserves the bit.
  - The pending-without-set_at invariant is treated as expired.
  - ``mark_notification_cleared`` re-derives; teardown seams drop the bit.
"""

from __future__ import annotations

import pytest

from cctelegram import route_runtime
from cctelegram.route_runtime import (
    NotificationMarkResult,
    RunState,
    TranscriptLifecycleEvent,
)

ROUTE: route_runtime.Route = (1, 42, "@7")

SET_AT = 2000.0
GEN = "2000.0-abcd1234"


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


@pytest.fixture(autouse=True)
def _reset():
    route_runtime.reset_for_tests()
    yield
    route_runtime.reset_for_tests()


def _st(route: route_runtime.Route = ROUTE) -> route_runtime._RouteState:
    return route_runtime._state[route]


async def _mk_running() -> None:
    await route_runtime.ingest_transcript_event(ROUTE, _evt("user", "text"))


async def _mk_running_tool(
    tool_id: str = "wf-1", name: str = "Workflow", *, timestamp: float | None = None
) -> None:
    await _mk_running()
    await route_runtime.ingest_transcript_event(
        ROUTE,
        _evt(
            "assistant",
            "tool_use",
            tool_use_id=tool_id,
            tool_name=name,
            timestamp=timestamp,
        ),
    )


async def _mark(
    set_at: float = SET_AT, generation: str = GEN
) -> NotificationMarkResult:
    return await route_runtime.mark_notification_pending(
        ROUTE, set_at=set_at, generation=generation
    )


# ── mark_notification_pending authority matrix ──────────────────────────


async def test_workflow_open_notification_promotes_waiting():
    """THE target case: a non-interactive Workflow tool open (RUNNING_TOOL) +
    notification → WAITING_ON_USER with typing off."""
    await _mk_running_tool()
    assert route_runtime.snapshot(ROUTE).run_state is RunState.RUNNING_TOOL
    result = await _mark()
    assert result is NotificationMarkResult.COMMITTED_LIVE
    snap = route_runtime.snapshot(ROUTE)
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.typing_eligible is False
    assert snap.notification_pending is True
    assert snap.notification_set_at == SET_AT
    assert snap.notification_generation == GEN


async def test_running_empty_notification_promotes_waiting():
    await _mk_running()
    result = await _mark()
    assert result is NotificationMarkResult.COMMITTED_LIVE
    snap = route_runtime.snapshot(ROUTE)
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.notification_pending is True


async def test_transcript_waiting_notification_is_redundant():
    """A transcript-set WAITING (interactive id open) is already 🔔 — the
    notification must NOT set the bit, so no stale re-light survives the
    transcript WAITING's own clear."""
    await _mk_running()
    await route_runtime.ingest_transcript_event(
        ROUTE,
        _evt("assistant", "tool_use", tool_use_id="auq-1", tool_name="AskUserQuestion"),
    )
    assert route_runtime.snapshot(ROUTE).run_state is RunState.WAITING_ON_USER
    result = await _mark()
    assert result is NotificationMarkResult.REDUNDANT_TRANSCRIPT_WAITING
    assert route_runtime.snapshot(ROUTE).notification_pending is False
    # The transcript WAITING clears via its tool_result — no re-light.
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("user", "tool_result", tool_use_id="auq-1", timestamp=SET_AT + 10)
    )
    snap = route_runtime.snapshot(ROUTE)
    assert snap.run_state is RunState.RUNNING
    assert snap.notification_pending is False


async def test_idle_transcript_notification_is_stale():
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "text", stop_reason="end_turn")
    )
    result = await _mark()
    assert result is NotificationMarkResult.STALE_UNLINK
    snap = route_runtime.snapshot(ROUTE)
    assert snap.run_state in (RunState.IDLE_RECENT, RunState.IDLE_CLEARED)
    assert snap.notification_pending is False


async def test_idle_pane_with_stash_resurrects_to_waiting():
    """v4 fix 1: IDLE(pane) with a live stash is positive live proof — the
    notification RESTORES the stash and derives WAITING_ON_USER."""
    await _mk_running_tool(tool_id="agent-1", name="Agent")
    await route_runtime.mark_pane_idle(ROUTE)
    assert _st().idle_source == "pane"
    assert _st().suspended_tools
    result = await _mark()
    assert result is NotificationMarkResult.COMMITTED_LIVE
    snap = route_runtime.snapshot(ROUTE)
    assert snap.run_state is RunState.WAITING_ON_USER
    assert "agent-1" in snap.open_tools
    assert snap.notification_pending is True
    assert not _st().suspended_tools


async def test_idle_pane_empty_stash_is_stale():
    await _mk_running()
    await route_runtime.mark_pane_idle(ROUTE)
    assert _st().idle_source == "pane"
    assert not _st().suspended_tools
    result = await _mark()
    assert result is NotificationMarkResult.STALE_UNLINK
    assert route_runtime.snapshot(ROUTE).run_state is RunState.IDLE_CLEARED
    assert route_runtime.snapshot(ROUTE).notification_pending is False


async def test_unseen_route_is_ignored_never_seeded():
    result = await _mark()
    assert result is NotificationMarkResult.IGNORED_NO_UNLINK
    assert ROUTE not in route_runtime._state or not route_runtime._state[ROUTE].seen
    assert route_runtime.snapshot(ROUTE).notification_pending is False


async def test_pane_set_waiting_commits_independently():
    """A pane-set WAITING (pane bit, empty open_tools) accepts the
    notification; the two bits then clear INDEPENDENTLY."""
    await _mk_running()
    await route_runtime.mark_interactive_pending(ROUTE)
    assert route_runtime.snapshot(ROUTE).run_state is RunState.WAITING_ON_USER
    result = await _mark()
    assert result is NotificationMarkResult.COMMITTED_LIVE
    snap = route_runtime.snapshot(ROUTE)
    assert snap.notification_pending is True
    assert snap.interactive_pending is True
    # Clearing the notification bit must NOT clear the pane bit, and the
    # route stays WAITING via the pane bit.
    await route_runtime.mark_notification_cleared(ROUTE)
    snap = route_runtime.snapshot(ROUTE)
    assert snap.notification_pending is False
    assert snap.interactive_pending is True
    assert snap.run_state is RunState.WAITING_ON_USER


# ── timestamp-qualified clears (v4 fix 2) ───────────────────────────────


async def test_user_event_clears_unconditionally():
    await _mk_running()
    await _mark()
    # Even with NO timestamp the user event clears (the user acted).
    await route_runtime.ingest_transcript_event(ROUTE, _evt("user", "text"))
    snap = route_runtime.snapshot(ROUTE)
    assert snap.notification_pending is False
    assert snap.run_state is RunState.RUNNING


async def test_older_buffered_events_preserve_bit_regression():
    """v4 fix 2 regression: notification set → older buffered known
    tool_result → preserved → older Workflow tool_use → preserved →
    route WAITING_ON_USER throughout."""
    await _mk_running_tool(tool_id="wf-1", name="Workflow")
    await _mark(set_at=SET_AT)
    # Older buffered tool_result for the open Workflow id.
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("user", "tool_result", tool_use_id="wf-1", timestamp=SET_AT - 10)
    )
    snap = route_runtime.snapshot(ROUTE)
    assert snap.notification_pending is True
    assert snap.run_state is RunState.WAITING_ON_USER
    # Older buffered Workflow tool_use.
    await route_runtime.ingest_transcript_event(
        ROUTE,
        _evt(
            "assistant",
            "tool_use",
            tool_use_id="wf-2",
            tool_name="Workflow",
            timestamp=SET_AT - 5,
        ),
    )
    snap = route_runtime.snapshot(ROUTE)
    assert snap.notification_pending is True
    assert snap.run_state is RunState.WAITING_ON_USER


async def test_none_timestamp_tool_result_preserves_bit():
    await _mk_running_tool(tool_id="wf-1", name="Workflow")
    await _mark()
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("user", "tool_result", tool_use_id="wf-1", timestamp=None)
    )
    snap = route_runtime.snapshot(ROUTE)
    assert snap.notification_pending is True
    assert snap.run_state is RunState.WAITING_ON_USER


async def test_newer_tool_result_clears_bit():
    await _mk_running_tool(tool_id="wf-1", name="Workflow")
    await _mark()
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("user", "tool_result", tool_use_id="wf-1", timestamp=SET_AT + 10)
    )
    snap = route_runtime.snapshot(ROUTE)
    assert snap.notification_pending is False
    assert snap.run_state is RunState.RUNNING


async def test_newer_assistant_text_clears_bit():
    await _mk_running()
    await _mark()
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "text", timestamp=SET_AT + 10)
    )
    snap = route_runtime.snapshot(ROUTE)
    assert snap.notification_pending is False
    assert snap.run_state is RunState.RUNNING


async def test_older_assistant_text_preserves_bit():
    await _mk_running()
    await _mark()
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "text", timestamp=SET_AT - 10)
    )
    snap = route_runtime.snapshot(ROUTE)
    assert snap.notification_pending is True
    assert snap.run_state is RunState.WAITING_ON_USER


async def test_newer_end_of_turn_clears_and_idles():
    await _mk_running()
    await _mark()
    await route_runtime.ingest_transcript_event(
        ROUTE,
        _evt("assistant", "text", stop_reason="end_turn", timestamp=SET_AT + 10),
    )
    snap = route_runtime.snapshot(ROUTE)
    assert snap.notification_pending is False
    assert snap.run_state is RunState.IDLE_RECENT


async def test_older_end_of_turn_preserves_waiting():
    """An older buffered end-of-turn must not re-hide the wait — the route
    stays WAITING_ON_USER instead of idling."""
    await _mk_running()
    await _mark()
    await route_runtime.ingest_transcript_event(
        ROUTE,
        _evt("assistant", "text", stop_reason="end_turn", timestamp=SET_AT - 10),
    )
    snap = route_runtime.snapshot(ROUTE)
    assert snap.notification_pending is True
    assert snap.run_state is RunState.WAITING_ON_USER


async def test_unknown_tool_result_preserves_bit_even_if_newer():
    """Mirror of the pane-bit contract: an unknown tool_result (stale /
    pre-startup id) is not proof of resumption."""
    await _mk_running()
    await _mark()
    await route_runtime.ingest_transcript_event(
        ROUTE,
        _evt("user", "tool_result", tool_use_id="ghost-1", timestamp=SET_AT + 100),
    )
    snap = route_runtime.snapshot(ROUTE)
    assert snap.notification_pending is True
    assert snap.run_state is RunState.WAITING_ON_USER


async def test_pending_without_set_at_invariant_treated_as_expired():
    """Codex r4 P3 (a): pending without set_at is an invariant violation —
    the next transcript event treats it as expired and clears."""
    await _mk_running()
    await _mark()
    _st().notification_set_at = None  # corrupt the invariant
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "text", timestamp=None)
    )
    snap = route_runtime.snapshot(ROUTE)
    assert snap.notification_pending is False


# ── mark_notification_cleared ───────────────────────────────────────────


async def test_cleared_rederives_running_tool():
    await _mk_running_tool(tool_id="wf-1", name="Workflow")
    await _mark()
    assert route_runtime.snapshot(ROUTE).run_state is RunState.WAITING_ON_USER
    await route_runtime.mark_notification_cleared(ROUTE)
    snap = route_runtime.snapshot(ROUTE)
    assert snap.notification_pending is False
    assert snap.run_state is RunState.RUNNING_TOOL


async def test_cleared_rederives_running_on_empty():
    await _mk_running()
    await _mark()
    await route_runtime.mark_notification_cleared(ROUTE)
    snap = route_runtime.snapshot(ROUTE)
    assert snap.notification_pending is False
    assert snap.run_state is RunState.RUNNING


async def test_cleared_never_strips_transcript_waiting():
    await _mk_running()
    await route_runtime.ingest_transcript_event(
        ROUTE,
        _evt("assistant", "tool_use", tool_use_id="auq-1", tool_name="AskUserQuestion"),
    )
    await route_runtime.mark_notification_cleared(ROUTE)
    assert route_runtime.snapshot(ROUTE).run_state is RunState.WAITING_ON_USER


async def test_cleared_on_unseen_route_is_noop():
    snap = await route_runtime.mark_notification_cleared(ROUTE)
    assert snap.notification_pending is False
    assert ROUTE not in route_runtime._state


# ── teardown seams ──────────────────────────────────────────────────────


async def test_session_reset_drops_notification_bit():
    await _mk_running()
    await _mark()
    await route_runtime.mark_session_reset(ROUTE)
    snap = route_runtime.snapshot(ROUTE)
    assert snap.notification_pending is False
    assert snap.run_state is RunState.IDLE_CLEARED


async def test_clear_route_drops_notification_bit():
    await _mk_running()
    await _mark()
    route_runtime.clear_route(ROUTE)
    assert route_runtime.snapshot(ROUTE).notification_pending is False


async def test_clear_routes_for_topic_drops_notification_bit():
    await _mk_running()
    await _mark()
    route_runtime.clear_routes_for_topic(ROUTE[0], ROUTE[1])
    assert route_runtime.snapshot(ROUTE).notification_pending is False


# ── stash × notification matrix (codex r3 P2) ───────────────────────────


async def test_sidechain_resurrection_then_notification():
    """Sidechain resurrection (already-RUNNING_TOOL path) followed by a
    notification → 🔔 over the restored tools."""
    await _mk_running_tool(tool_id="agent-1", name="Agent")
    await route_runtime.mark_pane_idle(ROUTE)
    await route_runtime.mark_background_agent_activity(ROUTE, "sc-key", None)
    assert route_runtime.snapshot(ROUTE).run_state is RunState.RUNNING_TOOL
    result = await _mark()
    assert result is NotificationMarkResult.COMMITTED_LIVE
    snap = route_runtime.snapshot(ROUTE)
    assert snap.run_state is RunState.WAITING_ON_USER
    assert "agent-1" in snap.open_tools


async def test_notification_resurrect_then_restored_tool_result_pairs():
    """The notification-restored stash pairs with its late tool_result via
    the normal path (newer ts also clears the bit)."""
    await _mk_running_tool(tool_id="agent-1", name="Agent")
    await route_runtime.mark_pane_idle(ROUTE)
    await _mark()
    await route_runtime.ingest_transcript_event(
        ROUTE,
        _evt("user", "tool_result", tool_use_id="agent-1", timestamp=SET_AT + 10),
    )
    snap = route_runtime.snapshot(ROUTE)
    assert snap.notification_pending is False
    assert "agent-1" not in snap.open_tools
    assert snap.run_state is RunState.RUNNING
