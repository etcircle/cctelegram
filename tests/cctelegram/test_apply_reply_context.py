"""Unit tests for ``bot._apply_reply_context`` cross-session behaviour (P1.5)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cctelegram import bot as bot_module
from cctelegram.handlers.reply_context import ReplyContext


def _make_message_with_reply() -> MagicMock:
    """A message stub whose ``extract_reply_context`` will see a referent."""
    msg = MagicMock()
    msg.chat = MagicMock(id=1001)
    # extract_reply_context will be patched, so this only needs to be truthy.
    msg.reply_to_message = MagicMock()
    return msg


@pytest.mark.asyncio
async def test_same_session_quote_renders_normally() -> None:
    """Baseline: reply to a message from the current session → no marker."""
    message = _make_message_with_reply()
    ctx = ReplyContext(
        original_message_id=42,
        quoted_text="prior text",
        original_text="prior text",
        session_id="sess-current",
    )

    current_session = MagicMock()
    current_session.session_id = "sess-current"

    with (
        patch.object(bot_module, "extract_reply_context", return_value=ctx),
        patch.object(
            bot_module.reply_context_mod,
            "resolve",
            new_callable=AsyncMock,
            return_value=ctx,
        ),
        patch.object(
            bot_module.session_manager,
            "resolve_window_for_thread",
            return_value="@0",
        ),
        patch.object(
            bot_module.session_manager,
            "resolve_session_for_window",
            new_callable=AsyncMock,
            return_value=current_session,
        ),
        patch.object(bot_module.config, "reply_context_enabled", True),
        patch.object(bot_module.config, "reply_context_cross_session_enabled", True),
    ):
        rendered = await bot_module._apply_reply_context(message, 7, 99, "ask")

    assert "Cross-session reply" not in rendered
    assert "[User message]\nask" in rendered


@pytest.mark.asyncio
async def test_cross_session_quote_renders_with_marker_by_default() -> None:
    """P1.5: stale-session quote → annotated render, NOT silent drop."""
    message = _make_message_with_reply()
    ctx = ReplyContext(
        original_message_id=42,
        quoted_text="from old session",
        original_text="from old session",
        session_id="sess-OLD",
    )

    current_session = MagicMock()
    current_session.session_id = "sess-NEW"

    with (
        patch.object(bot_module, "extract_reply_context", return_value=ctx),
        patch.object(
            bot_module.reply_context_mod,
            "resolve",
            new_callable=AsyncMock,
            return_value=ctx,
        ),
        patch.object(
            bot_module.session_manager,
            "resolve_window_for_thread",
            return_value="@0",
        ),
        patch.object(
            bot_module.session_manager,
            "resolve_session_for_window",
            new_callable=AsyncMock,
            return_value=current_session,
        ),
        patch.object(bot_module.config, "reply_context_enabled", True),
        patch.object(bot_module.config, "reply_context_cross_session_enabled", True),
    ):
        rendered = await bot_module._apply_reply_context(message, 7, 99, "ask")

    assert "Cross-session reply" in rendered
    assert "from old session" in rendered  # quoted body still present
    assert "[User message]\nask" in rendered


@pytest.mark.asyncio
async def test_kill_switch_restores_silent_drop() -> None:
    """``CC_TELEGRAM_REPLY_CROSS_SESSION=false`` → original silent-drop behaviour."""
    message = _make_message_with_reply()
    ctx = ReplyContext(
        original_message_id=42,
        quoted_text="from old session",
        original_text="from old session",
        session_id="sess-OLD",
    )

    current_session = MagicMock()
    current_session.session_id = "sess-NEW"

    with (
        patch.object(bot_module, "extract_reply_context", return_value=ctx),
        patch.object(
            bot_module.reply_context_mod,
            "resolve",
            new_callable=AsyncMock,
            return_value=ctx,
        ),
        patch.object(
            bot_module.session_manager,
            "resolve_window_for_thread",
            return_value="@0",
        ),
        patch.object(
            bot_module.session_manager,
            "resolve_session_for_window",
            new_callable=AsyncMock,
            return_value=current_session,
        ),
        patch.object(bot_module.config, "reply_context_enabled", True),
        patch.object(bot_module.config, "reply_context_cross_session_enabled", False),
    ):
        rendered = await bot_module._apply_reply_context(message, 7, 99, "ask")

    # Silent drop: user_text returned verbatim, no quote block, no marker.
    assert rendered == "ask"
    assert "Cross-session reply" not in rendered


@pytest.mark.asyncio
async def test_master_kill_switch_off_skips_entire_path() -> None:
    """``reply_context_enabled=False`` short-circuits before any resolution."""
    message = _make_message_with_reply()

    with (
        patch.object(bot_module.config, "reply_context_enabled", False),
        patch.object(bot_module, "extract_reply_context") as mock_extract,
    ):
        rendered = await bot_module._apply_reply_context(message, 7, 99, "ask")

    assert rendered == "ask"
    # Never even tried to extract — fast path on master kill switch.
    mock_extract.assert_not_called()


@pytest.mark.asyncio
async def test_no_reply_returns_text_unchanged() -> None:
    """No referent → ``extract_reply_context`` returns None → text passthrough."""
    message = _make_message_with_reply()
    with (
        patch.object(bot_module.config, "reply_context_enabled", True),
        patch.object(bot_module, "extract_reply_context", return_value=None),
    ):
        rendered = await bot_module._apply_reply_context(message, 7, 99, "ask")

    assert rendered == "ask"


@pytest.mark.asyncio
async def test_unknown_session_treats_as_non_stale() -> None:
    """When provenance is missing on either side, fall through to normal render.

    The stale-quote guard requires *both* ``reply_ctx.session_id`` AND the
    current session id to be known and different. If either is None, we
    render normally (existing behaviour preserved). This matches the §2.5.4
    routing intent that the topic's window binding remains authoritative.
    """
    message = _make_message_with_reply()
    ctx = ReplyContext(
        original_message_id=42,
        quoted_text="unknown provenance",
        original_text="unknown provenance",
        session_id=None,  # missing
    )

    with (
        patch.object(bot_module, "extract_reply_context", return_value=ctx),
        patch.object(
            bot_module.reply_context_mod,
            "resolve",
            new_callable=AsyncMock,
            return_value=ctx,
        ),
        patch.object(
            bot_module.session_manager,
            "resolve_window_for_thread",
            return_value="@0",
        ),
        patch.object(
            bot_module.session_manager,
            "resolve_session_for_window",
            new_callable=AsyncMock,
            return_value=MagicMock(session_id="sess-current"),
        ),
        patch.object(bot_module.config, "reply_context_enabled", True),
        patch.object(bot_module.config, "reply_context_cross_session_enabled", True),
    ):
        rendered = await bot_module._apply_reply_context(message, 7, 99, "ask")

    # Normal render, no cross-session marker.
    assert "Cross-session reply" not in rendered
    assert "unknown provenance" in rendered
    assert "[User message]\nask" in rendered
