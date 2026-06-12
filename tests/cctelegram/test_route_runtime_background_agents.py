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
    NotificationMarkResult,
    RunState,
    TranscriptLifecycleEvent,
)

ROUTE: route_runtime.Route = (1, 42, "@7")
KEY = "a092b6b478733eef0"
KEY2 = "ac36ad1b438c51f11"

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
