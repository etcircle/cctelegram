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
from cctelegram.handlers.message_sender import topic_delete, topic_send  # noqa: E402


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
