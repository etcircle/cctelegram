"""Unit tests for the Bug 2 live-prose delivery + dedup primitives (PR-C+D).

Covers the freshness selection + shown-live marker store in ``md_capture`` and
the batch/group dedup ``session_monitor.filter_live_prose_duplicates`` — the
adversarial cases the plan calls out: identical text under a different message
id must NOT suppress, a group without an interactive tool_use must NOT suppress,
the synthetic ExitPlanMode plan text is excluded from the aggregate, and a
two-candidate ambiguity suppresses none.
"""

from __future__ import annotations

import json
import time

import pytest

from cctelegram import md_capture
from cctelegram.md_capture import (
    ProseRecord,
    prose_norm_hash,
    read_prose_records,
    select_fresh_prose,
)
from cctelegram.session_monitor import NewMessage, filter_live_prose_duplicates
from cctelegram.transcript_parser import BLOCK_ORIGIN_EXIT_PLAN

_SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_PROSE = "SQLite is a zero config serverless embedded relational database"
_PLAN = (
    "# Plan: add a docs/README.md index\n\n## Context\n\nThe docs dir lacks an index."
)


@pytest.fixture
def cc_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
    md_capture.msg_display_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    return tmp_path


def _seed(session_id: str, *, message_id: str, delta: str, captured_at: float) -> None:
    line = {
        "captured_at": captured_at,
        "payload": {
            "message_id": message_id,
            "index": 0,
            "final": True,
            "delta": delta,
            "transcript_path": f"/p/{session_id}.jsonl",
        },
    }
    path = md_capture.session_ndjson_path(session_id)
    with path.open("a") as f:
        f.write(json.dumps(line) + "\n")


def _nm(
    *,
    text: str,
    content_type: str,
    message_id: str,
    session_id: str = _SID,
    tool_name: str | None = None,
    block_origin: str | None = None,
) -> NewMessage:
    return NewMessage(
        session_id=session_id,
        text=text,
        content_type=content_type,
        role="assistant",
        tool_name=tool_name,
        image_data=None,
        message_id=message_id,
        block_origin=block_origin,
    )


# ── Freshness selection ──────────────────────────────────────────────────────


def test_select_fresh_prose_picks_most_recent_within_ttl(cc_dir):
    now = time.time()
    _seed(_SID, message_id="OLD", delta="stale", captured_at=now - 50)
    _seed(_SID, message_id="FRESH", delta=_PROSE, captured_at=now - 2)
    rec = select_fresh_prose(_SID, now=now, ttl_seconds=8.0)
    assert rec is not None and rec.md_message_id == "FRESH"
    assert rec.text == _PROSE


def test_select_fresh_prose_rejects_all_stale(cc_dir):
    now = time.time()
    _seed(_SID, message_id="OLD", delta="stale", captured_at=now - 50)
    assert select_fresh_prose(_SID, now=now, ttl_seconds=8.0) is None


def test_select_fresh_prose_missing_file(cc_dir):
    assert select_fresh_prose("no-such", now=time.time(), ttl_seconds=8.0) is None


def test_freshness_ttls_are_named_constants():
    assert md_capture.AUQ_PROSE_TTL_S > 0
    assert md_capture.EPM_PROSE_TTL_S >= md_capture.AUQ_PROSE_TTL_S


# ── emission-anchor additive-OR (PR-1) ───────────────────────────────────────
#
# The freshness upper bound was render-time `now` only, so a poller that detects
# the picker tens of seconds after the prose finalized (live: 20.7s) blew the TTL
# and the prose was never posted above the card. PR-1 adds a STRICTLY-ADDITIVE OR
# leg anchored to a STABLE emission instant (AUQ: side-file `written_at`; EPM: the
# poller's first-detect stamp): keep r iff
#   (now - final_at) <= ttl                                   # TTL leg (today)
#   OR (emitted_at is not None and
#       final_at <= emitted_at + eps and                      # upper: turn-identity
#       final_at >= emitted_at - lookback)                    # lower: A1-FIX restart guard
#   AND (not_before is None or final_at > not_before)         # Item-3 — UNCHANGED


def test_emit_anchor_constants_exist_and_relationships(cc_dir):
    # Named, fixture-pinned (Wave-0 capture 2026-06-17 gap=5.44s + live 20.7s).
    assert md_capture._EMIT_ANCHOR_EPS_S > 0
    assert md_capture._EMIT_ANCHOR_LOOKBACK_S > 0
    assert md_capture._EMIT_ANCHOR_EPS_EPM_S > 0
    assert md_capture._EMIT_ANCHOR_LOOKBACK_EPM_S > 0
    # EPM's poller-stamp anchor lags the tool_use by the whole detect latency, so
    # EPM needs a LARGER lookback than AUQ's hook-stamped written_at.
    assert md_capture._EMIT_ANCHOR_LOOKBACK_EPM_S > md_capture._EMIT_ANCHOR_LOOKBACK_S
    # The EPM lookback must cover the measured idle gap (5.44s) with margin.
    assert md_capture._EMIT_ANCHOR_LOOKBACK_EPM_S >= 20.0


def test_anchor_or_accepts_what_ttl_accepts(cc_dir):
    """Additive: a prose the TTL alone accepts is STILL accepted with the anchor
    leg present (the OR can only widen)."""
    now = time.time()
    _seed(_SID, message_id="FRESH", delta=_PROSE, captured_at=now - 2)
    rec = select_fresh_prose(
        _SID,
        now=now,
        ttl_seconds=8.0,
        emitted_at=now
        - 100,  # anchor leg would NOT fire (final_at far above emitted+eps)
        emit_anchor_eps_s=2.0,
        emit_anchor_lookback_s=10.0,
    )
    assert rec is not None and rec.md_message_id == "FRESH"


def test_anchor_or_accepts_beyond_ttl_within_anchor_window(cc_dir):
    """The core fix: prose OUTSIDE the TTL (now - final_at > ttl) but inside the
    emission anchor window [emitted - lookback, emitted + eps] is now accepted."""
    now = time.time()
    # final_at is 20s old → blows the 8s TTL. But the picker was detected (emitted_at)
    # only ~1s after the prose finalized, so the anchor leg accepts it.
    final_at = now - 20
    _seed(_SID, message_id="LAGGED", delta=_PROSE, captured_at=final_at)
    rec = select_fresh_prose(
        _SID,
        now=now,
        ttl_seconds=8.0,
        emitted_at=final_at + 1.0,  # detected 1s after prose finalized
        emit_anchor_eps_s=2.0,
        emit_anchor_lookback_s=30.0,
    )
    assert rec is not None and rec.md_message_id == "LAGGED"


def test_anchor_or_rejects_when_neither_leg_matches(cc_dir):
    """Outside BOTH the TTL and the anchor window → still rejected."""
    now = time.time()
    final_at = now - 50  # blows TTL
    _seed(_SID, message_id="OLD", delta=_PROSE, captured_at=final_at)
    rec = select_fresh_prose(
        _SID,
        now=now,
        ttl_seconds=8.0,
        emitted_at=now
        - 1.0,  # anchor window [now-31, now+1]; final_at=now-50 is below it
        emit_anchor_eps_s=2.0,
        emit_anchor_lookback_s=30.0,
    )
    assert rec is None


def test_anchor_or_upper_bound_rejects_future_prose(cc_dir):
    """A record whose final_at is ABOVE emitted_at + eps (a later turn's prose) is
    rejected by the anchor leg (and is outside the TTL too)."""
    now = time.time()
    # final_at well after the anchor instant + eps; also > ttl old? No — make it
    # within ttl-OLD-direction impossible: place final_at slightly in the future of
    # emitted_at+eps but still TTL-fresh would be accepted by TTL. To isolate the
    # upper bound, blow the TTL: final_at is 30s in the past, emitted_at 60s past.
    final_at = now - 30
    _seed(_SID, message_id="UP", delta=_PROSE, captured_at=final_at)
    rec = select_fresh_prose(
        _SID,
        now=now,
        ttl_seconds=8.0,
        emitted_at=now
        - 60,  # final_at (now-30) > emitted+eps (now-58) → upper-bound reject
        emit_anchor_eps_s=2.0,
        emit_anchor_lookback_s=30.0,
    )
    assert rec is None


def test_anchor_or_not_before_still_filters_prior_turn(cc_dir):
    """not_before ANDs against BOTH legs: a prior-turn prose the anchor leg WOULD
    accept is STILL filtered by not_before (the Item-3 leak stays closed in-process)."""
    now = time.time()
    final_at = now - 20  # outside TTL, inside anchor window
    _seed(_SID, message_id="PRIOR", delta=_PROSE, captured_at=final_at)
    rec = select_fresh_prose(
        _SID,
        now=now,
        ttl_seconds=8.0,
        not_before=final_at + 0.5,  # delivered AFTER this prose finalized → prior turn
        emitted_at=final_at + 1.0,
        emit_anchor_eps_s=2.0,
        emit_anchor_lookback_s=30.0,
    )
    assert rec is None


def test_anchor_absent_is_byte_identical_to_ttl_only(cc_dir):
    """emitted_at=None reproduces today's TTL-only behavior exactly."""
    now = time.time()
    _seed(_SID, message_id="A", delta=_PROSE, captured_at=now - 3)
    _seed(_SID, message_id="B", delta="stale", captured_at=now - 50)
    a = select_fresh_prose(_SID, now=now, ttl_seconds=8.0, emitted_at=None)
    b = select_fresh_prose(_SID, now=now, ttl_seconds=8.0)
    assert a is not None and b is not None
    assert a.md_message_id == b.md_message_id == "A"


def test_anchor_or_lookback_rejects_stale_prior_turn_when_not_before_wiped(cc_dir):
    """A1-FIX restart asymmetry: after a restart the on-disk AUQ `written_at`
    survives (emitted_at non-None) but the in-memory not_before is wiped (None).
    The OR-leg's OWN lookback lower bound must still reject a stale prior-turn prose
    finalized far before this picker's tool_use — even with not_before=None."""
    now = time.time()
    written_at = now - 2  # this turn's tool_use (survives restart)
    # A stale prior-turn prose finalized 40s before the tool_use — far outside the
    # 10s AUQ lookback. not_before is None (wiped by restart).
    _seed(_SID, message_id="STALE_PRIOR", delta=_PROSE, captured_at=written_at - 40)
    rec = select_fresh_prose(
        _SID,
        now=now,
        ttl_seconds=8.0,
        not_before=None,
        emitted_at=written_at,
        emit_anchor_eps_s=2.0,
        emit_anchor_lookback_s=10.0,
    )
    assert rec is None


# ── not_before turn-boundary filter (Item 3 / P2-1) ──────────────────────────
#
# `not_before` is the wall-clock instant the bot delivered the current user turn
# into the session (same `time.time()` clock as the prose `captured_at`). A prior
# turn's prose finalized BEFORE that boundary; the current turn's prose AFTER it.
# Filter is STRICT `final_at > not_before`.


def test_select_fresh_prose_not_before_excludes_prior_turn(cc_dir):
    """A prior turn's prose (final_at BEFORE the current delivery boundary) is
    excluded even though it is still within the TTL window — the P2-1 leak."""
    now = time.time()
    _seed(_SID, message_id="PRIOR", delta="prior turn prose", captured_at=now - 3)
    assert (
        select_fresh_prose(_SID, now=now, ttl_seconds=8.0, not_before=now - 1) is None
    )


def test_select_fresh_prose_not_before_includes_current_turn(cc_dir):
    """The current turn's prose (final_at AFTER the boundary) passes."""
    now = time.time()
    _seed(_SID, message_id="CUR", delta=_PROSE, captured_at=now - 0.5)
    rec = select_fresh_prose(_SID, now=now, ttl_seconds=8.0, not_before=now - 1)
    assert rec is not None and rec.md_message_id == "CUR"


def test_select_fresh_prose_not_before_is_strict(cc_dir):
    """final_at == not_before is EXCLUDED (strict >): prose captured exactly at
    the boundary is not causally after the delivered user message."""
    now = time.time()
    boundary = now - 2
    _seed(_SID, message_id="EQ", delta=_PROSE, captured_at=boundary)
    assert (
        select_fresh_prose(_SID, now=now, ttl_seconds=8.0, not_before=boundary) is None
    )


def test_select_fresh_prose_not_before_none_is_ttl_only(cc_dir):
    """not_before=None (default) reproduces today's TTL-only behavior — a prose
    a boundary WOULD exclude still returns when not_before is None."""
    now = time.time()
    _seed(_SID, message_id="OLDISH", delta=_PROSE, captured_at=now - 3)
    assert (
        select_fresh_prose(_SID, now=now, ttl_seconds=8.0, not_before=None) is not None
    )
    assert select_fresh_prose(_SID, now=now, ttl_seconds=8.0) is not None


def test_select_fresh_prose_not_before_and_ttl_both_apply(cc_dir):
    """Both gates apply: a prose passing not_before but OUTSIDE the TTL is still
    excluded (the TTL remains the orphan time-bound)."""
    now = time.time()
    _seed(_SID, message_id="OLD", delta=_PROSE, captured_at=now - 50)
    assert (
        select_fresh_prose(_SID, now=now, ttl_seconds=8.0, not_before=now - 100) is None
    )


# ── Shown-live markers ───────────────────────────────────────────────────────


def test_marker_record_read_consume_roundtrip(cc_dir):
    nh = prose_norm_hash(_PROSE)
    md_capture.record_shown_live(_SID, md_message_id="M1", norm_hash=nh, shown_at=1.0)
    markers = md_capture.read_shown_live_markers(_SID)
    assert [(m.md_message_id, m.norm_hash) for m in markers] == [("M1", nh)]
    md_capture.consume_shown_live(_SID, "M1")
    assert md_capture.read_shown_live_markers(_SID) == []


def test_was_shown_live_is_consume_inclusive(cc_dir):
    nh = prose_norm_hash(_PROSE)
    assert md_capture.was_shown_live(_SID, "M1") is False
    md_capture.record_shown_live(_SID, md_message_id="M1", norm_hash=nh, shown_at=1.0)
    assert md_capture.was_shown_live(_SID, "M1") is True
    # Still True after consume — the render-path idempotency must survive the
    # dedup consuming the marker (regression for the scenario double-post).
    md_capture.consume_shown_live(_SID, "M1")
    assert md_capture.read_shown_live_markers(_SID) == []
    assert md_capture.was_shown_live(_SID, "M1") is True


def test_markers_coexist_with_delta_lines(cc_dir):
    now = time.time()
    _seed(_SID, message_id="MD", delta=_PROSE, captured_at=now)
    md_capture.record_shown_live(
        _SID, md_message_id="MD", norm_hash=prose_norm_hash(_PROSE), shown_at=now
    )
    # delta reader ignores the marker line; marker reader ignores the delta line.
    assert len(read_prose_records(_SID)) == 1
    assert len(md_capture.read_shown_live_markers(_SID)) == 1


def test_prose_norm_hash_matches_record(cc_dir):
    now = time.time()
    _seed(_SID, message_id="MD", delta=_PROSE, captured_at=now)
    rec = read_prose_records(_SID)[0]
    assert rec.norm_hash == prose_norm_hash(_PROSE)


# ── Batch dedup ──────────────────────────────────────────────────────────────


def _mark(session_id: str, text: str) -> None:
    md_capture.record_shown_live(
        session_id,
        md_message_id="MDLIVE",
        norm_hash=prose_norm_hash(text),
        shown_at=time.time(),
    )


def test_dedup_suppresses_matched_prose_and_keeps_tool_use(cc_dir):
    _mark(_SID, _PROSE)
    batch = [
        _nm(text=_PROSE, content_type="text", message_id="MID"),
        _nm(
            text="**AskUserQuestion**(Which DB?)",
            content_type="tool_use",
            message_id="MID",
            tool_name="AskUserQuestion",
        ),
    ]
    out = filter_live_prose_duplicates(batch)
    assert [m.content_type for m in out] == ["tool_use"]
    # marker consumed
    assert md_capture.read_shown_live_markers(_SID) == []


def test_dedup_no_marker_keeps_prose(cc_dir):
    batch = [
        _nm(text=_PROSE, content_type="text", message_id="MID"),
        _nm(
            text="x",
            content_type="tool_use",
            message_id="MID",
            tool_name="AskUserQuestion",
        ),
    ]
    out = filter_live_prose_duplicates(batch)
    assert any(m.content_type == "text" for m in out)


def test_dedup_requires_interactive_tool_use_in_group(cc_dir):
    _mark(_SID, _PROSE)
    # Same prose + a NON-interactive tool_use → not a candidate group.
    batch = [
        _nm(text=_PROSE, content_type="text", message_id="MID"),
        _nm(
            text="**Read**(x)",
            content_type="tool_use",
            message_id="MID",
            tool_name="Read",
        ),
    ]
    out = filter_live_prose_duplicates(batch)
    assert any(m.content_type == "text" for m in out)


# ── EPM plan-body dedup (the plan-before-card fix) ───────────────────────────


def _mark_epm_plan(session_id: str, plan_text: str) -> None:
    md_capture.record_epm_plan_shown_live(
        session_id, norm_hash=prose_norm_hash(plan_text), shown_at=1.0
    )


def test_epm_plan_dedup_suppresses_synthetic_block(cc_dir):
    # RED before the session_monitor EPM arm: the synthetic BLOCK_ORIGIN_EXIT_PLAN
    # block (block_origin != None) was EXCLUDED from dedup, so the plan
    # double-posted after the card. With the arm + a recorded marker it is
    # suppressed (consume-once), the ExitPlanMode tool_use survives.
    _mark_epm_plan(_SID, _PLAN)
    batch = [
        _nm(
            text=_PLAN,
            content_type="text",
            message_id="MID",
            block_origin=BLOCK_ORIGIN_EXIT_PLAN,
        ),
        _nm(
            text="**ExitPlanMode**",
            content_type="tool_use",
            message_id="MID",
            tool_name="ExitPlanMode",
        ),
    ]
    out = filter_live_prose_duplicates(batch)
    assert [m.content_type for m in out] == ["tool_use"]
    assert md_capture.read_epm_plan_shown_live_markers(_SID) == []  # consumed


def test_epm_plan_dedup_no_marker_keeps_plan(cc_dir):
    # No marker (plan was never posted before the card — e.g. file gone) → the
    # JSONL plan copy MUST still deliver post-resolution (no silent loss).
    batch = [
        _nm(
            text=_PLAN,
            content_type="text",
            message_id="MID",
            block_origin=BLOCK_ORIGIN_EXIT_PLAN,
        ),
        _nm(
            text="**ExitPlanMode**",
            content_type="tool_use",
            message_id="MID",
            tool_name="ExitPlanMode",
        ),
    ]
    out = filter_live_prose_duplicates(batch)
    assert any(m.block_origin == BLOCK_ORIGIN_EXIT_PLAN for m in out)


def test_epm_plan_marker_does_not_suppress_real_prose(cc_dir):
    # A REAL prose block (block_origin None) with the SAME text/hash as the EPM
    # marker must SURVIVE — the EPM arm only eats synthetic blocks; the two
    # marker kinds never cross-match.
    _mark_epm_plan(_SID, _PLAN)
    batch = [
        _nm(text=_PLAN, content_type="text", message_id="MID", block_origin=None),
        _nm(
            text="**ExitPlanMode**",
            content_type="tool_use",
            message_id="MID",
            tool_name="ExitPlanMode",
        ),
    ]
    out = filter_live_prose_duplicates(batch)
    assert any(m.content_type == "text" and m.block_origin is None for m in out)


def test_dedup_identical_text_different_message_id_not_suppressed(cc_dir):
    """The look-alike sibling contract: identical prose under a DIFFERENT
    message_id (no interactive tool_use in ITS group) must NOT be suppressed,
    even when a marker for that text exists."""
    _mark(_SID, _PROSE)
    batch = [
        # The real paired group (suppressed).
        _nm(text=_PROSE, content_type="text", message_id="MID_A"),
        _nm(
            text="x",
            content_type="tool_use",
            message_id="MID_A",
            tool_name="AskUserQuestion",
        ),
        # A look-alike prose in a different message with no interactive tool_use.
        _nm(text=_PROSE, content_type="text", message_id="MID_B"),
    ]
    out = filter_live_prose_duplicates(batch)
    texts = [m for m in out if m.content_type == "text"]
    assert len(texts) == 1 and texts[0].message_id == "MID_B"


def test_dedup_excludes_exitplan_plan_text_from_aggregate(cc_dir):
    """A group whose only text is the synthetic ExitPlanMode plan body
    (block_origin set) does NOT match a REAL-prose marker — the plan text is
    excluded from the aggregate, so its norm_hash differs."""
    _mark(_SID, _PROSE)  # marker for the real prose
    batch = [
        _nm(
            text=_PROSE, content_type="text", message_id="MID", block_origin="exit_plan"
        ),
        _nm(
            text="**ExitPlanMode**",
            content_type="tool_use",
            message_id="MID",
            tool_name="ExitPlanMode",
        ),
    ]
    out = filter_live_prose_duplicates(batch)
    # plan text not excluded would have matched + suppressed; it must survive.
    assert any(m.content_type == "text" for m in out)


def test_dedup_two_candidate_ambiguity_suppresses_none(cc_dir):
    """EPM ambiguity: >1 group sharing one (session, norm_hash) marker →
    suppress NONE, consume no marker."""
    _mark(_SID, _PROSE)
    batch = [
        _nm(text=_PROSE, content_type="text", message_id="MID_1"),
        _nm(
            text="e",
            content_type="tool_use",
            message_id="MID_1",
            tool_name="ExitPlanMode",
        ),
        _nm(text=_PROSE, content_type="text", message_id="MID_2"),
        _nm(
            text="e",
            content_type="tool_use",
            message_id="MID_2",
            tool_name="ExitPlanMode",
        ),
    ]
    out = filter_live_prose_duplicates(batch)
    assert sum(1 for m in out if m.content_type == "text") == 2
    # marker NOT consumed
    assert len(md_capture.read_shown_live_markers(_SID)) == 1


def test_dedup_multiblock_adjacent_blocks_match(cc_dir):
    """Adjacent multi-block prose ("A" + "B") aggregates to "A\\nB" and matches
    a marker hashed from the same joined form."""
    md_capture.record_shown_live(
        _SID,
        md_message_id="MDLIVE",
        norm_hash=prose_norm_hash("Part one.\nPart two."),
        shown_at=time.time(),
    )
    batch = [
        _nm(text="Part one.", content_type="text", message_id="MID"),
        _nm(text="Part two.", content_type="text", message_id="MID"),
        _nm(
            text="x",
            content_type="tool_use",
            message_id="MID",
            tool_name="AskUserQuestion",
        ),
    ]
    out = filter_live_prose_duplicates(batch)
    assert not any(m.content_type == "text" for m in out)


def test_dedup_empty_and_no_group_passthrough(cc_dir):
    assert filter_live_prose_duplicates([]) == []
    batch = [_nm(text="hi", content_type="text", message_id="MID")]
    assert filter_live_prose_duplicates(batch) == batch


def test_prose_record_is_frozen_and_fields():
    rec = ProseRecord(
        session_id=_SID,
        transcript_path="t",
        md_message_id="M",
        text="x",
        raw_hash="r",
        norm_hash="n",
        first_seen_at=1.0,
        final_at=2.0,
    )
    with pytest.raises(Exception):
        rec.text = "y"  # type: ignore[misc]
