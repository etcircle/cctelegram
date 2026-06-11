"""GH #42 leg 1 — out-of-order JSONL tool pairs must not leak open tools.

Claude Code JSONL can flush a ``tool_result`` line BEFORE its ``tool_use``
line (interleaved write, observed broadly: 27/40 recent session files on the
incident machine). The live pipeline ingests the unknown tool_result (ignored
by design), then the tool_use opens a slot nothing ever closes — and a
non-empty ``open_tools`` blocks the authoritative end-of-turn idle, leaving
the route stuck RUNNING_TOOL (the 2026-06-11 di-copilot-2 incident).

Fix under test: ``route_runtime`` records unknown tool_result ids in a
bounded ``early_tool_results`` buffer; a later ``tool_use`` for a recorded id
is treated as already-closed (never opens), with the known-tool_result
reclaim side effects driven by the STORED result timestamp, terminal-state
aware (an end_turn that straddled the pair must not be revived), and without
counting as pane-idle re-arm activity.

The fixture ``fixtures/gh42_out_of_order_tail.jsonl`` is sanitized REAL
incident JSONL (structure, ids, timestamps, ordering preserved; all prose
replaced) — verified to reproduce the leak pre-fix.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cctelegram import route_runtime, transcript_event_adapter
from cctelegram.route_runtime import RunState, TranscriptLifecycleEvent
from cctelegram.session_monitor import TranscriptEvent
from cctelegram.transcript_parser import TranscriptParser

FIXTURE = Path(__file__).parent / "fixtures" / "gh42_out_of_order_tail.jsonl"

ROUTE: route_runtime.Route = (1, 378, "@4")


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
    transcript_event_adapter.reset_for_tests()
    yield
    route_runtime.reset_for_tests()
    transcript_event_adapter.reset_for_tests()


async def _ingest(
    event: TranscriptLifecycleEvent,
) -> route_runtime.RouteRuntimeSnapshot:
    return await route_runtime.ingest_transcript_event(ROUTE, event)


# ── fixture replay (the incident, end to end through the live path) ─────


async def _replay_fixture_per_line() -> route_runtime.RouteRuntimeSnapshot:
    """Replay the sanitized real fixture through the LIVE pipeline:
    TranscriptParser.parse_entries → adapter → route_runtime, one JSONL
    line per poll batch (the maximally cross-batch shape; the leak is
    batching-independent — verified against the unsanitized incident file).
    """
    lines = [json.loads(ln) for ln in FIXTURE.read_text().splitlines()]
    pending: dict = {}
    for line in lines:
        parsed, pending = TranscriptParser.parse_entries([line], pending_tools=pending)
        for entry in parsed:
            if entry.role not in ("user", "assistant"):
                continue
            if entry.content_type not in (
                "text",
                "thinking",
                "tool_use",
                "tool_result",
            ):
                continue
            event = TranscriptEvent(
                session_id="gh42-fixture-session",
                role=entry.role,
                block_type=entry.content_type,
                tool_use_id=entry.tool_use_id,
                tool_name=entry.tool_name,
                stop_reason=entry.stop_reason,
                timestamp=entry.timestamp,
                text=entry.text,
                image_data=entry.image_data,
                tool_input=entry.tool_input,
                transcript_uuid=entry.uuid,
                message_id=entry.message_id,
                block_origin=entry.block_origin,
            )
            await transcript_event_adapter.dispatch_transcript_event(event, [ROUTE])
    return route_runtime.snapshot(ROUTE)


async def test_fixture_replay_idles_after_genuine_end_turn():
    """THE incident assertion: after the fixture's final authoritative
    end_turn, the route must be idle with no leaked open tools.

    Pre-fix this fails: the out-of-order SendMessage pair leaks one open
    tool and the route ends RUNNING_TOOL (stuck Busy + typing)."""
    snap = await _replay_fixture_per_line()
    assert snap.open_tools == frozenset(), (
        f"leaked open tools: {sorted(snap.open_tools)}"
    )
    assert snap.run_state in (RunState.IDLE_RECENT, RunState.IDLE_CLEARED)


def test_replay_walker_parity_on_fixture():
    """The restart walker is order-insensitive and must agree with the live
    end-state (no pending tools) on the same fixture — the walker-parity
    disagreement is exactly what made a kickstart 'fix' the incident."""
    pending = route_runtime.parse_pending_tools_from_jsonl(str(FIXTURE))
    assert pending == {}


# ── unit: result-before-use pairing ──────────────────────────────────────


async def test_result_then_use_never_opens():
    """A tool_use whose tool_result already passed must be treated as
    already-closed: no open slot, end-of-turn not blocked."""
    await _ingest(_evt("assistant", "text"))  # route active (RUNNING)
    await _ingest(_evt("user", "tool_result", tool_use_id="t-early", timestamp=100.0))
    snap = await _ingest(
        _evt("assistant", "tool_use", tool_use_id="t-early", tool_name="SendMessage")
    )
    assert snap.open_tools == frozenset()
    assert snap.run_state is RunState.RUNNING
    # The genuine end_turn that follows must idle the route.
    snap = await _ingest(_evt("assistant", "text", stop_reason="end_turn"))
    assert snap.run_state is RunState.IDLE_RECENT


async def test_unknown_result_recording_preserves_state_and_bits():
    """Recording an early result keeps today's unknown-tool_result
    semantics: no run-state mutation, notification bit preserved."""
    await _ingest(_evt("assistant", "tool_use", tool_use_id="wf", tool_name="Bash"))
    await route_runtime.mark_notification_pending(ROUTE, set_at=500.0, generation="g1")
    assert route_runtime.snapshot(ROUTE).run_state is RunState.WAITING_ON_USER
    # Unknown tool_result, even with a NEWER timestamp, preserves the bit.
    snap = await _ingest(
        _evt("user", "tool_result", tool_use_id="t-unknown", timestamp=600.0)
    )
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.notification_pending is True


async def test_matched_use_on_idle_preserves_idle():
    """hermes r1 P1: an end_turn straddling the pair idles the route; the
    late tool_use must NOT revive it."""
    await _ingest(_evt("assistant", "text"))
    await _ingest(_evt("user", "tool_result", tool_use_id="t1", timestamp=100.0))
    snap = await _ingest(
        _evt("assistant", "text", stop_reason="end_turn", timestamp=101.0)
    )
    assert snap.run_state is RunState.IDLE_RECENT
    idle_clear_at = snap.idle_clear_at
    # The straddled pair's tool_use arrives after the turn already ended.
    snap = await _ingest(
        _evt("assistant", "tool_use", tool_use_id="t1", tool_name="SendMessage")
    )
    assert snap.run_state is RunState.IDLE_RECENT
    assert snap.open_tools == frozenset()
    assert snap.idle_clear_at == idle_clear_at  # decay deadline untouched


async def test_matched_use_on_idle_suppresses_pane_rearm():
    """codex r2 P2: the terminal matched-early tool_use is historical, not
    activity — it must not cancel an armed pane-idle deadline or reset the
    latch via the unconditional ingest re-arm."""
    await _ingest(_evt("assistant", "text"))
    await _ingest(_evt("user", "tool_result", tool_use_id="t1", timestamp=100.0))
    await _ingest(_evt("assistant", "text", stop_reason="end_turn", timestamp=101.0))
    # Poller observes the (now genuinely idle) pane and arms the card clear.
    route_runtime.arm_pane_idle_clear(ROUTE, now=200.0)
    armed_at = route_runtime.snapshot(ROUTE).pane_idle_clear_at
    assert armed_at is not None
    # Straddled tool_use lands — must leave the armed deadline alone.
    await _ingest(
        _evt("assistant", "tool_use", tool_use_id="t1", tool_name="SendMessage")
    )
    assert route_runtime.snapshot(ROUTE).pane_idle_clear_at == armed_at


async def test_matched_use_clears_notification_by_stored_result_ts():
    """codex r1 P1: the notification clear must use the STORED early-result
    timestamp — newer than set_at clears, older/None preserves. Never the
    tool_use event's own timestamp."""
    # Case A: stored result ts NEWER than set_at → bit clears.
    await _ingest(_evt("assistant", "tool_use", tool_use_id="wf", tool_name="Bash"))
    await route_runtime.mark_notification_pending(ROUTE, set_at=500.0, generation="g1")
    await _ingest(_evt("user", "tool_result", tool_use_id="tA", timestamp=600.0))
    snap = await _ingest(
        _evt(
            "assistant",
            "tool_use",
            tool_use_id="tA",
            tool_name="SendMessage",
            timestamp=400.0,  # use's own ts is OLDER — must not matter
        )
    )
    assert snap.notification_pending is False
    assert snap.run_state is RunState.RUNNING_TOOL  # wf still open

    # Case B: stored result ts OLDER than set_at → bit preserved, even
    # though the tool_use event's own timestamp is newer.
    route_runtime.reset_for_tests()
    await _ingest(_evt("assistant", "tool_use", tool_use_id="wf", tool_name="Bash"))
    await route_runtime.mark_notification_pending(ROUTE, set_at=500.0, generation="g2")
    await _ingest(_evt("user", "tool_result", tool_use_id="tB", timestamp=400.0))
    snap = await _ingest(
        _evt(
            "assistant",
            "tool_use",
            tool_use_id="tB",
            tool_name="SendMessage",
            timestamp=600.0,  # use's own ts is NEWER — must not clear
        )
    )
    assert snap.notification_pending is True
    assert snap.run_state is RunState.WAITING_ON_USER


async def test_matched_use_with_none_result_ts_preserves_notification():
    """A stored ``None`` result timestamp (parse failure) preserves the bit
    — same contract as the known-tool_result branch."""
    await _ingest(_evt("assistant", "tool_use", tool_use_id="wf", tool_name="Bash"))
    await route_runtime.mark_notification_pending(ROUTE, set_at=500.0, generation="g1")
    await _ingest(_evt("user", "tool_result", tool_use_id="tC", timestamp=None))
    snap = await _ingest(
        _evt("assistant", "tool_use", tool_use_id="tC", tool_name="SendMessage")
    )
    assert snap.notification_pending is True
    assert snap.run_state is RunState.WAITING_ON_USER


async def test_early_results_cap_evicts_oldest():
    """The buffer is bounded: past the cap the OLDEST id is evicted (its
    late tool_use then opens normally — the accepted residual); retained
    ids still pair."""
    await _ingest(_evt("assistant", "text"))
    cap = route_runtime._EARLY_RESULTS_CAP
    for i in range(cap + 1):  # one over cap → evicts t-0
        await _ingest(
            _evt("user", "tool_result", tool_use_id=f"t-{i}", timestamp=float(i))
        )
    # Evicted id opens (leak accepted past the cap)...
    snap = await _ingest(
        _evt("assistant", "tool_use", tool_use_id="t-0", tool_name="Bash")
    )
    assert "t-0" in snap.open_tools
    # ...while a retained id is still recognized as already-closed.
    snap = await _ingest(
        _evt("assistant", "tool_use", tool_use_id=f"t-{cap}", tool_name="Bash")
    )
    assert f"t-{cap}" not in snap.open_tools


async def test_early_results_cleared_on_session_reset():
    """`/clear` rotates the session — early results belong to the dead one."""
    await _ingest(_evt("assistant", "text"))
    await _ingest(_evt("user", "tool_result", tool_use_id="t1", timestamp=100.0))
    await route_runtime.mark_session_reset(ROUTE)
    await _ingest(_evt("assistant", "text"))  # route active again
    snap = await _ingest(
        _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash")
    )
    assert "t1" in snap.open_tools  # records did not survive the reset


async def test_suspended_stash_restore_still_wins_over_early_results():
    """A tool_result whose id sits in the pane-clear stash must keep taking
    the existing restore+close path (checked BEFORE the early-result
    recording) — the Wave A contract is untouched."""
    await _ingest(_evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash"))
    await route_runtime.mark_pane_idle(ROUTE)  # moves t1 → suspended stash
    snap = await _ingest(_evt("user", "tool_result", tool_use_id="t1"))
    # Restored + closed via the normal pairing — not recorded as early.
    assert snap.open_tools == frozenset()
    await _ingest(_evt("assistant", "text"))  # active
    snap = await _ingest(
        _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash")
    )
    # t1 was NOT in early_tool_results, so this (id-reuse, impossible in
    # practice) opens normally — proving the stash path didn't record.
    assert "t1" in snap.open_tools


async def test_end_of_turn_blocked_log_is_bounded(caplog):
    """W3: a genuine end_turn blocked by open tools logs route + count + a
    BOUNDED id sample (first 8), never the whole set."""
    import logging

    await _ingest(_evt("assistant", "text"))
    for i in range(12):
        await _ingest(
            _evt("assistant", "tool_use", tool_use_id=f"blk-{i:02d}", tool_name="Bash")
        )
    with caplog.at_level(logging.INFO, logger="cctelegram.route_runtime"):
        await _ingest(_evt("assistant", "text", stop_reason="end_turn"))
    blocked = [r for r in caplog.records if "end_of_turn blocked" in r.getMessage()]
    assert len(blocked) == 1
    msg = blocked[0].getMessage()
    assert "count=12" in msg
    assert msg.count("blk-") == 8  # bounded sample, not the whole set
