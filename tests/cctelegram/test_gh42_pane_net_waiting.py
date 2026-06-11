"""GH #42 leg 2 — the pane-idle net must survive a WAITING_ON_USER interlude.

Incident mechanics (2026-06-11 di-copilot-2): ``commit_pane_idle_clear`` on a
WAITING route preserved the run-state but consumed the armed deadline and
latched ``pane_idle_cleared=True``. When the WAITING was later retracted
programmatically (the 30-min notification TTL via
``mark_notification_cleared``), the route fell back to RUNNING_TOOL with the
net permanently disarmed — the latch only resets on a pane-ACTIVE observation
or transcript/inbound activity, neither of which occurs on an abandoned idle
pane. Result: 31 minutes of stuck Busy + typing until kickstart.

Fix under test (plan v2 W2):
  - W2a: commit on a WAITING route returns False WITHOUT consuming the
    deadline and WITHOUT latching.
  - W2c: ``mark_notification_cleared`` / ``mark_interactive_cleared`` reset
    the pane-idle machinery when (and only when) their retract actually
    transitions WAITING → RUNNING/RUNNING_TOOL.
(The W2b poller gate lives in ``handlers/test_status_polling`` tests.)
"""

from __future__ import annotations

import pytest

from cctelegram import route_runtime
from cctelegram.route_runtime import (
    IDLE_CLEAR_DELAY_SECONDS,
    RunState,
    TranscriptLifecycleEvent,
)

ROUTE: route_runtime.Route = (1, 378, "@4")
_DELAY = IDLE_CLEAR_DELAY_SECONDS


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


# ── W2a: commit on WAITING preserves the net ─────────────────────────────


async def test_commit_on_waiting_no_consume_no_latch():
    """Commit against a WAITING route must return False, leave the deadline
    armed, and leave the latch open — pre-fix it consumed + latched +
    returned True, which is what disarmed the incident's net."""
    await route_runtime.ingest_transcript_event(
        ROUTE,
        _evt("assistant", "tool_use", tool_use_id="auq", tool_name="AskUserQuestion"),
    )
    assert route_runtime.snapshot(ROUTE).run_state is RunState.WAITING_ON_USER
    route_runtime.arm_pane_idle_clear(ROUTE, now=100.0)
    assert (
        await route_runtime.commit_pane_idle_clear(ROUTE, now=100.0 + _DELAY) is False
    )
    snap = route_runtime.snapshot(ROUTE)
    assert snap.run_state is RunState.WAITING_ON_USER
    # Deadline NOT consumed, latch NOT set: the net stays usable.
    assert snap.pane_idle_clear_at == 100.0 + _DELAY
    assert route_runtime.pane_idle_clear_due(ROUTE, now=100.0 + _DELAY) is True


async def test_commit_on_notification_waiting_no_consume_no_latch():
    """Same contract for a notification-set WAITING over an open tool (the
    incident's exact flavor)."""
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash")
    )
    await route_runtime.mark_notification_pending(ROUTE, set_at=500.0, generation="g1")
    assert route_runtime.snapshot(ROUTE).run_state is RunState.WAITING_ON_USER
    route_runtime.arm_pane_idle_clear(ROUTE, now=600.0)
    assert (
        await route_runtime.commit_pane_idle_clear(ROUTE, now=600.0 + _DELAY) is False
    )
    snap = route_runtime.snapshot(ROUTE)
    assert snap.pane_idle_clear_at == 600.0 + _DELAY


# ── W2c: retract seams re-enable the net on a real WAITING → active ─────


async def test_notification_ttl_retract_resets_latched_net():
    """A latched net (pre-fix W2a leftovers, or any historic latch) must be
    reset when the TTL retract drops WAITING back to an active state, so the
    next confirmed-idle observation can arm a fresh debounce."""
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash")
    )
    await route_runtime.mark_notification_pending(ROUTE, set_at=500.0, generation="g1")
    assert route_runtime.snapshot(ROUTE).run_state is RunState.WAITING_ON_USER
    # Simulate a stranded latch from an earlier stretch.
    st = route_runtime._state[ROUTE]
    st.pane_idle_clear_at = None
    st.pane_idle_cleared = True
    # TTL expiry retracts the bit → RUNNING_TOOL (t1 still open).
    snap = await route_runtime.mark_notification_cleared(ROUTE)
    assert snap.run_state is RunState.RUNNING_TOOL
    # The net must be fresh: arming works immediately.
    snap = route_runtime.arm_pane_idle_clear(ROUTE, now=900.0)
    assert snap.pane_idle_clear_at == 900.0 + _DELAY
    # And the full incident tail: the due commit reconciles to idle.
    assert await route_runtime.commit_pane_idle_clear(ROUTE, now=900.0 + _DELAY) is True
    assert route_runtime.snapshot(ROUTE).run_state is RunState.IDLE_CLEARED


async def test_interactive_retract_resets_latched_net():
    """Sibling seam: ``mark_interactive_cleared`` retracting a pane-set
    WAITING back to RUNNING must also re-enable a latched net."""
    await route_runtime.ingest_transcript_event(ROUTE, _evt("assistant", "text"))
    await route_runtime.mark_interactive_pending(ROUTE)
    assert route_runtime.snapshot(ROUTE).run_state is RunState.WAITING_ON_USER
    st = route_runtime._state[ROUTE]
    st.pane_idle_clear_at = None
    st.pane_idle_cleared = True
    snap = await route_runtime.mark_interactive_cleared(ROUTE)
    assert snap.run_state is RunState.RUNNING
    snap = route_runtime.arm_pane_idle_clear(ROUTE, now=900.0)
    assert snap.pane_idle_clear_at == 900.0 + _DELAY


async def test_retract_leaving_waiting_does_not_touch_net():
    """hermes r1 P2: with BOTH lower bits set, clearing one keeps the route
    WAITING via the other — the net must NOT be reset by that retract (the
    bits clear independently; only a real WAITING → active transition
    re-enables)."""
    await route_runtime.ingest_transcript_event(ROUTE, _evt("assistant", "text"))
    await route_runtime.mark_interactive_pending(ROUTE)  # pane bit
    await route_runtime.mark_notification_pending(ROUTE, set_at=500.0, generation="g1")
    assert route_runtime.snapshot(ROUTE).run_state is RunState.WAITING_ON_USER
    st = route_runtime._state[ROUTE]
    st.pane_idle_clear_at = None
    st.pane_idle_cleared = True
    # Clear the notification — pane bit still holds WAITING.
    snap = await route_runtime.mark_notification_cleared(ROUTE)
    assert snap.run_state is RunState.WAITING_ON_USER
    assert route_runtime._state[ROUTE].pane_idle_cleared is True  # untouched
    # Now the interactive retract drops it to RUNNING → net re-enabled.
    snap = await route_runtime.mark_interactive_cleared(ROUTE)
    assert snap.run_state is RunState.RUNNING
    assert route_runtime._state[ROUTE].pane_idle_cleared is False


async def test_retract_preserves_status_card_msg_id():
    """hermes r2 P3: the W2c reset must not touch status-card bookkeeping."""
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash")
    )
    route_runtime.mark_status_card_published(ROUTE, 4242)
    await route_runtime.mark_notification_pending(ROUTE, set_at=500.0, generation="g1")
    snap = await route_runtime.mark_notification_cleared(ROUTE)
    assert snap.status_card_msg_id == 4242


async def test_incident_sequence_end_to_end_reaches_idle():
    """The full leg-2 incident sequence must end IDLE:

    leaked-open RUNNING_TOOL → pane commit (idle(pane) + stash) →
    notification resurrects (WAITING, stash restored) → net arm/commit
    attempts during WAITING are no-ops → TTL retract → RUNNING_TOOL →
    fresh arm + commit → IDLE_CLEARED.

    Pre-fix: the during-WAITING commit latched the net, the TTL retract
    left it latched, and the route stayed RUNNING_TOOL forever."""
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="leak", tool_name="Bash")
    )
    # Pane net clears the phantom-busy route: idle(pane) + stash.
    route_runtime.arm_pane_idle_clear(ROUTE, now=100.0)
    assert await route_runtime.commit_pane_idle_clear(ROUTE, now=100.0 + _DELAY) is True
    assert route_runtime.snapshot(ROUTE).run_state is RunState.IDLE_CLEARED
    # Notification lands on idle(pane)+stash → resurrect → WAITING.
    res = await route_runtime.mark_notification_pending(
        ROUTE, set_at=200.0, generation="g1"
    )
    assert res is route_runtime.NotificationMarkResult.COMMITTED_LIVE
    assert route_runtime.snapshot(ROUTE).run_state is RunState.WAITING_ON_USER
    # Poller keeps observing the idle pane during the WAITING window; any
    # arm/commit attempts must not poison the net (W2a; W2b also gates these
    # out poller-side, this is the route_runtime-level guarantee).
    route_runtime.arm_pane_idle_clear(ROUTE, now=300.0)
    assert (
        await route_runtime.commit_pane_idle_clear(ROUTE, now=300.0 + _DELAY) is False
    )
    # TTL expiry: WAITING retracts → RUNNING_TOOL (the restored leak).
    snap = await route_runtime.mark_notification_cleared(ROUTE)
    assert snap.run_state is RunState.RUNNING_TOOL
    # The net must now work: fresh arm (or surviving deadline) → commit →
    # the route finally idles instead of typing for 31 minutes.
    route_runtime.arm_pane_idle_clear(ROUTE, now=400.0)
    assert await route_runtime.commit_pane_idle_clear(ROUTE, now=400.0 + _DELAY) is True
    assert route_runtime.snapshot(ROUTE).run_state is RunState.IDLE_CLEARED
