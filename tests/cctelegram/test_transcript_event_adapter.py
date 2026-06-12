"""Unit tests for ``cctelegram.transcript_event_adapter`` — the adapter
that translates raw ``TranscriptEvent`` shapes into
``TranscriptLifecycleEvent`` and fans out per route.

Coverage:
  - ``to_lifecycle_event`` drops malformed / ignorable events.
  - ``dispatch_transcript_event`` returns one snapshot per route in
    input order.
  - ``dispatch_transcript_event`` returns an empty list (no mutation)
    when translation rejects the event.
  - ``dispatch_context_usage`` is the bulk context-usage fan-out path.
  - ``dispatch_seed_open_tools`` is the startup-replay entry point.
  - Once-per-session warning suppression doesn't crash the dispatch.
"""

from __future__ import annotations

from typing import Iterable

import pytest

from cctelegram import route_runtime, transcript_event_adapter
from cctelegram.route_runtime import RunState
from cctelegram.session_monitor import TranscriptEvent


def _event(
    *,
    role: str = "assistant",
    block_type: str = "text",
    tool_use_id: str | None = None,
    tool_name: str | None = None,
    stop_reason: str | None = None,
    session_id: str = "sess-A",
    timestamp: str | None = None,
) -> TranscriptEvent:
    return TranscriptEvent(
        session_id=session_id,
        role=role,  # type: ignore[arg-type]
        block_type=block_type,  # type: ignore[arg-type]
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        stop_reason=stop_reason,
        timestamp=timestamp,
        text="",
        image_data=None,
    )


@pytest.fixture(autouse=True)
def _reset() -> Iterable[None]:
    route_runtime.reset_for_tests()
    transcript_event_adapter.reset_for_tests()
    yield
    route_runtime.reset_for_tests()
    transcript_event_adapter.reset_for_tests()


# ── translation ─────────────────────────────────────────────────────────


def test_to_lifecycle_event_assistant_text():
    out = transcript_event_adapter.to_lifecycle_event(
        _event(role="assistant", block_type="text", stop_reason="end_turn")
    )
    assert out is not None
    assert out.role == "assistant"
    assert out.block_type == "text"
    assert out.stop_reason == "end_turn"


def test_to_lifecycle_event_drops_tool_use_without_id():
    out = transcript_event_adapter.to_lifecycle_event(
        _event(block_type="tool_use", tool_use_id=None, tool_name="Bash")
    )
    assert out is None


def test_to_lifecycle_event_drops_unknown_role():
    out = transcript_event_adapter.to_lifecycle_event(
        _event(role="system")  # type: ignore[arg-type]
    )
    assert out is None


def test_to_lifecycle_event_drops_unknown_block():
    out = transcript_event_adapter.to_lifecycle_event(
        _event(block_type="image")  # type: ignore[arg-type]
    )
    assert out is None


# ── dispatch ─────────────────────────────────────────────────────────────


ROUTE_A: route_runtime.Route = (1, 11, "@1")
ROUTE_B: route_runtime.Route = (1, 22, "@2")


async def test_dispatch_returns_snapshot_per_route():
    snaps = await transcript_event_adapter.dispatch_transcript_event(
        _event(block_type="tool_use", tool_use_id="t1", tool_name="Bash"),
        [ROUTE_A, ROUTE_B],
    )
    assert len(snaps) == 2
    assert all(s.run_state is RunState.RUNNING_TOOL for s in snaps)
    # Each route sees its own snapshot identity.
    assert snaps[0].route == ROUTE_A
    assert snaps[1].route == ROUTE_B


async def test_dispatch_returns_empty_on_malformed_event():
    snaps = await transcript_event_adapter.dispatch_transcript_event(
        _event(block_type="image"),  # unrecognised  # type: ignore[arg-type]
        [ROUTE_A, ROUTE_B],
    )
    assert snaps == []
    # State machine untouched.
    assert route_runtime.snapshot(ROUTE_A).run_state is RunState.IDLE_CLEARED
    assert route_runtime.snapshot(ROUTE_B).run_state is RunState.IDLE_CLEARED


async def test_dispatch_full_turn_idle_recent():
    """Full turn lifecycle: tool_use → tool_result → end_turn → IDLE_RECENT.

    Exercises the integration path end-to-end through the adapter.
    """
    await transcript_event_adapter.dispatch_transcript_event(
        _event(block_type="tool_use", tool_use_id="t1", tool_name="Bash"),
        [ROUTE_A],
    )
    await transcript_event_adapter.dispatch_transcript_event(
        _event(role="user", block_type="tool_result", tool_use_id="t1"),
        [ROUTE_A],
    )
    snaps = await transcript_event_adapter.dispatch_transcript_event(
        _event(block_type="text", stop_reason="end_turn"),
        [ROUTE_A],
    )
    assert snaps[0].run_state is RunState.IDLE_RECENT


# ── helpers ─────────────────────────────────────────────────────────────


def test_dispatch_context_usage_fans_out():
    transcript_event_adapter.dispatch_context_usage(
        [ROUTE_A, ROUTE_B], tokens=50_000, model="claude"
    )
    assert route_runtime.snapshot(ROUTE_A).context_usage is not None
    assert route_runtime.snapshot(ROUTE_B).context_usage is not None


def test_dispatch_seed_open_tools_idempotent_against_live_state():
    transcript_event_adapter.dispatch_seed_open_tools(ROUTE_A, {"replay-tool": False})
    snap = route_runtime.snapshot(ROUTE_A)
    assert "replay-tool" in snap.open_tools


def test_warn_once_suppresses_repeat_session_failures(caplog):
    """A malformed event in a single session logs only once.

    The suppression is keyed by session_id so a malformed event in
    session-B still surfaces independently of session-A.
    """
    caplog.set_level("WARNING")
    bad = _event(block_type="image", session_id="sess-A")  # type: ignore[arg-type]
    transcript_event_adapter.to_lifecycle_event(bad)
    # _warn_once is called from the dispatch path; trigger it.
    import asyncio

    asyncio.run(transcript_event_adapter.dispatch_transcript_event(bad, [ROUTE_A]))
    asyncio.run(transcript_event_adapter.dispatch_transcript_event(bad, [ROUTE_A]))
    warn_records = [r for r in caplog.records if r.levelname == "WARNING"]
    # First call logs; second call is suppressed.
    assert len(warn_records) == 1


# ── Wave B: event-timestamp plumbing (B1b prerequisite) ─────────────────


def test_timestamp_parsed_iso8601_z_suffix():
    from datetime import datetime, timezone

    out = transcript_event_adapter.to_lifecycle_event(
        _event(timestamp="2026-06-10T12:00:00.500Z")
    )
    assert out is not None
    expected = datetime(2026, 6, 10, 12, 0, 0, 500000, tzinfo=timezone.utc).timestamp()
    assert out.timestamp == pytest.approx(expected)


def test_timestamp_parsed_with_explicit_offset():
    from datetime import datetime, timezone

    out = transcript_event_adapter.to_lifecycle_event(
        _event(timestamp="2026-06-10T13:00:00+01:00")
    )
    assert out is not None
    expected = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    assert out.timestamp == pytest.approx(expected)


def test_timestamp_garbage_parses_to_none():
    out = transcript_event_adapter.to_lifecycle_event(_event(timestamp="not-a-time"))
    assert out is not None
    assert out.timestamp is None


def test_timestamp_absent_is_none():
    out = transcript_event_adapter.to_lifecycle_event(_event(timestamp=None))
    assert out is not None
    assert out.timestamp is None


def test_adapter_loc_budget_intact():
    """The 250-line kill signal from the adapter's charter must hold after
    the timestamp field addition."""
    import cctelegram.transcript_event_adapter as mod
    from pathlib import Path

    assert len(Path(mod.__file__).read_text().splitlines()) <= 250


# ── GH #44: is_task_notification stamping ───────────────────────────────


def _user_text_event(text: str) -> TranscriptEvent:
    return TranscriptEvent(
        session_id="sess-A",
        role="user",
        block_type="text",
        tool_use_id=None,
        tool_name=None,
        stop_reason=None,
        timestamp=None,
        text=text,
        image_data=None,
    )


def test_task_notification_user_event_is_stamped():
    """hermes r3 P3-2: the flag must be derivable from a RAW TranscriptEvent
    through the adapter — hand-built lifecycle events alone can fake-green."""
    out = transcript_event_adapter.to_lifecycle_event(
        _user_text_event(
            "<task-notification>\n<task-id>abc123</task-id>\n"
            "<status>completed</status>\n</task-notification>"
        )
    )
    assert out is not None
    assert out.is_task_notification is True


def test_ordinary_user_text_is_not_stamped():
    out = transcript_event_adapter.to_lifecycle_event(_user_text_event("hi there"))
    assert out is not None
    assert out.is_task_notification is False


def test_assistant_text_is_never_stamped():
    out = transcript_event_adapter.to_lifecycle_event(
        _event(role="assistant", block_type="text")
    )
    assert out is not None
    assert out.is_task_notification is False
