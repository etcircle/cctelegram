"""Tests for the §2.5.3 Stage 5.c persistent message-refs SQLite store."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cctelegram import message_refs
from cctelegram.config import config
from cctelegram.message_refs import MessageRef, TRUNCATION_MARKER


def _make_ref(
    *,
    chat_id: int = -100123,
    message_id: int = 1,
    user_id: int = 7,
    role: str = "assistant",
    content_type: str = "text",
    text: str | None = "hello",
    created_at: str | None = None,
    session_id: str | None = "sess-A",
    transcript_uuid: str | None = "uuid-A",
    window_id: str | None = "@0",
    thread_id: int | None = 42,
    part_index: int = 0,
) -> MessageRef:
    return MessageRef(
        chat_id=chat_id,
        thread_id=thread_id,
        message_id=message_id,
        user_id=user_id,
        window_id=window_id,
        session_id=session_id,
        transcript_uuid=transcript_uuid,
        transcript_byte_start=None,
        transcript_byte_end=None,
        role=role,
        content_type=content_type,
        part_index=part_index,
        text=text,
        text_sha256=None,
        created_at=created_at or message_refs.now_iso(),
    )


@pytest.fixture(autouse=True)
async def _isolated_db(tmp_path: Path):
    """Each test gets a fresh per-tempfile database."""
    message_refs._reset_for_tests()
    db_path = tmp_path / "refs.db"
    await message_refs.init_db(db_path)
    yield db_path
    await message_refs.close()
    message_refs._reset_for_tests()


async def test_init_db_creates_schema(_isolated_db: Path) -> None:
    conn = message_refs._require_conn()
    async with conn.execute("SELECT name FROM sqlite_master WHERE type='table'") as cur:
        rows = await cur.fetchall()
    names = [row[0] for row in rows]
    assert "telegram_message_refs" in names


async def test_insert_and_lookup_round_trip() -> None:
    ref = _make_ref(message_id=10, text="round trip")
    await message_refs.insert(ref)
    out = await message_refs.lookup(ref.chat_id, ref.message_id)
    assert out is not None
    assert out.role == "assistant"
    assert out.content_type == "text"
    assert out.text == "round trip"
    assert out.session_id == "sess-A"
    assert out.transcript_uuid == "uuid-A"
    expected_sha = hashlib.sha256(b"round trip").hexdigest()
    assert out.text_sha256 == expected_sha


async def test_insert_or_replace_overwrites() -> None:
    first = _make_ref(message_id=11, role="status", content_type="status", text="A")
    await message_refs.insert(first)
    second = _make_ref(message_id=11, role="assistant", content_type="text", text="B")
    await message_refs.insert(second)
    out = await message_refs.lookup(first.chat_id, 11)
    assert out is not None
    assert out.role == "assistant"
    assert out.content_type == "text"
    assert out.text == "B"


async def test_text_truncated_at_max_chars() -> None:
    cap = config.message_ref_text_max_chars
    long_text = "X" * (cap + 500)
    ref = _make_ref(message_id=12, text=long_text)
    await message_refs.insert(ref)
    out = await message_refs.lookup(ref.chat_id, 12)
    assert out is not None
    assert out.text is not None
    assert len(out.text) <= cap
    assert out.text.endswith(TRUNCATION_MARKER)


async def test_text_sha256_is_full_text_hash() -> None:
    cap = config.message_ref_text_max_chars
    long_text = "Y" * (cap + 1000)
    ref = _make_ref(message_id=13, text=long_text)
    await message_refs.insert(ref)
    out = await message_refs.lookup(ref.chat_id, 13)
    assert out is not None
    expected = hashlib.sha256(long_text.encode("utf-8")).hexdigest()
    assert out.text_sha256 == expected


async def test_update_role_and_content_type() -> None:
    ref = _make_ref(message_id=14, role="status", content_type="status", text="busy")
    await message_refs.insert(ref)
    await message_refs.update_role_and_content_type(
        ref.chat_id, 14, "assistant", "text"
    )
    out = await message_refs.lookup(ref.chat_id, 14)
    assert out is not None
    assert out.role == "assistant"
    assert out.content_type == "text"


async def test_delete_removes_row() -> None:
    ref = _make_ref(message_id=15)
    await message_refs.insert(ref)
    await message_refs.delete(ref.chat_id, 15)
    out = await message_refs.lookup(ref.chat_id, 15)
    assert out is None


async def test_prune_older_than_drops_old_rows() -> None:
    old_iso = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    fresh_iso = datetime.now(timezone.utc).isoformat()
    await message_refs.insert(_make_ref(message_id=16, created_at=old_iso))
    await message_refs.insert(_make_ref(message_id=17, created_at=fresh_iso))
    deleted = await message_refs.prune_older_than(30)
    assert deleted == 1
    assert await message_refs.lookup(-100123, 16) is None
    assert await message_refs.lookup(-100123, 17) is not None


async def test_concurrent_inserts_serialize_safely() -> None:
    refs = [_make_ref(message_id=1000 + i, text=f"row-{i}") for i in range(100)]
    await asyncio.gather(*(message_refs.insert(r) for r in refs))
    for r in refs:
        out = await message_refs.lookup(r.chat_id, r.message_id)
        assert out is not None
        assert out.text == r.text


async def test_init_db_is_idempotent(tmp_path: Path) -> None:
    """Test A: ``init_db`` MUST be a no-op on the second call.

    The ``_isolated_db`` fixture already opens a connection. Calling
    ``init_db`` again with the same path must not re-apply schema, must not
    reopen the connection, and must not error. This locks in the early-
    return at ``message_refs.init_db`` so a future refactor can't quietly
    regress to running the schema on every call.
    """
    conn_before = message_refs._require_conn()
    # Second call against the same db file: must short-circuit.
    await message_refs.init_db(tmp_path / "refs.db")
    conn_after = message_refs._require_conn()
    assert conn_before is conn_after


async def test_text_none_roundtrip() -> None:
    """Test B: storing ``text=None`` round-trips as ``None``/``None``."""
    ref = _make_ref(message_id=20, text=None)
    await message_refs.insert(ref)
    out = await message_refs.lookup(ref.chat_id, 20)
    assert out is not None
    assert out.text is None
    assert out.text_sha256 is None


async def test_text_empty_string_roundtrip() -> None:
    """Test C: ``text=""`` is normalized to ``None``/``None`` per ``_bound_text``."""
    ref = _make_ref(message_id=21, text="")
    await message_refs.insert(ref)
    out = await message_refs.lookup(ref.chat_id, 21)
    assert out is not None
    assert out.text is None
    assert out.text_sha256 is None


async def test_close_with_pending_writes_drains(tmp_path: Path) -> None:
    """Test D: spawning many ``_spawn_ref_insert`` then ``close()`` must
    not leak unhandled exceptions on shutdown — the production teardown
    race the §2.5.3 risks called out.

    The thin async wrapper inside ``_spawn_ref_insert`` swallows shutdown-
    race exceptions; this test exercises the real spawn path then closes
    the database before all tasks have necessarily run, then re-inits
    against a fresh path. Any unhandled-exception leak would surface as a
    pytest warning or RuntimeError on close.
    """
    from cctelegram.handlers.message_sender import _spawn_ref_insert

    for i in range(50):
        _spawn_ref_insert(
            chat_id=-100123,
            thread_id=42,
            message_id=2000 + i,
            user_id=7,
            window_id="@0",
            session_id="sess-X",
            transcript_uuid=None,
            role="activity",
            content_type="activity",
            part_index=0,
            text=f"pending-{i}",
        )

    # Close immediately — some tasks may still be queued at the aiosqlite
    # worker. The wrapper swallows the resulting "connection closed" race.
    await message_refs.close()
    message_refs._reset_for_tests()

    # Drain any still-pending tasks so they don't dangle into the next test.
    await asyncio.sleep(0.05)

    # Re-init against a fresh path: should succeed cleanly.
    await message_refs.init_db(tmp_path / "fresh.db")
    # Sanity: the new database is empty and writable.
    fresh = _make_ref(message_id=9999, text="post-reopen")
    await message_refs.insert(fresh)
    out = await message_refs.lookup(fresh.chat_id, 9999)
    assert out is not None
    assert out.text == "post-reopen"


async def test_spawn_ref_insert_writes_via_create_task() -> None:
    """Test E: the production ``_spawn_ref_insert`` path actually writes.

    ``test_concurrent_inserts_serialize_safely`` exercises ``insert`` via
    ``gather``; this test instead drives the fire-and-forget spawn helper
    used by the real send hot path, then drains and verifies all rows
    landed.
    """
    from cctelegram.handlers.message_sender import _spawn_ref_insert

    n = 100
    for i in range(n):
        _spawn_ref_insert(
            chat_id=-100123,
            thread_id=42,
            message_id=3000 + i,
            user_id=7,
            window_id="@0",
            session_id="sess-Y",
            transcript_uuid=None,
            role="assistant",
            content_type="text",
            part_index=0,
            text=f"spawn-{i}",
        )
    # Let the spawned tasks run. asyncio.sleep(0) runs one tick; multiple
    # ticks let the aiosqlite worker drain.
    for _ in range(20):
        await asyncio.sleep(0.01)

    found = 0
    for i in range(n):
        out = await message_refs.lookup(-100123, 3000 + i)
        if out is not None and out.text == f"spawn-{i}":
            found += 1
    assert found == n
