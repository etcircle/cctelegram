"""Wave A unit tests — sidechain activity keep-alive in ``route_runtime``.

Covers the busy-signal Wave A contract:

  - ``mark_subagent_activity`` authority matrix (unseen / IDLE(transcript) /
    IDLE(pane)+stash / IDLE(pane)+empty stash / RUNNING / RUNNING_TOOL /
    WAITING_ON_USER transcript-set and pane-bit-set).
  - ``idle_source`` transition rules: end-of-turn → "transcript"; pane clear
    sets "pane" only when it reconciled an active route; lazy decay preserves;
    leaves-idle resets to None.
  - ``suspended_tools`` stash: pane-idle reconciliation MOVES open_tools into
    the stash; sidechain resurrection restores them; a transcript tool_result
    for a suspended id pairs via the normal path; the stash drops on
    end-of-turn / user event / ``mark_inbound_sent`` / ``mark_session_reset``
    / route teardown.
  - The narrowed card claim: after a committed pane-idle clear plus a drained
    status clear, resurrection restores run-state truth (typing on) without
    asserting card survival.
"""

from __future__ import annotations

import pytest

from cctelegram import route_runtime
from cctelegram.route_runtime import (
    IDLE_CLEAR_DELAY_SECONDS,
    RunState,
    TranscriptLifecycleEvent,
)


ROUTE: route_runtime.Route = (1, 42, "@7")


def _evt(
    role: str = "assistant",
    block: str = "text",
    *,
    tool_use_id: str | None = None,
    tool_name: str | None = None,
    stop_reason: str | None = None,
) -> TranscriptLifecycleEvent:
    return TranscriptLifecycleEvent(
        role=role,  # type: ignore[arg-type]
        block_type=block,  # type: ignore[arg-type]
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        stop_reason=stop_reason,
    )


@pytest.fixture(autouse=True)
def _reset() -> None:
    route_runtime.reset_for_tests()
    yield
    route_runtime.reset_for_tests()


async def _open_agent_tool(route=ROUTE, tool_id: str = "agent-1") -> None:
    await route_runtime.ingest_transcript_event(
        route, _evt("assistant", "tool_use", tool_use_id=tool_id, tool_name="Agent")
    )


def _st(route=ROUTE) -> route_runtime._RouteState:
    return route_runtime._state[route]


# ── idle_source transition rules (A1a) ──────────────────────────────────


async def test_end_of_turn_sets_idle_source_transcript():
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "text", stop_reason="end_turn")
    )
    assert _st().idle_source == "transcript"


async def test_lazy_decay_preserves_idle_source():
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "text", stop_reason="end_turn")
    )
    # Force the IDLE_RECENT deadline into the past, then read.
    _st().idle_clear_at = 0.0
    snap = route_runtime.snapshot(ROUTE)
    assert snap.run_state is RunState.IDLE_CLEARED
    assert _st().idle_source == "transcript"


async def test_pane_clear_on_active_route_sets_idle_source_pane():
    await _open_agent_tool()
    snap = await route_runtime.mark_pane_idle(ROUTE)
    assert snap.run_state is RunState.IDLE_CLEARED
    assert _st().idle_source == "pane"


async def test_pane_clear_on_already_idle_route_preserves_transcript_source():
    """The r2 corruption sequence: transcript end-of-turn → pane clear on the
    now-idle route must NOT overwrite "transcript" with "pane"."""
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "text", stop_reason="end_turn")
    )
    await route_runtime.mark_pane_idle(ROUTE)
    assert _st().idle_source == "transcript"


async def test_idle_source_resets_when_route_leaves_idle():
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "text", stop_reason="end_turn")
    )
    assert _st().idle_source == "transcript"
    await route_runtime.ingest_transcript_event(ROUTE, _evt("user", "text"))
    assert _st().idle_source is None


async def test_session_reset_clears_idle_source_and_stash():
    await _open_agent_tool()
    await route_runtime.mark_pane_idle(ROUTE)
    assert _st().idle_source == "pane"
    assert _st().suspended_tools
    await route_runtime.mark_session_reset(ROUTE)
    assert _st().idle_source is None
    assert _st().suspended_tools == {}


# ── suspended_tools stash (A1b) ─────────────────────────────────────────


async def test_pane_clear_moves_open_tools_into_stash():
    await _open_agent_tool()
    await route_runtime.mark_pane_idle(ROUTE)
    st = _st()
    assert st.open_tools == {}
    assert st.suspended_tools == {"agent-1": False}


async def test_late_tool_result_pairs_against_suspended_stash():
    """Spec test 5: a late parent Agent tool_result after a false pane clear
    pairs against the stash via the normal path — never the unknown-id branch
    (which would preserve IDLE_CLEARED)."""
    await _open_agent_tool()
    await route_runtime.mark_pane_idle(ROUTE)
    snap = await route_runtime.ingest_transcript_event(
        ROUTE, _evt("user", "tool_result", tool_use_id="agent-1")
    )
    # Normal pairing: tool closed, run-state re-derived from the open set.
    assert snap.run_state is RunState.RUNNING
    st = _st()
    assert st.suspended_tools == {}
    assert st.open_tools == {}


async def test_unknown_tool_result_still_ignored_with_empty_stash():
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "text", stop_reason="end_turn")
    )
    snap = await route_runtime.ingest_transcript_event(
        ROUTE, _evt("user", "tool_result", tool_use_id="never-opened")
    )
    assert snap.run_state is RunState.IDLE_RECENT


async def test_end_of_turn_drops_stash():
    await _open_agent_tool()
    await route_runtime.mark_pane_idle(ROUTE)
    assert _st().suspended_tools
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "text", stop_reason="end_turn")
    )
    assert _st().suspended_tools == {}


async def test_user_event_drops_stash():
    await _open_agent_tool()
    await route_runtime.mark_pane_idle(ROUTE)
    assert _st().suspended_tools
    await route_runtime.ingest_transcript_event(ROUTE, _evt("user", "text"))
    assert _st().suspended_tools == {}


async def test_mark_inbound_sent_drops_stash():
    """Spec test 9 (v4 wording fix): a new Telegram prompt reaches the route
    BEFORE its transcript user event — the stash must drop at delivery."""
    await _open_agent_tool()
    await route_runtime.mark_pane_idle(ROUTE)
    assert _st().suspended_tools
    await route_runtime.mark_inbound_sent(ROUTE)
    assert _st().suspended_tools == {}
    # And a later pane-clear + sidechain resurrection sees an EMPTY stash.
    await route_runtime.mark_pane_idle(ROUTE)
    snap = await route_runtime.mark_subagent_activity(ROUTE)
    assert snap.run_state is RunState.RUNNING
    assert snap.open_tools == frozenset()


async def test_clear_route_drops_stash_state():
    await _open_agent_tool()
    await route_runtime.mark_pane_idle(ROUTE)
    route_runtime.clear_route(ROUTE)
    assert ROUTE not in route_runtime._state
    # A post-teardown sidechain mark must not seed the route back.
    await route_runtime.mark_subagent_activity(ROUTE)
    assert ROUTE not in route_runtime._state


async def test_clear_routes_for_topic_drops_stash_state():
    await _open_agent_tool()
    await route_runtime.mark_pane_idle(ROUTE)
    route_runtime.clear_routes_for_topic(ROUTE[0], ROUTE[1])
    assert ROUTE not in route_runtime._state


# ── mark_subagent_activity authority matrix (A1) ────────────────────────


async def test_subagent_activity_never_seeds_unseen_route():
    snap = await route_runtime.mark_subagent_activity(ROUTE)
    assert snap.run_state is RunState.IDLE_CLEARED
    assert ROUTE not in route_runtime._state


async def test_subagent_activity_noop_on_transcript_idle():
    """Spec test 4: transcript end-of-turn then a stray sidechain write must
    stay idle — the transcript has spoken."""
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "text", stop_reason="end_turn")
    )
    snap = await route_runtime.mark_subagent_activity(ROUTE)
    assert snap.run_state is RunState.IDLE_RECENT
    assert snap.typing_eligible is False


async def test_subagent_activity_noop_on_idle_with_none_source():
    """An idle route that never recorded an idle source (e.g. after a session
    reset) is not resurrectable."""
    await route_runtime.mark_session_reset(ROUTE)
    snap = await route_runtime.mark_subagent_activity(ROUTE)
    assert snap.run_state is RunState.IDLE_CLEARED


async def test_subagent_activity_resurrects_pane_idle_with_stash():
    """Spec test 2: pane false-clear then sidechain activity → resurrect to
    RUNNING_TOOL with the suspended tools restored."""
    await _open_agent_tool()
    await route_runtime.mark_pane_idle(ROUTE)
    snap = await route_runtime.mark_subagent_activity(ROUTE)
    assert snap.run_state is RunState.RUNNING_TOOL
    assert snap.open_tools == frozenset({"agent-1"})
    assert snap.typing_eligible is True
    assert _st().suspended_tools == {}
    assert _st().idle_source is None
    # Idle deadlines cleared.
    assert snap.idle_clear_at is None


async def test_subagent_activity_resurrects_pane_idle_with_empty_stash():
    # RUNNING route (no open tools) falsely pane-cleared.
    await route_runtime.ingest_transcript_event(ROUTE, _evt("assistant", "text"))
    await route_runtime.mark_pane_idle(ROUTE)
    assert _st().idle_source == "pane"
    snap = await route_runtime.mark_subagent_activity(ROUTE)
    assert snap.run_state is RunState.RUNNING
    assert snap.typing_eligible is True


async def test_subagent_activity_refreshes_running():
    await route_runtime.ingest_transcript_event(ROUTE, _evt("assistant", "text"))
    before = _st().last_event_at
    route_runtime.arm_pane_idle_clear(ROUTE, now=100.0)
    snap = await route_runtime.mark_subagent_activity(ROUTE)
    assert snap.run_state is RunState.RUNNING
    assert snap.last_event_at >= before
    # The pane-idle debounce was re-armed (cancelled).
    assert snap.pane_idle_clear_at is None


async def test_subagent_activity_refreshes_running_tool_without_tool_mutation():
    await _open_agent_tool()
    route_runtime.arm_pane_idle_clear(ROUTE, now=100.0)
    snap = await route_runtime.mark_subagent_activity(ROUTE)
    assert snap.run_state is RunState.RUNNING_TOOL
    assert snap.open_tools == frozenset({"agent-1"})
    assert snap.pane_idle_clear_at is None


async def test_subagent_activity_never_overrides_transcript_waiting():
    await route_runtime.ingest_transcript_event(
        ROUTE,
        _evt("assistant", "tool_use", tool_use_id="ask-1", tool_name="AskUserQuestion"),
    )
    snap = await route_runtime.mark_subagent_activity(ROUTE)
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.waiting_on_user_tools == frozenset({"ask-1"})


async def test_subagent_activity_never_overrides_pane_bit_waiting():
    await route_runtime.ingest_transcript_event(ROUTE, _evt("assistant", "text"))
    await route_runtime.mark_interactive_pending(ROUTE)
    assert route_runtime.snapshot(ROUTE).run_state is RunState.WAITING_ON_USER
    snap = await route_runtime.mark_subagent_activity(ROUTE)
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.interactive_pending is True


# ── keep-alive + race semantics (A4 / narrowed card claim) ──────────────


async def test_long_subagent_run_survives_transient_idle_pane_frames():
    """Spec test 1: a long subagent run with transient confirmed-idle pane
    frames stays busy as long as sidechain activity keeps arriving."""
    await _open_agent_tool()
    now = 1000.0
    # Pane frame looks idle → poller arms the debounce.
    route_runtime.arm_pane_idle_clear(ROUTE, now=now)
    # Sidechain activity lands before the deadline → re-arms (cancels).
    await route_runtime.mark_subagent_activity(ROUTE)
    assert (
        route_runtime.pane_idle_clear_due(
            ROUTE, now=now + IDLE_CLEAR_DELAY_SECONDS + 10.0
        )
        is False
    )
    snap = route_runtime.snapshot(ROUTE)
    assert snap.run_state is RunState.RUNNING_TOOL
    assert snap.typing_eligible is True


async def test_resurrection_final_state_after_queued_status_clear_drains():
    """Spec test 2 (race): a status clear already enqueued before resurrection
    MAY still delete the card — assert only the FINAL run-state truth after
    the queued clear drains: run_state correct, typing on. No "card survives"
    claim."""
    await _open_agent_tool()
    route_runtime.mark_status_card_published(ROUTE, 555)
    now = 2000.0
    route_runtime.arm_pane_idle_clear(ROUTE, now=now)
    fired = await route_runtime.commit_pane_idle_clear(
        ROUTE, now=now + IDLE_CLEAR_DELAY_SECONDS
    )
    assert fired is True  # the poller would now enqueue a status clear
    # Sidechain activity resurrects before the queue drains.
    snap = await route_runtime.mark_subagent_activity(ROUTE)
    assert snap.run_state is RunState.RUNNING_TOOL
    # The queued clear drains (message_queue deletes the card).
    route_runtime.mark_status_card_cleared(ROUTE)
    final = route_runtime.snapshot(ROUTE)
    assert final.run_state is RunState.RUNNING_TOOL
    assert final.typing_eligible is True
    assert final.status_card_visible is False  # accepted residual — re-published
    # on the next active status tick by message_queue, not by route_runtime.


async def test_corruption_regression_transcript_idle_pane_clear_sidechain():
    """Spec test 3: transcript end-of-turn → pane clear on the already-idle
    route → sidechain write ⇒ stays idle."""
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "text", stop_reason="end_turn")
    )
    await route_runtime.mark_pane_idle(ROUTE)
    snap = await route_runtime.mark_subagent_activity(ROUTE)
    assert snap.run_state is RunState.IDLE_CLEARED
    assert snap.typing_eligible is False


async def test_commit_pane_idle_clear_stashes_tools():
    """The debounced production clear shares the reconciliation — it must
    stash (not drop) the open tools too."""
    await _open_agent_tool()
    now = 3000.0
    route_runtime.arm_pane_idle_clear(ROUTE, now=now)
    fired = await route_runtime.commit_pane_idle_clear(
        ROUTE, now=now + IDLE_CLEAR_DELAY_SECONDS
    )
    assert fired is True
    st = _st()
    assert st.suspended_tools == {"agent-1": False}
    assert st.idle_source == "pane"


# ── bot fan-out helper ───────────────────────────────────────────────────


async def test_bot_fanout_marks_each_route_once_per_tick(monkeypatch):
    """bot.mark_subagent_activity_for_parents resolves bound routes per parent
    session and marks each route at most once per tick."""
    from cctelegram import bot as bot_module

    calls: list[route_runtime.Route] = []

    async def fake_mark(route: route_runtime.Route):
        calls.append(route)
        return route_runtime.snapshot(route)

    monkeypatch.setattr(route_runtime, "mark_subagent_activity", fake_mark)

    async def fake_find(session_id: str):
        # Both parents resolve to the same (user, window, thread) binding.
        return [(1, "@7", 42)]

    monkeypatch.setattr(bot_module.session_manager, "find_users_for_session", fake_find)

    await bot_module.mark_subagent_activity_for_parents({"parent-a", "parent-b"})
    assert calls == [(1, 42, "@7")]
