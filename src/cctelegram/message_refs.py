"""Persistent provenance map: Telegram message_id ↔ Claude transcript entry.

Stage 5.c of the event-driven busy/route-queues plan. Every successful
Telegram send/edit/delete that targets a topic feeds this table so a future
reply (the user taps Reply on an old card) can be enriched with its original
role, content_type, and Claude session_id without having to re-derive it
from JSONL on demand.

Single global ``aiosqlite.Connection`` opened lazily by ``init_db`` and
closed by ``close``. WAL mode is set on init so the read-side resolver does
not block the write-side fire-and-forget ``insert`` tasks coming off the
send hot path.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from .config import config

logger = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS telegram_message_refs (
    chat_id INTEGER NOT NULL,
    thread_id INTEGER,
    message_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    window_id TEXT,
    session_id TEXT,
    transcript_uuid TEXT,
    transcript_byte_start INTEGER,
    transcript_byte_end INTEGER,
    role TEXT NOT NULL,
    content_type TEXT NOT NULL,
    part_index INTEGER NOT NULL DEFAULT 0,
    text TEXT,
    text_sha256 TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_message_refs_session ON telegram_message_refs(session_id);
CREATE INDEX IF NOT EXISTS idx_message_refs_window ON telegram_message_refs(window_id);
CREATE INDEX IF NOT EXISTS idx_message_refs_thread ON telegram_message_refs(thread_id);
"""

TRUNCATION_MARKER = "… [truncated]"


@dataclass
class MessageRef:
    """One row of ``telegram_message_refs``."""

    chat_id: int
    thread_id: int | None
    message_id: int
    user_id: int
    window_id: str | None
    session_id: str | None
    transcript_uuid: str | None
    transcript_byte_start: int | None
    transcript_byte_end: int | None
    role: str
    content_type: str
    part_index: int
    text: str | None
    text_sha256: str | None
    created_at: str


_conn: aiosqlite.Connection | None = None


def _bound_text(text: str | None) -> tuple[str | None, str | None]:
    """Apply the MESSAGE_REF_TEXT_MAX_CHARS cap and compute the full-text sha.

    The sha is over the original (untruncated) text so a rehydrate can verify
    the row matches what Telegram actually rendered, even when the stored
    ``text`` was clipped.
    """
    if text is None or text == "":
        return None, None
    sha = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    cap = config.message_ref_text_max_chars
    if len(text) <= cap:
        return text, sha
    cut = max(0, cap - len(TRUNCATION_MARKER))
    return text[:cut].rstrip() + TRUNCATION_MARKER, sha


def now_iso() -> str:
    """Canonical ``created_at`` producer.

    IMPORTANT: every writer of ``telegram_message_refs.created_at`` MUST go
    through this helper. ``prune_older_than`` does string comparison on the
    column (``WHERE created_at < ?``); that comparison is only valid as long
    as every row uses the identical offset-form ISO 8601 layout that
    ``datetime.isoformat()`` emits here (e.g. ``2026-05-02T10:30:00+00:00``).
    A future change to a ``Z`` suffix or to naive timestamps would silently
    misclassify rows as old or new. If we ever need a different format,
    migrate the column to a numeric epoch first.
    """
    return datetime.now(timezone.utc).isoformat()


def _require_conn() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("message_refs.init_db has not been called")
    return _conn


async def init_db(path: Path) -> None:
    """Open the database and apply the schema. Idempotent.

    WAL: cross-process and cross-coroutine reads stay non-blocking while the
    fire-and-forget insert path commits. Writes still serialize through the
    single shared connection.
    """
    global _conn
    if _conn is not None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(path))
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.executescript(SCHEMA)
    await conn.commit()
    _conn = conn
    logger.info("message_refs database opened at %s", path)


async def close() -> None:
    """Close the database connection. Idempotent."""
    global _conn
    if _conn is None:
        return
    try:
        await _conn.close()
    finally:
        _conn = None


async def insert(ref: MessageRef) -> None:
    """INSERT OR REPLACE — keys are (chat_id, message_id).

    Fire-and-forget safe: callers wrap this in ``asyncio.create_task`` (see
    ``handlers.message_sender._spawn_ref_insert``) so a SQLite stall never
    blocks the Telegram send path.

    Backpressure note: writes are unbounded. Under burst load the queue
    depth at aiosqlite's worker thread is bounded only by Python memory.
    Today's send rate (≈30/s ceiling from ``AIORateLimiter``) is well
    inside the safe zone. If load patterns change — e.g. 100+ concurrent
    sends sustained for seconds — add an ``asyncio.Semaphore`` or a
    bounded queue at the spawn helper rather than here. Current design is
    intentionally simple; do not over-engineer until measurement says so.
    """
    conn = _require_conn()
    text, sha = _bound_text(ref.text)
    try:
        await conn.execute(
            """
            INSERT OR REPLACE INTO telegram_message_refs (
                chat_id, thread_id, message_id, user_id,
                window_id, session_id, transcript_uuid,
                transcript_byte_start, transcript_byte_end,
                role, content_type, part_index,
                text, text_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ref.chat_id,
                ref.thread_id,
                ref.message_id,
                ref.user_id,
                ref.window_id,
                ref.session_id,
                ref.transcript_uuid,
                ref.transcript_byte_start,
                ref.transcript_byte_end,
                ref.role,
                ref.content_type,
                ref.part_index,
                text,
                sha,
                ref.created_at,
            ),
        )
        await conn.commit()
    except Exception as e:
        logger.warning(
            "message_refs.insert failed chat=%d msg=%d: %s",
            ref.chat_id,
            ref.message_id,
            e,
        )


async def update_role_and_content_type(
    chat_id: int,
    message_id: int,
    role: str,
    content_type: str,
) -> None:
    """For ``_convert_status_to_content``: the same Telegram message changes
    role from ``status`` to assistant ``text`` (or whichever first content
    part lands). The provenance row must follow the edit so a later reply
    sees the new role, not the stale ``status``.
    """
    conn = _require_conn()
    try:
        await conn.execute(
            """
            UPDATE telegram_message_refs
            SET role = ?, content_type = ?
            WHERE chat_id = ? AND message_id = ?
            """,
            (role, content_type, chat_id, message_id),
        )
        await conn.commit()
    except Exception as e:
        logger.warning(
            "message_refs.update_role_and_content_type failed chat=%d msg=%d: %s",
            chat_id,
            message_id,
            e,
        )


async def delete(chat_id: int, message_id: int) -> None:
    """Drop a row when the underlying Telegram message is deleted.

    No-op if the row does not exist; safe to call from cleanup paths that
    do not know whether a row was ever written.
    """
    conn = _require_conn()
    try:
        await conn.execute(
            "DELETE FROM telegram_message_refs WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        )
        await conn.commit()
    except Exception as e:
        logger.warning(
            "message_refs.delete failed chat=%d msg=%d: %s",
            chat_id,
            message_id,
            e,
        )


async def lookup(chat_id: int, message_id: int) -> MessageRef | None:
    """Resolve a row by primary key — used by ``reply_context.resolve``."""
    conn = _require_conn()
    try:
        async with conn.execute(
            """
            SELECT chat_id, thread_id, message_id, user_id,
                   window_id, session_id, transcript_uuid,
                   transcript_byte_start, transcript_byte_end,
                   role, content_type, part_index,
                   text, text_sha256, created_at
            FROM telegram_message_refs
            WHERE chat_id = ? AND message_id = ?
            """,
            (chat_id, message_id),
        ) as cursor:
            row = await cursor.fetchone()
    except Exception as e:
        logger.warning(
            "message_refs.lookup failed chat=%d msg=%d: %s",
            chat_id,
            message_id,
            e,
        )
        return None
    if row is None:
        return None
    return MessageRef(
        chat_id=row[0],
        thread_id=row[1],
        message_id=row[2],
        user_id=row[3],
        window_id=row[4],
        session_id=row[5],
        transcript_uuid=row[6],
        transcript_byte_start=row[7],
        transcript_byte_end=row[8],
        role=row[9],
        content_type=row[10],
        part_index=row[11],
        text=row[12],
        text_sha256=row[13],
        created_at=row[14],
    )


async def prune_older_than(days: int) -> int:
    """Delete rows whose ``created_at`` is older than ``days`` ago. Returns count."""
    conn = _require_conn()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_iso = cutoff.isoformat()
    try:
        cursor = await conn.execute(
            "DELETE FROM telegram_message_refs WHERE created_at < ?",
            (cutoff_iso,),
        )
        deleted = cursor.rowcount or 0
        await conn.commit()
        await cursor.close()
        return deleted
    except Exception as e:
        logger.warning("message_refs.prune_older_than failed: %s", e)
        return 0


def _reset_for_tests() -> None:
    """Test-only: drop the cached connection without closing it.

    Tests open and close per-tempfile databases; pytest-asyncio runs them in
    fresh event loops so an aiosqlite connection from a previous test cannot
    be reused. This helper drops the module-level reference so ``init_db``
    re-opens against the new path.
    """
    global _conn
    _conn = None
