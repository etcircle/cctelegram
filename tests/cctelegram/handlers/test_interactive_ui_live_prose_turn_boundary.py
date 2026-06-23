"""Item 3 / P2-1: ``_maybe_post_live_prose`` turn-boundary integration.

The live-prose path posts assistant prose buffered behind a live AUQ/EPM picker
BEFORE the picker card. ``select_fresh_prose`` is now gated on a ``not_before``
turn boundary — the wall-clock instant the bot delivered the CURRENT user turn
into tmux (``message_queue.peek_route_user_turn_at``). These tests prove the
function resolves that stamp INSIDE itself and passes it through, so:

  * a PRIOR turn's leftover prose (final_at <= boundary, still within the TTL)
    is NOT posted above a picker whose own turn produced no prose;
  * the CURRENT turn's prose (final_at > boundary) IS posted;
  * with NO stamp for the route (restart) it degrades to TTL-only (documented
    degradation — the prior-turn leak is NOT fixed across a restart).
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock

import pytest

from cctelegram import md_capture
from cctelegram.handlers import interactive_ui
from cctelegram.handlers import message_queue

_SID = "feedface-0000-1111-2222-333344445555"


@pytest.fixture
def cc_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
    md_capture.msg_display_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture(autouse=True)
def _clean_state():
    message_queue._route_user_turn_at.clear()
    interactive_ui._interactive_msgs.clear()
    yield
    message_queue._route_user_turn_at.clear()
    interactive_ui._interactive_msgs.clear()


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
    with md_capture.session_ndjson_path(session_id).open("a") as f:
        f.write(json.dumps(line) + "\n")


@pytest.fixture
def captured_posts(monkeypatch):
    """Patch topic_send + session_id_for_window the way the live-prose path
    reads them; record what (if anything) gets posted."""
    posts: list[str] = []

    sent_msg = AsyncMock()
    sent_msg.message_id = 555

    async def fake_topic_send(bot, **kwargs):
        posts.append(kwargs["text"])
        return sent_msg, None

    monkeypatch.setattr(interactive_ui, "topic_send", fake_topic_send)
    monkeypatch.setattr(interactive_ui, "session_id_for_window", lambda _wid: _SID)
    return posts


@pytest.mark.asyncio
async def test_prior_turn_prose_not_posted(cc_dir, captured_posts):
    """A prior turn's prose finalized BEFORE the current delivery boundary is
    filtered out — the P2-1 leak."""
    now = time.time()
    # Prior-turn prose finalized 3s ago, well within the AUQ TTL.
    _seed(_SID, message_id="PRIOR", delta="prior turn prose", captured_at=now - 3)
    # Stamp the CURRENT user-turn boundary AFTER that prose finalized.
    message_queue.set_route_user_turn_at(1, 100, "@0")

    await interactive_ui._maybe_post_live_prose(
        AsyncMock(),
        user_id=1,
        thread_id=100,
        chat_id=42,
        window_id="@0",
        ui_name="AskUserQuestion",
    )
    assert captured_posts == [], (
        "a prior turn's leftover prose was posted above the picker"
    )


@pytest.mark.asyncio
async def test_current_turn_prose_posted(cc_dir, captured_posts):
    """The current turn's prose finalized AFTER the boundary (and BEFORE now)
    passes. The boundary is set in the recent past so the seeded ``final_at`` is
    a realistic value strictly between the boundary and the function's internal
    ``time.time()`` — NOT future-dated relative to ``now`` (Hermes P3)."""
    now = time.time()
    # Boundary 1s ago; current-turn prose finalized 0.5s ago — after the
    # boundary, before the internal now.
    message_queue._route_user_turn_at[(1, 100, "@0")] = now - 1.0
    _seed(_SID, message_id="CUR", delta="current turn prose", captured_at=now - 0.5)

    await interactive_ui._maybe_post_live_prose(
        AsyncMock(),
        user_id=1,
        thread_id=100,
        chat_id=42,
        window_id="@0",
        ui_name="AskUserQuestion",
    )
    assert captured_posts == ["current turn prose"]


@pytest.mark.asyncio
async def test_no_stamp_degrades_to_ttl_only(cc_dir, captured_posts):
    """Restart degradation: no stamp for the route (peek → None) → not_before is
    None → TTL-only behavior. A within-TTL prose still posts (the prior-turn
    leak is NOT fixed across a restart — documented degradation)."""
    now = time.time()
    _seed(_SID, message_id="ANY", delta="prose within ttl", captured_at=now - 2)
    assert message_queue.peek_route_user_turn_at(1, 100, "@0") is None

    await interactive_ui._maybe_post_live_prose(
        AsyncMock(),
        user_id=1,
        thread_id=100,
        chat_id=42,
        window_id="@0",
        ui_name="AskUserQuestion",
    )
    assert captured_posts == ["prose within ttl"]


@pytest.mark.asyncio
async def test_on_pane_picker_reads_prior_stamp_not_a_new_one(cc_dir, captured_posts):
    """inbound:1061 ordering: an on-pane picker whose turn produced prose is
    served by the PRIOR delivery stamp. A NEW turn's stamp is NOT written before
    this picker render, so the within-boundary prose still posts.

    (We stamp once for the turn that produced the picker, finalize its prose
    after, and confirm a later un-stamped render still posts it.)"""
    now = time.time()
    # The picker's turn was delivered 1s ago; its prose finalized 0.5s ago —
    # after that boundary, before now (no later turn has clobbered the stamp).
    message_queue._route_user_turn_at[(1, 100, "@0")] = now - 1.0
    _seed(_SID, message_id="ONPANE", delta="on-pane prose", captured_at=now - 0.5)

    await interactive_ui._maybe_post_live_prose(
        AsyncMock(),
        user_id=1,
        thread_id=100,
        chat_id=42,
        window_id="@0",
        ui_name="AskUserQuestion",
    )
    assert captured_posts == ["on-pane prose"]


@pytest.mark.asyncio
async def test_concurrent_send_clobber_is_bounded_degradation(cc_dir, captured_posts):
    """Documented residual (Codex diff-review P2 → adversary-confirmed P3): a
    LATER delivery whose stamp clobbers the route boundary BEFORE an earlier
    picker first-renders can suppress the earlier turn's prose. This is a BOUNDED
    DEGRADATION — the JSONL copy still delivers the prose post-resolution (never
    a wrong post). The common 'send while a picker is on the pane' case is
    defused UPSTREAM (inbound_telegram renders the picker with the prior stamp
    before offering the new message); the only residual is delivering into a
    still-streaming Claude before its picker appears. This test pins the bounded
    degraded path so it can't silently change."""
    now = time.time()
    # Turn A's prose finalized 1s ago; a later delivery (B) then clobbered the
    # route boundary to 0.2s ago, before A's picker first-rendered.
    _seed(_SID, message_id="A", delta="turn A prose", captured_at=now - 1.0)
    message_queue._route_user_turn_at[(1, 100, "@0")] = now - 0.2

    await interactive_ui._maybe_post_live_prose(
        AsyncMock(),
        user_id=1,
        thread_id=100,
        chat_id=42,
        window_id="@0",
        ui_name="AskUserQuestion",
    )
    # final_at (now-1.0) <= clobbered boundary (now-0.2) → filtered. The JSONL
    # copy delivers it post-resolution (the documented degradation).
    assert captured_posts == []


def _late_record(now: float) -> md_capture.ProseRecord:
    return md_capture.ProseRecord(
        session_id=_SID,
        transcript_path=f"/p/{_SID}.jsonl",
        md_message_id="LATE",
        text="late-finalizing findings",
        raw_hash="r",
        norm_hash="n",
        first_seen_at=now - 1.0,
        final_at=now - 0.1,
    )


class TestLiveProseLateFinalizeStreamWait:
    """Late-finalize fix: when the base catch-up budget expires with no finalized
    prose but a prose message is ACTIVELY STREAMING, extend the wait ONCE so the
    prose still posts BEFORE the picker card (the EPM#2 'findings after the card'
    miss). A prose-less picker adds zero delay; a never-finalizing stream
    degrades to today's miss, bounded (never hangs)."""

    @pytest.mark.asyncio
    async def test_streaming_then_finalizes_posts_prose_before_card(
        self, cc_dir, captured_posts, monkeypatch
    ):
        # Base budget bails on the first deadline check; a generous extension
        # keeps polling. select returns None until the 3rd poll — i.e. AFTER the
        # base budget would have bailed pre-fix; only the extension reaches it.
        now = time.time()
        monkeypatch.setattr(interactive_ui, "_LIVE_PROSE_RETRY_BUDGET_S", 0.0)
        monkeypatch.setattr(interactive_ui, "_LIVE_PROSE_STREAM_WAIT_BUDGET_S", 2.0)
        monkeypatch.setattr(md_capture, "is_prose_streaming", lambda *a, **k: True)
        calls = {"n": 0}
        rec = _late_record(now)

        def fake_select(*a, **k):
            calls["n"] += 1
            return rec if calls["n"] >= 3 else None

        monkeypatch.setattr(md_capture, "select_fresh_prose", fake_select)

        await interactive_ui._maybe_post_live_prose(
            AsyncMock(),
            user_id=1,
            thread_id=100,
            chat_id=42,
            window_id="@0",
            ui_name="ExitPlanMode",
        )
        # The extension kept polling past the base bail and posted the prose.
        assert captured_posts == ["late-finalizing findings"]
        assert calls["n"] >= 3

    @pytest.mark.asyncio
    async def test_streaming_never_finalizes_no_post_bounded(
        self, cc_dir, captured_posts, monkeypatch
    ):
        monkeypatch.setattr(interactive_ui, "_LIVE_PROSE_RETRY_BUDGET_S", 0.05)
        monkeypatch.setattr(interactive_ui, "_LIVE_PROSE_STREAM_WAIT_BUDGET_S", 0.3)
        monkeypatch.setattr(md_capture, "is_prose_streaming", lambda *a, **k: True)
        monkeypatch.setattr(md_capture, "select_fresh_prose", lambda *a, **k: None)

        start = time.monotonic()
        await interactive_ui._maybe_post_live_prose(
            AsyncMock(),
            user_id=1,
            thread_id=100,
            chat_id=42,
            window_id="@0",
            ui_name="ExitPlanMode",
        )
        elapsed = time.monotonic() - start
        # Never finalized → no live post (JSONL delivers post-resolution).
        assert captured_posts == []
        # Fail-safe: bounded by base + extension (+ slack); never hangs.
        assert elapsed < 0.05 + 0.3 + 1.0

    @pytest.mark.asyncio
    async def test_prose_less_picker_no_extension(
        self, cc_dir, captured_posts, monkeypatch
    ):
        # No NDJSON → the REAL is_prose_streaming returns False. Spy that it is
        # consulted exactly once at the base deadline and the (large) extension
        # never engages — a prose-less picker incurs zero added delay.
        monkeypatch.setattr(interactive_ui, "_LIVE_PROSE_RETRY_BUDGET_S", 0.05)
        monkeypatch.setattr(interactive_ui, "_LIVE_PROSE_STREAM_WAIT_BUDGET_S", 5.0)
        monkeypatch.setattr(md_capture, "select_fresh_prose", lambda *a, **k: None)
        calls = {"n": 0}
        real = md_capture.is_prose_streaming

        def spy(*a, **k):
            calls["n"] += 1
            return real(*a, **k)

        monkeypatch.setattr(md_capture, "is_prose_streaming", spy)

        start = time.monotonic()
        await interactive_ui._maybe_post_live_prose(
            AsyncMock(),
            user_id=1,
            thread_id=100,
            chat_id=42,
            window_id="@0",
            ui_name="ExitPlanMode",
        )
        elapsed = time.monotonic() - start
        assert captured_posts == []
        assert calls["n"] == 1  # consulted once at the base deadline, then bailed
        assert elapsed < 0.05 + 1.0  # the 5s extension never engaged

    @pytest.mark.asyncio
    async def test_modality_agnostic_auq(self, cc_dir, captured_posts, monkeypatch):
        # The extension is modality-agnostic — identical behavior for AUQ.
        now = time.time()
        monkeypatch.setattr(interactive_ui, "_LIVE_PROSE_RETRY_BUDGET_S", 0.0)
        monkeypatch.setattr(interactive_ui, "_LIVE_PROSE_STREAM_WAIT_BUDGET_S", 2.0)
        monkeypatch.setattr(md_capture, "is_prose_streaming", lambda *a, **k: True)
        calls = {"n": 0}
        rec = _late_record(now)

        def fake_select(*a, **k):
            calls["n"] += 1
            return rec if calls["n"] >= 3 else None

        monkeypatch.setattr(md_capture, "select_fresh_prose", fake_select)

        await interactive_ui._maybe_post_live_prose(
            AsyncMock(),
            user_id=1,
            thread_id=100,
            chat_id=42,
            window_id="@0",
            ui_name="AskUserQuestion",
        )
        assert captured_posts == ["late-finalizing findings"]
