"""GH #44 unit tests — the ``background_agents`` snapshot-time projection.

Pins the plan-v4 contract:

  - Projection: stored-idle + live background key → snapshot RUNNING +
    typing_eligible, with the STORED run_state untouched (no stranded
    RUNNING after clears).
  - SET qualification matrix (idle path is timestamp-qualified; active /
    WAITING recording is unconditional but foreground-presumed).
  - Launch-time ``is_background`` registration; the end-of-turn prune is
    provenance-only (foreground keys dropped, background keys survive —
    the codex/hermes round-2 silent-tool-gap regression test).
  - Expire-before-classify (an expired key cannot relift via the
    EXISTING-key refresh path).
  - Tombstones: done-clear blocks re-record; a genuine user turn resets;
    a task-notification user event does NOT.
  - §3.6: ``mark_notification_pending`` commits on stored-idle + live
    background key; 🔔 outranks projected Busy.
  - §3.7: task-notification user events re-derive with preserved gates
    (never a forced RUNNING) — the ``interactive_pending ⇔ pane-set
    WAITING`` invariant holds.
"""

from __future__ import annotations

import pytest

from cctelegram import route_runtime
from cctelegram.route_runtime import (
    BG_AGENT_TTL_SECONDS,
    NotificationClearReason,
    NotificationMarkResult,
    RunState,
    TranscriptLifecycleEvent,
)

WF_KEY = "wf-task:wrnmrbn3s"

ROUTE: route_runtime.Route = (1, 42, "@7")
KEY = "a1b2c3d4e5f6a7b89"
KEY2 = "b9f8e7d6c5b4a3210"

# A stable wall-clock origin for the injectable clock.
T0 = 1_750_000_000.0


def _evt(
    role: str = "assistant",
    block: str = "text",
    *,
    tool_use_id: str | None = None,
    tool_name: str | None = None,
    stop_reason: str | None = None,
    timestamp: float | None = None,
    is_task_notification: bool = False,
) -> TranscriptLifecycleEvent:
    return TranscriptLifecycleEvent(
        role=role,  # type: ignore[arg-type]
        block_type=block,  # type: ignore[arg-type]
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        stop_reason=stop_reason,
        timestamp=timestamp,
        is_task_notification=is_task_notification,
    )


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch):
    route_runtime.reset_for_tests()
    monkeypatch.setattr(route_runtime, "_wall_now", lambda: T0)
    yield
    route_runtime.reset_for_tests()


def _st(route=ROUTE) -> route_runtime._RouteState:
    return route_runtime._state[route]


async def _idle_transcript(route=ROUTE, *, end_ts: float = 100.0) -> None:
    """Drive the route to a genuine transcript idle with a known turn stamp."""
    await route_runtime.ingest_transcript_event(
        route, _evt("assistant", "text", stop_reason="end_turn", timestamp=end_ts)
    )
    assert _st(route).idle_source == "transcript"
    assert _st(route).last_assistant_turn_ended_at == end_ts


# ── projection basics ────────────────────────────────────────────────────


async def test_idle_plus_qualified_key_projects_running_and_typing():
    await _idle_transcript(end_ts=100.0)
    snap = await route_runtime.mark_background_agent_activity(ROUTE, KEY, 150.0)
    assert snap.run_state is RunState.RUNNING
    assert snap.typing_eligible is True
    assert snap.background_agents == (KEY,)
    # The STORED state is untouched — the lift is projection-only.
    assert _st().run_state in (RunState.IDLE_RECENT, RunState.IDLE_CLEARED)
    assert _st().idle_source == "transcript"


async def test_snapshot_read_path_applies_the_same_projection():
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_activity(ROUTE, KEY, 150.0)
    snap = route_runtime.snapshot(ROUTE)
    assert snap.run_state is RunState.RUNNING
    assert snap.typing_eligible is True
    assert snap.background_agents == (KEY,)


async def test_done_clear_reports_stored_idle_with_provenance_intact():
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_activity(ROUTE, KEY, 150.0)
    snap = await route_runtime.mark_background_agent_done(ROUTE, KEY)
    assert snap.run_state in (RunState.IDLE_RECENT, RunState.IDLE_CLEARED)
    assert snap.typing_eligible is False
    assert snap.background_agents == ()
    assert _st().idle_source == "transcript"  # no stranded RUNNING, no lost base


async def test_lift_never_touches_non_idle_stored_states():
    # Active RUNNING_TOOL stays RUNNING_TOOL.
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash")
    )
    snap = await route_runtime.mark_background_agent_activity(ROUTE, KEY, 50.0)
    assert snap.run_state is RunState.RUNNING_TOOL
    assert snap.background_agents == (KEY,)  # key recorded under active


async def test_multi_agent_lift_holds_until_last_key_clears():
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_activity(ROUTE, KEY, 150.0)
    await route_runtime.mark_background_agent_activity(ROUTE, KEY2, 160.0)
    snap = await route_runtime.mark_background_agent_done(ROUTE, KEY)
    assert snap.run_state is RunState.RUNNING
    snap = await route_runtime.mark_background_agent_done(ROUTE, KEY2)
    assert snap.run_state in (RunState.IDLE_RECENT, RunState.IDLE_CLEARED)


async def test_lazy_idle_decay_still_runs_under_the_lift():
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_activity(ROUTE, KEY, 150.0)
    _st().idle_clear_at = 0.0  # force the IDLE_RECENT deadline into the past
    snap = route_runtime.snapshot(ROUTE)
    assert snap.run_state is RunState.RUNNING  # projection wins the visible state
    assert _st().run_state is RunState.IDLE_CLEARED  # stored decay happened


# ── SET qualification matrix (idle path) ─────────────────────────────────


async def test_idle_set_rejects_older_or_equal_timestamp():
    await _idle_transcript(end_ts=100.0)
    snap = await route_runtime.mark_background_agent_activity(ROUTE, KEY, 100.0)
    assert snap.background_agents == ()
    snap = await route_runtime.mark_background_agent_activity(ROUTE, KEY, 99.0)
    assert snap.background_agents == ()
    assert snap.run_state in (RunState.IDLE_RECENT, RunState.IDLE_CLEARED)


async def test_idle_set_rejects_none_timestamp():
    await _idle_transcript(end_ts=100.0)
    snap = await route_runtime.mark_background_agent_activity(ROUTE, KEY, None)
    assert snap.background_agents == ()


async def test_idle_set_fails_closed_when_stamp_is_none():
    """The post-restart shape: persisted sidechain offsets keep producing
    batches but ``last_assistant_turn_ended_at`` is gone — no lift."""
    await _idle_transcript(end_ts=100.0)
    _st().last_assistant_turn_ended_at = None
    snap = await route_runtime.mark_background_agent_activity(ROUTE, KEY, 150.0)
    assert snap.background_agents == ()
    assert snap.run_state in (RunState.IDLE_RECENT, RunState.IDLE_CLEARED)


async def test_idle_qualified_set_is_marked_background():
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_activity(ROUTE, KEY, 150.0)
    assert _st().background_agents[KEY].is_background is True


async def test_keys_are_recorded_under_waiting_without_state_mutation():
    # Transcript-set WAITING via an interactive tool_use.
    await route_runtime.ingest_transcript_event(
        ROUTE,
        _evt("assistant", "tool_use", tool_use_id="q1", tool_name="AskUserQuestion"),
    )
    snap = await route_runtime.mark_background_agent_activity(ROUTE, KEY, 50.0)
    assert snap.run_state is RunState.WAITING_ON_USER  # never overridden
    assert snap.background_agents == (KEY,)  # but the key IS captured
    assert _st().background_agents[KEY].is_background is False


async def test_key_normalization_applied_at_the_mark_seams():
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_activity(ROUTE, f"agent-{KEY}", 150.0)
    assert KEY in _st().background_agents
    snap = await route_runtime.mark_background_agent_done(ROUTE, f"agent-{KEY}")
    assert snap.background_agents == ()
    assert KEY in _st().background_agents_done


async def test_marks_never_seed_an_unseen_route():
    snap = await route_runtime.mark_background_agent_activity(ROUTE, KEY, 150.0)
    assert snap.run_state is RunState.IDLE_CLEARED
    assert ROUTE not in route_runtime._state
    snap = await route_runtime.mark_background_agent_launched(ROUTE, KEY)
    assert ROUTE not in route_runtime._state


# ── launch registration + the end-of-turn prune ──────────────────────────


async def test_launch_marks_background_before_any_sidechain_batch():
    await route_runtime.ingest_transcript_event(ROUTE, _evt("user", "text"))
    snap = await route_runtime.mark_background_agent_launched(ROUTE, KEY)
    assert snap.background_agents == (KEY,)
    rec = _st().background_agents[KEY]
    assert rec.is_background is True
    assert rec.last_event_ts is None


async def test_silent_tool_gap_regression_lift_survives_end_of_turn():
    """The codex/hermes round-2 P1 scenario: launch + early batch (ts before
    the turn end) + NO post-end batch → snapshot stays RUNNING after the
    parent's end-of-turn."""
    await route_runtime.ingest_transcript_event(ROUTE, _evt("user", "text"))
    await route_runtime.mark_background_agent_launched(ROUTE, KEY)
    await route_runtime.mark_background_agent_activity(ROUTE, KEY, 90.0)
    snap = await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "text", stop_reason="end_turn", timestamp=100.0)
    )
    assert snap.run_state is RunState.RUNNING
    assert snap.typing_eligible is True
    assert snap.background_agents == (KEY,)
    assert _st().run_state is RunState.IDLE_RECENT  # stored idle committed
    assert _st().last_assistant_turn_ended_at == 100.0  # side effects intact


async def test_end_of_turn_prunes_foreground_keys_unconditionally():
    await route_runtime.ingest_transcript_event(ROUTE, _evt("user", "text"))
    # Active-time recording without launch evidence = foreground-presumed.
    await route_runtime.mark_background_agent_activity(ROUTE, KEY, 90.0)
    assert _st().background_agents[KEY].is_background is False
    snap = await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "text", stop_reason="end_turn", timestamp=100.0)
    )
    assert snap.background_agents == ()
    assert snap.run_state is RunState.IDLE_RECENT


async def test_end_of_turn_prune_fires_even_without_event_timestamp():
    """Provenance-only prune: no timestamp comparison, so a None end-of-turn
    timestamp still drops foreground keys (hermes r2 P2-2)."""
    await route_runtime.ingest_transcript_event(ROUTE, _evt("user", "text"))
    await route_runtime.mark_background_agent_activity(
        ROUTE, KEY, None
    )  # active: recorded
    assert KEY in _st().background_agents
    snap = await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "text", stop_reason="end_turn", timestamp=None)
    )
    assert snap.background_agents == ()


async def test_launch_upgrade_preserves_last_event_ts():
    await route_runtime.ingest_transcript_event(ROUTE, _evt("user", "text"))
    await route_runtime.mark_background_agent_activity(ROUTE, KEY, 90.0)
    await route_runtime.mark_background_agent_launched(ROUTE, KEY)
    rec = _st().background_agents[KEY]
    assert rec.is_background is True
    assert rec.last_event_ts == 90.0  # not clobbered to None (hermes r3 P3-1)


# ── TTL: wall-clock, expire-before-classify ──────────────────────────────


async def test_ttl_expiry_is_lazy_at_snapshot_time(monkeypatch: pytest.MonkeyPatch):
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_activity(ROUTE, KEY, 150.0)
    assert route_runtime.snapshot(ROUTE).run_state is RunState.RUNNING
    monkeypatch.setattr(
        route_runtime, "_wall_now", lambda: T0 + BG_AGENT_TTL_SECONDS + 1
    )
    snap = route_runtime.snapshot(ROUTE)
    assert snap.run_state in (RunState.IDLE_RECENT, RunState.IDLE_CLEARED)
    assert snap.background_agents == ()


async def test_refresh_extends_ttl_even_on_none_ts_batch(
    monkeypatch: pytest.MonkeyPatch,
):
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_activity(ROUTE, KEY, 150.0)
    # Half a TTL later, a batch whose timestamps all failed to parse.
    monkeypatch.setattr(
        route_runtime, "_wall_now", lambda: T0 + BG_AGENT_TTL_SECONDS / 2
    )
    await route_runtime.mark_background_agent_activity(ROUTE, KEY, None)
    rec = _st().background_agents[KEY]
    assert rec.last_seen_wall == T0 + BG_AGENT_TTL_SECONDS / 2  # wall refreshed
    assert rec.last_event_ts == 150.0  # event ts NOT poisoned
    # The lift survives past the original deadline.
    monkeypatch.setattr(
        route_runtime, "_wall_now", lambda: T0 + BG_AGENT_TTL_SECONDS + 1
    )
    assert route_runtime.snapshot(ROUTE).run_state is RunState.RUNNING


async def test_expired_key_cannot_relift_via_existing_refresh(
    monkeypatch: pytest.MonkeyPatch,
):
    """hermes r2 P1-3: step-0 expire-before-classify — a late None-ts batch
    for an expired key re-runs FULL qualification and fails closed."""
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_activity(ROUTE, KEY, 150.0)
    monkeypatch.setattr(
        route_runtime, "_wall_now", lambda: T0 + BG_AGENT_TTL_SECONDS + 1
    )
    snap = await route_runtime.mark_background_agent_activity(ROUTE, KEY, None)
    assert snap.background_agents == ()
    assert KEY not in _st().background_agents  # deleted, not refreshed


# ── tombstones ────────────────────────────────────────────────────────────


async def test_done_tombstone_blocks_re_record_and_re_launch():
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_activity(ROUTE, KEY, 150.0)
    await route_runtime.mark_background_agent_done(ROUTE, KEY)
    snap = await route_runtime.mark_background_agent_activity(ROUTE, KEY, 200.0)
    assert snap.background_agents == ()
    snap = await route_runtime.mark_background_agent_launched(ROUTE, KEY)
    assert snap.background_agents == ()


async def test_genuine_user_turn_resets_tombstones():
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_activity(ROUTE, KEY, 150.0)
    await route_runtime.mark_background_agent_done(ROUTE, KEY)
    await route_runtime.ingest_transcript_event(ROUTE, _evt("user", "text"))
    assert _st().background_agents_done == set()


async def test_task_notification_user_event_preserves_tombstones():
    """The self-defeat test (hermes r1 P1-2): the completion notification
    must not clear the tombstone its own done-mark creates."""
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_activity(ROUTE, KEY, 150.0)
    await route_runtime.mark_background_agent_done(ROUTE, KEY)
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("user", "text", timestamp=300.0, is_task_notification=True)
    )
    assert KEY in _st().background_agents_done


# ── §3.6: notification commits on stored-idle + live bg key ──────────────


async def test_notification_commits_on_projected_busy_route():
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_activity(ROUTE, KEY, 150.0)
    result = await route_runtime.mark_notification_pending(
        ROUTE, set_at=T0 + 10, generation="g1"
    )
    assert result is NotificationMarkResult.COMMITTED_LIVE
    snap = route_runtime.snapshot(ROUTE)
    assert snap.run_state is RunState.WAITING_ON_USER  # 🔔 outranks the lift
    assert snap.typing_eligible is False
    assert snap.notification_pending is True
    # Stored state remains idle throughout — projection-only (§3.6).
    assert _st().run_state in (RunState.IDLE_RECENT, RunState.IDLE_CLEARED)


async def test_notification_clear_returns_route_to_projected_running():
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_activity(ROUTE, KEY, 150.0)
    await route_runtime.mark_notification_pending(ROUTE, set_at=T0 + 10, generation="g")
    snap = await route_runtime.mark_notification_cleared(ROUTE)
    assert snap.run_state is RunState.RUNNING  # bg key still live
    assert snap.notification_pending is False


async def test_notification_still_stale_on_idle_without_bg_keys():
    await _idle_transcript(end_ts=100.0)
    result = await route_runtime.mark_notification_pending(
        ROUTE, set_at=T0 + 10, generation="g1"
    )
    assert result is NotificationMarkResult.STALE_UNLINK
    assert route_runtime.snapshot(ROUTE).notification_pending is False


async def test_notification_ignores_expired_bg_key(monkeypatch: pytest.MonkeyPatch):
    """codex r3 P3-2: the §3.6 liveness check shares the TTL filter."""
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_activity(ROUTE, KEY, 150.0)
    monkeypatch.setattr(
        route_runtime, "_wall_now", lambda: T0 + BG_AGENT_TTL_SECONDS + 1
    )
    result = await route_runtime.mark_notification_pending(
        ROUTE, set_at=T0 + 10, generation="g1"
    )
    assert result is NotificationMarkResult.STALE_UNLINK


# ── WAITING-clear inversion (hermes r2 P2-1) ─────────────────────────────


async def test_transcript_interactive_close_with_live_bg_key_projects_running():
    await route_runtime.ingest_transcript_event(
        ROUTE,
        _evt("assistant", "tool_use", tool_use_id="q1", tool_name="AskUserQuestion"),
    )
    await route_runtime.mark_background_agent_activity(ROUTE, KEY, 50.0)
    # Resolve the picker, end the turn.
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("user", "tool_result", tool_use_id="q1", timestamp=60.0)
    )
    snap = await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "text", stop_reason="end_turn", timestamp=70.0)
    )
    # Foreground prune dropped KEY (no launch evidence) — so re-record it
    # post-turn as a background agent would.
    await route_runtime.mark_background_agent_launched(ROUTE, KEY)
    snap = route_runtime.snapshot(ROUTE)
    assert snap.run_state is RunState.RUNNING
    assert snap.typing_eligible is True


# ── §3.7: task-notification user events — preserved-gate re-derivation ───


async def test_task_notification_with_pane_bit_keeps_waiting():
    """The invariant test (hermes r3 P2): pane bit set + empty open_tools +
    task-notification user event ⇒ committed WAITING, never RUNNING."""
    await route_runtime.ingest_transcript_event(ROUTE, _evt("user", "text"))
    await route_runtime.mark_interactive_pending(ROUTE)
    assert route_runtime.snapshot(ROUTE).run_state is RunState.WAITING_ON_USER
    snap = await route_runtime.ingest_transcript_event(
        ROUTE, _evt("user", "text", timestamp=200.0, is_task_notification=True)
    )
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.interactive_pending is True  # bit preserved AND state matches


async def test_task_notification_clears_notification_only_when_newer():
    await route_runtime.ingest_transcript_event(ROUTE, _evt("user", "text"))
    await route_runtime.mark_notification_pending(ROUTE, set_at=500.0, generation="g")
    # Older task-notification → preserved.
    snap = await route_runtime.ingest_transcript_event(
        ROUTE, _evt("user", "text", timestamp=400.0, is_task_notification=True)
    )
    assert snap.notification_pending is True
    assert snap.run_state is RunState.WAITING_ON_USER
    # Newer task-notification → cleared, route derives RUNNING.
    snap = await route_runtime.ingest_transcript_event(
        ROUTE, _evt("user", "text", timestamp=600.0, is_task_notification=True)
    )
    assert snap.notification_pending is False
    assert snap.run_state is RunState.RUNNING


async def test_task_notification_preserves_suspended_tools():
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Agent")
    )
    await route_runtime.mark_pane_idle(ROUTE)
    assert _st().suspended_tools == {"t1": False}
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("user", "text", timestamp=200.0, is_task_notification=True)
    )
    assert _st().suspended_tools == {"t1": False}  # stash untouched


async def test_task_notification_fires_activity_side_effects():
    """Plan §7 ("ages/debounce fire"): a task-notification user event
    refreshes last_event_at, cancels a pending pane-idle deadline, and
    resets the cleared sentinel — full activity treatment."""
    await _idle_transcript(end_ts=100.0)
    route_runtime.arm_pane_idle_clear(ROUTE, now=1000.0)
    _st().pane_idle_cleared = True  # simulate a prior cleared stretch
    before = _st().last_event_at
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("user", "text", timestamp=200.0, is_task_notification=True)
    )
    assert _st().last_event_at >= before
    assert _st().pane_idle_clear_at is None  # deadline cancelled (re-armed)
    assert _st().pane_idle_cleared is False  # sentinel reset


async def test_task_notification_counts_as_activity():
    await _idle_transcript(end_ts=100.0)
    snap = await route_runtime.ingest_transcript_event(
        ROUTE, _evt("user", "text", timestamp=200.0, is_task_notification=True)
    )
    assert snap.run_state is RunState.RUNNING  # ungated → parent resumed
    assert _st().idle_source is None


async def test_genuine_user_turn_keeps_todays_unconditional_behavior():
    await route_runtime.ingest_transcript_event(ROUTE, _evt("user", "text"))
    await route_runtime.mark_notification_pending(ROUTE, set_at=500.0, generation="g")
    assert route_runtime.snapshot(ROUTE).notification_pending is True
    # A genuine user turn with an OLDER timestamp still clears (unconditional).
    snap = await route_runtime.ingest_transcript_event(
        ROUTE, _evt("user", "text", timestamp=400.0)
    )
    assert snap.notification_pending is False
    assert snap.run_state is RunState.RUNNING


# ── teardown seams ────────────────────────────────────────────────────────


async def test_session_reset_drops_both_structures():
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_activity(ROUTE, KEY, 150.0)
    await route_runtime.mark_background_agent_done(ROUTE, KEY2)
    await route_runtime.mark_session_reset(ROUTE)
    assert _st().background_agents == {}
    assert _st().background_agents_done == set()


async def test_clear_route_drops_state():
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_activity(ROUTE, KEY, 150.0)
    route_runtime.clear_route(ROUTE)
    assert route_runtime.snapshot(ROUTE).run_state is RunState.IDLE_CLEARED
    assert route_runtime.snapshot(ROUTE).background_agents == ()


# ── status-card surface (codex r2 P2 documentation test) ─────────────────


async def test_pane_idle_card_clear_proceeds_while_projected_running():
    """The recorded product decision: typing + digest/dashboard Busy are
    projection-driven; the status CARD stays pane-driven and clears on a
    genuinely idle pane even while the lift holds."""
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_activity(ROUTE, KEY, 150.0)
    route_runtime.arm_pane_idle_clear(ROUTE, now=1000.0)
    assert route_runtime.pane_idle_clear_due(ROUTE, now=1000.0 + 10.0)
    fired = await route_runtime.commit_pane_idle_clear(ROUTE, now=1000.0 + 10.0)
    # The card-clear protocol ran; the visible state stays lifted.
    assert fired is True
    assert _st().pane_idle_cleared is True
    assert route_runtime.snapshot(ROUTE).run_state is RunState.RUNNING


# ── ISSUE-6: the Workflow-tool bracket key (`wf-task:<id>`) ────────────────
#
# Fix 2 reuses the existing background-agent marks VERBATIM with a
# prefix-namespaced `wf-task:<id>` key. These tests PIN that contract (they
# pass on the pre-Fix-2 route_runtime — proving Fix 2 is pure wiring + parser,
# no route_runtime surgery — and guard against regression). The actual lift
# is wired by session_monitor (launch branch + mtime heartbeat) and bot.

WF_KEY = "wf-task:wfk0a1b2c3d4"


async def test_wf_task_key_namespace_isolated_from_agent_id():
    """`wf-task:<id>` passes through normalize as identity (no `agent-`
    prefix) and never collides with an Agent/Task `agentId` key."""
    from cctelegram.utils import normalize_background_agent_key as _norm

    assert _norm(WF_KEY) == WF_KEY
    assert _norm(WF_KEY) != "wfk0a1b2c3d4"
    # A wf-task key and an agentId key coexist as distinct entries.
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_launched(ROUTE, WF_KEY)
    await route_runtime.mark_background_agent_activity(ROUTE, KEY, 150.0)
    assert WF_KEY in _st().background_agents
    assert KEY in _st().background_agents
    assert _st().background_agents[WF_KEY].is_background is True


async def test_wf_task_launch_survives_end_of_turn_prune():
    """ISSUE-6: a Workflow bracket launched on the active parent turn keeps
    its lift after the parent's authoritative end-of-turn (is_background key
    is never pruned) → snapshot projects RUNNING + typing while the workflow
    runs in the background."""
    await route_runtime.ingest_transcript_event(ROUTE, _evt("user", "text"))
    await route_runtime.mark_background_agent_launched(ROUTE, WF_KEY)
    await route_runtime.mark_background_agent_activity(ROUTE, WF_KEY, 90.0)
    snap = await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "text", stop_reason="end_turn", timestamp=100.0)
    )
    assert snap.run_state is RunState.RUNNING
    assert snap.typing_eligible is True
    assert snap.background_agents == (WF_KEY,)
    assert _st().run_state is RunState.IDLE_RECENT


async def test_wf_task_arm_b_relight_commits_notification_on_idle():
    """ISSUE-5 arm B: a stored-idle route with a live `wf-task:` key accepts
    a Notification re-fire (COMMITTED_LIVE) and projects WAITING above the
    lift — 🔔 outranks projected Busy. This is the coupling: Fix 2's bg key
    makes ISSUE-5's arm-B re-light fire instead of STALE_UNLINK."""
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_launched(ROUTE, WF_KEY)
    result = await route_runtime.mark_notification_pending(
        ROUTE, set_at=T0 + 10, generation="g1"
    )
    assert result is NotificationMarkResult.COMMITTED_LIVE
    snap = route_runtime.snapshot(ROUTE)
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.notification_pending is True
    assert snap.typing_eligible is False
    assert _st().run_state in (RunState.IDLE_RECENT, RunState.IDLE_CLEARED)


async def test_wf_task_out_of_order_done_before_launch_stays_closed():
    """Fix 2d fail-closed: a `<task-notification>` close that lands before
    the launch tombstones the key; the later launch no-ops (never lifts)."""
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_done(ROUTE, WF_KEY)
    snap = await route_runtime.mark_background_agent_launched(ROUTE, WF_KEY)
    assert snap.background_agents == ()
    assert WF_KEY in _st().background_agents_done


async def test_wf_task_heartbeat_backstop_ages_out(monkeypatch: pytest.MonkeyPatch):
    """Fix 2c backstop (codex R4 P2): an open bracket whose sidechain stops
    writing (no further mtime-advance heartbeat) freezes its last_seen_wall
    and ages out via BG_AGENT_TTL_SECONDS — a dead/never-closed Workflow
    never lifts Busy indefinitely."""
    await route_runtime.ingest_transcript_event(ROUTE, _evt("user", "text"))
    await route_runtime.mark_background_agent_launched(ROUTE, WF_KEY)
    snap = await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "text", stop_reason="end_turn", timestamp=100.0)
    )
    assert snap.run_state is RunState.RUNNING  # lifted while open
    monkeypatch.setattr(
        route_runtime, "_wall_now", lambda: T0 + BG_AGENT_TTL_SECONDS + 1
    )
    snap = route_runtime.snapshot(ROUTE)
    assert snap.run_state in (RunState.IDLE_RECENT, RunState.IDLE_CLEARED)
    assert snap.background_agents == ()


# ── reconciler seed seam (PR-1 Half B / B1-FIX) ──────────────────────────────
#
# The restart reconciler re-arms a still-running Workflow's busy lift from the
# filesystem, but a backgrounded-Workflow parent has NO _RouteState at startup
# (seed_open_tools no-ops on the zero pending tools of an ended turn), so
# mark_background_agent_launched would no-op (st is None). The seed seam creates
# an IDLE_CLEARED state AND records the launch in ONE critical section.


async def test_seed_idle_and_launched_on_unseen_route_projects_running():
    assert ROUTE not in route_runtime._state
    snap = await route_runtime.seed_idle_and_mark_background_agent_launched(ROUTE, KEY)
    assert snap.run_state is RunState.RUNNING  # projection lift (typing on)
    assert snap.typing_eligible is True
    assert snap.background_agents == (KEY,)
    # Stored state is genuinely IDLE (projection-only lift) + observed.
    assert _st().run_state is RunState.IDLE_CLEARED
    assert _st().seen is True
    assert _st().background_agents[KEY].is_background is True


async def test_seed_idle_is_noop_seed_when_route_already_has_state():
    """On a route that already has live state (the normal live launch path) the
    seed is a no-op; the launch is recorded exactly like the unseeded mark —
    identical behavior so switching the bot fan-out to this seam is safe."""
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash")
    )
    snap = await route_runtime.seed_idle_and_mark_background_agent_launched(ROUTE, KEY)
    assert snap.run_state is RunState.RUNNING_TOOL  # active state NOT clobbered
    assert snap.background_agents == (KEY,)
    assert _st().background_agents[KEY].is_background is True


async def test_seed_idle_respects_done_tombstone():
    """A tombstoned key is not relit by the seam (fail-closed; the B2 gate-on-
    close is the primary guard but the seam must also respect an in-memory tomb)."""
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_activity(ROUTE, KEY, 150.0)
    await route_runtime.mark_background_agent_done(ROUTE, KEY)
    assert KEY in _st().background_agents_done
    snap = await route_runtime.seed_idle_and_mark_background_agent_launched(ROUTE, KEY)
    assert snap.background_agents == ()  # tombstone holds → no lift
    assert snap.run_state in (RunState.IDLE_RECENT, RunState.IDLE_CLEARED)


# ── Fix #1: bg-heartbeat clears a §3.6 projected-busy notification ──────────
# (the dominant 30-min typing-dark strand — a background agent works while the
# parent is idle, so the poller's PANE_RUNNING clear can never fire and only
# the 30-min TTL clears the 🔔. A PLAIN background-agent heartbeat strictly
# after set_at is positive proof the bg work resumed → clear, scoped HARD to
# routes with NO live Workflow `wf-task:` key so a genuine Workflow Bash-
# approval gate is never auto-dismissed.)

_MARGIN = (
    route_runtime.NOTIFY_BG_CLEAR_MARGIN_S
    if hasattr(route_runtime, "NOTIFY_BG_CLEAR_MARGIN_S")
    else 1.5
)


async def _idle_with_committed_bg_notification(
    key: str = KEY,
    *,
    end_ts: float = T0 + 5,
    evt_ts: float = T0 + 8,
    set_at: float = T0 + 10,
) -> None:
    """Drive ROUTE to: stored-idle + a live bg key + a §3.6-committed 🔔.

    All on the wall-clock T0 scale so the event_ts↔set_at strict-newer
    comparison is meaningful (in production both are epoch seconds; the harness
    otherwise mixes small JSONL stamps with huge wall stamps)."""
    await _idle_transcript(end_ts=end_ts)
    await route_runtime.mark_background_agent_activity(ROUTE, key, evt_ts)
    result = await route_runtime.mark_notification_pending(
        ROUTE, set_at=set_at, generation="g1"
    )
    assert result is NotificationMarkResult.COMMITTED_LIVE
    snap = route_runtime.snapshot(ROUTE)
    assert snap.notification_pending is True
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.typing_eligible is False


async def test_bg_committed_notification_cleared_by_newer_plain_agent_heartbeat(
    monkeypatch,
):
    """RED pre-fix: the §3.6 idle('transcript') heartbeat falls through the
    no-op branches and the bit holds to TTL. Post-fix: a plain heartbeat after
    set_at+margin clears it (reason BG_RUNNING) → projected RUNNING + typing."""
    await _idle_with_committed_bg_notification()
    monkeypatch.setattr(route_runtime, "_wall_now", lambda: T0 + 20)
    snap = await route_runtime.mark_background_agent_activity(ROUTE, KEY, T0 + 15)
    assert snap.notification_pending is False
    assert snap.notification_clear_reason is NotificationClearReason.BG_RUNNING
    assert snap.run_state is RunState.RUNNING
    assert snap.typing_eligible is True


async def test_bg_heartbeat_within_margin_does_not_clear(monkeypatch):
    """A heartbeat observed within NOTIFY_BG_CLEAR_MARGIN_S of set_at must NOT
    clear (mirrors the pane-running margin — guards a same-tick pre-prompt frame)."""
    await _idle_with_committed_bg_notification(set_at=T0 + 10)
    monkeypatch.setattr(route_runtime, "_wall_now", lambda: T0 + 10 + _MARGIN / 2)
    snap = await route_runtime.mark_background_agent_activity(ROUTE, KEY, T0 + 11)
    assert snap.notification_pending is True
    assert snap.run_state is RunState.WAITING_ON_USER


async def test_buffered_pre_notification_bg_heartbeat_preserves_bit(monkeypatch):
    """A heartbeat whose event_ts is NOT strictly newer than set_at (an older
    buffered flush) must NOT clear, even when observed past the margin."""
    await _idle_with_committed_bg_notification(set_at=T0 + 10)
    monkeypatch.setattr(route_runtime, "_wall_now", lambda: T0 + 20)
    snap = await route_runtime.mark_background_agent_activity(ROUTE, KEY, T0 + 10)
    assert snap.notification_pending is True
    # And a None event_ts (parse failure) also fails closed.
    snap = await route_runtime.mark_background_agent_activity(ROUTE, KEY, None)
    assert snap.notification_pending is True


async def test_wf_task_committed_notification_not_cleared_by_heartbeat(monkeypatch):
    """SAFETY: a route whose live bg key is a Workflow `wf-task:` key must NOT
    have its §3.6 🔔 auto-cleared — the dir-wide heartbeat collapses all the
    Workflow's sub-agents, so a sibling's write must never dismiss what may be
    one sub-agent's genuine Bash-approval gate."""
    await _idle_with_committed_bg_notification(key=WF_KEY)
    monkeypatch.setattr(route_runtime, "_wall_now", lambda: T0 + 20)
    snap = await route_runtime.mark_background_agent_activity(ROUTE, WF_KEY, T0 + 15)
    assert snap.notification_pending is True
    assert snap.run_state is RunState.WAITING_ON_USER


async def test_mixed_route_plain_heartbeat_held_when_wf_task_live(monkeypatch):
    """A route with BOTH a plain Agent key AND a live Workflow key: a plain
    heartbeat must NOT clear (a live wf-task key could harbor a blocked gate)."""
    await _idle_transcript(end_ts=T0 + 5)
    await route_runtime.mark_background_agent_activity(ROUTE, KEY, T0 + 8)
    await route_runtime.mark_background_agent_activity(ROUTE, WF_KEY, T0 + 8)
    result = await route_runtime.mark_notification_pending(
        ROUTE, set_at=T0 + 10, generation="g1"
    )
    assert result is NotificationMarkResult.COMMITTED_LIVE
    monkeypatch.setattr(route_runtime, "_wall_now", lambda: T0 + 20)
    snap = await route_runtime.mark_background_agent_activity(ROUTE, KEY, T0 + 15)
    assert snap.notification_pending is True
    assert snap.run_state is RunState.WAITING_ON_USER


async def test_stored_waiting_notification_not_cleared_by_bg_heartbeat(monkeypatch):
    """A foreground Workflow-approval 🔔 (stored RUNNING_TOOL → WAITING via the
    bit) must NOT be cleared by a bg heartbeat — the shape gate requires stored
    IDLE, so the ISSUE-5 foreground contract is fully preserved."""
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash")
    )
    assert _st().run_state is RunState.RUNNING_TOOL
    result = await route_runtime.mark_notification_pending(
        ROUTE, set_at=T0 + 10, generation="g1"
    )
    assert result is NotificationMarkResult.COMMITTED_LIVE
    assert route_runtime.snapshot(ROUTE).run_state is RunState.WAITING_ON_USER
    monkeypatch.setattr(route_runtime, "_wall_now", lambda: T0 + 20)
    snap = await route_runtime.mark_background_agent_activity(ROUTE, KEY, T0 + 15)
    assert snap.notification_pending is True
    assert snap.run_state is RunState.WAITING_ON_USER


async def test_sibling_plain_heartbeat_holds_notification(monkeypatch):
    """SAFETY (hermes P1): with TWO live plain Agents, a heartbeat from one
    (KEY2) must NOT clear a 🔔 that may belong to the other (KEY1) — the bit has
    no per-agent linkage, so the clear fails closed unless the live set is the
    single heartbeating key."""
    await _idle_transcript(end_ts=T0 + 5)
    await route_runtime.mark_background_agent_activity(ROUTE, KEY, T0 + 8)
    await route_runtime.mark_background_agent_activity(ROUTE, KEY2, T0 + 8)
    result = await route_runtime.mark_notification_pending(
        ROUTE, set_at=T0 + 10, generation="g1"
    )
    assert result is NotificationMarkResult.COMMITTED_LIVE
    monkeypatch.setattr(route_runtime, "_wall_now", lambda: T0 + 20)
    snap = await route_runtime.mark_background_agent_activity(ROUTE, KEY2, T0 + 15)
    assert snap.notification_pending is True  # held — sibling proof is not enough
    assert snap.run_state is RunState.WAITING_ON_USER


async def test_no_live_bg_key_holds_notification(monkeypatch):
    """If the only bg key has expired (no live bg work), TTL still owns the bit:
    a heartbeat whose own key fails the idle re-record qualification leaves the
    live set empty, so the singleton gate cannot clear."""
    await _idle_with_committed_bg_notification(set_at=T0 + 10)
    # Jump the wall clock past the BG_AGENT_TTL so the recorded key expires, and
    # send an OLDER event_ts so the NEW-key idle re-record qualification fails
    # (event_ts must exceed last_assistant_turn_ended_at = T0+5).
    monkeypatch.setattr(
        route_runtime, "_wall_now", lambda: T0 + BG_AGENT_TTL_SECONDS + 5
    )
    snap = await route_runtime.mark_background_agent_activity(ROUTE, KEY, T0 + 4)
    assert snap.notification_pending is True
    assert snap.notification_clear_reason is not NotificationClearReason.BG_RUNNING
