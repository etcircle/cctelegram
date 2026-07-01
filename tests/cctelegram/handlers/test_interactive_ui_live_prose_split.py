"""Live-prose >4096 split: ``_maybe_post_live_prose`` must send its findings in
Telegram-safe chunks BEFORE the picker card.

``topic_send`` does NOT split at Telegram's 4096-char limit, so a long findings
message (common for di-copilot) previously failed with "Message is too long",
the function returned silently (no marker), and the findings were re-delivered
SPLIT by the normal JSONL path only AFTER the AUQ resolved — i.e. after the user
already answered. These tests pin the split-loop fix:

  * a >4096 prose ⇒ MULTIPLE ``topic_send`` calls (all op="content"), the
    shown-live marker recorded, the "posted before picker" success logged;
  * a chunk send failure (``sent is None``) ⇒ NO marker recorded, so the JSONL
    copy still delivers the full prose post-resolution (fail-open, no silent
    loss);
  * a short (≤4096) prose still sends in ONE message and records the marker
    (the existing behavior stays green).
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from cctelegram import md_capture
from cctelegram.handlers import interactive_ui, message_queue, status_polling

_SID = "feedface-0000-1111-2222-333344445555"


@pytest.fixture(autouse=True)
def _clean_state():
    message_queue._route_user_turn_at.clear()
    interactive_ui._interactive_msgs.clear()
    status_polling._epm_surface_first_seen_at.clear()
    yield
    message_queue._route_user_turn_at.clear()
    interactive_ui._interactive_msgs.clear()
    status_polling._epm_surface_first_seen_at.clear()


def _record(text: str) -> md_capture.ProseRecord:
    return md_capture.ProseRecord(
        session_id=_SID,
        transcript_path=f"/p/{_SID}.jsonl",
        md_message_id="MSG",
        text=text,
        raw_hash="r",
        norm_hash="n",
        first_seen_at=time.time() - 1.0,
        final_at=time.time() - 0.1,
    )


@pytest.fixture
def harness(monkeypatch):
    """Record every ``topic_send`` call + the shown-live markers; make
    ``select_fresh_prose`` return a controllable candidate."""
    posts: list[dict] = []
    markers: list[dict] = []

    sent_msg = AsyncMock()
    sent_msg.message_id = 555

    state = {"candidate": None, "send_results": None}

    async def fake_topic_send(bot, **kwargs):
        posts.append(kwargs)
        # ``send_results`` (if set) drives per-call outcomes; else always OK.
        results = state["send_results"]
        if results is not None:
            idx = len(posts) - 1
            if idx < len(results) and results[idx] is None:
                return None, None
        return sent_msg, None

    def fake_select(session_id, **kwargs):
        return state["candidate"]

    def fake_record_shown_live(session_id, **kwargs):
        markers.append({"session_id": session_id, **kwargs})

    monkeypatch.setattr(interactive_ui, "topic_send", fake_topic_send)
    monkeypatch.setattr(interactive_ui, "session_id_for_window", lambda _wid: _SID)
    monkeypatch.setattr(md_capture, "select_fresh_prose", fake_select)
    monkeypatch.setattr(md_capture, "was_shown_live", lambda *a, **k: False)
    monkeypatch.setattr(md_capture, "record_shown_live", fake_record_shown_live)
    return {"posts": posts, "markers": markers, "state": state}


@pytest.mark.asyncio
async def test_live_prose_over_4096_is_split_before_card(harness, caplog):
    """A >4096 prose is sent as MULTIPLE op="content" chunks (each ≤4096) and the
    marker is recorded once for the FULL text's norm_hash."""
    long_text = "\n".join("line %04d of the findings prose" % i for i in range(400))
    assert len(long_text) > 4096
    harness["state"]["candidate"] = _record(long_text)

    with caplog.at_level("INFO"):
        await interactive_ui._maybe_post_live_prose(
            AsyncMock(),
            user_id=1,
            thread_id=100,
            chat_id=42,
            window_id="@0",
            ui_name="AskUserQuestion",
        )

    posts = harness["posts"]
    assert len(posts) >= 2, "long prose must be split into multiple sends"
    # Every chunk is a content send, in order, each within Telegram's hard limit.
    for p in posts:
        assert p["op"] == "content"
        assert len(p["text"]) <= 4096
    # The marker is recorded once, for the FULL text's norm_hash (dedup parity).
    assert len(harness["markers"]) == 1
    assert harness["markers"][0]["norm_hash"] == "n"
    assert any("posted before picker" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_live_prose_with_fenced_code_block_stays_under_4096(harness):
    """A fenced code block straddling the split boundary must not push any chunk
    over Telegram's 4096 hard limit — ``split_message`` can add a few chars when
    it auto-closes the fence at the cut, so the conservative ``_LIVE_PROSE_CHUNK_MAX``
    (< 4096) is what keeps every boundary chunk sendable (Hermes P3)."""
    assert interactive_ui._LIVE_PROSE_CHUNK_MAX < 4096  # headroom is the guarantee
    pre = "\n".join("intro paragraph line number %04d here" % i for i in range(120))
    code = "\n".join("    step_%04d = compute(value_%04d)" % (i, i) for i in range(200))
    long_text = pre + "\n\n```\n" + code + "\n```\n"
    assert len(long_text) > 4096
    harness["state"]["candidate"] = _record(long_text)

    await interactive_ui._maybe_post_live_prose(
        AsyncMock(),
        user_id=1,
        thread_id=100,
        chat_id=42,
        window_id="@0",
        ui_name="AskUserQuestion",
    )

    posts = harness["posts"]
    assert len(posts) >= 2
    for p in posts:
        assert p["op"] == "content"
        assert len(p["text"]) <= 4096, f"chunk exceeded 4096: len={len(p['text'])}"
    assert len(harness["markers"]) == 1


@pytest.mark.asyncio
async def test_live_prose_chunk_send_failure_records_no_marker(harness):
    """If ANY chunk fails to send (``sent is None``), NO marker is recorded — so
    the post-resolution JSONL copy still delivers the full prose (fail-open)."""
    long_text = "\n".join("line %04d of the findings prose" % i for i in range(400))
    assert len(long_text) > 4096
    harness["state"]["candidate"] = _record(long_text)
    # The SECOND chunk send fails.
    harness["state"]["send_results"] = [object(), None]

    await interactive_ui._maybe_post_live_prose(
        AsyncMock(),
        user_id=1,
        thread_id=100,
        chat_id=42,
        window_id="@0",
        ui_name="AskUserQuestion",
    )

    # No marker → the JSONL copy is not suppressed (no silent loss).
    assert harness["markers"] == []


@pytest.mark.asyncio
async def test_live_prose_first_chunk_failure_records_no_marker(harness):
    """The very first chunk failing (the ≤4096 single-message case at the API)
    also records no marker."""
    harness["state"]["candidate"] = _record("short findings")
    harness["state"]["send_results"] = [None]

    await interactive_ui._maybe_post_live_prose(
        AsyncMock(),
        user_id=1,
        thread_id=100,
        chat_id=42,
        window_id="@0",
        ui_name="AskUserQuestion",
    )

    assert harness["markers"] == []


@pytest.mark.asyncio
async def test_short_prose_single_message_still_records_marker(harness):
    """A ≤4096 prose still sends in ONE message and records the marker — the
    existing behavior stays green."""
    harness["state"]["candidate"] = _record("SQLite is a serverless database")

    await interactive_ui._maybe_post_live_prose(
        AsyncMock(),
        user_id=1,
        thread_id=100,
        chat_id=42,
        window_id="@0",
        ui_name="AskUserQuestion",
    )

    posts = harness["posts"]
    assert len(posts) == 1
    assert posts[0]["op"] == "content"
    assert posts[0]["text"] == "SQLite is a serverless database"
    assert len(harness["markers"]) == 1
    assert harness["markers"][0]["norm_hash"] == "n"
