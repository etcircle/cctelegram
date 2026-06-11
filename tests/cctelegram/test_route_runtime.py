"""Unit tests for ``cctelegram.route_runtime`` — the run-state / context-usage
/ idle-clear snapshot state machine.

Covers the transition table plus the snapshot interface:

  - Snapshots are frozen — mutating internal state does not change a
    captured snapshot.
  - Per-route locks serialise within a route but do **not** serialise
    across routes.
  - ``mark_session_reset`` drops in-flight ``open_tools`` + context_usage.
  - Status card publish/clear bookkeeping mutates only the
    ``status_card_*`` snapshot fields.
  - ``parse_pending_tools_from_jsonl`` startup replay parsing.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from cctelegram import route_runtime
from cctelegram.route_runtime import (
    ContextUsage,
    RunState,
    TranscriptLifecycleEvent,
)


ROUTE: route_runtime.Route = (1, 42, "@7")
ROUTE_2: route_runtime.Route = (1, 99, "@9")


def _evt(
    role: str = "assistant",
    block: str = "text",
    *,
    tool_use_id: str | None = None,
    tool_name: str | None = None,
    stop_reason: str | None = None,
) -> TranscriptLifecycleEvent:
    """Test-side TranscriptLifecycleEvent constructor with safe defaults."""
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


# ── default snapshot ────────────────────────────────────────────────────


def test_default_snapshot_for_unknown_route():
    snap = route_runtime.snapshot(ROUTE)
    assert snap.run_state is RunState.IDLE_CLEARED
    assert snap.open_tools == frozenset()
    assert snap.waiting_on_user_tools == frozenset()
    assert snap.context_usage is None
    assert snap.idle_clear_at is None
    assert snap.pane_idle_clear_at is None
    assert snap.typing_eligible is False
    assert snap.status_card_visible is False
    assert snap.status_card_msg_id is None


# ── transition table ────────────────────────────────────────────────────


async def test_tool_use_opens_running_tool():
    snap = await route_runtime.ingest_transcript_event(
        ROUTE,
        _evt("assistant", "tool_use", tool_use_id="tool-1", tool_name="Bash"),
    )
    assert snap.run_state is RunState.RUNNING_TOOL
    assert snap.open_tools == frozenset({"tool-1"})
    assert snap.waiting_on_user_tools == frozenset()
    assert snap.typing_eligible is True


async def test_interactive_tool_opens_waiting_on_user():
    snap = await route_runtime.ingest_transcript_event(
        ROUTE,
        _evt(
            "assistant",
            "tool_use",
            tool_use_id="tool-1",
            tool_name="AskUserQuestion",
        ),
    )
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.waiting_on_user_tools == frozenset({"tool-1"})
    # WAITING_ON_USER is not typing-eligible — the user is the one acting.
    assert snap.typing_eligible is False


async def test_tool_result_closes_slot():
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash")
    )
    snap = await route_runtime.ingest_transcript_event(
        ROUTE, _evt("user", "tool_result", tool_use_id="t1")
    )
    assert snap.run_state is RunState.RUNNING
    assert snap.open_tools == frozenset()


async def test_close_interactive_tool_while_noninteractive_open_drops_to_running_tool():
    """Mixed parallel tools: closing the interactive tool while a
    non-interactive tool stays open must re-derive WAITING_ON_USER →
    RUNNING_TOOL from the REMAINING open set. Regression guard ported from the
    deleted test_busy_indicator.py mixed-parallel case (Hermes 8c P2) — the
    state machine derives run-state from open_tools, but the dynamic
    tool_result-on-interactive-while-other-open transition needs its own lock."""
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="bash-1", tool_name="Bash")
    )
    assert route_runtime.snapshot(ROUTE).run_state is RunState.RUNNING_TOOL
    await route_runtime.ingest_transcript_event(
        ROUTE,
        _evt("assistant", "tool_use", tool_use_id="ask-1", tool_name="AskUserQuestion"),
    )
    assert route_runtime.snapshot(ROUTE).run_state is RunState.WAITING_ON_USER
    # Close the interactive tool; Bash remains open → back to RUNNING_TOOL.
    snap = await route_runtime.ingest_transcript_event(
        ROUTE, _evt("user", "tool_result", tool_use_id="ask-1")
    )
    assert snap.run_state is RunState.RUNNING_TOOL
    assert snap.open_tools == frozenset({"bash-1"})


async def test_tool_result_stale_id_ignored():
    snap = await route_runtime.ingest_transcript_event(
        ROUTE, _evt("user", "tool_result", tool_use_id="never-opened")
    )
    # Unknown id → no state change, stays at default IDLE_CLEARED.
    assert snap.run_state is RunState.IDLE_CLEARED


async def test_end_of_turn_text_idle_recent():
    snap = await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "text", stop_reason="end_turn")
    )
    assert snap.run_state is RunState.IDLE_RECENT
    assert snap.idle_clear_at is not None
    assert snap.idle_clear_at > snap.last_event_at


async def test_idle_recent_decays_on_read():
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "text", stop_reason="end_turn")
    )
    # Force decay by reaching past the deadline.
    state = route_runtime._state[ROUTE]
    state.idle_clear_at = 0.0  # immediate decay
    snap = route_runtime.snapshot(ROUTE)
    assert snap.run_state is RunState.IDLE_CLEARED


async def test_thinking_lights_up_running_when_idle():
    snap = await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "thinking")
    )
    assert snap.run_state is RunState.RUNNING


async def test_thinking_preserves_running_tool():
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash")
    )
    snap = await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "thinking")
    )
    assert snap.run_state is RunState.RUNNING_TOOL


# ── mark_* mutations ────────────────────────────────────────────────────


async def test_mark_inbound_sent_idle_to_running():
    snap = await route_runtime.mark_inbound_sent(ROUTE)
    assert snap.run_state is RunState.RUNNING


async def test_mark_inbound_sent_preserves_running_tool():
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash")
    )
    snap = await route_runtime.mark_inbound_sent(ROUTE)
    assert snap.run_state is RunState.RUNNING_TOOL


async def test_mark_pane_idle_preserves_waiting_on_user():
    await route_runtime.ingest_transcript_event(
        ROUTE,
        _evt(
            "assistant",
            "tool_use",
            tool_use_id="t1",
            tool_name="AskUserQuestion",
        ),
    )
    snap = await route_runtime.mark_pane_idle(ROUTE)
    assert snap.run_state is RunState.WAITING_ON_USER


async def test_mark_pane_idle_drops_lingering_tools():
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash")
    )
    snap = await route_runtime.mark_pane_idle(ROUTE)
    assert snap.run_state is RunState.IDLE_CLEARED
    assert snap.open_tools == frozenset()


# ── debounced pane-idle card-clear triad (8b) ────────────────────────────
#
# These exercise the route_runtime-owned debounce in isolation, with an
# injected ``now`` so the deadline timing is deterministic (no wall clock).
# The DELAY is route_runtime.IDLE_CLEAR_DELAY_SECONDS.

_DELAY = route_runtime.IDLE_CLEAR_DELAY_SECONDS


async def test_arm_pane_idle_clear_sets_deadline():
    snap = route_runtime.arm_pane_idle_clear(ROUTE, now=100.0)
    assert snap.pane_idle_clear_at == 100.0 + _DELAY
    # Not yet due immediately after arming.
    assert route_runtime.pane_idle_clear_due(ROUTE, now=100.0) is False


async def test_arm_is_idempotent_does_not_push_deadline_forward():
    """A second arm during the same idle stretch must NOT extend the
    deadline (legacy ``state is None`` arm only fires once)."""
    route_runtime.arm_pane_idle_clear(ROUTE, now=100.0)
    snap = route_runtime.arm_pane_idle_clear(ROUTE, now=102.0)
    # Still anchored to the first observation, not 102 + DELAY.
    assert snap.pane_idle_clear_at == 100.0 + _DELAY


async def test_pane_idle_clear_not_due_before_delay():
    route_runtime.arm_pane_idle_clear(ROUTE, now=100.0)
    # One tick before the deadline.
    assert route_runtime.pane_idle_clear_due(ROUTE, now=100.0 + _DELAY - 0.001) is False


async def test_pane_idle_clear_due_at_and_after_deadline():
    route_runtime.arm_pane_idle_clear(ROUTE, now=100.0)
    assert route_runtime.pane_idle_clear_due(ROUTE, now=100.0 + _DELAY) is True
    assert route_runtime.pane_idle_clear_due(ROUTE, now=100.0 + _DELAY + 5) is True


async def test_unarmed_route_is_never_due():
    """``_process_idle_clear_only`` relies on this: no arm → never commit."""
    assert route_runtime.pane_idle_clear_due(ROUTE, now=1_000_000.0) is False


async def test_commit_pane_idle_clear_reconciles_and_latches():
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash")
    )
    route_runtime.arm_pane_idle_clear(ROUTE, now=100.0)
    assert await route_runtime.commit_pane_idle_clear(ROUTE, now=100.0 + _DELAY) is True
    snap = route_runtime.snapshot(ROUTE)
    # Same reconciliation as mark_pane_idle.
    assert snap.run_state is RunState.IDLE_CLEARED
    assert snap.open_tools == frozenset()
    # Deadline dropped; cleared sentinel latched.
    assert snap.pane_idle_clear_at is None


async def test_commit_then_arm_is_noop_until_activity():
    """After a commit, re-arming during the same idle stretch is a no-op
    (mirrors the legacy ``_idle_state[key] == 'cleared'`` early return) so
    the card-clear fires exactly once."""
    route_runtime.arm_pane_idle_clear(ROUTE, now=100.0)
    await route_runtime.commit_pane_idle_clear(ROUTE, now=100.0 + _DELAY)
    snap = route_runtime.arm_pane_idle_clear(ROUTE, now=200.0)
    assert snap.pane_idle_clear_at is None  # still cleared, not re-armed
    assert route_runtime.pane_idle_clear_due(ROUTE, now=10_000.0) is False


async def test_commit_preserves_waiting_on_user():
    """An open interactive prompt must survive a debounced clear, exactly
    like mark_pane_idle.

    GH #42 leg 2 (W2a) tightened the contract: commit on a WAITING route
    now returns False WITHOUT consuming the deadline or latching the
    cleared sentinel — the pre-fix consume+latch (True) permanently
    disarmed the net when a notification-TTL retract later dropped the
    route back to an active state (the 2026-06-11 stuck-route incident)."""
    await route_runtime.ingest_transcript_event(
        ROUTE,
        _evt("assistant", "tool_use", tool_use_id="auq", tool_name="AskUserQuestion"),
    )
    route_runtime.arm_pane_idle_clear(ROUTE, now=100.0)
    assert (
        await route_runtime.commit_pane_idle_clear(ROUTE, now=100.0 + _DELAY) is False
    )
    snap = route_runtime.snapshot(ROUTE)
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.pane_idle_clear_at == 100.0 + _DELAY  # NOT consumed (W2a)


async def test_transcript_activity_rearms_pending_clear():
    """c313657 guard: a transcript event during the debounce window cancels
    the pending clear so the card stays up."""
    route_runtime.arm_pane_idle_clear(ROUTE, now=100.0)
    assert route_runtime.pane_idle_clear_due(ROUTE, now=100.0 + _DELAY) is True
    # Real activity lands before the poller commits.
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="t9", tool_name="Bash")
    )
    # Pending clear cancelled — no longer due.
    assert route_runtime.pane_idle_clear_due(ROUTE, now=100.0 + _DELAY) is False
    assert route_runtime.snapshot(ROUTE).pane_idle_clear_at is None


async def test_inbound_sent_rearms_pending_clear():
    route_runtime.arm_pane_idle_clear(ROUTE, now=100.0)
    await route_runtime.mark_inbound_sent(ROUTE)
    assert route_runtime.pane_idle_clear_due(ROUTE, now=100.0 + _DELAY) is False


async def test_commit_noops_if_rearmed_after_lockless_due_check():
    """TOCTOU re-validation (Codex 8b P1).

    ``status_polling`` checks ``pane_idle_clear_due`` WITHOUT the lock, then
    ``await``\\s ``commit_pane_idle_clear``. If a transcript event lands in
    between (re-arming → cancelling the deadline), commit must re-check under
    the lock and NO-OP — committing the now-stale clear would blank the card
    mid-turn after fresh activity. Without the in-lock re-validation this test
    fails (run-state would be IDLE_CLEARED)."""
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash")
    )
    route_runtime.arm_pane_idle_clear(ROUTE, now=100.0)
    # Caller's lockless due-check sees True at the deadline...
    assert route_runtime.pane_idle_clear_due(ROUTE, now=100.0 + _DELAY) is True
    # ...but real activity lands (re-arm cancels the deadline) before commit.
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="t2", tool_name="Bash")
    )
    # Commit with the stale 'due' now must NOT clear: it returns False (the
    # explicit signal status_polling uses to decide whether to enqueue the
    # clear), and run-state stays running.
    assert (
        await route_runtime.commit_pane_idle_clear(ROUTE, now=100.0 + _DELAY) is False
    )
    snap = route_runtime.snapshot(ROUTE)
    assert snap.run_state is RunState.RUNNING_TOOL
    assert snap.pane_idle_clear_at is None  # cancelled by the re-arm


async def test_commit_noops_if_rearmed_to_waiting_on_user():
    """Re-arm to a NON-running state must STILL no-op the clear (Codex 8b
    re-review P1/P2). An AskUserQuestion landing between the lockless due-check
    and the lock puts the route in WAITING_ON_USER and cancels the deadline; a
    run-state proxy would mis-read WAITING_ON_USER as a legitimate clear, but
    the explicit bool returns False so status_polling does NOT blank the card."""
    route_runtime.arm_pane_idle_clear(ROUTE, now=100.0)
    assert route_runtime.pane_idle_clear_due(ROUTE, now=100.0 + _DELAY) is True
    await route_runtime.ingest_transcript_event(
        ROUTE,
        _evt("assistant", "tool_use", tool_use_id="auq", tool_name="AskUserQuestion"),
    )
    assert route_runtime.snapshot(ROUTE).run_state is RunState.WAITING_ON_USER
    # Non-running, but commit must still NOT clear (deadline was cancelled).
    assert (
        await route_runtime.commit_pane_idle_clear(ROUTE, now=100.0 + _DELAY) is False
    )


async def test_activity_after_commit_reenables_arming():
    """After a clear, real activity resets the cleared sentinel so the next
    idle stretch can re-arm and fire again (a new turn → new debounce)."""
    route_runtime.arm_pane_idle_clear(ROUTE, now=100.0)
    await route_runtime.commit_pane_idle_clear(ROUTE, now=100.0 + _DELAY)
    # New turn.
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="t2", tool_name="Bash")
    )
    # Now arming works again with a fresh deadline.
    snap = route_runtime.arm_pane_idle_clear(ROUTE, now=300.0)
    assert snap.pane_idle_clear_at == 300.0 + _DELAY


async def test_reset_pane_idle_clear_cancels_and_reenables():
    """Pane-running observation cancels a pending clear AND resets the
    cleared sentinel (mirrors the legacy ``_idle_state.pop`` in the
    running branch)."""
    route_runtime.arm_pane_idle_clear(ROUTE, now=100.0)
    route_runtime.reset_pane_idle_clear(ROUTE)
    assert route_runtime.snapshot(ROUTE).pane_idle_clear_at is None
    # Re-arming works immediately (sentinel was cleared, not latched).
    snap = route_runtime.arm_pane_idle_clear(ROUTE, now=200.0)
    assert snap.pane_idle_clear_at == 200.0 + _DELAY


async def test_session_reset_clears_pending_pane_idle():
    route_runtime.arm_pane_idle_clear(ROUTE, now=100.0)
    snap = await route_runtime.mark_session_reset(ROUTE)
    assert snap.pane_idle_clear_at is None
    assert route_runtime.pane_idle_clear_due(ROUTE, now=10_000.0) is False


async def test_mark_session_reset_drops_open_tools_and_usage():
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash")
    )
    route_runtime.update_context_usage(ROUTE, 50_000, "claude-opus-4-7")
    snap = await route_runtime.mark_session_reset(ROUTE)
    assert snap.run_state is RunState.IDLE_CLEARED
    assert snap.open_tools == frozenset()
    assert snap.context_usage is None


# ── pane_interactive_pending: promote / clear / branch-scoped reclaim ─────
#
# Bug 1: while Claude Code buffers an interactive tool_use in JSONL, the poller
# PROMOTES an active RUNNING route to WAITING_ON_USER via
# ``mark_interactive_pending`` (lower authority than the transcript). The
# transcript reclaim (and ``mark_interactive_cleared`` / ``mark_session_reset``)
# retract it. Invariant: ``interactive_pending`` is True ⟺ a *pane-set*
# WAITING_ON_USER (empty open_tools).


def test_derive_run_state_ordering():
    """The deriver: transcript open-tool set is strictly above the pane bit;
    the bit is consulted only on an empty open set."""
    derive = route_runtime._state_from_open_tools
    # Interactive transcript id wins regardless of the bit.
    assert (
        derive({"a": True}, pane_interactive_pending=True) is RunState.WAITING_ON_USER
    )
    assert derive({"a": True}) is RunState.WAITING_ON_USER
    # Non-interactive tool open → RUNNING_TOOL; the pane bit is ignored.
    assert derive({"a": False}, pane_interactive_pending=True) is RunState.RUNNING_TOOL
    assert derive({"a": False}) is RunState.RUNNING_TOOL
    # Empty open set: the bit decides WAITING vs RUNNING.
    assert derive({}, pane_interactive_pending=True) is RunState.WAITING_ON_USER
    assert derive({}) is RunState.RUNNING


async def test_mark_interactive_pending_promotes_running_only():
    # RUNNING → WAITING_ON_USER + bit; typing suppressed.
    await route_runtime.mark_inbound_sent(ROUTE)
    assert route_runtime.snapshot(ROUTE).run_state is RunState.RUNNING
    snap = await route_runtime.mark_interactive_pending(ROUTE)
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.interactive_pending is True
    assert snap.typing_eligible is False

    # Idempotent: a second promote on the now-WAITING route is a no-op.
    snap = await route_runtime.mark_interactive_pending(ROUTE)
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.interactive_pending is True


async def test_mark_interactive_pending_no_op_on_running_tool():
    # RUNNING_TOOL (non-interactive tool open) is NOT promoted — the transcript
    # has authority and the bit must not co-exist with an open tool.
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash")
    )
    assert route_runtime.snapshot(ROUTE).run_state is RunState.RUNNING_TOOL
    snap = await route_runtime.mark_interactive_pending(ROUTE)
    assert snap.run_state is RunState.RUNNING_TOOL
    assert snap.interactive_pending is False


async def test_mark_interactive_pending_no_op_on_running_with_stale_open_tools():
    """codex/hermes P1: ``RUNNING`` does NOT imply an empty open set. A ``user``
    turn mid-tool sets ``RUNNING`` while leaving a stale ``open_tools`` entry.
    Promoting then would set the bit while the deriver returns RUNNING_TOOL /
    transcript-WAITING, breaking the invariant. The promote must no-op."""
    # (a) non-interactive tool still open under a RUNNING route.
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="bash", tool_name="Bash")
    )
    snap = await route_runtime.ingest_transcript_event(ROUTE, _evt("user", "text"))
    assert snap.run_state is RunState.RUNNING  # user turn set RUNNING...
    assert snap.open_tools == frozenset({"bash"})  # ...but the tool is still open
    snap = await route_runtime.mark_interactive_pending(ROUTE)
    assert snap.interactive_pending is False  # NOT promoted
    assert snap.run_state is RunState.RUNNING

    # (b) interactive tool still open under a RUNNING route (Hermes path):
    # promoting would have left the bit True on a transcript-set WAITING.
    route_runtime.reset_for_tests()
    await route_runtime.ingest_transcript_event(
        ROUTE,
        _evt("assistant", "tool_use", tool_use_id="auq", tool_name="AskUserQuestion"),
    )
    await route_runtime.ingest_transcript_event(ROUTE, _evt("user", "text"))
    snap = await route_runtime.mark_interactive_pending(ROUTE)
    assert snap.interactive_pending is False  # NOT promoted (stale open tool)


async def test_mark_interactive_pending_no_op_on_idle_and_unseen():
    # Unseen route → no-op (never seeds / resurrects).
    snap = await route_runtime.mark_interactive_pending(ROUTE)
    assert snap.run_state is RunState.IDLE_CLEARED
    assert snap.interactive_pending is False

    # IDLE_RECENT (seen, idle) → no-op (never resurrects idle).
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "text", stop_reason="end_turn")
    )
    assert route_runtime.snapshot(ROUTE).run_state is RunState.IDLE_RECENT
    snap = await route_runtime.mark_interactive_pending(ROUTE)
    assert snap.run_state is RunState.IDLE_RECENT
    assert snap.interactive_pending is False


async def test_mark_interactive_pending_no_op_on_transcript_waiting():
    """Double-resume guard: a transcript-set WAITING (interactive id open) is
    NOT touched — promote fires only from RUNNING, so the bit is never set when
    a transcript interactive tool is already open."""
    await route_runtime.ingest_transcript_event(
        ROUTE,
        _evt("assistant", "tool_use", tool_use_id="auq", tool_name="AskUserQuestion"),
    )
    assert route_runtime.snapshot(ROUTE).run_state is RunState.WAITING_ON_USER
    snap = await route_runtime.mark_interactive_pending(ROUTE)
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.interactive_pending is False  # bit NOT set on transcript-WAITING


async def test_mark_interactive_cleared_retracts_pane_set_waiting():
    await route_runtime.mark_inbound_sent(ROUTE)
    await route_runtime.mark_interactive_pending(ROUTE)
    assert route_runtime.snapshot(ROUTE).run_state is RunState.WAITING_ON_USER

    snap = await route_runtime.mark_interactive_cleared(ROUTE)
    # Empty open set → back to RUNNING; bit dropped.
    assert snap.run_state is RunState.RUNNING
    assert snap.interactive_pending is False

    # Idempotent: clearing an already-cleared route is a no-op.
    snap = await route_runtime.mark_interactive_cleared(ROUTE)
    assert snap.run_state is RunState.RUNNING
    assert snap.interactive_pending is False


async def test_mark_interactive_cleared_noops_against_transcript_waiting():
    """A transcript-set WAITING (interactive id open) must NOT be retracted by
    the pane clear — run_state is unchanged; only the (already-False) bit is
    dropped."""
    await route_runtime.ingest_transcript_event(
        ROUTE,
        _evt("assistant", "tool_use", tool_use_id="auq", tool_name="AskUserQuestion"),
    )
    snap = await route_runtime.mark_interactive_cleared(ROUTE)
    assert snap.run_state is RunState.WAITING_ON_USER  # UNCHANGED
    assert snap.interactive_pending is False
    assert snap.waiting_on_user_tools == frozenset({"auq"})


async def _pane_set_waiting(route: route_runtime.Route) -> None:
    """Drive ``route`` into the pane-set WAITING state (bit True, empty
    open_tools) via the production path."""
    await route_runtime.mark_inbound_sent(route)
    await route_runtime.mark_interactive_pending(route)
    snap = route_runtime.snapshot(route)
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.interactive_pending is True


async def test_transcript_reclaim_preserves_bit_on_text_and_thinking():
    """A buffered plain-text / thinking continuation during a live prompt must
    NOT strip the pane-set WAITING badge."""
    await _pane_set_waiting(ROUTE)
    snap = await route_runtime.ingest_transcript_event(ROUTE, _evt("assistant", "text"))
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.interactive_pending is True

    snap = await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "thinking")
    )
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.interactive_pending is True


async def test_transcript_reclaim_interactive_tool_use_takes_over_bit():
    await _pane_set_waiting(ROUTE)
    # The buffered interactive tool_use finally flushes → transcript-set WAITING.
    snap = await route_runtime.ingest_transcript_event(
        ROUTE,
        _evt("assistant", "tool_use", tool_use_id="auq", tool_name="AskUserQuestion"),
    )
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.interactive_pending is False  # bit superseded by the transcript
    assert snap.waiting_on_user_tools == frozenset({"auq"})


async def test_transcript_reclaim_noninteractive_tool_use_clears_bit():
    await _pane_set_waiting(ROUTE)
    snap = await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash")
    )
    assert snap.run_state is RunState.RUNNING_TOOL
    assert snap.interactive_pending is False


async def test_transcript_reclaim_end_of_turn_clears_bit():
    await _pane_set_waiting(ROUTE)
    snap = await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "text", stop_reason="end_turn")
    )
    assert snap.run_state is RunState.IDLE_RECENT
    assert snap.interactive_pending is False


async def test_transcript_reclaim_user_turn_clears_bit():
    await _pane_set_waiting(ROUTE)
    snap = await route_runtime.ingest_transcript_event(ROUTE, _evt("user", "text"))
    assert snap.run_state is RunState.RUNNING
    assert snap.interactive_pending is False


async def test_tool_result_unknown_id_preserves_bit():
    """An unknown-id tool_result early-returns: it must NOT strand a pane-set
    WAITING (the early-return at the unknown-id branch leaves run_state + bit
    untouched)."""
    await _pane_set_waiting(ROUTE)
    snap = await route_runtime.ingest_transcript_event(
        ROUTE, _evt("user", "tool_result", tool_use_id="never-opened")
    )
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.interactive_pending is True


async def test_known_tool_result_reclaim_is_bit_safe():
    """Defensive belt-and-suspenders: the known-id tool_result branch zeroes the
    bit before re-deriving. Unreachable with the bit True via the public API
    (promote requires an empty open set), so the artificial state is seeded
    directly to exercise the branch."""
    st = route_runtime._RouteState()
    st.seen = True
    st.run_state = RunState.WAITING_ON_USER
    st.open_tools = {"t1": False}
    st.invalidate_tool_cache()
    st.pane_interactive_pending = True
    route_runtime._state[ROUTE] = st

    snap = await route_runtime.ingest_transcript_event(
        ROUTE, _evt("user", "tool_result", tool_use_id="t1")
    )
    assert snap.run_state is RunState.RUNNING  # open set now empty
    assert snap.interactive_pending is False


async def test_pane_idle_clear_preserves_pane_set_waiting():
    """A pane-set WAITING survives a due pane-idle card-clear (the same
    preservation as a transcript-set WAITING; ``_reconcile_pane_idle_in_place``
    is unchanged). GH #42 leg 2 (W2a): the commit now returns False on ANY
    WAITING flavor — deadline left armed, sentinel left open — consistent
    with ``test_commit_preserves_waiting_on_user``; the run-state stays
    WAITING and the bit survives."""
    await _pane_set_waiting(ROUTE)
    route_runtime.arm_pane_idle_clear(ROUTE, now=100.0)
    assert (
        await route_runtime.commit_pane_idle_clear(ROUTE, now=100.0 + _DELAY) is False
    )
    snap = route_runtime.snapshot(ROUTE)
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.interactive_pending is True
    assert snap.pane_idle_clear_at == 100.0 + _DELAY  # NOT consumed (W2a)


async def test_mark_interactive_pending_rearms_stale_pane_idle_deadline():
    """Defensive hardening: a pane-idle deadline armed during the prior RUNNING
    stretch is cancelled by the promote, so a pane-set WAITING never carries a
    live card-clear deadline (invariant held locally in route_runtime, not via
    the status_polling control-flow contract)."""
    await route_runtime.mark_inbound_sent(ROUTE)  # RUNNING
    route_runtime.arm_pane_idle_clear(ROUTE, now=100.0)
    assert route_runtime.snapshot(ROUTE).pane_idle_clear_at == 100.0 + _DELAY
    snap = await route_runtime.mark_interactive_pending(ROUTE)
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.interactive_pending is True
    assert snap.pane_idle_clear_at is None  # stale deadline cancelled


async def test_session_reset_drops_pane_bit():
    await _pane_set_waiting(ROUTE)
    snap = await route_runtime.mark_session_reset(ROUTE)
    assert snap.run_state is RunState.IDLE_CLEARED
    assert snap.interactive_pending is False


def test_default_snapshot_has_interactive_pending_false():
    assert route_runtime.snapshot(ROUTE).interactive_pending is False


# ── status card lifecycle ───────────────────────────────────────────────


def test_status_card_published_and_cleared():
    assert route_runtime.snapshot(ROUTE).status_card_visible is False
    route_runtime.mark_status_card_published(ROUTE, msg_id=42)
    snap = route_runtime.snapshot(ROUTE)
    assert snap.status_card_visible is True
    assert snap.status_card_msg_id == 42

    route_runtime.mark_status_card_cleared(ROUTE)
    snap = route_runtime.snapshot(ROUTE)
    assert snap.status_card_visible is False
    assert snap.status_card_msg_id is None


def test_mark_session_reset_preserves_status_card():
    """Status-card msg_id outlives a session reset.

    message_queue may want to edit the existing card to render the new
    session's first reply rather than send a fresh card.
    """
    # Direct mark — synchronous, no await required.
    route_runtime.mark_status_card_published(ROUTE, msg_id=99)
    asyncio.run(route_runtime.mark_session_reset(ROUTE))
    snap = route_runtime.snapshot(ROUTE)
    assert snap.status_card_msg_id == 99


# ── context usage ───────────────────────────────────────────────────────


def test_update_context_usage_default_max_200k():
    route_runtime.update_context_usage(ROUTE, 50_000, "claude")
    snap = route_runtime.snapshot(ROUTE)
    assert snap.context_usage == ContextUsage(tokens=50_000, max_tokens=200_000)


def test_update_context_usage_latches_1m_after_overflow():
    # First observation crosses the 200k threshold → latch to 1M.
    route_runtime.update_context_usage(ROUTE, 250_000, "claude")
    assert route_runtime.snapshot(ROUTE).context_usage == ContextUsage(
        tokens=250_000, max_tokens=1_000_000
    )
    # Subsequent observation below 200k stays on 1M (latch preserved).
    route_runtime.update_context_usage(ROUTE, 80_000, "claude")
    assert route_runtime.snapshot(ROUTE).context_usage == ContextUsage(
        tokens=80_000, max_tokens=1_000_000
    )


def test_update_context_usage_none_drops_entry():
    route_runtime.update_context_usage(ROUTE, 50_000, "claude")
    route_runtime.update_context_usage(ROUTE, None, None)
    assert route_runtime.snapshot(ROUTE).context_usage is None


# ── seed_open_tools ─────────────────────────────────────────────────────


def test_seed_open_tools_no_op_for_empty_input():
    route_runtime.seed_open_tools(ROUTE, {})
    assert route_runtime.snapshot(ROUTE).run_state is RunState.IDLE_CLEARED


def test_seed_open_tools_populates_run_state():
    route_runtime.seed_open_tools(ROUTE, {"t1": False, "t2": True})
    snap = route_runtime.snapshot(ROUTE)
    # Mixed open tools with one interactive → WAITING_ON_USER.
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.open_tools == frozenset({"t1", "t2"})
    assert snap.waiting_on_user_tools == frozenset({"t2"})


async def test_seed_open_tools_skips_route_with_live_state():
    """Live events have higher authority than a JSONL replay snapshot."""
    await route_runtime.ingest_transcript_event(
        ROUTE,
        _evt("assistant", "tool_use", tool_use_id="live-tool", tool_name="Bash"),
    )
    # Replay should NOT overwrite the live state.
    route_runtime.seed_open_tools(ROUTE, {"replay-tool": False})
    snap = route_runtime.snapshot(ROUTE)
    assert snap.open_tools == frozenset({"live-tool"})


# ── snapshot immutability ───────────────────────────────────────────────


async def test_snapshots_are_frozen_dataclasses():
    import dataclasses

    snap = await route_runtime.mark_inbound_sent(ROUTE)
    # ``replace`` returns a copy — that's allowed.
    _ = replace(snap, run_state=RunState.IDLE_CLEARED)
    # Direct attribute mutation must raise on a frozen dataclass.
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.run_state = RunState.IDLE_CLEARED  # type: ignore[misc]


# ── per-route lock isolation ────────────────────────────────────────────


async def test_independent_routes_do_not_serialise():
    """Two routes can ingest concurrently without blocking each other.

    Holding ROUTE's lock open must not stop ROUTE_2 from committing — the
    per-route locks are independent. We grab ROUTE's lock by hand to model
    an in-flight mutation, then prove ROUTE_2 still commits while it's held.
    """
    lock = route_runtime._lock_for_route(ROUTE)
    async with lock:
        # ROUTE's lock is held; ROUTE_2 must still be able to commit because
        # it acquires a different lock.
        snap2 = await asyncio.wait_for(
            route_runtime.mark_inbound_sent(ROUTE_2), timeout=1.0
        )
        assert snap2.run_state is RunState.RUNNING


async def test_same_route_serialises():
    """A same-route mutation must WAIT for an in-flight one to release the lock.

    A bare ``gather`` of mutators cannot prove this: each mutator's critical
    section (``ingest_transcript_event`` at route_runtime.py:475-480) is
    synchronous between lock acquisition and release, so asyncio never
    interleaves them even with a no-op lock — the final open-set would be the
    same either way (a vacuous assertion). Instead we hold ROUTE's lock by
    hand to model an in-flight mutation, launch a SECOND same-route mutation,
    and prove it stays PENDING (its effect has not landed) until we release —
    then completes once released. Replacing the ``async with lock`` in the
    mutators with a no-op would let the second mutation finish while the lock
    is held, failing the ``not task.done()`` / empty-open-set assertions.
    """
    lock = route_runtime._lock_for_route(ROUTE)
    await lock.acquire()
    try:
        blocked = asyncio.create_task(
            route_runtime.ingest_transcript_event(
                ROUTE,
                _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash"),
            )
        )
        # Yield so the task is scheduled; it must block on the held route lock.
        await asyncio.sleep(0)
        assert not blocked.done(), "same-route mutation ran while the lock was held"
        assert route_runtime.snapshot(ROUTE).open_tools == frozenset(), (
            "same-route mutation landed its effect without acquiring the lock"
        )
    finally:
        lock.release()
    # Once released, the blocked mutation acquires the lock and commits.
    snap = await asyncio.wait_for(blocked, timeout=1.0)
    assert snap.open_tools == frozenset({"t1"})
    assert route_runtime.snapshot(ROUTE).open_tools == frozenset({"t1"})


# ── clear_route ─────────────────────────────────────────────────────────


async def test_clear_route_drops_all_state():
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash")
    )
    route_runtime.update_context_usage(ROUTE, 50_000, "claude")
    route_runtime.mark_status_card_published(ROUTE, msg_id=42)

    route_runtime.clear_route(ROUTE)
    snap = route_runtime.snapshot(ROUTE)
    assert snap.run_state is RunState.IDLE_CLEARED
    assert snap.context_usage is None
    assert snap.status_card_msg_id is None


async def test_clear_routes_for_topic_drops_all_matching_routes():
    """hermes round-2 P2: route_runtime's own topic-teardown seam clears EVERY
    route under (user, thread) — including a pane-set WAITING route that never
    had a message_queue queue — without touching other topics/users."""
    user_id, thread_id = 1, 42
    r_a = (user_id, thread_id, "@a")  # pane-set WAITING, no queue
    r_b = (user_id, thread_id, "@b")  # RUNNING_TOOL via transcript
    other_thread = (user_id, 99, "@c")  # different topic — must survive
    other_user = (2, thread_id, "@d")  # different user — must survive

    await route_runtime.mark_inbound_sent(r_a)
    await route_runtime.mark_interactive_pending(r_a)
    assert route_runtime.snapshot(r_a).interactive_pending is True
    await route_runtime.ingest_transcript_event(
        r_b, _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash")
    )
    await route_runtime.mark_inbound_sent(other_thread)
    await route_runtime.mark_inbound_sent(other_user)

    route_runtime.clear_routes_for_topic(user_id, thread_id)

    assert route_runtime.snapshot(r_a).run_state is RunState.IDLE_CLEARED
    assert route_runtime.snapshot(r_a).interactive_pending is False
    assert route_runtime.snapshot(r_b).run_state is RunState.IDLE_CLEARED
    # Other topic / other user untouched.
    assert route_runtime.snapshot(other_thread).run_state is RunState.RUNNING
    assert route_runtime.snapshot(other_user).run_state is RunState.RUNNING


# ── parse_pending_tools_from_jsonl (startup replay) ──────────────────────


def _write_jsonl(path: str, entries: list[dict]) -> None:
    """Write one JSON entry per line. Test helper for the replay parser."""
    import json

    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _entry(content: list[dict], *, is_sidechain: bool = False) -> dict:
    """Wrap a content list in the JSONL envelope shape the parser expects."""
    return {
        "type": "assistant",
        "isSidechain": is_sidechain,
        "message": {"role": "assistant", "content": content},
    }


def test_replay_returns_unmatched_tool_uses(tmp_path):
    """tool_use without a matching tool_result remains in the open set."""
    p = tmp_path / "sess.jsonl"
    _write_jsonl(
        str(p),
        [
            _entry([{"type": "tool_use", "id": "t1", "name": "Bash"}]),
            _entry([{"type": "tool_result", "tool_use_id": "t1"}]),
            _entry([{"type": "tool_use", "id": "t2", "name": "Task"}]),
        ],
    )
    pending = route_runtime.parse_pending_tools_from_jsonl(str(p))
    assert pending == {"t2": False}


def test_replay_marks_interactive_tool(tmp_path):
    """An open AskUserQuestion is flagged as interactive in the replay."""
    p = tmp_path / "sess.jsonl"
    _write_jsonl(
        str(p),
        [
            _entry([{"type": "tool_use", "id": "q1", "name": "AskUserQuestion"}]),
        ],
    )
    pending = route_runtime.parse_pending_tools_from_jsonl(str(p))
    assert pending == {"q1": True}


def test_replay_skips_sidechain_entries(tmp_path):
    """isSidechain=true entries are ignored — they belong to a sub-agent."""
    p = tmp_path / "sess.jsonl"
    _write_jsonl(
        str(p),
        [
            _entry(
                [{"type": "tool_use", "id": "s1", "name": "Bash"}],
                is_sidechain=True,
            ),
            _entry([{"type": "tool_use", "id": "p1", "name": "Bash"}]),
        ],
    )
    pending = route_runtime.parse_pending_tools_from_jsonl(str(p))
    assert pending == {"p1": False}


def test_replay_tolerates_malformed_lines(tmp_path):
    """Bad JSON lines don't break the scan — we just skip them."""
    p = tmp_path / "sess.jsonl"
    p.write_text(
        "\n".join(
            [
                "not json {",
                "",
                '{"type":"assistant","message":{"content":'
                '[{"type":"tool_use","id":"ok","name":"Read"}]}}',
                '{"type":"assistant","message":"not-a-dict"}',
                '{"type":"assistant","message":{"content":"not-a-list"}}',
            ]
        )
        + "\n"
    )
    pending = route_runtime.parse_pending_tools_from_jsonl(str(p))
    assert pending == {"ok": False}


def test_replay_returns_empty_for_missing_file(tmp_path):
    """Missing JSONL is non-fatal: replay yields nothing, route stays idle."""
    pending = route_runtime.parse_pending_tools_from_jsonl(
        str(tmp_path / "does-not-exist.jsonl")
    )
    assert pending == {}


def test_replay_pairs_tool_result_before_tool_use(tmp_path):
    """Branch / rewind / --resume can lay tool_result *before* its tool_use.

    A forward-pop walk would leave a phantom open tool here. Set-difference
    semantics correctly pair them regardless of line order.
    """
    p = tmp_path / "sess.jsonl"
    _write_jsonl(
        str(p),
        [
            _entry([{"type": "tool_result", "tool_use_id": "rewound"}]),
            _entry([{"type": "tool_use", "id": "rewound", "name": "Bash"}]),
        ],
    )
    pending = route_runtime.parse_pending_tools_from_jsonl(str(p))
    assert pending == {}


def test_replay_repeated_tool_use_same_id_collapses(tmp_path):
    """A duplicate tool_use line (same id) does not produce two open entries."""
    p = tmp_path / "sess.jsonl"
    _write_jsonl(
        str(p),
        [
            _entry([{"type": "tool_use", "id": "dup", "name": "Bash"}]),
            _entry([{"type": "tool_use", "id": "dup", "name": "Bash"}]),
        ],
    )
    pending = route_runtime.parse_pending_tools_from_jsonl(str(p))
    assert pending == {"dup": False}


def test_replay_skips_string_content(tmp_path):
    """Some entries have ``message.content`` as a string, not a list. Skip."""
    p = tmp_path / "sess.jsonl"
    _write_jsonl(
        str(p),
        [
            {"type": "user", "message": {"role": "user", "content": "hello world"}},
            _entry([{"type": "tool_use", "id": "t1", "name": "Read"}]),
        ],
    )
    pending = route_runtime.parse_pending_tools_from_jsonl(str(p))
    assert pending == {"t1": False}


def test_replay_then_seed_open_tools_round_trip(tmp_path):
    """The replay output feeds ``seed_open_tools`` and lights the run state."""
    p = tmp_path / "sess.jsonl"
    _write_jsonl(
        str(p),
        [
            _entry([{"type": "tool_use", "id": "task-1", "name": "Task"}]),
        ],
    )
    pending = route_runtime.parse_pending_tools_from_jsonl(str(p))
    route_runtime.seed_open_tools(ROUTE, pending)
    assert route_runtime.snapshot(ROUTE).run_state is RunState.RUNNING_TOOL


# ── 2026-06-11 stuck-route RCA: post-false-pane-clear recovery sequence ──


async def test_false_pane_clear_then_transcript_sequence_reactivates():
    """Pins the Q2 finding of the 2026-06-11 @4 incident: after a FALSE
    pane-idle commit stashes an open Bash into ``suspended_tools``
    (IDLE_CLEARED / idle_source="pane"), the live JSONL sequence —
    tool_result for the suspended id, then thinking, then a fresh tool_use —
    must walk the route straight back to RUNNING_TOOL with
    ``typing_eligible`` True. Event shapes mirror the session
    fd08a1ff JSONL timeline (Bash tool_use 08:38:03.764Z, tool_result
    08:42:21.652Z, thinking stop_reason='tool_use' 08:42:34.230Z, Read
    tool_use 08:42:35.361Z).

    Verified against the live log: typing for the route resumed at
    09:42:24 local, ~2s after the tool_result ingest — the restore+close
    pairing works as contracted, so the ONLY live bug was the false commit
    itself (Q1, covered in test_status_polling / test_terminal_parser).
    """
    t0 = 1_762_000_000.0  # arbitrary wall-clock epoch base

    # Open Bash → RUNNING_TOOL.
    snap = await route_runtime.ingest_transcript_event(
        ROUTE,
        TranscriptLifecycleEvent(
            role="assistant",
            block_type="tool_use",
            tool_use_id="bash-hermes",
            tool_name="Bash",
            stop_reason="tool_use",
            timestamp=t0,
        ),
    )
    assert snap.run_state is RunState.RUNNING_TOOL
    assert snap.typing_eligible is True

    # False pane-idle clear commits (deadline armed + due): the open Bash is
    # MOVED to suspended_tools and the route reads IDLE_CLEARED(pane).
    route_runtime.arm_pane_idle_clear(ROUTE, now=100.0)
    assert await route_runtime.commit_pane_idle_clear(ROUTE, now=105.0) is True
    snap = route_runtime.snapshot(ROUTE)
    assert snap.run_state is RunState.IDLE_CLEARED
    assert snap.open_tools == frozenset()
    assert snap.typing_eligible is False

    # tool_result for the SUSPENDED id: restore + close through the normal
    # pairing → empty open set on an active turn → RUNNING.
    snap = await route_runtime.ingest_transcript_event(
        ROUTE,
        TranscriptLifecycleEvent(
            role="user",
            block_type="tool_result",
            tool_use_id="bash-hermes",
            tool_name="Bash",
            stop_reason=None,
            timestamp=t0 + 258.0,
        ),
    )
    assert snap.run_state is RunState.RUNNING
    assert snap.typing_eligible is True
    assert snap.open_tools == frozenset()

    # Mid-stream thinking (stop_reason carries 'tool_use' in live JSONL —
    # NOT a turn end): stays RUNNING.
    snap = await route_runtime.ingest_transcript_event(
        ROUTE,
        TranscriptLifecycleEvent(
            role="assistant",
            block_type="thinking",
            tool_use_id=None,
            tool_name=None,
            stop_reason="tool_use",
            timestamp=t0 + 270.5,
        ),
    )
    assert snap.run_state is RunState.RUNNING
    assert snap.typing_eligible is True

    # Fresh tool_use: derives RUNNING_TOOL.
    snap = await route_runtime.ingest_transcript_event(
        ROUTE,
        TranscriptLifecycleEvent(
            role="assistant",
            block_type="tool_use",
            tool_use_id="read-1",
            tool_name="Read",
            stop_reason="tool_use",
            timestamp=t0 + 271.6,
        ),
    )
    assert snap.run_state is RunState.RUNNING_TOOL
    assert snap.typing_eligible is True
    assert snap.open_tools == frozenset({"read-1"})
