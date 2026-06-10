"""Tests for the topic-targeted send/edit/delete classifier in message_sender.

The classifier converts Telegram error strings/types into ``TopicSendOutcome``
so that callers (status, content, activity, attention, interactive) can
distinguish a genuinely dead topic from a transient/format failure and route
to repair or emergency DM accordingly.
"""

from __future__ import annotations

import pytest
from telegram.error import BadRequest, Forbidden, RetryAfter

from cctelegram.handlers.message_sender import TopicSendOutcome, _classify_bad_request


@pytest.mark.parametrize(
    "message",
    [
        "Message thread not found",
        "message thread not found",
        "Bad Request: message thread not found",
        "Topic_id_invalid",
        "TOPIC_ID_INVALID",
        "Bad Request: TOPIC_ID_INVALID",
        "Topic not found",
    ],
)
def test_classify_topic_not_found(message: str) -> None:
    assert (
        _classify_bad_request(BadRequest(message)) is TopicSendOutcome.TOPIC_NOT_FOUND
    )


@pytest.mark.parametrize(
    "message",
    [
        "Topic_closed",
        "TOPIC_CLOSED",
        "Bad Request: topic is closed",
    ],
)
def test_classify_topic_closed(message: str) -> None:
    assert _classify_bad_request(BadRequest(message)) is TopicSendOutcome.TOPIC_CLOSED


def test_classify_forbidden() -> None:
    assert (
        _classify_bad_request(Forbidden("Forbidden: bot was kicked"))
        is TopicSendOutcome.FORBIDDEN
    )


def test_classify_rate_limited() -> None:
    assert _classify_bad_request(RetryAfter(5)) is TopicSendOutcome.RATE_LIMITED


@pytest.mark.parametrize(
    "message",
    [
        "Bad Request: chat not found",
        "Bad Request: message to edit not found",
        "Bad Request: can't parse entities",
        "completely unknown error string",
    ],
)
def test_classify_other_bad_request(message: str) -> None:
    assert _classify_bad_request(BadRequest(message)) is TopicSendOutcome.OTHER


def test_classify_message_not_modified() -> None:
    # ``message is not modified`` is a benign no-op edit response — pinned to
    # its own outcome so attention.notify_waiting can short-circuit instead of
    # falling through to a fresh, audible card.
    assert (
        _classify_bad_request(BadRequest("Bad Request: message is not modified"))
        is TopicSendOutcome.MESSAGE_NOT_MODIFIED
    )


def test_classify_random_exception() -> None:
    assert (
        _classify_bad_request(RuntimeError("not a telegram error"))
        is TopicSendOutcome.OTHER
    )


def test_classify_outcome_values_are_stable() -> None:
    # Logged into launchd.err.log; downstream tooling parses these.
    assert TopicSendOutcome.OK.value == "OK"
    assert TopicSendOutcome.TOPIC_NOT_FOUND.value == "TOPIC_NOT_FOUND"
    assert TopicSendOutcome.TOPIC_CLOSED.value == "TOPIC_CLOSED"
    assert TopicSendOutcome.FORBIDDEN.value == "FORBIDDEN"
    assert TopicSendOutcome.RATE_LIMITED.value == "RATE_LIMITED"
    assert TopicSendOutcome.OTHER.value == "OTHER"


# ── §2.5.3 Stage 5.c: provenance row writes on topic_send / topic_delete ──


import asyncio  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

from cctelegram import message_refs  # noqa: E402
from cctelegram.handlers.message_sender import (  # noqa: E402
    topic_delete,
    topic_edit,
    topic_send,
)


@pytest.fixture
async def _refs_db(tmp_path: Path):
    message_refs._reset_for_tests()
    await message_refs.init_db(tmp_path / "refs.db")
    yield
    await message_refs.close()
    message_refs._reset_for_tests()


async def _drain_pending_tasks() -> None:
    """Yield control so fire-and-forget create_task tasks finish."""
    for _ in range(5):
        await asyncio.sleep(0)


async def test_topic_send_writes_message_ref(_refs_db: None) -> None:
    bot = MagicMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 555
    bot.send_message = AsyncMock(return_value=sent_msg)

    result, outcome = await topic_send(
        bot,
        op="content",
        user_id=7,
        chat_id=-100123,
        thread_id=42,
        window_id="@0",
        text="hello world",
        role="assistant",
        content_type="text",
        transcript_uuid="uuid-X",
        session_id="sess-X",
    )
    assert outcome is TopicSendOutcome.OK
    assert result is sent_msg

    await _drain_pending_tasks()
    row = await message_refs.lookup(-100123, 555)
    assert row is not None
    assert row.role == "assistant"
    assert row.content_type == "text"
    assert row.transcript_uuid == "uuid-X"
    assert row.session_id == "sess-X"
    assert row.window_id == "@0"
    assert row.text == "hello world"


async def test_topic_delete_removes_message_ref(_refs_db: None) -> None:
    bot = MagicMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 777
    bot.send_message = AsyncMock(return_value=sent_msg)
    bot.delete_message = AsyncMock(return_value=None)

    await topic_send(
        bot,
        op="content",
        user_id=7,
        chat_id=-100123,
        thread_id=42,
        window_id="@0",
        text="hi",
        role="assistant",
        content_type="text",
    )
    await _drain_pending_tasks()
    assert await message_refs.lookup(-100123, 777) is not None

    outcome = await topic_delete(
        bot,
        op="content",
        user_id=7,
        chat_id=-100123,
        thread_id=42,
        window_id="@0",
        message_id=777,
    )
    assert outcome is TopicSendOutcome.OK
    await _drain_pending_tasks()
    assert await message_refs.lookup(-100123, 777) is None


async def test_topic_edit_message_not_modified_updates_message_ref(
    _refs_db: None,
) -> None:
    """W8 P2-1 provenance: MESSAGE_NOT_MODIFIED is caller-success — the body
    already matches the intended content — so a status→content repurposing
    edit must still flip the provenance row's role/content_type, exactly like
    an OK edit. Otherwise reply enrichment keeps seeing the (now content)
    message as ``status``."""
    bot = MagicMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 888
    bot.send_message = AsyncMock(return_value=sent_msg)

    # Seed a status-role row (the pre-conversion state).
    await topic_send(
        bot,
        op="status",
        user_id=7,
        chat_id=-100123,
        thread_id=42,
        window_id="@0",
        text="🟡 Busy",
        role="status",
        content_type="status",
    )
    await _drain_pending_tasks()
    row = await message_refs.lookup(-100123, 888)
    assert row is not None and row.role == "status"

    bot.edit_message_text = AsyncMock(
        side_effect=BadRequest("Bad Request: message is not modified")
    )
    outcome = await topic_edit(
        bot,
        op="content",
        user_id=7,
        chat_id=-100123,
        thread_id=42,
        window_id="@0",
        message_id=888,
        text="🟡 Busy",
        role="assistant",
        content_type="text",
    )
    assert outcome is TopicSendOutcome.MESSAGE_NOT_MODIFIED
    await _drain_pending_tasks()
    row = await message_refs.lookup(-100123, 888)
    assert row is not None
    assert row.role == "assistant", (
        "MESSAGE_NOT_MODIFIED repurposing edit must flip the provenance row "
        f"like an OK edit; still {row.role!r}"
    )
    assert row.content_type == "text"


async def test_topic_edit_message_not_modified_on_plaintext_fallback_updates_ref(
    _refs_db: None,
) -> None:
    """W8 R2 P2-1: topic_edit's plain-text FALLBACK path can also classify
    MESSAGE_NOT_MODIFIED (formatted attempt fails with a non-shortcircuit
    outcome, the plain retry hits "message is not modified"). That return
    site must flip the provenance row too — the caller treats any
    MESSAGE_NOT_MODIFIED as converted success."""
    bot = MagicMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 889
    bot.send_message = AsyncMock(return_value=sent_msg)

    await topic_send(
        bot,
        op="status",
        user_id=7,
        chat_id=-100123,
        thread_id=42,
        window_id="@0",
        text="🟡 Busy",
        role="status",
        content_type="status",
    )
    await _drain_pending_tasks()
    row = await message_refs.lookup(-100123, 889)
    assert row is not None and row.role == "status"

    # Formatted attempt -> generic parse error (OTHER, falls through);
    # plain-text retry -> "message is not modified".
    bot.edit_message_text = AsyncMock(
        side_effect=[
            BadRequest("Bad Request: can't parse entities"),
            BadRequest("Bad Request: message is not modified"),
        ]
    )
    outcome = await topic_edit(
        bot,
        op="content",
        user_id=7,
        chat_id=-100123,
        thread_id=42,
        window_id="@0",
        message_id=889,
        text="🟡 Busy",
        role="assistant",
        content_type="text",
    )
    assert outcome is TopicSendOutcome.MESSAGE_NOT_MODIFIED
    assert bot.edit_message_text.await_count == 2
    await _drain_pending_tasks()
    row = await message_refs.lookup(-100123, 889)
    assert row is not None
    assert row.role == "assistant", (
        "fallback-path MESSAGE_NOT_MODIFIED must flip the provenance row "
        f"like an OK edit; still {row.role!r}"
    )
    assert row.content_type == "text"
