"""Tests for handlers.attention — heuristic, state machine, and digest indicator.

The state-machine tests pin the four corners of ``notify_waiting`` so the
topic-first attention card stays predictable:

  - idle → waiting fires a single fresh, audible ``topic_send``.
  - waiting → waiting (same fingerprint, dwell window) is a silent no-op.
  - waiting → waiting (different fingerprint) edits the live card silently.
  - dismiss flips state back to idle and edits the ack trailer.
  - the anti-flap guard prevents a second fresh send when a user reply has
    just dismissed the card and a follow-up notify_waiting fires inside the
    dwell window (the regression in the architect review).
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cctelegram.handlers import attention
from cctelegram.handlers.message_queue import (
    ActivityDigestState,
    _activity_msg_info,
    _finalize_activity_digest,
    _refresh_activity_digest_if_present,
    _render_activity_digest,
)
from cctelegram.handlers.message_sender import TopicSendOutcome


def test_is_attention_request_empty():
    assert attention.is_attention_request("") is False
    assert attention.is_attention_request("   \n  ") is False


def test_is_attention_request_cue_phrases():
    assert attention.is_attention_request("Do you want me to keep going?") is True
    assert attention.is_attention_request("Please confirm before I proceed.") is True
    assert attention.is_attention_request("Tell me which approach you want.") is True
    assert attention.is_attention_request("ok unless you object") is True


def test_is_attention_request_long_question():
    long_q = (
        "We have two reasonable migrations on the table; do you have a strong "
        "preference for the staged rollout over the dual-write?"
    )
    assert attention.is_attention_request(long_q) is True


def test_is_attention_request_short_question_ignored():
    # Short questions shouldn't trip the heuristic — too prone to false positives.
    assert attention.is_attention_request("Done?") is False


def test_is_attention_request_normal_status_text():
    assert attention.is_attention_request("Wrote 12 files. All tests pass.") is False


def test_render_activity_digest_waiting_indicator():
    state = ActivityDigestState(message_id=0, window_id="@0")
    state.lines = ["⚙️ Read foo.py"]
    state.tool_count = 1
    state.completed_count = 1

    busy = _render_activity_digest(state, waiting=False)
    assert busy.startswith("✅ Done") or busy.startswith("🟡 Busy")

    waiting = _render_activity_digest(state, waiting=True)
    assert waiting.startswith("🔔 Waiting on you")


def test_render_activity_digest_done_when_not_waiting():
    state = ActivityDigestState(message_id=0, window_id="@0", done=True)
    rendered = _render_activity_digest(state, waiting=False)
    assert rendered.startswith("✅ Done")


@pytest.mark.asyncio
async def test_finalize_activity_digest_marks_done_even_for_attention_text():
    """Stage 4 / Option A: assistant text never raises an attention card,
    so the digest finalizes to its terminal state regardless of question
    cues in the text. The previous "skip Done if it looks like a question"
    short-circuit left the digest stuck on Busy when no card raised."""
    bot = AsyncMock()
    key = (1, 10)
    state = ActivityDigestState(message_id=123, window_id="@0")
    _activity_msg_info[key] = state
    try:
        with patch(
            "cctelegram.handlers.message_queue._upsert_activity_digest",
            new_callable=AsyncMock,
        ) as mock_upsert:
            await _finalize_activity_digest(
                bot,
                user_id=1,
                thread_id=10,
                window_id="@0",
            )

            assert state.done is True
            mock_upsert.assert_awaited_once_with(bot, 1, 10, state)
    finally:
        _activity_msg_info.pop(key, None)


@pytest.mark.asyncio
async def test_finalize_activity_digest_marks_done_for_non_attention_text():
    bot = AsyncMock()
    key = (1, 10)
    state = ActivityDigestState(message_id=123, window_id="@0")
    _activity_msg_info[key] = state
    try:
        with patch(
            "cctelegram.handlers.message_queue._upsert_activity_digest",
            new_callable=AsyncMock,
        ) as mock_upsert:
            await _finalize_activity_digest(
                bot,
                user_id=1,
                thread_id=10,
                window_id="@0",
            )

            assert state.done is True
            mock_upsert.assert_awaited_once_with(bot, 1, 10, state)
    finally:
        _activity_msg_info.pop(key, None)


@pytest.mark.asyncio
async def test_refresh_activity_digest_renders_waiting_after_attention_state_changes(
    _reset_attention, mock_session_manager, monkeypatch
):
    # Pins the V1 attention-driven header path. V2 sources the header from
    # ``RunState`` and ignores attention state, so this test scopes itself
    # to V1 explicitly. Equivalent V2 coverage lives in test_busy_indicator.
    monkeypatch.setattr(
        "cctelegram.handlers.message_queue.config.busy_indicator_v2", False
    )
    bot = AsyncMock()
    key = (1, 10)
    state = ActivityDigestState(message_id=123, window_id="@0")
    state.lines = ["⚙️ Read foo.py"]
    state.tool_count = 1
    _activity_msg_info[key] = state
    try:
        sent = _make_sent_message(message_id=42)
        with (
            patch(
                "cctelegram.handlers.attention.topic_send",
                new_callable=AsyncMock,
            ) as mock_send,
            patch(
                "cctelegram.handlers.message_queue.topic_edit",
                new_callable=AsyncMock,
            ) as mock_edit,
            patch("cctelegram.handlers.message_queue.session_manager") as mq_session,
        ):
            mock_send.return_value = (sent, TopicSendOutcome.OK)
            mock_edit.return_value = TopicSendOutcome.OK
            mq_session.resolve_chat_id.return_value = -100123
            mq_session.get_display_name.return_value = "cctelegram"

            await attention.notify_waiting(
                bot,
                user_id=1,
                thread_id=10,
                window_id="@0",
                prompt_text="Do you want me to proceed?",
                kind="interactive_ui",
            )
            await _refresh_activity_digest_if_present(bot, 1, 10, "@0")

            mock_edit.assert_awaited_once()
            assert mock_edit.await_args.kwargs["text"].startswith("🔔 Waiting on you")
    finally:
        _activity_msg_info.pop(key, None)


# ── State machine ──────────────────────────────────────────────────────────


@pytest.fixture
def _reset_attention():
    attention.reset_for_tests()
    yield
    attention.reset_for_tests()


@pytest.fixture
def mock_session_manager():
    """Patch session_manager used by attention.notify_waiting/dismiss."""
    with patch("cctelegram.handlers.attention.session_manager") as sm:
        sm.resolve_chat_id.return_value = -100123
        sm.get_display_name.return_value = "cctelegram"
        yield sm


def _make_sent_message(message_id: int = 555) -> MagicMock:
    sent = MagicMock()
    sent.message_id = message_id
    return sent


@pytest.mark.usefixtures("_reset_attention", "mock_session_manager")
class TestAttentionStateMachine:
    @pytest.mark.asyncio
    async def test_idle_to_waiting_sends_fresh_audible_card(self):
        bot = AsyncMock()
        sent = _make_sent_message(message_id=42)
        with (
            patch(
                "cctelegram.handlers.attention.topic_send",
                new_callable=AsyncMock,
            ) as mock_send,
            patch(
                "cctelegram.handlers.attention.topic_edit",
                new_callable=AsyncMock,
            ) as mock_edit,
        ):
            mock_send.return_value = (sent, TopicSendOutcome.OK)

            outcome = await attention.notify_waiting(
                bot,
                user_id=1,
                thread_id=10,
                window_id="@0",
                prompt_text="Do you want me to proceed?",
                kind="interactive_ui",
            )

            assert outcome is TopicSendOutcome.OK
            mock_send.assert_awaited_once()
            mock_edit.assert_not_called()
            # Audible: notification must NOT be silenced for fresh sends.
            assert mock_send.await_args.kwargs.get("disable_notification") is False
            assert attention.is_waiting(1, 10) is True

    @pytest.mark.asyncio
    async def test_idle_to_waiting_writes_activity_role_for_ui_noise_demotion(self):
        """§2.5.5 regression: attention cards are bot UI, not Claude
        assistant text. Quote-replying to one MUST hit the UI-noise header
        path in ``reply_context.render_for_claude``. That path keys on
        ``role IN ('status','activity')``, so the topic_send for a fresh
        attention card must carry ``role='activity'`` and
        ``content_type='activity'`` — not ``role='assistant'``."""
        bot = AsyncMock()
        sent = _make_sent_message(message_id=42)
        with (
            patch(
                "cctelegram.handlers.attention.topic_send",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            mock_send.return_value = (sent, TopicSendOutcome.OK)

            await attention.notify_waiting(
                bot,
                user_id=1,
                thread_id=10,
                window_id="@0",
                prompt_text="Do you want me to proceed?",
                kind="interactive_ui",
            )

            kwargs = mock_send.await_args.kwargs
            assert kwargs.get("role") == "activity"
            assert kwargs.get("content_type") == "activity"

    @pytest.mark.asyncio
    async def test_waiting_same_fingerprint_is_silent_noop(self):
        bot = AsyncMock()
        sent = _make_sent_message(message_id=42)
        with (
            patch(
                "cctelegram.handlers.attention.topic_send",
                new_callable=AsyncMock,
            ) as mock_send,
            patch(
                "cctelegram.handlers.attention.topic_edit",
                new_callable=AsyncMock,
            ) as mock_edit,
        ):
            mock_send.return_value = (sent, TopicSendOutcome.OK)
            await attention.notify_waiting(
                bot,
                user_id=1,
                thread_id=10,
                window_id="@0",
                prompt_text="Do you want me to proceed?",
                kind="interactive_ui",
            )
            assert mock_send.await_count == 1

            # Identical follow-up inside the dwell window: zero Telegram I/O.
            outcome = await attention.notify_waiting(
                bot,
                user_id=1,
                thread_id=10,
                window_id="@0",
                prompt_text="Do you want me to proceed?",
                kind="interactive_ui",
            )
            assert outcome is TopicSendOutcome.OK
            assert mock_send.await_count == 1
            mock_edit.assert_not_called()

    @pytest.mark.asyncio
    async def test_waiting_different_fingerprint_edits_silently(self):
        bot = AsyncMock()
        sent = _make_sent_message(message_id=42)
        with (
            patch(
                "cctelegram.handlers.attention.topic_send",
                new_callable=AsyncMock,
            ) as mock_send,
            patch(
                "cctelegram.handlers.attention.topic_edit",
                new_callable=AsyncMock,
            ) as mock_edit,
        ):
            mock_send.return_value = (sent, TopicSendOutcome.OK)
            mock_edit.return_value = TopicSendOutcome.OK

            await attention.notify_waiting(
                bot,
                user_id=1,
                thread_id=10,
                window_id="@0",
                prompt_text="Do you want me to proceed?",
                kind="interactive_ui",
            )
            outcome = await attention.notify_waiting(
                bot,
                user_id=1,
                thread_id=10,
                window_id="@0",
                prompt_text="Different question — confirm before I write?",
                kind="interactive_ui",
            )

            assert outcome is TopicSendOutcome.OK
            # Exactly one fresh send (the original idle→waiting) and one edit.
            assert mock_send.await_count == 1
            mock_edit.assert_awaited_once()
            assert mock_edit.await_args.kwargs["message_id"] == 42

    @pytest.mark.asyncio
    async def test_waiting_edit_returning_message_not_modified_is_treated_as_ok(self):
        """Telegram says "no-op" when the body is already identical; no fresh card."""
        bot = AsyncMock()
        sent = _make_sent_message(message_id=42)
        with (
            patch(
                "cctelegram.handlers.attention.topic_send",
                new_callable=AsyncMock,
            ) as mock_send,
            patch(
                "cctelegram.handlers.attention.topic_edit",
                new_callable=AsyncMock,
            ) as mock_edit,
        ):
            mock_send.return_value = (sent, TopicSendOutcome.OK)
            mock_edit.return_value = TopicSendOutcome.MESSAGE_NOT_MODIFIED

            await attention.notify_waiting(
                bot,
                user_id=1,
                thread_id=10,
                window_id="@0",
                prompt_text="Do you want me to proceed?",
                kind="interactive_ui",
            )
            # Different fingerprint to force the edit branch.
            outcome = await attention.notify_waiting(
                bot,
                user_id=1,
                thread_id=10,
                window_id="@0",
                prompt_text="Slightly different question?",
                kind="interactive_ui",
            )

            assert outcome is TopicSendOutcome.OK
            mock_edit.assert_awaited_once()
            # Critical: must NOT fall through to a second fresh topic_send.
            assert mock_send.await_count == 1

    @pytest.mark.asyncio
    async def test_dismiss_edits_ack_trailer_and_flips_to_idle(self):
        bot = AsyncMock()
        sent = _make_sent_message(message_id=42)
        with (
            patch(
                "cctelegram.handlers.attention.topic_send",
                new_callable=AsyncMock,
            ) as mock_send,
            patch(
                "cctelegram.handlers.attention.topic_edit",
                new_callable=AsyncMock,
            ) as mock_edit,
        ):
            mock_send.return_value = (sent, TopicSendOutcome.OK)
            mock_edit.return_value = TopicSendOutcome.OK

            await attention.notify_waiting(
                bot,
                user_id=1,
                thread_id=10,
                window_id="@0",
                prompt_text="Do you want me to proceed?",
                kind="interactive_ui",
            )
            assert attention.is_waiting(1, 10) is True

            await attention.dismiss(bot, user_id=1, thread_id=10)

            assert attention.is_waiting(1, 10) is False
            mock_edit.assert_awaited_once()
            assert mock_edit.await_args.kwargs["message_id"] == 42
            assert attention.DISMISS_TRAILER in mock_edit.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_anti_flap_after_user_reply_dismiss(self):
        """User replies → dismiss → another notify_waiting must NOT push fresh card.

        This is the exact ping-pong path called out in the architect review:
        ``bot.py`` dismisses on user reply, then ``handle_interactive_ui`` runs
        and calls ``attention.notify_waiting`` again. Without the anti-flap
        guard the second call sees state=idle and would emit a fresh audible
        notification.
        """
        bot = AsyncMock()
        sent = _make_sent_message(message_id=42)
        with (
            patch(
                "cctelegram.handlers.attention.topic_send",
                new_callable=AsyncMock,
            ) as mock_send,
            patch(
                "cctelegram.handlers.attention.topic_edit",
                new_callable=AsyncMock,
            ) as mock_edit,
        ):
            mock_send.return_value = (sent, TopicSendOutcome.OK)
            mock_edit.return_value = TopicSendOutcome.OK

            await attention.notify_waiting(
                bot,
                user_id=1,
                thread_id=10,
                window_id="@0",
                prompt_text="Do you want me to proceed?",
                kind="interactive_ui",
            )
            await attention.dismiss(bot, user_id=1, thread_id=10)

            # Reset call counters so we can isolate the anti-flap behaviour.
            mock_send.reset_mock()
            mock_edit.reset_mock()

            outcome = await attention.notify_waiting(
                bot,
                user_id=1,
                thread_id=10,
                window_id="@0",
                prompt_text="A new but rapidly-following prompt?",
                kind="interactive_ui",
            )

            assert outcome is TopicSendOutcome.OK
            mock_send.assert_not_called()
            mock_edit.assert_awaited_once()
            assert mock_edit.await_args.kwargs["message_id"] == 42
            assert attention.is_waiting(1, 10) is True


# ── §2.6 narrow end-of-turn-question trigger ──────────────────────────────


def _make_event(
    *,
    role: str = "assistant",
    block_type: str = "text",
    text: str = "",
    stop_reason: str | None = "end_turn",
    tool_use_id: str | None = None,
    tool_name: str | None = None,
):
    """Construct a TranscriptEvent for the §2.6 predicate tests."""
    from cctelegram.session_monitor import TranscriptEvent

    return TranscriptEvent(
        session_id="sess-1",
        role=role,  # type: ignore[arg-type]
        block_type=block_type,  # type: ignore[arg-type]
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        stop_reason=stop_reason,
        timestamp=None,
        text=text,
        image_data=None,
    )


class TestFinalParagraphHelper:
    def test_simple_question(self):
        assert (
            attention.final_paragraph_ends_with_question_mark("Want me to do X?")
            is True
        )

    def test_question_with_trailing_bold(self):
        assert (
            attention.final_paragraph_ends_with_question_mark("**Want me to do X?**")
            is True
        )

    def test_question_in_middle_paragraph_not_final(self):
        text = "Did you see the report?\n\nI shipped the fix."
        assert attention.final_paragraph_ends_with_question_mark(text) is False

    def test_question_in_final_paragraph(self):
        text = "I shipped the fix.\n\nWant me to keep going?"
        assert attention.final_paragraph_ends_with_question_mark(text) is True

    def test_empty_text(self):
        assert attention.final_paragraph_ends_with_question_mark("") is False

    def test_no_question_mark(self):
        assert attention.final_paragraph_ends_with_question_mark("All done.") is False

    def test_trailing_single_newline_after_question_mark(self):
        # "?\n" — single trailing newline must not defeat the predicate.
        assert (
            attention.final_paragraph_ends_with_question_mark("Want me to do X?\n")
            is True
        )

    def test_trailing_period_after_question_mark(self):
        # "?." — trailing markdown punctuation strip must surface the "?".
        assert (
            attention.final_paragraph_ends_with_question_mark("Want me to do X?.")
            is True
        )


class TestEndOfTurnQuestionTrigger:
    def test_end_turn_with_final_question_fires(self):
        from cctelegram.handlers.busy_indicator import RunState

        ev = _make_event(
            text="I have two paths. Do you want me to pick the staged rollout?",
            stop_reason="end_turn",
        )
        assert attention.is_end_of_turn_question(ev, RunState.IDLE_RECENT) is True

    def test_mid_turn_question_does_not_fire(self):
        from cctelegram.handlers.busy_indicator import RunState

        ev = _make_event(
            text="I have two paths. Do you want me to pick the staged rollout?",
            stop_reason="tool_use",
        )
        assert attention.is_end_of_turn_question(ev, RunState.RUNNING) is False

    def test_end_turn_without_question_does_not_fire(self):
        from cctelegram.handlers.busy_indicator import RunState

        ev = _make_event(text="Done.", stop_reason="end_turn")
        assert attention.is_end_of_turn_question(ev, RunState.IDLE_RECENT) is False

    def test_question_in_middle_paragraph_does_not_fire(self):
        from cctelegram.handlers.busy_indicator import RunState

        # Final paragraph ends with "." not "?" — even though an earlier
        # paragraph contained a question, the predicate must not fire.
        ev = _make_event(
            text="Do you want me to pick A or B?\n\nI went with A and shipped it.",
            stop_reason="end_turn",
        )
        assert attention.is_end_of_turn_question(ev, RunState.IDLE_RECENT) is False

    def test_waiting_on_user_state_suppresses_double_card(self):
        from cctelegram.handlers.busy_indicator import RunState

        ev = _make_event(
            text="Two reasonable migrations on the table; do you want me to "
            "proceed with the staged rollout?",
            stop_reason="end_turn",
        )
        assert attention.is_end_of_turn_question(ev, RunState.WAITING_ON_USER) is False

    def test_thinking_block_does_not_fire(self):
        from cctelegram.handlers.busy_indicator import RunState

        # A thinking block with the same trailing-question shape must NOT
        # trigger a card — it's not text the user sees.
        ev = _make_event(
            block_type="thinking",
            text="Should I proceed with the staged rollout?",
            stop_reason="end_turn",
        )
        assert attention.is_end_of_turn_question(ev, RunState.IDLE_RECENT) is False

    def test_user_role_does_not_fire(self):
        from cctelegram.handlers.busy_indicator import RunState

        # User-role events must never raise an attention card, even with an
        # otherwise-matching question shape.
        ev = _make_event(
            role="user",
            text="Should I proceed with the staged rollout?",
            stop_reason="end_turn",
        )
        assert attention.is_end_of_turn_question(ev, RunState.IDLE_RECENT) is False

    def test_stop_sequence_fires(self):
        from cctelegram.handlers.busy_indicator import RunState

        # ``stop_reason`` accepts "end_turn" or "stop_sequence" — verify the
        # second positive case so the predicate doesn't drift back to
        # end-turn-only.
        ev = _make_event(
            text="Two reasonable migrations on the table; do you want me to "
            "proceed with the staged rollout?",
            stop_reason="stop_sequence",
        )
        assert attention.is_end_of_turn_question(ev, RunState.IDLE_RECENT) is True


# ── Shared emergency-DM fence ──────────────────────────────────────────────


@pytest.mark.usefixtures("_reset_attention")
def test_should_emit_emergency_dm_first_call_allowed():
    assert attention.should_emit_emergency_dm(1, 10, "@0") is True


@pytest.mark.usefixtures("_reset_attention")
def test_should_emit_emergency_dm_second_call_blocked():
    assert attention.should_emit_emergency_dm(1, 10, "@0") is True
    # Second call inside the cooldown window must be blocked, regardless of
    # whether the message_queue or interactive_ui surface tripped it.
    assert attention.should_emit_emergency_dm(1, 10, "@0") is False


@pytest.mark.usefixtures("_reset_attention")
def test_should_emit_emergency_dm_distinct_routes_independent():
    assert attention.should_emit_emergency_dm(1, 10, "@0") is True
    # Different thread/window forms a distinct waiting episode.
    assert attention.should_emit_emergency_dm(1, 11, "@0") is True
    assert attention.should_emit_emergency_dm(1, 10, "@1") is True


# ── §2.9 Inline-keyboard buttons on end-of-turn-question cards ────────────


@pytest.mark.usefixtures("_reset_attention", "mock_session_manager")
class TestAttentionButtons:
    @pytest.mark.asyncio
    async def test_end_of_turn_card_includes_three_buttons(self):
        from telegram import InlineKeyboardMarkup

        bot = AsyncMock()
        sent = _make_sent_message(message_id=42)
        with patch(
            "cctelegram.handlers.attention.topic_send",
            new_callable=AsyncMock,
        ) as mock_send:
            mock_send.return_value = (sent, TopicSendOutcome.OK)

            await attention.notify_waiting(
                bot,
                user_id=1,
                thread_id=10,
                window_id="@0",
                prompt_text='🔔 Awaiting your reply — cctelegram\n"Want me to do X?"',
                kind="end_of_turn_question",
            )

            kwargs = mock_send.await_args.kwargs
            markup = kwargs.get("reply_markup")
            assert isinstance(markup, InlineKeyboardMarkup)
            buttons = list(markup.inline_keyboard[0])
            assert len(buttons) == 3
            assert buttons[0].callback_data is not None
            assert buttons[0].callback_data.startswith("attn:yes:")
            assert buttons[1].callback_data is not None
            assert buttons[1].callback_data.startswith("attn:no:")
            assert buttons[2].callback_data is not None
            assert buttons[2].callback_data.startswith("attn:type:")

    @pytest.mark.asyncio
    async def test_other_attention_kinds_no_attn_buttons(self):
        bot = AsyncMock()
        sent = _make_sent_message(message_id=42)
        with patch(
            "cctelegram.handlers.attention.topic_send",
            new_callable=AsyncMock,
        ) as mock_send:
            mock_send.return_value = (sent, TopicSendOutcome.OK)

            await attention.notify_waiting(
                bot,
                user_id=1,
                thread_id=10,
                window_id="@0",
                prompt_text="Do you want me to proceed?",
                kind="interactive_ui",
            )

            kwargs = mock_send.await_args.kwargs
            # interactive_ui cards must not carry the §2.9 attn:* buttons.
            # (They get their own keyboards rendered by interactive_ui.py.)
            markup = kwargs.get("reply_markup")
            if markup is None:
                return
            for row in markup.inline_keyboard:
                for btn in row:
                    assert not (
                        btn.callback_data and btn.callback_data.startswith("attn:")
                    )

    @pytest.mark.asyncio
    async def test_attention_buttons_disabled_via_flag(self):
        bot = AsyncMock()
        sent = _make_sent_message(message_id=42)
        with (
            patch(
                "cctelegram.handlers.attention.topic_send",
                new_callable=AsyncMock,
            ) as mock_send,
            patch.object(attention.config, "attention_buttons", False),
        ):
            mock_send.return_value = (sent, TopicSendOutcome.OK)

            await attention.notify_waiting(
                bot,
                user_id=1,
                thread_id=10,
                window_id="@0",
                prompt_text='🔔 Awaiting your reply — cctelegram\n"Want me to do X?"',
                kind="end_of_turn_question",
            )

            kwargs = mock_send.await_args.kwargs
            markup = kwargs.get("reply_markup")
            # Either no markup at all, or a markup that has no attn:* buttons.
            if markup is None:
                return
            for row in markup.inline_keyboard:
                for btn in row:
                    assert not (
                        btn.callback_data and btn.callback_data.startswith("attn:")
                    )

    @pytest.mark.asyncio
    async def test_consume_attention_token_returns_route_then_none(self):
        bot = AsyncMock()
        sent = _make_sent_message(message_id=42)
        with patch(
            "cctelegram.handlers.attention.topic_send",
            new_callable=AsyncMock,
        ) as mock_send:
            mock_send.return_value = (sent, TopicSendOutcome.OK)

            await attention.notify_waiting(
                bot,
                user_id=1,
                thread_id=10,
                window_id="@0",
                prompt_text='🔔 Awaiting your reply — cctelegram\n"Want me to do X?"',
                kind="end_of_turn_question",
            )

            kwargs = mock_send.await_args.kwargs
            markup = kwargs.get("reply_markup")
            assert markup is not None
            cb = markup.inline_keyboard[0][0].callback_data
            assert cb is not None
            token = cb.split(":", 2)[2]

            entry = attention.consume_attention_token(token)
            assert entry is not None
            assert entry.route == (1, 10, "@0")
            # Idempotency: second call returns None.
            assert attention.consume_attention_token(token) is None


@pytest.mark.usefixtures("_reset_attention")
def test_prune_expired_attention_tokens_drops_old_entries():
    # Inject one fresh entry and one backdated entry.
    fresh = "fresh-token"
    stale = "stale-token"
    now = time.monotonic()
    attention._attention_callback_routes[fresh] = attention._AttentionCallbackEntry(
        route=(1, 10, "@0"),
        created_at=now,
        rendered_text="body",
        parse_mode="MarkdownV2",
    )
    # Older than the default TTL (86400) — backdate by 2 days.
    attention._attention_callback_routes[stale] = attention._AttentionCallbackEntry(
        route=(1, 10, "@0"),
        created_at=now - (2 * 86400),
        rendered_text="body",
        parse_mode="MarkdownV2",
    )

    dropped = attention.prune_expired_attention_tokens()

    assert dropped == 1
    assert fresh in attention._attention_callback_routes
    assert stale not in attention._attention_callback_routes
