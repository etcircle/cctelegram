"""Tests for interactive_ui — handle_interactive_ui and keyboard layout."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cctelegram.handlers.interactive_ui import (
    _build_interactive_keyboard,
    handle_interactive_ui,
)
from cctelegram.handlers.callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_UP,
)


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 999
    bot.send_message.return_value = sent_msg
    return bot


@pytest.fixture
def _clear_interactive_state():
    """Ensure interactive state is clean before and after each test."""
    from cctelegram.handlers import attention
    from cctelegram.handlers.interactive_ui import _interactive_mode, _interactive_msgs

    _interactive_mode.clear()
    _interactive_msgs.clear()
    attention.reset_for_tests()
    yield
    _interactive_mode.clear()
    _interactive_msgs.clear()
    attention.reset_for_tests()


@pytest.mark.usefixtures("_clear_interactive_state")
class TestHandleInteractiveUI:
    @pytest.mark.asyncio
    async def test_handle_settings_ui_sends_keyboard(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """handle_interactive_ui captures Settings pane, sends message with keyboard.

        Topic-first attention card also fires (in the same chat/thread, not as
        a DM). We assert: (a) the keyboard message lands in the topic with the
        nav keyboard, and (b) no send goes to the user_id-as-chat (i.e. no DM).
        """
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("cctelegram.handlers.interactive_ui.tmux_manager") as mock_tmux,
            patch("cctelegram.handlers.interactive_ui.session_manager") as mock_sm_iu,
            patch("cctelegram.handlers.attention.session_manager") as mock_sm_att,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_sm_iu.resolve_chat_id.return_value = 100
            mock_sm_att.resolve_chat_id.return_value = 100
            mock_sm_att.get_display_name.return_value = "etcircle-dev"

            result = await handle_interactive_ui(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        assert result is True

        keyboard_calls = [
            c
            for c in mock_bot.send_message.call_args_list
            if c.kwargs.get("reply_markup") is not None
        ]
        assert len(keyboard_calls) == 1
        kw = keyboard_calls[0].kwargs
        assert kw["chat_id"] == 100
        assert kw["message_thread_id"] == 42

        # No DM: every send_message went to chat_id=100 (the topic).
        for call in mock_bot.send_message.call_args_list:
            assert call.kwargs["chat_id"] == 100, (
                f"unexpected DM-shaped send_message: {call.kwargs}"
            )

    @pytest.mark.asyncio
    async def test_interactive_ui_card_peeks_anchor_so_assistant_text_can_anchor(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """§2.5.2: the interactive-card send must not pop the anchor.

        Both the interactive card AND the assistant text Claude emits after
        the user resolves the card are responses to the same user prompt,
        so they should anchor to the same Telegram message_id. The
        canonical anchor consumer is ``_process_content_task``; the
        interactive-UI surface only peeks.
        """
        from telegram import ReplyParameters

        from cctelegram.handlers import message_queue
        from cctelegram.handlers.message_sender import TopicSendOutcome

        window_id = "@5"
        user_id = 1
        thread_id = 42
        anchor_message_id = 7777

        # Stash the anchor as if a prior text/photo offer recorded it.
        message_queue.set_route_last_user_message(
            user_id, thread_id, window_id, anchor_message_id
        )

        sent_msg = MagicMock()
        sent_msg.message_id = 9999
        send_calls: list[dict] = []

        async def fake_topic_send(
            bot, *, op, user_id, chat_id, thread_id, window_id, text, **kw
        ):
            send_calls.append({"op": op, "kw": kw})
            return sent_msg, TopicSendOutcome.OK

        async def fake_attention(*args, **kwargs):
            return TopicSendOutcome.OK

        mock_window = MagicMock()
        mock_window.window_id = window_id

        try:
            with (
                patch("cctelegram.handlers.interactive_ui.tmux_manager") as mock_tmux,
                patch(
                    "cctelegram.handlers.interactive_ui.session_manager"
                ) as mock_sm_iu,
                patch(
                    "cctelegram.handlers.interactive_ui.topic_send",
                    side_effect=fake_topic_send,
                ),
                patch(
                    "cctelegram.handlers.interactive_ui.attention.notify_waiting",
                    side_effect=fake_attention,
                ),
            ):
                mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
                mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_settings)
                mock_sm_iu.resolve_chat_id.return_value = 100
                mock_sm_iu.get_display_name.return_value = "topic-name"

                result = await handle_interactive_ui(
                    mock_bot,
                    user_id=user_id,
                    window_id=window_id,
                    thread_id=thread_id,
                )
            assert result is True
            # The card send carried the anchor.
            assert len(send_calls) == 1
            rp = send_calls[0]["kw"].get("reply_parameters")
            assert isinstance(rp, ReplyParameters)
            assert rp.message_id == anchor_message_id
            # CRITICAL: anchor still present after the card send (peek, not
            # consume). A subsequent assistant-text first-part send is the
            # canonical consumer.
            anchor_route = (user_id, thread_id, window_id)
            assert (
                message_queue._route_last_user_message.get(anchor_route)
                == anchor_message_id
            )
        finally:
            message_queue._route_last_user_message.pop(
                (user_id, thread_id, window_id), None
            )

    @pytest.mark.asyncio
    async def test_handle_no_ui_returns_false(self, mock_bot: AsyncMock):
        """Returns False when no interactive UI detected in pane."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("cctelegram.handlers.interactive_ui.tmux_manager") as mock_tmux,
            patch("cctelegram.handlers.interactive_ui.session_manager"),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value="$ echo hello\nhello\n$\n")

            result = await handle_interactive_ui(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        assert result is False
        mock_bot.send_message.assert_not_called()


class TestKeyboardLayoutForSettings:
    def test_settings_keyboard_includes_all_nav_keys(self):
        """Settings keyboard includes Tab, arrows (not vertical_only), Space, Esc, Enter."""
        keyboard = _build_interactive_keyboard("@5", ui_name="Settings")
        # Flatten all callback data values
        all_cb_data = [
            btn.callback_data for row in keyboard.inline_keyboard for btn in row
        ]
        assert any(CB_ASK_TAB in d for d in all_cb_data if d)
        assert any(CB_ASK_SPACE in d for d in all_cb_data if d)
        assert any(CB_ASK_UP in d for d in all_cb_data if d)
        assert any(CB_ASK_DOWN in d for d in all_cb_data if d)
        assert any(CB_ASK_LEFT in d for d in all_cb_data if d)
        assert any(CB_ASK_RIGHT in d for d in all_cb_data if d)
        assert any(CB_ASK_ESC in d for d in all_cb_data if d)
        assert any(CB_ASK_ENTER in d for d in all_cb_data if d)


# ── _render_ask_user_question ─────────────────────────────────────────────


from cctelegram.handlers.interactive_ui import (  # noqa: E402
    _render_ask_user_question,
)
from cctelegram.terminal_parser import (  # noqa: E402
    AskOption,
    AskTab,
    AskUserQuestionForm,
)


class TestShouldPostAuqContext:
    """Threshold gate for the AUQ context-message dump."""

    def test_long_description_triggers(self):
        from cctelegram.handlers.interactive_ui import _should_post_auq_context

        tool_input = {
            "questions": [
                {
                    "question": "Q?",
                    "options": [
                        {"label": "A", "description": "x" * 260},
                        {"label": "B", "description": "short"},
                    ],
                }
            ]
        }
        assert _should_post_auq_context(tool_input) is True

    def test_short_descriptions_still_fire(self):
        """v3+: gate is question-text-based, not description-length-based.

        Under v2 (250-char threshold), short descriptions returned False.
        Under v3+ (gate aligns with formatter, which keys on question
        text), any question with non-empty text + at least one labeled
        option returns True regardless of description length. This is
        the user invariant from 2026-05-22: always post info+picker.
        """
        from cctelegram.handlers.interactive_ui import _should_post_auq_context

        tool_input = {
            "questions": [
                {
                    "question": "Q?",
                    "options": [
                        {"label": "A", "description": "short A"},
                        {"label": "B", "description": "short B"},
                    ],
                }
            ]
        }
        assert _should_post_auq_context(tool_input) is True

    def test_missing_descriptions_still_fire(self):
        """v3+: gate is question-text-based; descriptions are optional.

        Under v2 (250-char threshold), descriptionless options returned
        False. Under v3+, question text is the trigger.
        """
        from cctelegram.handlers.interactive_ui import _should_post_auq_context

        tool_input = {
            "questions": [
                {
                    "question": "Q?",
                    "options": [{"label": "A"}, {"label": "B"}],
                }
            ]
        }
        assert _should_post_auq_context(tool_input) is True

    def test_label_only_no_question_text_skipped(self):
        """v3+ (Codex P2 #4): label-only forms with no question text
        are skipped because the formatter (``_format_auq_context_message``)
        skips the whole question when ``question``/``header`` is empty.
        Firing the gate would consume the claim and post a header-only
        message.
        """
        from cctelegram.handlers.interactive_ui import _should_post_auq_context

        tool_input = {
            "questions": [
                {
                    "options": [{"label": "A"}, {"label": "B"}],
                }
            ]
        }
        assert _should_post_auq_context(tool_input) is False

    def test_question_only_no_options_fires(self):
        """Hermes additional finding: question-only forms still trigger
        the info message — content exists, just no options. Formatter
        renders the question text in this case.
        """
        from cctelegram.handlers.interactive_ui import _should_post_auq_context

        tool_input = {
            "questions": [
                {"question": "Are you sure?"},
            ]
        }
        assert _should_post_auq_context(tool_input) is True

    def test_header_only_fires(self):
        """Gate accepts ``header`` as a question-text fallback (mirrors
        what _format_auq_context_message uses)."""
        from cctelegram.handlers.interactive_ui import _should_post_auq_context

        tool_input = {
            "questions": [
                {"header": "Confirm migration", "options": [{"label": "Yes"}]},
            ]
        }
        assert _should_post_auq_context(tool_input) is True

    def test_blank_question_text_returns_false(self):
        """Whitespace-only question text is treated as empty."""
        from cctelegram.handlers.interactive_ui import _should_post_auq_context

        tool_input = {
            "questions": [
                {"question": "   ", "options": [{"label": "A"}]},
            ]
        }
        assert _should_post_auq_context(tool_input) is False

    def test_multi_question_only_one_long_triggers(self):
        from cctelegram.handlers.interactive_ui import _should_post_auq_context

        tool_input = {
            "questions": [
                {
                    "question": "Q1?",
                    "options": [{"label": "A", "description": "short"}],
                },
                {
                    "question": "Q2?",
                    "options": [{"label": "B", "description": "y" * 300}],
                },
            ]
        }
        assert _should_post_auq_context(tool_input) is True

    def test_malformed_inputs_return_false(self):
        from cctelegram.handlers.interactive_ui import _should_post_auq_context

        assert _should_post_auq_context(None) is False
        assert _should_post_auq_context({}) is False
        assert _should_post_auq_context({"questions": "nope"}) is False
        assert _should_post_auq_context({"questions": [None, 1]}) is False
        assert _should_post_auq_context({"questions": [{"options": "nope"}]}) is False


class TestFormatAuqContextMessage:
    """Plain-text formatter for the AUQ context-message dump."""

    def test_single_question_format(self):
        from cctelegram.handlers.interactive_ui import _format_auq_context_message

        out = _format_auq_context_message(
            {
                "questions": [
                    {
                        "question": "D5 — Pick the migration strategy.",
                        "header": "Migration",
                        "options": [
                            {
                                "label": "Drop the flag",
                                "description": (
                                    "Long description explaining the trade-off "
                                    "of dropping the flag entirely."
                                ),
                            },
                            {
                                "label": "Keep the flag",
                                "description": "Short description.",
                            },
                        ],
                    }
                ]
            }
        )
        # Header line present
        assert out.startswith("📋 AskUserQuestion — full details")
        # Question text present (no Q1. prefix in single-question mode)
        assert "D5 — Pick the migration strategy." in out
        # Both options listed with full descriptions
        assert "1. Drop the flag" in out
        assert "Long description explaining the trade-off" in out
        assert "2. Keep the flag" in out
        assert "Short description." in out
        # No multi-question hint
        assert "Picker below answers each question one at a time" not in out

    def test_multi_question_format(self):
        from cctelegram.handlers.interactive_ui import _format_auq_context_message

        out = _format_auq_context_message(
            {
                "questions": [
                    {
                        "question": "Q1: which approach?",
                        "options": [
                            {"label": "A1", "description": "alpha"},
                            {"label": "B1", "description": "beta"},
                        ],
                    },
                    {
                        "question": "Q2: which timing?",
                        "options": [
                            {"label": "Now", "description": "Right away."},
                            {"label": "Later", "description": "Next sprint."},
                        ],
                    },
                ]
            }
        )
        assert "📋 AskUserQuestion — full details" in out
        assert "Picker below answers each question one at a time" in out
        assert "Q1. Q1: which approach?" in out
        assert "Q2. Q2: which timing?" in out
        # Per-question option numbering resets
        assert "1. A1" in out
        assert "2. B1" in out
        assert "1. Now" in out
        assert "2. Later" in out
        # Descriptions intact
        assert "Right away." in out
        assert "Next sprint." in out

    def test_description_preserves_line_breaks(self):
        from cctelegram.handlers.interactive_ui import _format_auq_context_message

        out = _format_auq_context_message(
            {
                "questions": [
                    {
                        "question": "Q?",
                        "options": [
                            {
                                "label": "Multi-line",
                                "description": "Line one.\nLine two.",
                            }
                        ],
                    }
                ]
            }
        )
        assert "   Line one." in out
        assert "   Line two." in out

    def test_empty_options_skipped(self):
        from cctelegram.handlers.interactive_ui import _format_auq_context_message

        out = _format_auq_context_message(
            {
                "questions": [
                    {
                        "question": "Q?",
                        "options": [
                            {"label": "", "description": "skipped — no label"},
                            {"label": "Real", "description": "kept"},
                        ],
                    }
                ]
            }
        )
        assert "skipped — no label" not in out
        assert "1. Real" in out  # numbering still 1-based


class TestAuqContextFromForm:
    """v5 fix (2026-05-24): pane-derived ``AskUserQuestionForm`` is an
    accepted source for the context-message gate, so live AUQs (no
    JSONL) still post a "📋 — full details" prelude before the picker.

    The four tests here cover the matrix of cases listed in the
    handoff: gate predicate, form formatter, dedup across mixed
    sources, and the integration through ``handle_interactive_ui``
    when only the status-polling path is feeding the renderer."""

    def setup_method(self):
        from cctelegram.handlers import interactive_ui as iui

        iui._last_completed_ask_tool_input.clear()
        iui._last_auq_tool_use_id.clear()
        iui._auq_context_posted.clear()

    def test_should_post_auq_context_from_form_when_jsonl_absent(self):
        """Predicate accepts an AskUserQuestionForm and returns True
        when the form has any renderable content. The form path is
        intentionally looser than the JSONL path — pane parses
        commonly lack option descriptions, and a labeled option list
        still carries real context value."""
        from cctelegram.handlers.interactive_ui import _should_post_auq_context
        from cctelegram.terminal_parser import AskOption, AskUserQuestionForm

        # Has only options (no title) — labels alone are enough on the
        # form path because the picker may have truncated the option
        # labels and the user benefits from seeing them in full prose
        # context.
        form_options_only = AskUserQuestionForm(
            options=(
                AskOption(
                    label="Drop the flag", recommended=False, cursor=False, number=1
                ),
            ),
        )
        assert _should_post_auq_context(form_options_only) is True

        # Has pane_walkback_title only — the live AUQ before Claude
        # Code flushes JSONL: this is the most common live-AUQ shape.
        form_title_only = AskUserQuestionForm(
            pane_walkback_title="Pick the migration strategy",
        )
        assert _should_post_auq_context(form_title_only) is True

        # current_question_title set (resolver may pin this from pane
        # parse even without JSONL).
        form_current_title_only = AskUserQuestionForm(
            current_question_title="Confirm migration?",
        )
        assert _should_post_auq_context(form_current_title_only) is True

        # Empty form → nothing to render → False.
        form_empty = AskUserQuestionForm()
        assert _should_post_auq_context(form_empty) is False

    def test_format_auq_context_message_from_form_uses_pane_walkback(self):
        """Formatter renders the pane fallback shape: walkback title as
        the question line, visible options with labels (descriptions
        usually empty since pane parses don't carry them)."""
        from cctelegram.handlers.interactive_ui import _format_auq_context_message
        from cctelegram.terminal_parser import AskOption, AskUserQuestionForm

        form = AskUserQuestionForm(
            pane_walkback_title="Pick the migration strategy that minimises blast radius.",
            options=(
                AskOption(
                    label="Drop the flag",
                    recommended=False,
                    cursor=True,
                    number=1,
                ),
                AskOption(
                    label="Keep the flag",
                    recommended=True,
                    cursor=False,
                    number=2,
                ),
                AskOption(
                    label="Roll forward with a probe",
                    recommended=False,
                    cursor=False,
                    number=3,
                ),
            ),
        )
        out = _format_auq_context_message(form)
        assert out.startswith("📋 AskUserQuestion — full details")
        assert "Pick the migration strategy" in out
        # Options renumbered 1..N in display order, regardless of
        # pane numbering.
        assert "1. Drop the flag" in out
        assert "2. Keep the flag" in out
        assert "3. Roll forward with a probe" in out
        # Single-tab form → no multi-Q hint.
        assert "Picker below answers each question" not in out

    def test_format_auq_context_message_from_form_prefers_current_title(self):
        """When both ``current_question_title`` and ``pane_walkback_title``
        are set, the formatter prefers the authoritative
        ``current_question_title`` (mirrors the picker renderer's
        precedence)."""
        from cctelegram.handlers.interactive_ui import _format_auq_context_message
        from cctelegram.terminal_parser import AskOption, AskUserQuestionForm

        form = AskUserQuestionForm(
            current_question_title="Authoritative title",
            pane_walkback_title="Walked-back title",
            options=(AskOption(label="A", recommended=False, cursor=False, number=1),),
        )
        out = _format_auq_context_message(form)
        assert "Authoritative title" in out
        assert "Walked-back title" not in out

    def test_claim_auq_context_post_dedupes_across_form_and_jsonl_keys(self):
        """The live-AUQ scenario the v5 fix is designed for: form-key
        claims first (no JSONL available yet), then the JSONL
        ``tool_use_id`` arrives (after the user answers / status_polling
        re-fires with both data). The second claim with a different key
        must STILL return False — the marker is set and we don't want a
        duplicate context message."""
        from cctelegram.handlers.interactive_ui import claim_auq_context_post

        # Live AUQ → form fingerprint claim succeeds.
        assert claim_auq_context_post("@42", "form:deadbeefcafe1234") is True
        # JSONL arrives → different key but marker is set → False.
        assert claim_auq_context_post("@42", "toolu_abc") is False

    @pytest.mark.asyncio
    async def test_live_auq_posts_context_via_status_polling_path(self, monkeypatch):
        """Integration: status_polling drives ``handle_interactive_ui``
        with ``tool_input=None`` (the live-AUQ shape). The handler
        should still post the context message from the form fallback
        before sending the picker card.

        Builds a multi-tab AskUserQuestion pane (ETVoiceScribe shape
        from the 2026-05-24 screenshot) and confirms that:
          1. ``topic_send`` is called at least twice — once for the
             context message (assistant role) and once for the picker
             (tool role).
          2. The first send carries the "📋 AskUserQuestion — full
             details" header.
          3. ``_auq_context_posted[@40]`` is marked with a form-key
             dedup tag after the send."""
        from cctelegram.handlers import interactive_ui as iui

        window_id = "@40"
        user_id = 1
        thread_id = 42
        chat_id = 100

        # Pane text crafted to drive the parser into a multi-tab live
        # AUQ shape (3 tabs, one current). The exact parser-detected
        # output is verified indirectly via the formatter dispatch —
        # we just need ``content.name == "AskUserQuestion"`` + a
        # non-trivial form to come out of ``resolve_ask_form``. The
        # ``------`` separators are how
        # ``extract_interactive_content`` finds the block.
        pane_text = (
            "some scrollback ...\n"
            "│ Pick the safety patch so we can ship the Mac fix tonight.    │\n"
            "------\n"
            "AskUserQuestion\n"
            "    ☐ Mac patch    ☐ Dev keypair    ☐ Other prep\n"
            "\n"
            "  ❯ 1. Drop the flag\n"
            "    2. Keep the flag\n"
            "    3. Other\n"
            "  Enter to select\n"
            "------\n"
        )

        # Build mocks — MagicMock spec=SessionManager (4eabc64) so the
        # handler's ``resolve_chat_id`` / window-resolution calls work.
        from cctelegram.session import SessionManager
        from cctelegram.tmux_manager import TmuxWindow

        mock_window = MagicMock(spec=TmuxWindow)
        mock_window.window_id = window_id

        mock_tmux = MagicMock()
        mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
        mock_tmux.capture_pane = AsyncMock(return_value=pane_text)

        mock_sm = MagicMock(spec=SessionManager)
        mock_sm.resolve_chat_id.return_value = chat_id
        mock_sm.window_states = {}

        # Stub the picker liveness check + parser to ensure the pane is
        # treated as "present" with an AUQ block. We rely on the real
        # parser elsewhere — here we just need the gate to reach the
        # context-message step.
        monkeypatch.setattr(
            iui,
            "visible_pane_liveness",
            lambda _pane: "present",
        )

        # Sent messages captured here so we can assert on order +
        # content.
        sent_messages: list[dict] = []

        async def fake_topic_send(_bot, **kwargs):
            sent_messages.append(kwargs)
            sm = MagicMock()
            sm.message_id = 1000 + len(sent_messages)
            return sm, iui.TopicSendOutcome.OK

        monkeypatch.setattr(iui, "topic_send", fake_topic_send)
        monkeypatch.setattr(
            iui,
            "session_id_for_window",
            lambda _wid: "sess-abc",
        )
        # Block the "anchor reply to last user message" path so the
        # test doesn't have to seed message_queue state.
        monkeypatch.setattr(
            "cctelegram.handlers.message_queue.peek_route_last_user_message",
            lambda *_a, **_kw: None,
        )

        mock_bot = AsyncMock()
        result = await iui.handle_interactive_ui(
            mock_bot,
            user_id=user_id,
            window_id=window_id,
            thread_id=thread_id,
            tool_input=None,
            from_poller=True,
            tmux_mgr=mock_tmux,
            session_mgr=mock_sm,
        )

        # The handler returns True on a successful send. If the parser
        # didn't pin an AUQ at all (e.g. test fixture mismatch), we'd
        # get False — surface that explicitly so the failure mode is
        # legible.
        assert result is True, (
            "handle_interactive_ui returned False — pane fixture may not "
            "have parsed as an AskUserQuestion. sent_messages="
            f"{sent_messages!r}"
        )

        # Context message went out first, picker card second.
        assert len(sent_messages) >= 2, sent_messages
        first = sent_messages[0]
        assert "📋 AskUserQuestion — full details" in first.get("text", "")
        assert first.get("role") == "assistant"
        # Picker card lands with role=tool.
        picker_sends = [m for m in sent_messages if m.get("content_type") == "tool_use"]
        assert picker_sends, sent_messages

        # Marker tagged with a form-fingerprint key.
        marker = iui._auq_context_posted.get(window_id)
        assert marker is not None
        assert marker.startswith("form:"), marker


class TestClaimAuqContextPost:
    """Atomic check-and-set for the AUQ context-post gate."""

    def setup_method(self):
        from cctelegram.handlers import interactive_ui as iui

        iui._last_completed_ask_tool_input.clear()
        iui._last_auq_tool_use_id.clear()
        iui._auq_context_posted.clear()

    def test_empty_dedup_key_returns_false(self):
        """v5 (2026-05-24): claim() rejects an empty dedup_key.

        Under the new presence-based contract, ``dedup_key`` is the
        caller's responsibility. Passing "" means the caller couldn't
        compute either a JSONL ``tool_use_id`` or a form fingerprint —
        the gate site should not be calling claim() in that case.
        Treat as an obvious bug and return False so a stray invocation
        can't accidentally set the marker."""
        from cctelegram.handlers.interactive_ui import claim_auq_context_post

        assert claim_auq_context_post("@99", "") is False

    def test_first_claim_succeeds_second_fails(self):
        from cctelegram.handlers.interactive_ui import claim_auq_context_post

        assert claim_auq_context_post("@5", "toolu_1") is True
        assert claim_auq_context_post("@5", "toolu_1") is False

    def test_new_tool_use_id_resets_claim(self):
        """When remember_ask_tool_input sees a NEW tool_use_id replacing
        an old one for the same window, the context-post marker is
        auto-cleared so the new AUQ can claim a fresh post.

        Under the v5 presence-based dedup, the marker doesn't reset on
        its own (different value still counts as "set"); the reset is
        wired into ``remember_ask_tool_input`` for the JSONL path. The
        test exercises that wiring end-to-end."""
        from cctelegram.handlers.interactive_ui import (
            claim_auq_context_post,
            remember_ask_tool_input,
        )

        remember_ask_tool_input(
            "@5", {"questions": [{"options": [{"label": "A"}]}]}, "toolu_1"
        )
        assert claim_auq_context_post("@5", "toolu_1") is True
        # Same window, new AUQ tool_use_id → remember() drops the
        # marker; the new id can claim a fresh post.
        remember_ask_tool_input(
            "@5", {"questions": [{"options": [{"label": "B"}]}]}, "toolu_2"
        )
        assert claim_auq_context_post("@5", "toolu_2") is True
        assert claim_auq_context_post("@5", "toolu_2") is False

    def test_forget_drops_post_state(self):
        from cctelegram.handlers.interactive_ui import (
            claim_auq_context_post,
            forget_ask_tool_input,
            remember_ask_tool_input,
        )

        remember_ask_tool_input(
            "@5", {"questions": [{"options": [{"label": "A"}]}]}, "toolu_1"
        )
        assert claim_auq_context_post("@5", "toolu_1") is True
        forget_ask_tool_input("@5")
        # After tool_result clears the cache, a fresh re-cache (e.g. via
        # hydrate after restart) should be claimable again.
        remember_ask_tool_input(
            "@5", {"questions": [{"options": [{"label": "A"}]}]}, "toolu_1"
        )
        assert claim_auq_context_post("@5", "toolu_1") is True

    def test_remember_without_id_clears_stale_marker(self):
        """Hermes P3 hardening (2026-05-22): if an earlier call left a
        tool_use_id + posted state in place and a later caller invokes
        ``remember_ask_tool_input`` WITHOUT a ``tool_use_id`` (e.g. a
        test helper or unmigrated legacy path), the stale ID + marker
        must be cleared. Under the v5 form-source design the next
        claim() can then proceed with a form-fingerprint dedup_key."""
        from cctelegram.handlers.interactive_ui import (
            claim_auq_context_post,
            remember_ask_tool_input,
        )

        remember_ask_tool_input(
            "@5", {"questions": [{"options": [{"label": "A"}]}]}, "toolu_old"
        )
        assert claim_auq_context_post("@5", "toolu_old") is True
        # Caller without an ID overwrites the cache + clears the
        # marker, so a form-fingerprint claim succeeds next.
        remember_ask_tool_input("@5", {"questions": [{"options": [{"label": "B"}]}]})
        assert claim_auq_context_post("@5", "form:abc123") is True


class TestSendAuqContextMessage:
    """Behavior of the multi-part AUQ context-message sender."""

    def setup_method(self):
        from cctelegram.handlers import interactive_ui as iui

        iui._last_completed_ask_tool_input.clear()
        iui._last_auq_tool_use_id.clear()
        iui._auq_context_posted.clear()

    @pytest.mark.asyncio
    async def test_retry_after_propagates(self, monkeypatch):
        """Codex P2 (2026-05-22 diff review): RetryAfter must NOT be
        swallowed — the outer flood-control machinery owns back-off."""
        from telegram.error import RetryAfter

        from cctelegram.handlers import interactive_ui as iui

        async def _raise_retry_after(*args, **kwargs):
            raise RetryAfter(retry_after=5)

        monkeypatch.setattr(iui, "topic_send", _raise_retry_after)

        with pytest.raises(RetryAfter):
            await iui._send_auq_context_message(
                None,  # type: ignore[arg-type]
                user_id=1,
                thread_id=None,
                chat_id=1,
                window_id="@5",
                source={
                    "questions": [
                        {
                            "question": "Q?",
                            "options": [
                                {
                                    "label": "A",
                                    "description": "x" * 50,
                                }
                            ],
                        }
                    ]
                },
                dedup_key="t",
            )

    @pytest.mark.asyncio
    async def test_stops_on_failed_chunk(self, monkeypatch):
        """Hermes P3 (2026-05-22 diff review): structurally-failed
        chunk (topic_send returns sent=None) stops the sequence so
        the user doesn't see [1/N] then [3/N] with a hole."""
        from cctelegram.handlers import interactive_ui as iui
        from cctelegram.handlers.message_sender import TopicSendOutcome

        # Force the formatter to produce > one chunk by stuffing huge
        # descriptions; build_response_parts splits past 3000 chars.
        long_desc = ("paragraph " * 100).strip()  # ~1000 chars
        tool_input = {
            "questions": [
                {
                    "question": "Q?",
                    "options": [
                        {"label": f"Option {i}", "description": long_desc}
                        for i in range(1, 11)  # 10 options × ~1000 chars
                    ],
                }
            ]
        }

        send_calls: list[int] = []

        async def _fail_second(*args, **kwargs):
            send_calls.append(kwargs.get("part_index", 0))
            if len(send_calls) == 2:
                # Second chunk: structural failure.
                return None, TopicSendOutcome.TOPIC_NOT_FOUND
            # Other chunks: succeed.
            from unittest.mock import Mock

            return Mock(message_id=100 + len(send_calls)), TopicSendOutcome.OK

        monkeypatch.setattr(iui, "topic_send", _fail_second)

        await iui._send_auq_context_message(
            None,  # type: ignore[arg-type]
            user_id=1,
            thread_id=None,
            chat_id=1,
            window_id="@5",
            source=tool_input,
            dedup_key="t",
        )

        # Two send attempts: chunk 1 ok, chunk 2 failed, stop.
        assert len(send_calls) == 2


class TestRenderAskUserQuestion:
    def test_single_question_picker(self):
        form = AskUserQuestionForm(
            tabs=(),
            current_question_title="How is Claude doing this session? (optional)",
            options=(
                AskOption(label="Bad", recommended=False, cursor=True, number=1),
                AskOption(label="Fine", recommended=False, cursor=False, number=2),
                AskOption(label="Good", recommended=False, cursor=False, number=3),
            ),
        )
        out = _render_ask_user_question(form)
        # Title on top, then options, then footer hint
        assert "How is Claude doing this session?" in out
        assert "❯ 1. Bad" in out
        assert "  2. Fine" in out
        assert "  3. Good" in out
        assert "Enter to select" in out
        # No tab strip rendered for a single-question form
        assert "☒" not in out and "☐" not in out

    def test_walkback_title_fallback_when_jsonl_missing(self):
        """When ``current_question_title`` is None (pane-only parse
        before JSONL has flushed), the renderer falls back to
        ``pane_walkback_title`` so the user still sees the question
        header. Regression for the 2026-05-21 D5 incident where the
        Telegram card landed with options only, no context."""
        form = AskUserQuestionForm(
            tabs=(),
            current_question_title=None,
            options=(
                AskOption(
                    label="Drop the flag — ripple is just the new behavior",
                    recommended=False,
                    cursor=True,
                    number=1,
                ),
                AskOption(
                    label="One-time migration — v3 to v4 schema bump",
                    recommended=False,
                    cursor=False,
                    number=2,
                ),
            ),
            pane_walkback_title=(
                "D5 — If durationMode-as-permanent-flag is cruft, "
                "how do we handle legacy voice_patch ops?"
            ),
        )
        out = _render_ask_user_question(form)
        # Walk-back title appears at the top, ahead of the options.
        assert (
            "D5 — If durationMode-as-permanent-flag is cruft, "
            "how do we handle legacy voice_patch ops?"
        ) in out
        assert "❯ 1. Drop the flag" in out
        # The fallback only fires when current_question_title is None;
        # it must NOT shadow an authoritative title from JSONL.
        title_idx = out.index("D5 — If")
        option_idx = out.index("❯ 1. Drop the flag")
        assert title_idx < option_idx

    def test_jsonl_title_wins_over_walkback(self):
        """When both ``current_question_title`` and
        ``pane_walkback_title`` are set, the JSONL-authoritative title
        wins — the walk-back is strictly a fallback."""
        form = AskUserQuestionForm(
            tabs=(),
            current_question_title="JSONL authoritative title",
            options=(AskOption(label="A", recommended=False, cursor=True, number=1),),
            pane_walkback_title="Walkback guess that should be ignored",
        )
        out = _render_ask_user_question(form)
        assert "JSONL authoritative title" in out
        assert "Walkback guess" not in out

    def test_multitab_picker_with_recommended(self):
        form = AskUserQuestionForm(
            tabs=(
                AskTab(
                    label="Approach", answered=False, is_submit=False, is_current=False
                ),
                AskTab(
                    label="Positioning",
                    answered=False,
                    is_submit=False,
                    is_current=False,
                ),
                AskTab(label="", answered=False, is_submit=True, is_current=False),
            ),
            current_question_title="Which implementation approach should we lock in?",
            options=(
                AskOption(
                    label="C — Parallel tracks",
                    recommended=True,
                    cursor=True,
                    number=1,
                ),
                AskOption(
                    label="B — Copilot-first",
                    recommended=False,
                    cursor=False,
                    number=2,
                ),
            ),
            is_free_text=True,
        )
        out = _render_ask_user_question(form)
        # Tab strip uses ☐ for un-answered and ✔ for the submit cell
        assert "☐ Approach" in out
        assert "☐ Positioning" in out
        assert "✔" in out
        # Question title preserved
        assert "Which implementation approach" in out
        # Recommended option carries the "(Recommended)" suffix
        assert "❯ 1. C — Parallel tracks (Recommended)" in out
        assert "  2. B — Copilot-first" in out
        # Free-text hint surfaces when present
        assert "Type something" in out

    def test_review_screen(self):
        form = AskUserQuestionForm(
            tabs=(
                AskTab(
                    label="Approach", answered=True, is_submit=False, is_current=False
                ),
                AskTab(
                    label="Positioning",
                    answered=True,
                    is_submit=False,
                    is_current=False,
                ),
                AskTab(label="", answered=False, is_submit=True, is_current=False),
            ),
            options=(
                AskOption(
                    label="Submit answers", recommended=False, cursor=True, number=1
                ),
                AskOption(label="Cancel", recommended=False, cursor=False, number=2),
            ),
            is_review_screen=True,
        )
        out = _render_ask_user_question(form)
        # Header signals review-screen rather than picker
        assert "Review your answers" in out
        # Both content tabs marked answered; submit cell suppressed in the
        # "review" body (the Submit/Cancel choice below covers it).
        assert "☒ Approach" in out
        assert "☒ Positioning" in out
        assert "Submit" not in out.split("\n")[0]  # not on the first line
        # Submit/Cancel row visible with cursor on Submit
        assert "Ready to submit your answers?" in out
        assert "❯ 1. Submit answers" in out
        assert "  2. Cancel" in out

    def test_empty_render_when_no_structure(self):
        # No tabs, no options, no review flag → renderer returns "" so the
        # caller can fall back to the raw pane excerpt.
        form = AskUserQuestionForm()
        assert _render_ask_user_question(form) == ""

    def test_descriptions_inlined_under_each_option(self):
        """PR 2: per-option description text from the JSONL payload shows
        up indented under the option label. Empty descriptions skip the
        indent line (pane-only forms don't carry descriptions).
        """
        form = AskUserQuestionForm(
            tabs=(),
            current_question_title="Pick clip affordance.",
            options=(
                AskOption(
                    label="A — Top toolbar",
                    recommended=True,
                    cursor=True,
                    number=1,
                    description="Always-visible button next to Render. Clip labels readable at a glance.",
                ),
                AskOption(
                    label="B — Hover labels",
                    recommended=False,
                    cursor=False,
                    number=2,
                    description="Cleaner timeline; less visual noise but clip boundaries hidden.",
                ),
                AskOption(
                    label="C — Skip the feature",
                    recommended=False,
                    cursor=False,
                    number=3,
                    description="",  # no description, no indent line
                ),
            ),
        )
        out = _render_ask_user_question(form)
        # Option labels still visible.
        assert "❯ 1. A — Top toolbar (Recommended)" in out
        # Descriptions appear indented under their option.
        assert "    Always-visible button next to Render." in out
        assert "    Cleaner timeline; less visual noise" in out
        # An option with empty description does NOT get an empty indent line.
        lines = out.split("\n")
        for i, line in enumerate(lines):
            if "3. C — Skip" in line:
                # Next non-empty line should be the next option or footer,
                # not a stray "    " line.
                assert i + 1 < len(lines)
                # Either the blank-line-before-footer or "Enter to select".
                nxt = lines[i + 1]
                assert nxt == "" or "Enter to select" in nxt
                break

    def test_description_truncated_at_250_chars(self):
        """A description longer than 250 chars is hard-truncated with an
        ellipsis. Multi-line descriptions get collapsed first so the cap
        counts against visible characters.
        """
        long_desc = (
            "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 10
        )  # >>250
        form = AskUserQuestionForm(
            tabs=(),
            current_question_title="Q?",
            options=(
                AskOption(
                    label="A",
                    recommended=False,
                    cursor=True,
                    number=1,
                    description=long_desc,
                ),
            ),
        )
        out = _render_ask_user_question(form)
        # The rendered indent line must be ≤ 4 (indent) + 250 chars long.
        desc_lines = [line for line in out.split("\n") if line.startswith("    L")]
        assert desc_lines, "expected an indented description line"
        # 4 leading spaces + 250 chars max = 254 cap on the visible line.
        assert all(len(line) <= 4 + 250 for line in desc_lines)
        # Last char before any newline is the ellipsis.
        assert desc_lines[0].endswith("…")

    def test_multiline_description_collapsed_to_single_line(self):
        form = AskUserQuestionForm(
            tabs=(),
            current_question_title="Q?",
            options=(
                AskOption(
                    label="A",
                    recommended=False,
                    cursor=True,
                    number=1,
                    description="line one\nline two\n\nline three",
                ),
            ),
        )
        out = _render_ask_user_question(form)
        # The whole description renders on a single indented line.
        assert "    line one line two line three" in out

    def test_body_clipped_at_3800_chars(self):
        """Even with the per-option cap, a worst-case form could exceed
        3800 chars. The renderer hard-clips the whole body so the send
        layer never has to split (splitting would break the multi-tab
        message_ids invariant in PR 3).
        """
        # Build 20 options each with a 250-char description ≈ 5300 chars
        # of just descriptions. Total body well over the 3800 cap.
        opts = tuple(
            AskOption(
                label=f"Option {i}",
                recommended=False,
                cursor=(i == 1),
                number=i,
                description="X" * 250,
            )
            for i in range(1, 21)
        )
        form = AskUserQuestionForm(
            tabs=(),
            current_question_title="Pick.",
            options=opts,
        )
        out = _render_ask_user_question(form)
        # Body capped under 3800.
        assert len(out) <= 3800
        # Cut marker present so the user knows it's truncated.
        assert "body truncated" in out


# ── PR 2b: pick-token map + structured option keyboard ────────────────────


from cctelegram.handlers.interactive_ui import (  # noqa: E402
    _PICK_TOKEN_TTL_SECONDS,
    _build_pick_button_rows,
    _PickTokenEntry,
    _mint_pick_token,
    _pick_token_cache,
    _pick_tokens,
    clear_interactive_msg,
    consume_pick_token,
    peek_pick_token,
    reset_pick_tokens_for_tests,
    set_interactive_mode,
)


@pytest.fixture
def _clear_pick_tokens():
    reset_pick_tokens_for_tests()
    yield
    reset_pick_tokens_for_tests()


@pytest.mark.usefixtures("_clear_pick_tokens")
class TestPickTokenMap:
    def test_mint_and_consume_roundtrip(self):
        entry = _PickTokenEntry(
            window_id="@1",
            user_id=42,
            thread_id=7,
            fingerprint="abc123def456",
            option_number=2,
            option_label="Fine",
            is_review_submit=False,
            expires_at=time.monotonic() + 60,
        )
        token = _mint_pick_token(entry)
        # Token is short hex (12 chars) so the full ``aqp:<token>`` payload
        # fits well under the 64-byte callback_data cap.
        assert len(token) == 12
        all_hex_digits = set("0123456789abcdef")
        assert all(c in all_hex_digits for c in token)
        # Consume returns the entry once, then None (single-use).
        got = consume_pick_token(token)
        assert got is entry
        assert consume_pick_token(token) is None

    def test_consume_expired_returns_none(self):
        entry = _PickTokenEntry(
            window_id="@1",
            user_id=42,
            thread_id=None,
            fingerprint="x",
            option_number=1,
            option_label="A",
            is_review_submit=False,
            expires_at=time.monotonic() - 1,  # already past deadline
        )
        token = _mint_pick_token(entry)
        # The mint itself ran a prune pass that should have dropped this
        # token before we even tried to consume — consume sees nothing.
        assert consume_pick_token(token) is None

    def test_mint_unique_tokens(self):
        entry_template = _PickTokenEntry(
            window_id="@1",
            user_id=42,
            thread_id=None,
            fingerprint="abc",
            option_number=1,
            option_label="A",
            is_review_submit=False,
            expires_at=time.monotonic() + 60,
        )
        seen = set()
        for _ in range(20):
            token = _mint_pick_token(entry_template)
            assert token not in seen
            seen.add(token)


@pytest.mark.usefixtures("_clear_pick_tokens")
class TestBuildPickButtonRows:
    def test_no_options_returns_empty(self):
        form = AskUserQuestionForm()
        rows = _build_pick_button_rows(
            user_id=42, thread_id=7, window_id="@1", form=form
        )
        assert rows == []

    def test_one_button_per_numbered_option(self):
        form = AskUserQuestionForm(
            options=(
                AskOption(label="Bad", recommended=False, cursor=True, number=1),
                AskOption(label="Fine", recommended=False, cursor=False, number=2),
                AskOption(label="Good", recommended=True, cursor=False, number=3),
            ),
        )
        rows = _build_pick_button_rows(
            user_id=42, thread_id=7, window_id="@9", form=form
        )
        # All three buttons land on a single row (cap is 5).
        assert len(rows) == 1
        assert len(rows[0]) == 3
        # Button text starts with "N. " for non-submit options.
        assert rows[0][0].text.startswith("1. ")
        # Recommended star is appended.
        assert "★" in rows[0][2].text
        # Each button carries a unique aqp:<token> callback.
        tokens = [b.callback_data for b in rows[0]]
        assert len(set(tokens)) == 3
        assert all(t.startswith("aqp:") for t in tokens)

    def test_review_submit_button_flagged(self):
        # On the review screen with cursor on "1. Submit answers", the
        # builder must mark the first button as is_review_submit so the
        # callback handler can apply the tighter guardrail.
        form = AskUserQuestionForm(
            options=(
                AskOption(
                    label="Submit answers",
                    recommended=False,
                    cursor=True,
                    number=1,
                ),
                AskOption(label="Cancel", recommended=False, cursor=False, number=2),
            ),
            is_review_screen=True,
        )
        rows = _build_pick_button_rows(
            user_id=42, thread_id=7, window_id="@9", form=form
        )
        assert len(rows) == 1
        # The submit button reads "✅ Submit answers".
        assert rows[0][0].text.startswith("✅ ")
        # Consume Cancel first — consuming a token now wipes its whole form
        # generation (sibling invalidation, see TestPickTokenReuse), so we
        # can't pop Submit then Cancel from the same render.
        cancel_token = rows[0][1].callback_data[len("aqp:") :]
        cancel_entry = consume_pick_token(cancel_token)
        assert cancel_entry is not None
        assert cancel_entry.is_review_submit is False
        # Re-mint the form to check the Submit entry's flag.
        rows2 = _build_pick_button_rows(
            user_id=42, thread_id=7, window_id="@9", form=form
        )
        submit_token = rows2[0][0].callback_data[len("aqp:") :]
        submit_entry = consume_pick_token(submit_token)
        assert submit_entry is not None
        assert submit_entry.is_review_submit is True

    def test_skips_options_without_a_numeric_shortcut(self):
        # Parser may emit options with number=None for free-text rows it
        # detected but couldn't bind to a digit. Those must NOT get a pick
        # button — the keystroke fallback still reaches them.
        form = AskUserQuestionForm(
            options=(
                AskOption(label="Bad", recommended=False, cursor=False, number=1),
                AskOption(
                    label="Type something",
                    recommended=False,
                    cursor=False,
                    number=None,
                ),
            ),
        )
        rows = _build_pick_button_rows(
            user_id=42, thread_id=7, window_id="@9", form=form
        )
        assert rows == []

    def test_six_options_split_across_two_rows(self):
        form = AskUserQuestionForm(
            options=tuple(
                AskOption(label=f"opt{i}", recommended=False, cursor=False, number=i)
                for i in range(1, 7)
            ),
        )
        rows = _build_pick_button_rows(
            user_id=42, thread_id=7, window_id="@9", form=form
        )
        # Cap is 5 per row → first row has 5, second has 1.
        assert [len(r) for r in rows] == [5, 1]

    def test_token_carries_full_entry_for_staleness_check(self):
        form = AskUserQuestionForm(
            options=(
                AskOption(
                    label="C — Parallel tracks", recommended=True, cursor=True, number=1
                ),
            ),
            current_question_title="approach?",
        )
        fp = form.fingerprint()
        rows = _build_pick_button_rows(
            user_id=42, thread_id=7, window_id="@9", form=form
        )
        token = rows[0][0].callback_data[len("aqp:") :]
        entry = consume_pick_token(token)
        assert entry is not None
        # Everything the callback handler needs is on the entry.
        assert entry.window_id == "@9"
        assert entry.user_id == 42
        assert entry.thread_id == 7
        assert entry.fingerprint == fp
        assert entry.option_number == 1
        assert entry.option_label == "C — Parallel tracks"
        # Expiration roughly matches the configured TTL.
        assert entry.expires_at > time.monotonic()
        assert entry.expires_at <= time.monotonic() + _PICK_TOKEN_TTL_SECONDS + 1

    def test_multi_question_review_screen_still_mints_submit_cancel(self):
        # Regression: the FA5 guard suppressed pick buttons on every
        # multi-tab form with ``current_tab_inferred=False``. The
        # multi-question review-screen branch in resolve_ask_form
        # legitimately sets ``current_tab_inferred=False`` (no tab
        # inference happens — pane is authoritatively "review"), but the
        # ``options`` come directly from the live pane (Submit / Cancel)
        # so labels and dispatch are sound. Suppressing was hiding the
        # Submit answers / Cancel buttons mid-AUQ workflow.
        from cctelegram.terminal_parser import AskQuestion

        form = AskUserQuestionForm(
            current_question_title=None,
            options=(
                AskOption(
                    label="Submit answers",
                    recommended=False,
                    cursor=True,
                    number=1,
                ),
                AskOption(label="Cancel", recommended=False, cursor=False, number=2),
            ),
            is_review_screen=True,
            # Multi-question: ``questions`` matrix populated even on the
            # review screen so the tab strip context is preserved.
            questions=(
                AskQuestion(title="Approach?", header="Approach", options=()),
                AskQuestion(title="Positioning?", header="Positioning", options=()),
            ),
            current_tab_inferred=False,
        )
        rows = _build_pick_button_rows(
            user_id=42, thread_id=7, window_id="@9", form=form
        )
        assert len(rows) == 1
        assert len(rows[0]) == 2
        # Submit button gets the review-submit treatment.
        assert rows[0][0].text.startswith("✅ ")
        # Both buttons carry aqp: pick tokens.
        assert all(b.callback_data.startswith("aqp:") for b in rows[0])


@pytest.mark.usefixtures("_clear_pick_tokens")
class TestPickTokenReuse:
    """Token churn would defeat MESSAGE_NOT_MODIFIED on edit. Hermes review
    flagged this as the load-bearing fix before PR 2b can ship: a re-render
    of the same form (same fingerprint) MUST reuse the same callback tokens
    so the reply_markup is byte-identical and Telegram can dedupe the edit.
    """

    def test_same_fingerprint_reuses_tokens(self):
        form = AskUserQuestionForm(
            options=(
                AskOption(label="Bad", recommended=False, cursor=True, number=1),
                AskOption(label="Fine", recommended=False, cursor=False, number=2),
            ),
        )
        first = _build_pick_button_rows(
            user_id=42, thread_id=7, window_id="@1", form=form
        )
        second = _build_pick_button_rows(
            user_id=42, thread_id=7, window_id="@1", form=form
        )
        # Two renders against the same fingerprint must produce identical
        # callback_data — otherwise every status-polling tick rewrites the
        # reply_markup and Telegram never returns MESSAGE_NOT_MODIFIED.
        first_tokens = [b.callback_data for b in first[0]]
        second_tokens = [b.callback_data for b in second[0]]
        assert first_tokens == second_tokens

    def test_different_fingerprint_mints_fresh_tokens(self):
        form_a = AskUserQuestionForm(
            options=(AskOption(label="Bad", recommended=False, cursor=True, number=1),),
        )
        form_b = AskUserQuestionForm(
            options=(
                # Different label → different fingerprint → fresh tokens.
                AskOption(label="Terrible", recommended=False, cursor=True, number=1),
            ),
        )
        a_rows = _build_pick_button_rows(
            user_id=42, thread_id=7, window_id="@1", form=form_a
        )
        b_rows = _build_pick_button_rows(
            user_id=42, thread_id=7, window_id="@1", form=form_b
        )
        a_token = a_rows[0][0].callback_data
        b_token = b_rows[0][0].callback_data
        assert a_token != b_token

    def test_consume_invalidates_cache_for_that_generation(self):
        form = AskUserQuestionForm(
            options=(
                AskOption(label="Bad", recommended=False, cursor=True, number=1),
                AskOption(label="Fine", recommended=False, cursor=False, number=2),
            ),
        )
        rows = _build_pick_button_rows(
            user_id=42, thread_id=7, window_id="@1", form=form
        )
        first_token = rows[0][0].callback_data[len("aqp:") :]
        second_token = rows[0][1].callback_data[len("aqp:") :]
        # Click the first button — the cache row for this fingerprint dies,
        # AND every sibling token in that row dies too (the form is about to
        # advance, so a stale sibling click is a bug to prevent).
        consumed = consume_pick_token(first_token)
        assert consumed is not None
        # Sibling token no longer resolves.
        assert consume_pick_token(second_token) is None
        # Next render against the same fingerprint mints fresh tokens.
        rows2 = _build_pick_button_rows(
            user_id=42, thread_id=7, window_id="@1", form=form
        )
        new_token = rows2[0][0].callback_data
        assert new_token != f"aqp:{first_token}"


@pytest.mark.usefixtures("_clear_pick_tokens")
class TestPeekPickTokenIsNonDestructive:
    """CB3 — wrong-user clicks must NOT destroy the legitimate owner's token."""

    def _entry(self, user_id: int = 42) -> _PickTokenEntry:
        return _PickTokenEntry(
            window_id="@1",
            user_id=user_id,
            thread_id=7,
            fingerprint="fp1",
            option_number=1,
            option_label="A",
            is_review_submit=False,
            expires_at=time.monotonic() + 60,
        )

    def test_peek_returns_entry_without_consuming(self):
        token = _mint_pick_token(self._entry())
        # Peek N times → same entry every time, token still alive.
        for _ in range(3):
            got = peek_pick_token(token)
            assert got is not None
            assert got.user_id == 42
        # The real consume still works after peeks.
        consumed = consume_pick_token(token)
        assert consumed is not None
        # Now actually gone.
        assert peek_pick_token(token) is None
        assert consume_pick_token(token) is None

    def test_peek_does_not_drop_sibling_cache(self):
        # Mint two tokens in the same cache row (same fingerprint).
        e1 = self._entry()
        e2 = _PickTokenEntry(
            window_id=e1.window_id,
            user_id=e1.user_id,
            thread_id=e1.thread_id,
            fingerprint=e1.fingerprint,
            option_number=2,
            option_label="B",
            is_review_submit=False,
            expires_at=e1.expires_at,
        )
        t1 = _mint_pick_token(e1)
        t2 = _mint_pick_token(e2)
        cache_key = (e1.user_id, e1.thread_id or 0, e1.window_id, e1.fingerprint)
        _pick_token_cache[cache_key] = [t1, t2]
        # Peek t1 — neither t2 nor the cache row should be touched.
        assert peek_pick_token(t1) is e1
        assert peek_pick_token(t2) is e2
        assert _pick_token_cache.get(cache_key) == [t1, t2]


@pytest.mark.usefixtures("_clear_interactive_state")
class TestAssertNavDispatchable:
    """P1.1 + P1.3 + CB1 + CB5 + F2 — nav-callback guard helper.

    These tests pin the public contract of ``assert_nav_dispatchable``:
    return values and per-branch behaviour. They mock the tmux + helper
    surface so we can drive each branch deterministically.
    """

    def _query(self) -> AsyncMock:
        q = AsyncMock()
        q.answer = AsyncMock()
        return q

    @pytest.mark.asyncio
    async def test_no_interactive_surface_short_circuits(self):
        from cctelegram.handlers.interactive_ui import assert_nav_dispatchable

        q = self._query()
        result = await assert_nav_dispatchable(q, 42, 7, "@0")
        assert result is None
        q.answer.assert_awaited_once_with("No live interactive UI", show_alert=False)

    @pytest.mark.asyncio
    async def test_no_interactive_surface_esc_returns_clear_sentinel(self):
        # F2: ESC carve-out — picker is gone, but ESC should still proceed
        # to the cleanup branch in the caller.
        from cctelegram.handlers.interactive_ui import (
            NAV_ESC_CLEAR,
            assert_nav_dispatchable,
        )

        q = self._query()
        result = await assert_nav_dispatchable(q, 42, 7, "@0", is_esc=True)
        assert result == NAV_ESC_CLEAR
        # ESC carve-out doesn't answer the query (the caller does after
        # running clear_interactive_msg).
        q.answer.assert_not_called()

    @pytest.mark.asyncio
    async def test_window_mismatch_short_circuits(self):
        from cctelegram.handlers import interactive_ui as iui

        # has_interactive_surface checks _interactive_msgs OR
        # _multi_tab_sessions, not _interactive_mode. Set both so we reach
        # the window-mismatch guard rather than tripping the surface guard
        # first.
        iui._interactive_msgs[(42, 7)] = 999
        iui.set_interactive_mode(42, "@otherwindow", 7)
        q = self._query()
        result = await iui.assert_nav_dispatchable(q, 42, 7, "@requested")
        assert result is None
        q.answer.assert_awaited_once_with("Window changed", show_alert=False)

    @pytest.mark.asyncio
    async def test_visible_pane_absent_short_circuits(self):
        # User left the picker, terminal is back at shell. visible_pane
        # returns shell output → liveness=absent → short-circuit.
        from cctelegram.handlers import interactive_ui as iui

        iui._interactive_msgs[(42, 7)] = 999
        iui.set_interactive_mode(42, "@0", 7)
        q = self._query()
        fake_window = MagicMock()
        fake_window.window_id = "@0"
        with (
            patch.object(
                iui.tmux_manager,
                "find_window_by_id",
                new_callable=AsyncMock,
                return_value=fake_window,
            ),
            patch.object(
                iui.tmux_manager,
                "capture_pane",
                new_callable=AsyncMock,
                return_value="$ ls\nfile.txt\n$ \n",
            ),
        ):
            result = await iui.assert_nav_dispatchable(q, 42, 7, "@0")
        assert result is None
        q.answer.assert_awaited_once_with("Picker closed, refreshing", show_alert=False)

    @pytest.mark.asyncio
    async def test_visible_pane_unknown_proceeds(self):
        # CB1: empty visible capture (alt-screen / redraw race) is UNKNOWN.
        # MUST NOT short-circuit — that would destroy a live picker the
        # very next frame brings back.
        from cctelegram.handlers import interactive_ui as iui

        iui._interactive_msgs[(42, 7)] = 999
        iui.set_interactive_mode(42, "@0", 7)
        q = self._query()
        fake_window = MagicMock()
        fake_window.window_id = "@0"
        with (
            patch.object(
                iui.tmux_manager,
                "find_window_by_id",
                new_callable=AsyncMock,
                return_value=fake_window,
            ),
            patch.object(
                iui.tmux_manager,
                "capture_pane",
                new_callable=AsyncMock,
                return_value="",  # empty visible
            ),
        ):
            result = await iui.assert_nav_dispatchable(q, 42, 7, "@0")
        # Proceed: returns the live window object, no short-circuit answer.
        assert result is fake_window
        q.answer.assert_not_called()

    @pytest.mark.asyncio
    async def test_picker_present_returns_window(self):
        from cctelegram.handlers import interactive_ui as iui

        iui._interactive_msgs[(42, 7)] = 999
        iui.set_interactive_mode(42, "@0", 7)
        q = self._query()
        fake_window = MagicMock()
        fake_window.window_id = "@0"
        pane = (
            "Pick.\n"
            "\n"
            "❯ 1. A\n"
            "  2. B\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        with (
            patch.object(
                iui.tmux_manager,
                "find_window_by_id",
                new_callable=AsyncMock,
                return_value=fake_window,
            ),
            patch.object(
                iui.tmux_manager,
                "capture_pane",
                new_callable=AsyncMock,
                return_value=pane,
            ),
        ):
            result = await iui.assert_nav_dispatchable(q, 42, 7, "@0")
        assert result is fake_window
        q.answer.assert_not_called()


@pytest.mark.usefixtures("_clear_interactive_state")
class TestAskUserQuestionPaneOnlySafety:
    """Active AUQs render from the pane immediately, with safe pick minting."""

    @pytest.fixture(autouse=True)
    def _reset_cache(self):
        from cctelegram.handlers import interactive_ui as iui

        iui.forget_ask_tool_input("@5")
        yield
        iui.forget_ask_tool_input("@5")

    @staticmethod
    def _aqp_buttons(markup):
        if markup is None:
            return []
        return [
            b
            for row in markup.inline_keyboard
            for b in row
            if getattr(b, "callback_data", "").startswith("aqp:")
        ]

    @staticmethod
    async def _render(mock_bot, pane: str, *, from_poller: bool, tool_input=None):
        from cctelegram.handlers import interactive_ui as iui

        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        with (
            patch("cctelegram.handlers.interactive_ui.tmux_manager") as mock_tmux,
            patch("cctelegram.handlers.interactive_ui.session_manager") as mock_sm_iu,
            patch("cctelegram.handlers.attention.session_manager") as mock_sm_att,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=pane)
            mock_sm_iu.resolve_chat_id.return_value = 100
            mock_sm_att.resolve_chat_id.return_value = 100
            mock_sm_att.get_display_name.return_value = "topic"
            return await iui.handle_interactive_ui(
                mock_bot,
                user_id=1,
                window_id=window_id,
                thread_id=42,
                tool_input=tool_input,
                from_poller=from_poller,
            )

    @pytest.mark.asyncio
    async def test_poller_cache_empty_partial_renders_without_pick_buttons_and_notice(
        self, mock_bot
    ):
        pane = (
            "Fastest path to the CEO review.\n"
            "❯ 3. Type something\n"
            "  4. Chat about this\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )

        result = await self._render(mock_bot, pane, from_poller=True)

        assert result is True
        mock_bot.send_message.assert_called()
        sent = mock_bot.send_message.call_args.kwargs
        assert "Only options 3-4 are visible" in sent["text"]
        assert self._aqp_buttons(sent.get("reply_markup")) == []

    @pytest.mark.asyncio
    async def test_poller_cache_empty_first_option_one_renders_with_pick_buttons(
        self, mock_bot
    ):
        pane = (
            "Pick one.\n"
            "\n"
            "❯ 1. A\n"
            "  2. B\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )

        result = await self._render(mock_bot, pane, from_poller=True)

        assert result is True
        sent = mock_bot.send_message.call_args.kwargs
        assert "Only options" not in sent["text"]
        assert self._aqp_buttons(sent.get("reply_markup"))

    @pytest.mark.asyncio
    async def test_callback_rerender_partial_pane_renders_without_pick_buttons_and_notice(
        self, mock_bot
    ):
        from cctelegram.handlers import interactive_ui as iui

        iui.remember_ask_tool_input(
            "@5",
            {
                "questions": [
                    {
                        "question": "Previous question?",
                        "options": [{"label": "Old A"}, {"label": "Old B"}],
                    }
                ]
            },
        )
        pane = (
            "New question whose first options scrolled away.\n"
            "❯ 2. Visible B\n"
            "  3. Visible C\n"
            "  4. Visible D\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )

        result = await self._render(mock_bot, pane, from_poller=False)

        assert result is True
        sent = mock_bot.send_message.call_args.kwargs
        assert "Only options 2-4 are visible" in sent["text"]
        assert self._aqp_buttons(sent.get("reply_markup")) == []

    def test_contiguous_options_gate_blocks_pick_buttons_for_shifted_numbers(self):
        from cctelegram.handlers import interactive_ui as iui
        from cctelegram.terminal_parser import AskOption, AskUserQuestionForm

        form = AskUserQuestionForm(
            current_question_title="Partial pane",
            options=(
                AskOption(label="Visible B", recommended=False, cursor=True, number=2),
                AskOption(label="Visible C", recommended=False, cursor=False, number=3),
                AskOption(label="Visible D", recommended=False, cursor=False, number=4),
            ),
        )

        assert iui._build_pick_button_rows(1, 42, "@5", form) == []

    @pytest.mark.asyncio
    async def test_stale_cache_plus_complete_contiguous_pane_mints_pick_buttons(
        self, mock_bot
    ):
        # Regression (2026-05-17 13:43 incident): a previous AUQ sat in
        # ``_last_completed_ask_tool_input`` for window @5 while a brand new
        # AUQ rendered on the pane with a complete contiguous option list
        # starting at 1. ``resolve_ask_form`` correctly tagged the form
        # with ``_meta["stale_fallback"]="1"`` (cached question != pane
        # question). The earlier defensive ``elif stale_fallback_form:
        # p14_suppress_picks = True`` branch then dropped pick buttons,
        # leaving the user with only the keystroke nav keyboard. The
        # contiguous-from-1 gate in ``_build_pick_button_rows`` plus
        # pane-derived labels in the stale-fallback form make pick mint
        # safe here — labels and dispatch agree because both come from
        # the live pane, and Hermes confirmed (2026-05-17) that the
        # validator captures with the same 500-line scrollback so the
        # callback path stays sound.
        from cctelegram.handlers import interactive_ui as iui

        iui.remember_ask_tool_input(
            "@5",
            {
                "questions": [
                    {
                        "question": "Previous, completed question?",
                        "options": [{"label": "Old A"}, {"label": "Old B"}],
                    }
                ]
            },
        )
        # Live pane shows a NEW AUQ with complete 1..3 options. The
        # stale cache's question text is unrelated to the pane content,
        # so resolve_ask_form takes the stale-fallback branch.
        pane = (
            "A brand-new question?\n"
            "\n"
            "❯ 1. Fresh A\n"
            "  2. Fresh B\n"
            "  3. Fresh C\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )

        result = await self._render(mock_bot, pane, from_poller=True)

        assert result is True
        sent = mock_bot.send_message.call_args.kwargs
        assert "Only options" not in sent["text"]
        # Pick buttons MUST mint — stale cache alone is no longer a
        # suppression reason.
        assert self._aqp_buttons(sent.get("reply_markup"))


@pytest.mark.usefixtures("_clear_pick_tokens")
class TestClearInteractiveMsgPrunesTokens:
    """P2.2 — clear_interactive_msg must drop pick-tokens for the cleared route."""

    @pytest.mark.asyncio
    async def test_clear_drops_tokens_for_active_window(self):
        user_id, thread_id, window_id = 42, 7, "@1"
        # Set up interactive mode so clear_interactive_msg sees the route.
        set_interactive_mode(user_id, window_id, thread_id)
        entry = _PickTokenEntry(
            window_id=window_id,
            user_id=user_id,
            thread_id=thread_id,
            fingerprint="fp1",
            option_number=1,
            option_label="A",
            is_review_submit=False,
            expires_at=time.monotonic() + 60,
        )
        token = _mint_pick_token(entry)
        cache_key = (user_id, thread_id, window_id, "fp1")
        _pick_token_cache[cache_key] = [token]
        assert token in _pick_tokens
        # bot=None → no Telegram I/O; the prune still runs.
        await clear_interactive_msg(user_id, bot=None, thread_id=thread_id)
        assert token not in _pick_tokens
        assert cache_key not in _pick_token_cache

    @pytest.mark.asyncio
    async def test_clear_leaves_other_routes_alone(self):
        # Two routes for the same user but different threads / windows.
        user_id = 42
        set_interactive_mode(user_id, "@1", 7)
        set_interactive_mode(user_id, "@2", 8)
        e1 = _PickTokenEntry(
            window_id="@1",
            user_id=user_id,
            thread_id=7,
            fingerprint="fp1",
            option_number=1,
            option_label="A",
            is_review_submit=False,
            expires_at=time.monotonic() + 60,
        )
        e2 = _PickTokenEntry(
            window_id="@2",
            user_id=user_id,
            thread_id=8,
            fingerprint="fp2",
            option_number=1,
            option_label="A",
            is_review_submit=False,
            expires_at=time.monotonic() + 60,
        )
        t1 = _mint_pick_token(e1)
        t2 = _mint_pick_token(e2)
        _pick_token_cache[(user_id, 7, "@1", "fp1")] = [t1]
        _pick_token_cache[(user_id, 8, "@2", "fp2")] = [t2]
        # Clear thread 7 only.
        await clear_interactive_msg(user_id, bot=None, thread_id=7)
        assert t1 not in _pick_tokens
        assert (user_id, 7, "@1", "fp1") not in _pick_token_cache
        # Thread 8 untouched.
        assert t2 in _pick_tokens
        assert (user_id, 8, "@2", "fp2") in _pick_token_cache

    @pytest.mark.asyncio
    async def test_clear_no_active_window_is_noop(self):
        # No prior set_interactive_mode → _interactive_mode pop returns None.
        # Clear must not raise; the prune loop just doesn't fire.
        await clear_interactive_msg(99, bot=None, thread_id=1)
        # No tokens to assert about; this is just a non-crash check.


# ── PR 2: callback-validator parity via resolve_ask_form ─────────────────


class TestCallbackValidatorParityRender:
    """The render path and the pick-token callback validator MUST produce
    byte-identical fingerprints. PR 1 added ``resolve_ask_form``; PR 2
    wires it into both call sites. This test pins that both call sites
    produce the same fingerprint for the same (tool_input, pane_text)
    pair.

    Without this property, every multi-tab click would bounce as "Form
    changed, refreshing" because the validator's pane-only re-parse would
    never match a JSONL-overlay-derived mint.
    """

    def test_single_question_fingerprint_matches_across_callsites(self):
        from cctelegram.terminal_parser import resolve_ask_form

        tool_input = {
            "questions": [
                {
                    "question": "Pick one.",
                    "options": [
                        {"label": "A", "description": "first"},
                        {"label": "B", "description": "second"},
                    ],
                }
            ]
        }
        pane = (
            "Pick one.\n"
            "\n"
            "❯ 1. A\n"
            "  2. B\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        # Render path
        render_form = resolve_ask_form(tool_input, pane)
        # Validator path (same inputs, same call)
        validate_form = resolve_ask_form(tool_input, pane)
        assert render_form is not None and validate_form is not None
        assert render_form.fingerprint() == validate_form.fingerprint()

    def test_multi_question_fingerprint_matches_across_callsites(self):
        from cctelegram.terminal_parser import resolve_ask_form

        tool_input = {
            "questions": [
                {
                    "question": "Pick approach.",
                    "options": [{"label": "alpha"}, {"label": "beta"}],
                },
                {
                    "question": "Pick polish.",
                    "options": [{"label": "gamma"}, {"label": "delta"}],
                },
            ]
        }
        pane = (
            "Pick polish.\n"
            "\n"
            "❯ 1. gamma\n"
            "  2. delta\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        render_form = resolve_ask_form(tool_input, pane)
        validate_form = resolve_ask_form(tool_input, pane)
        assert render_form is not None and validate_form is not None
        assert render_form.fingerprint() == validate_form.fingerprint()
        # Inferred path — fingerprint includes INF:1
        assert "INF:1" in render_form._canonical_repr()

    def test_pane_only_validator_diverges_from_jsonl_render(self):
        """Sanity-check the bug this PR fixes: if the validator uses
        ``parse_ask_user_question`` alone (pane-only) while the render
        uses ``resolve_ask_form`` (JSONL overlay) for a multi-tab form,
        the fingerprints WILL differ. This test would have caught the
        pre-PR2 bug.
        """
        from cctelegram.terminal_parser import (
            parse_ask_user_question,
            resolve_ask_form,
        )

        tool_input = {
            "questions": [
                {
                    "question": "Pick approach.",
                    "options": [{"label": "alpha"}, {"label": "beta"}],
                },
                {
                    "question": "Pick polish.",
                    "options": [{"label": "gamma"}, {"label": "delta"}],
                },
            ]
        }
        pane = (
            "Pick polish.\n"
            "\n"
            "❯ 1. gamma\n"
            "  2. delta\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        jsonl_form = resolve_ask_form(tool_input, pane)
        pane_only_form = parse_ask_user_question(pane)
        assert jsonl_form is not None and pane_only_form is not None
        # The mismatch is exactly the bug: pane-only form has no
        # ``questions`` matrix, so no QS:/INF: lines, so different hash.
        assert jsonl_form.fingerprint() != pane_only_form.fingerprint()

    def test_resolve_ask_tool_input_public_alias(self):
        """``resolve_ask_tool_input`` is the public sibling-imported name
        used by bot.py to feed the validator the same cached JSONL the
        render path saw. PR 2 introduces this alias.
        """
        from cctelegram.handlers.interactive_ui import (
            remember_ask_tool_input,
            resolve_ask_tool_input,
        )

        # Cache a payload, then read it back via the public alias.
        sample = {"questions": [{"question": "Q", "options": [{"label": "A"}]}]}
        remember_ask_tool_input("@99", sample)
        try:
            assert resolve_ask_tool_input("@99") == sample
            assert resolve_ask_tool_input("@nonexistent") is None
        finally:
            # Clean up so the cache doesn't bleed into other tests.
            from cctelegram.handlers.interactive_ui import forget_ask_tool_input

            forget_ask_tool_input("@99")


# ── PR 3: multi-tab state machine, lock, generation guard, FA5+ gate ────


class TestAskToolInputDigest:
    def test_none_returns_none(self):
        from cctelegram.handlers.interactive_ui import _ask_tool_input_digest

        assert _ask_tool_input_digest(None) is None

    def test_content_based_equality(self):
        from cctelegram.handlers.interactive_ui import _ask_tool_input_digest

        # Structurally equal but distinct dict objects must produce equal
        # digests. Without content-based comparison the rerender_guard
        # would false-positive on every cache read.
        a = {"questions": [{"question": "Q", "options": [{"label": "A"}]}]}
        b = {"questions": [{"question": "Q", "options": [{"label": "A"}]}]}
        assert a is not b  # sanity — distinct objects
        assert _ask_tool_input_digest(a) == _ask_tool_input_digest(b)

    def test_content_changes_produce_different_digests(self):
        from cctelegram.handlers.interactive_ui import _ask_tool_input_digest

        a = {"questions": [{"question": "Q1", "options": [{"label": "A"}]}]}
        b = {"questions": [{"question": "Q2", "options": [{"label": "A"}]}]}
        assert _ask_tool_input_digest(a) != _ask_tool_input_digest(b)

    def test_no_guard_sentinel_distinct_from_none(self):
        from cctelegram.handlers.interactive_ui import _NO_GUARD

        # ``None`` is a real value (= "cache was cleared") and must be
        # distinguishable from ``_NO_GUARD`` (= "don't guard at all").
        assert _NO_GUARD is not None


class TestHasInteractiveSurface:
    def test_returns_false_when_neither_map(self):
        from cctelegram.handlers.interactive_ui import (
            _interactive_msgs,
            _multi_tab_sessions,
            has_interactive_surface,
        )

        _interactive_msgs.clear()
        _multi_tab_sessions.clear()
        assert has_interactive_surface(42, 7) is False

    def test_returns_true_for_single_card(self):
        from cctelegram.handlers.interactive_ui import (
            _interactive_msgs,
            _multi_tab_sessions,
            has_interactive_surface,
        )

        _interactive_msgs.clear()
        _multi_tab_sessions.clear()
        _interactive_msgs[(42, 7)] = 100
        try:
            assert has_interactive_surface(42, 7) is True
        finally:
            _interactive_msgs.clear()

    def test_returns_true_for_multi_tab(self):
        from cctelegram.handlers.interactive_ui import (
            _MultiTabSession,
            _interactive_msgs,
            _multi_tab_sessions,
            has_interactive_surface,
        )

        _interactive_msgs.clear()
        _multi_tab_sessions.clear()
        _multi_tab_sessions[(42, 7)] = _MultiTabSession(
            window_id="@1",
            shape_digest="x",
            message_ids=[1, 2, 3],
            current_tab_idx=0,
        )
        try:
            assert has_interactive_surface(42, 7) is True
        finally:
            _multi_tab_sessions.clear()


class TestPickButtonRowsFA5Gate:
    """FA5+ safety: multi-tab forms with current_tab_inferred=False MUST
    NOT mint pick buttons. The dispatched digit could answer the wrong
    tab in the live TUI.
    """

    def _multi_tab_form(self, inferred: bool):
        from cctelegram.terminal_parser import (
            AskOption,
            AskQuestion,
            AskUserQuestionForm,
        )

        q1 = AskQuestion(
            title="Q1?",
            header="A",
            options=(
                AskOption(label="alpha", recommended=False, cursor=False, number=1),
                AskOption(label="beta", recommended=False, cursor=False, number=2),
            ),
        )
        q2 = AskQuestion(
            title="Q2?",
            header="B",
            options=(
                AskOption(label="gamma", recommended=False, cursor=False, number=1),
                AskOption(label="delta", recommended=False, cursor=False, number=2),
            ),
        )
        return AskUserQuestionForm(
            tabs=(),
            current_question_title="Q1?",
            options=q1.options,
            questions=(q1, q2),
            current_tab_inferred=inferred,
        )

    def test_inferred_true_mints_buttons(self):
        from cctelegram.handlers.interactive_ui import _build_pick_button_rows

        rows = _build_pick_button_rows(
            user_id=1, thread_id=2, window_id="@1", form=self._multi_tab_form(True)
        )
        assert rows  # non-empty

    def test_inferred_false_returns_empty(self):
        from cctelegram.handlers.interactive_ui import _build_pick_button_rows

        rows = _build_pick_button_rows(
            user_id=1, thread_id=2, window_id="@1", form=self._multi_tab_form(False)
        )
        assert rows == []

    def test_single_question_form_ignores_inferred_flag(self):
        # Single-question forms always carry current_tab_inferred=True
        # by default; FA5+ only applies to multi-tab. Sanity-check that
        # a single-question form with inferred=False (artificial) still
        # gets buttons — the gate only fires for multi-tab.
        from cctelegram.handlers.interactive_ui import _build_pick_button_rows
        from cctelegram.terminal_parser import AskOption, AskUserQuestionForm

        form = AskUserQuestionForm(
            tabs=(),
            current_question_title="Pick.",
            options=(AskOption(label="A", recommended=False, cursor=False, number=1),),
            questions=(),  # single-question shape
            current_tab_inferred=False,
        )
        rows = _build_pick_button_rows(
            user_id=1, thread_id=2, window_id="@1", form=form
        )
        assert rows  # still gets buttons; FA5+ doesn't apply


class TestPickButtonRows19Cap:
    """Options 10+ render as text only — no pick button. Sending literal
    ``"10"`` would type ``1`` then ``0`` and dispatch wrong.
    """

    def test_options_10_plus_skipped(self):
        from cctelegram.handlers.interactive_ui import _build_pick_button_rows
        from cctelegram.terminal_parser import AskOption, AskUserQuestionForm

        opts = tuple(
            AskOption(
                label=f"opt {i}",
                recommended=False,
                cursor=(i == 1),
                number=i,
            )
            for i in range(1, 13)  # 1..12
        )
        form = AskUserQuestionForm(
            tabs=(),
            current_question_title="Pick.",
            options=opts,
        )
        rows = _build_pick_button_rows(
            user_id=1, thread_id=2, window_id="@1", form=form
        )
        # Flatten rows → button labels.
        labels = [btn.text for row in rows for btn in row]
        # Buttons exist for 1..9.
        for i in range(1, 10):
            assert any(f"{i}." in lab or lab.startswith(str(i)) for lab in labels), (
                f"missing button for option {i}: {labels}"
            )
        # No buttons for 10, 11, 12.
        for i in range(10, 13):
            assert not any(lab.startswith(f"{i}.") for lab in labels), (
                f"unexpected button for option {i}: {labels}"
            )


class TestMultiTabPostN:
    """First-render multi-tab flow: post one card per question."""

    @pytest.fixture
    def _clear_multi_state(self):
        from cctelegram.handlers.interactive_ui import (
            _interactive_mode,
            _interactive_msgs,
            _last_completed_ask_tool_input,
            _multi_tab_sessions,
            _route_locks,
        )

        _interactive_msgs.clear()
        _interactive_mode.clear()
        _multi_tab_sessions.clear()
        _last_completed_ask_tool_input.clear()
        _route_locks.clear()
        yield
        _interactive_msgs.clear()
        _interactive_mode.clear()
        _multi_tab_sessions.clear()
        _last_completed_ask_tool_input.clear()
        _route_locks.clear()

    @pytest.mark.skip(
        reason="Multi-tab dispatch disabled in handle_interactive_ui (2026-05-15) — "
        "user preferred the legacy single-card flow. The state-machine code remains "
        "in place; re-enable the dispatch in handle_interactive_ui to revive this test."
    )
    @pytest.mark.asyncio
    async def test_post_n_cards_for_multi_question(self, _clear_multi_state):
        """Multi-tab form posts N cards; current tab carries pick buttons,
        non-current tabs have no markup."""
        from cctelegram.handlers.interactive_ui import (
            _multi_tab_sessions,
            handle_interactive_ui,
            remember_ask_tool_input,
        )

        # Three-question form. Cache the JSONL payload so resolve_ask_form
        # picks it up.
        remember_ask_tool_input(
            "@multi",
            {
                "questions": [
                    {
                        "question": "Pick A.",
                        "options": [
                            {"label": "alpha", "description": "first"},
                            {"label": "beta", "description": "second"},
                        ],
                    },
                    {
                        "question": "Pick B.",
                        "options": [
                            {"label": "gamma", "description": "third"},
                            {"label": "delta", "description": "fourth"},
                        ],
                    },
                    {
                        "question": "Pick C.",
                        "options": [
                            {"label": "epsilon", "description": "fifth"},
                            {"label": "zeta", "description": "sixth"},
                        ],
                    },
                ]
            },
        )

        # Pane points to the FIRST question — current_tab_idx will be 0.
        pane_text = (
            "Pick A.\n"
            "\n"
            "❯ 1. alpha\n"
            "  2. beta\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )

        # Mock the bot: each topic_send returns a fresh fake message_id.
        bot = AsyncMock()
        sent_counter = [100]

        async def fake_send_message(*args, **kwargs):
            sent_counter[0] += 1
            msg = MagicMock()
            msg.message_id = sent_counter[0]
            return msg

        bot.send_message.side_effect = fake_send_message

        with patch("cctelegram.handlers.interactive_ui.tmux_manager") as mock_tmux:
            window_mock = MagicMock()
            window_mock.window_id = "@multi"
            mock_tmux.find_window_by_id = AsyncMock(return_value=window_mock)
            mock_tmux.capture_pane = AsyncMock(return_value=pane_text)

            with patch("cctelegram.handlers.interactive_ui.session_manager") as mock_sm:
                mock_sm.resolve_chat_id = MagicMock(return_value=12345)
                mock_sm.resolve_session_for_window = AsyncMock(return_value=None)
                with patch(
                    "cctelegram.handlers.interactive_ui.session_id_for_window",
                    return_value="sess-1",
                ):
                    result = await handle_interactive_ui(
                        bot, user_id=7, window_id="@multi", thread_id=42
                    )

        assert result is True
        # 3 cards sent (one per question).
        assert bot.send_message.call_count == 3
        # Session recorded all 3 message_ids.
        session = _multi_tab_sessions.get((7, 42))
        assert session is not None
        assert len(session.message_ids) == 3
        assert session.current_tab_idx == 0
        # Current tab (card 0) has reply_markup; others don't.
        calls = bot.send_message.call_args_list
        # First call: current tab → reply_markup present.
        assert calls[0].kwargs.get("reply_markup") is not None
        # Subsequent cards: no markup.
        assert calls[1].kwargs.get("reply_markup") is None
        assert calls[2].kwargs.get("reply_markup") is None


class TestClearInteractiveMsgWalksBothMaps:
    @pytest.mark.asyncio
    async def test_clear_walks_multi_tab_message_ids(self):
        from cctelegram.handlers.interactive_ui import (
            _MultiTabSession,
            _interactive_msgs,
            _multi_tab_sessions,
            _route_locks,
            clear_interactive_msg,
        )

        # Seed both maps for one route.
        _interactive_msgs.clear()
        _multi_tab_sessions.clear()
        _route_locks.clear()
        _interactive_msgs[(42, 7)] = 50
        _multi_tab_sessions[(42, 7)] = _MultiTabSession(
            window_id="@1",
            shape_digest="x",
            message_ids=[100, 101, 102],
            current_tab_idx=1,
        )

        bot = AsyncMock()
        deleted_ids: list[int] = []

        async def fake_delete_message(chat_id, message_id, **kwargs):
            deleted_ids.append(message_id)

        bot.delete_message.side_effect = fake_delete_message

        with patch("cctelegram.handlers.interactive_ui.session_manager") as mock_sm:
            mock_sm.resolve_chat_id = MagicMock(return_value=12345)
            with patch("cctelegram.handlers.interactive_ui.attention") as mock_att:
                mock_att.dismiss = AsyncMock()
                await clear_interactive_msg(42, bot, 7)

        # Single card AND all 3 multi-tab cards deleted.
        assert sorted(deleted_ids) == [50, 100, 101, 102]
        # Both maps cleared.
        assert (42, 7) not in _interactive_msgs
        assert (42, 7) not in _multi_tab_sessions

        _route_locks.clear()


# ─────────────────────────────────────────────────────────────────────────
# Wave A (Bug A — duplicate picker on restart) + Bug B
# Persistence + hydrate + tri-state context-send tests (plan v5).
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def _isolated_interactive_state_file(tmp_path, monkeypatch):
    """Redirect interactive_state.json to a tmp_path and clear all state.

    Used by every persistence/hydrate test so we never touch the real
    ~/.cc-telegram/interactive_state.json.
    """
    from cctelegram.handlers import interactive_ui as iui

    fake_interactive_file = tmp_path / "interactive_state.json"

    def _fake_path():
        return fake_interactive_file

    monkeypatch.setattr(iui, "_interactive_state_file_path", _fake_path)

    iui._interactive_msgs.clear()
    iui._interactive_msg_meta.clear()
    iui._auq_context_posted.clear()
    iui._auq_context_msgs.clear()
    iui._last_completed_ask_tool_input.clear()
    iui._last_auq_tool_use_id.clear()
    yield fake_interactive_file
    iui._interactive_msgs.clear()
    iui._interactive_msg_meta.clear()
    iui._auq_context_posted.clear()
    iui._auq_context_msgs.clear()
    iui._last_completed_ask_tool_input.clear()
    iui._last_auq_tool_use_id.clear()


@pytest.mark.usefixtures("_isolated_interactive_state_file")
class TestInteractiveStatePersistence:
    """Write-through persistence for _interactive_msgs + _auq_context_posted."""

    def test_set_interactive_msg_persists_to_disk(
        self, _isolated_interactive_state_file
    ):
        import json as _json

        from cctelegram.handlers.interactive_ui import _set_interactive_msg

        _set_interactive_msg(
            (1, 10),
            msg_id=42,
            window_id="@5",
            session_id="sess-uuid",
            tool_use_id="toolu_abc",
        )
        assert _isolated_interactive_state_file.exists()
        data = _json.loads(_isolated_interactive_state_file.read_text())
        assert data["interactive_msgs"]["1:10"]["msg_id"] == 42
        assert data["interactive_msgs"]["1:10"]["window_id"] == "@5"
        assert data["interactive_msgs"]["1:10"]["session_id"] == "sess-uuid"
        assert data["interactive_msgs"]["1:10"]["tool_use_id"] == "toolu_abc"
        assert data["auq_context_posted"] == {}

    def test_clear_interactive_msg_persists(self, _isolated_interactive_state_file):
        import json as _json

        from cctelegram.handlers.interactive_ui import (
            _clear_interactive_msg,
            _set_interactive_msg,
        )

        _set_interactive_msg(
            (1, 10), msg_id=42, window_id="@5", session_id="s", tool_use_id=None
        )
        returned = _clear_interactive_msg((1, 10))
        assert returned == 42
        data = _json.loads(_isolated_interactive_state_file.read_text())
        assert data["interactive_msgs"] == {}

    def test_persist_handles_io_error_gracefully(self, monkeypatch, caplog):
        """OSError on disk write must be logged, not raised."""
        from cctelegram.handlers import interactive_ui as iui

        def _raise_oserror(path, data, *, indent=2):
            raise OSError("disk full")

        monkeypatch.setattr(iui, "atomic_write_json", _raise_oserror)
        with caplog.at_level("WARNING"):
            iui._set_interactive_msg(
                (1, 10),
                msg_id=42,
                window_id="@5",
                session_id="s",
                tool_use_id=None,
            )
        # In-memory mutation still happened.
        assert iui._interactive_msgs[(1, 10)] == 42
        assert any(
            "Failed to persist interactive_state.json" in r.message
            for r in caplog.records
        )

    def test_claim_auq_context_persists(self, _isolated_interactive_state_file):
        import json as _json

        from cctelegram.handlers import interactive_ui as iui

        claimed = iui.claim_auq_context_post("@5", "toolu_xyz")
        assert claimed is True
        data = _json.loads(_isolated_interactive_state_file.read_text())
        assert data["auq_context_posted"]["@5"] == "toolu_xyz"

    def test_forget_ask_tool_input_persists_drop(
        self, _isolated_interactive_state_file
    ):
        import json as _json

        from cctelegram.handlers import interactive_ui as iui

        iui.claim_auq_context_post("@5", "toolu_xyz")
        iui.forget_ask_tool_input("@5")
        data = _json.loads(_isolated_interactive_state_file.read_text())
        assert data["auq_context_posted"] == {}


@pytest.mark.usefixtures("_isolated_interactive_state_file")
class TestHydrateInteractiveState:
    """Hydrate at bot startup — restoration + staleness/remap/normalization."""

    def _write_state(self, path, **kwargs):
        import json as _json

        path.write_text(_json.dumps(kwargs))

    @pytest.fixture(autouse=True)
    def _bind_monkeypatch(self, monkeypatch):
        """Capture pytest's monkeypatch for use inside ``_session_mgr_stub``.

        Avoids threading ``monkeypatch`` through every test method's
        signature.
        """
        self._mp = monkeypatch
        yield

    def _session_mgr_stub(self, window_states, route_window_map):
        """Build a minimal mock of session_manager API used by hydrate.

        ``window_states``: ``{window_id: session_id}``.
        ``route_window_map``: ``{(user_id, thread_id_or_None): window_id}``.

        ``session_id_for_window`` is a MODULE-LEVEL function in
        ``session.py`` (line 1023), NOT a method on SessionManager.
        We monkeypatch the imported name in ``interactive_ui`` so the
        test maps the window→session lookup through ``window_states``.
        Regression: bf840e6 called ``session_mgr.session_id_for_window``
        which AttributeError'd at runtime against the real instance.
        """
        from unittest.mock import MagicMock

        from cctelegram.handlers import interactive_ui as iui
        from cctelegram.session import SessionManager

        # spec=SessionManager makes MagicMock raise AttributeError on
        # any attribute that doesn't exist on the real class — second
        # line of defense against the bf840e6 mistake.
        sm = MagicMock(spec=SessionManager)
        sm.window_states = {wid: object() for wid in window_states}

        def _sid_for_win(wid):
            return window_states.get(wid)

        def _win_for_thread(user_id, thread_id):
            return route_window_map.get((user_id, thread_id or 0))

        self._mp.setattr(iui, "session_id_for_window", _sid_for_win)
        sm.resolve_window_for_thread = _win_for_thread
        return sm

    def test_hydrate_handles_missing_file(self, _isolated_interactive_state_file):
        from cctelegram.handlers import interactive_ui as iui

        assert not _isolated_interactive_state_file.exists()
        sm = self._session_mgr_stub({}, {})
        iui.hydrate_interactive_state(sm)
        assert iui._interactive_msgs == {}
        assert iui._interactive_msg_meta == {}

    def test_hydrate_handles_malformed_json(self, _isolated_interactive_state_file):
        from cctelegram.handlers import interactive_ui as iui

        _isolated_interactive_state_file.write_text("{not json")
        sm = self._session_mgr_stub({}, {})
        iui.hydrate_interactive_state(sm)  # must not raise
        assert iui._interactive_msgs == {}

    def test_hydrate_restores_matching_session(self, _isolated_interactive_state_file):
        from cctelegram.handlers import interactive_ui as iui

        self._write_state(
            _isolated_interactive_state_file,
            interactive_msgs={
                "1:10": {
                    "msg_id": 42,
                    "window_id": "@5",
                    "session_id": "sess-1",
                    "tool_use_id": "toolu_a",
                    "created_at": "2026-05-22T00:00:00+00:00",
                }
            },
            auq_context_posted={},
        )
        sm = self._session_mgr_stub({"@5": "sess-1"}, {(1, 10): "@5"})
        iui.hydrate_interactive_state(sm)
        assert iui._interactive_msgs[(1, 10)] == 42
        assert iui._interactive_msg_meta[(1, 10)].window_id == "@5"

    def test_hydrate_drops_mismatching_session(self, _isolated_interactive_state_file):
        from cctelegram.handlers import interactive_ui as iui

        self._write_state(
            _isolated_interactive_state_file,
            interactive_msgs={
                "1:10": {
                    "msg_id": 42,
                    "window_id": "@5",
                    "session_id": "old-sess",
                    "tool_use_id": "toolu_a",
                    "created_at": "x",
                }
            },
            auq_context_posted={},
        )
        sm = self._session_mgr_stub({"@5": "new-sess"}, {(1, 10): "@5"})
        iui.hydrate_interactive_state(sm)
        assert (1, 10) not in iui._interactive_msgs
        # Crucially: no topic_delete is called by hydrate (it's sync,
        # no Telegram I/O at all). The orphan card stays.

    def test_hydrate_drops_on_route_rebind(self, _isolated_interactive_state_file):
        """Codex P2 #2 (v3): route now resolves to a DIFFERENT window
        with DIFFERENT session. Persisted-window fallback would
        mis-attribute the msg_id; v3+ drops instead."""
        from cctelegram.handlers import interactive_ui as iui

        self._write_state(
            _isolated_interactive_state_file,
            interactive_msgs={
                "1:10": {
                    "msg_id": 42,
                    "window_id": "@5",
                    "session_id": "old-sess",
                    "tool_use_id": "toolu_a",
                    "created_at": "x",
                }
            },
            auq_context_posted={},
        )
        # Route now bound to @7 (different window) with a different session.
        # The OLD window @5 still exists with its old session.
        sm = self._session_mgr_stub(
            {"@5": "old-sess", "@7": "new-sess"},
            {(1, 10): "@7"},
        )
        iui.hydrate_interactive_state(sm)
        # MUST drop — falling back to rec.window_id @5 would attribute
        # the msg to a route that no longer owns it.
        assert (1, 10) not in iui._interactive_msgs

    def test_hydrate_handles_partial_record(self, _isolated_interactive_state_file):
        """from_dict rejects msg_id <= 0 + empty window_id (P3 hardening)."""
        from cctelegram.handlers import interactive_ui as iui

        self._write_state(
            _isolated_interactive_state_file,
            interactive_msgs={
                "1:10": {"msg_id": 0, "window_id": "@5", "session_id": "s"},
                "2:20": {"msg_id": 100, "window_id": "", "session_id": "s"},
                "3:30": {
                    "msg_id": 42,
                    "window_id": "@7",
                    "session_id": "s",
                    "tool_use_id": None,
                    "created_at": "x",
                },
            },
            auq_context_posted={},
        )
        sm = self._session_mgr_stub({"@7": "s"}, {(3, 30): "@7"})
        iui.hydrate_interactive_state(sm)
        # Only the valid record survives.
        assert list(iui._interactive_msgs.keys()) == [(3, 30)]

    def test_hydrate_remaps_window_id(self, _isolated_interactive_state_file):
        """Pure @12 → @13 remap (same session, route is still bound)."""
        from cctelegram.handlers import interactive_ui as iui

        self._write_state(
            _isolated_interactive_state_file,
            interactive_msgs={
                "1:10": {
                    "msg_id": 42,
                    "window_id": "@12",
                    "session_id": "s",
                    "tool_use_id": None,
                    "created_at": "x",
                }
            },
            auq_context_posted={},
        )
        # tmux server restart: route's window remapped to @13.
        sm = self._session_mgr_stub({"@13": "s"}, {(1, 10): "@13"})
        iui.hydrate_interactive_state(sm)
        assert iui._interactive_msgs[(1, 10)] == 42
        assert iui._interactive_msg_meta[(1, 10)].window_id == "@13"

    def test_hydrate_normalizes_none_vs_empty_session(
        self, _isolated_interactive_state_file
    ):
        """Persisted session_id='', current_session_id_for_window=None → KEEP."""
        from cctelegram.handlers import interactive_ui as iui

        self._write_state(
            _isolated_interactive_state_file,
            interactive_msgs={
                "1:10": {
                    "msg_id": 42,
                    "window_id": "@5",
                    "session_id": "",
                    "tool_use_id": None,
                    "created_at": "x",
                }
            },
            auq_context_posted={},
        )
        # session_id_for_window returns None for @5; treat as equal to "".
        sm = self._session_mgr_stub({"@5": None}, {(1, 10): "@5"})
        iui.hydrate_interactive_state(sm)
        assert (1, 10) in iui._interactive_msgs

    def test_hydrate_prunes_unknown_context_marker(
        self, _isolated_interactive_state_file
    ):
        """Markers whose window is not in window_states get pruned."""
        from cctelegram.handlers import interactive_ui as iui

        self._write_state(
            _isolated_interactive_state_file,
            interactive_msgs={},
            auq_context_posted={"@99": "toolu_dead"},
        )
        sm = self._session_mgr_stub({"@5": "s"}, {})
        iui.hydrate_interactive_state(sm)
        assert iui._auq_context_posted == {}

    def test_hydrate_remaps_context_marker_on_window_remap(
        self, _isolated_interactive_state_file
    ):
        """v4 intent / v5 ordering: when meta remaps @12→@13 AND the
        marker on @12 matches rec.tool_use_id, mirror the remap."""
        from cctelegram.handlers import interactive_ui as iui

        self._write_state(
            _isolated_interactive_state_file,
            interactive_msgs={
                "1:10": {
                    "msg_id": 42,
                    "window_id": "@12",
                    "session_id": "s",
                    "tool_use_id": "toolu_a",
                    "created_at": "x",
                }
            },
            auq_context_posted={"@12": "toolu_a"},
        )
        sm = self._session_mgr_stub({"@13": "s"}, {(1, 10): "@13"})
        iui.hydrate_interactive_state(sm)
        # Both moved to @13.
        assert iui._interactive_msg_meta[(1, 10)].window_id == "@13"
        assert iui._auq_context_posted == {"@13": "toolu_a"}

    def test_hydrate_marker_remap_works_from_cold_module_state(
        self, _isolated_interactive_state_file
    ):
        """Codex P2 #1 (v4→v5): cold restart — module dicts empty before
        hydrate. Persisted markers are loaded into the LOCAL ctx_markers
        dict FIRST, then the meta loop reads/mutates THAT dict, then
        commits at the end. Without the v5 ordering fix, the meta loop
        would read from the empty module dict and the remap would
        dead-code on cold restart (the exact case that matters)."""
        from cctelegram.handlers import interactive_ui as iui

        # Cold state simulation.
        assert iui._interactive_msgs == {}
        assert iui._auq_context_posted == {}

        self._write_state(
            _isolated_interactive_state_file,
            interactive_msgs={
                "1:10": {
                    "msg_id": 42,
                    "window_id": "@12",
                    "session_id": "s",
                    "tool_use_id": "toolu_a",
                    "created_at": "x",
                }
            },
            auq_context_posted={"@12": "toolu_a"},
        )
        sm = self._session_mgr_stub({"@13": "s"}, {(1, 10): "@13"})
        iui.hydrate_interactive_state(sm)
        # The remap had to read from a freshly-loaded local dict.
        assert iui._auq_context_posted == {"@13": "toolu_a"}

    def test_session_id_for_window_is_module_level_not_method(self):
        """Anti-regression for bf840e6:

        ``session_id_for_window`` is a module-level function in
        ``session.py`` (line 1023), NOT a method on SessionManager.
        Commit bf840e6 introduced ``session_mgr.session_id_for_window(...)``
        in ``hydrate_interactive_state`` which AttributeError'd against
        the real singleton, crash-looped the bot on startup, and
        Telegram rate-limited getUpdates for ~36 minutes.

        Future typos of this shape are caught by the ``spec=SessionManager``
        mocks in the surrounding tests, but this explicit assertion
        documents the contract.
        """
        from cctelegram.session import SessionManager, session_manager

        assert not hasattr(SessionManager, "session_id_for_window"), (
            "SessionManager class must NOT have a session_id_for_window "
            "method. The function is module-level (session.py:1023). "
            "Calling session_mgr.session_id_for_window(...) would "
            "AttributeError at runtime."
        )
        assert not hasattr(session_manager, "session_id_for_window")

    def test_hydrate_mismatch_marker_not_remapped(
        self, _isolated_interactive_state_file
    ):
        """If marker on @12 != rec.tool_use_id, it's NOT moved.
        The marker belongs to a different AUQ that happened on the old
        window. Natural prune (@12 not in window_states) drops it."""
        from cctelegram.handlers import interactive_ui as iui

        self._write_state(
            _isolated_interactive_state_file,
            interactive_msgs={
                "1:10": {
                    "msg_id": 42,
                    "window_id": "@12",
                    "session_id": "s",
                    "tool_use_id": "toolu_NEW",
                    "created_at": "x",
                }
            },
            auq_context_posted={"@12": "toolu_OLD"},
        )
        # @12 is no longer in window_states; @13 is current.
        sm = self._session_mgr_stub({"@13": "s"}, {(1, 10): "@13"})
        iui.hydrate_interactive_state(sm)
        # Meta remapped to @13.
        assert iui._interactive_msg_meta[(1, 10)].window_id == "@13"
        # Marker NOT moved (mismatch). And @12 is unknown to session_mgr,
        # so it's pruned. Result: empty.
        assert iui._auq_context_posted == {}


@pytest.mark.usefixtures("_isolated_interactive_state_file")
class TestSendAuqContextMessageTriState:
    """v3 tri-state return values + v5 explicit NONE_SENT on no-op exits."""

    @pytest.mark.asyncio
    async def test_no_renderable_text_returns_none_sent(self, monkeypatch):
        from cctelegram.handlers import interactive_ui as iui

        # Empty questions list → formatter returns just the header
        # (whitespace-only after strip). v5 must return NONE_SENT
        # explicitly so caller's rollback fires.
        result = await iui._send_auq_context_message(
            None,  # type: ignore[arg-type]
            user_id=1,
            thread_id=None,
            chat_id=1,
            window_id="@5",
            source={"questions": []},
            dedup_key="t",
        )
        assert result is iui._ContextSendResult.NONE_SENT

    @pytest.mark.asyncio
    async def test_no_parts_returns_none_sent(self, monkeypatch):
        from cctelegram.handlers import interactive_ui as iui

        # Force build_response_parts to return []
        monkeypatch.setattr(
            "cctelegram.handlers.response_builder.build_response_parts",
            lambda *a, **kw: [],
        )
        result = await iui._send_auq_context_message(
            None,  # type: ignore[arg-type]
            user_id=1,
            thread_id=None,
            chat_id=1,
            window_id="@5",
            source={"questions": [{"question": "Q?"}]},
            dedup_key="t",
        )
        assert result is iui._ContextSendResult.NONE_SENT

    @pytest.mark.asyncio
    async def test_full_send_returns_full_sent(self, monkeypatch):
        from unittest.mock import Mock

        from cctelegram.handlers import interactive_ui as iui
        from cctelegram.handlers.message_sender import TopicSendOutcome

        async def _ok(*args, **kwargs):
            return Mock(message_id=100), TopicSendOutcome.OK

        monkeypatch.setattr(iui, "topic_send", _ok)
        result = await iui._send_auq_context_message(
            None,  # type: ignore[arg-type]
            user_id=1,
            thread_id=None,
            chat_id=1,
            window_id="@5",
            source={"questions": [{"question": "Q?"}]},
            dedup_key="t",
        )
        assert result is iui._ContextSendResult.FULL_SENT

    @pytest.mark.asyncio
    async def test_first_chunk_fails_returns_none_sent(self, monkeypatch):
        from cctelegram.handlers import interactive_ui as iui
        from cctelegram.handlers.message_sender import TopicSendOutcome

        async def _fail(*args, **kwargs):
            return None, TopicSendOutcome.TOPIC_NOT_FOUND

        monkeypatch.setattr(iui, "topic_send", _fail)
        result = await iui._send_auq_context_message(
            None,  # type: ignore[arg-type]
            user_id=1,
            thread_id=None,
            chat_id=1,
            window_id="@5",
            source={"questions": [{"question": "Q?"}]},
            dedup_key="t",
        )
        assert result is iui._ContextSendResult.NONE_SENT

    @pytest.mark.asyncio
    async def test_partial_send_returns_partial_sent(self, monkeypatch):
        """First chunk lands, second fails → PARTIAL_SENT (caller MUST
        keep the claim; rolling back would duplicate chunk 1)."""
        from unittest.mock import Mock

        from cctelegram.handlers import interactive_ui as iui
        from cctelegram.handlers.message_sender import TopicSendOutcome

        # Produce a tool_input that splits into multiple chunks.
        long_desc = ("paragraph " * 100).strip()
        tool_input = {
            "questions": [
                {
                    "question": "Q?",
                    "options": [
                        {"label": f"Option {i}", "description": long_desc}
                        for i in range(1, 11)
                    ],
                }
            ]
        }

        call_counter = {"n": 0}

        async def _ok_then_fail(*args, **kwargs):
            call_counter["n"] += 1
            if call_counter["n"] == 1:
                return Mock(message_id=100), TopicSendOutcome.OK
            return None, TopicSendOutcome.TOPIC_NOT_FOUND

        monkeypatch.setattr(iui, "topic_send", _ok_then_fail)
        result = await iui._send_auq_context_message(
            None,  # type: ignore[arg-type]
            user_id=1,
            thread_id=None,
            chat_id=1,
            window_id="@5",
            source=tool_input,
            dedup_key="t",
        )
        assert result is iui._ContextSendResult.PARTIAL_SENT

    @pytest.mark.asyncio
    async def test_exception_after_first_chunk_returns_partial(self, monkeypatch):
        """Non-RetryAfter exception after chunk 1 lands → PARTIAL_SENT."""
        from unittest.mock import Mock

        from cctelegram.handlers import interactive_ui as iui
        from cctelegram.handlers.message_sender import TopicSendOutcome

        long_desc = ("paragraph " * 100).strip()
        tool_input = {
            "questions": [
                {
                    "question": "Q?",
                    "options": [
                        {"label": f"Option {i}", "description": long_desc}
                        for i in range(1, 11)
                    ],
                }
            ]
        }

        call_counter = {"n": 0}

        async def _ok_then_raise(*args, **kwargs):
            call_counter["n"] += 1
            if call_counter["n"] == 1:
                return Mock(message_id=100), TopicSendOutcome.OK
            raise RuntimeError("network blew up")

        monkeypatch.setattr(iui, "topic_send", _ok_then_raise)
        result = await iui._send_auq_context_message(
            None,  # type: ignore[arg-type]
            user_id=1,
            thread_id=None,
            chat_id=1,
            window_id="@5",
            source=tool_input,
            dedup_key="t",
        )
        assert result is iui._ContextSendResult.PARTIAL_SENT

    @pytest.mark.asyncio
    async def test_exception_before_any_chunk_returns_none(self, monkeypatch):
        """Non-RetryAfter exception before any chunk lands → NONE_SENT."""
        from cctelegram.handlers import interactive_ui as iui

        async def _raise(*args, **kwargs):
            raise RuntimeError("immediate failure")

        monkeypatch.setattr(iui, "topic_send", _raise)
        result = await iui._send_auq_context_message(
            None,  # type: ignore[arg-type]
            user_id=1,
            thread_id=None,
            chat_id=1,
            window_id="@5",
            source={"questions": [{"question": "Q?"}]},
            dedup_key="t",
        )
        assert result is iui._ContextSendResult.NONE_SENT


@pytest.mark.usefixtures("_isolated_interactive_state_file")
class TestPostInitHydrateOrdering:
    """Codex P2 #1 (v3): load_session_map must run BEFORE hydrate
    so window_states[wid].session_id is populated."""

    @pytest.mark.asyncio
    async def test_post_init_calls_load_session_map_before_hydrate(self, monkeypatch):
        """Sequence check: in bot.post_init, load_session_map runs
        BEFORE hydrate_interactive_state. We verify the source has
        them in that order; this guards against future reordering."""
        from pathlib import Path

        src = Path("src/cctelegram/bot.py").read_text()
        # Find post_init body and check the relative position of the
        # two calls.
        post_init_idx = src.index("async def post_init(")
        body = src[post_init_idx:]
        lsm_idx = body.index("await session_manager.load_session_map()")
        hyd_idx = body.index("hydrate_interactive_state(session_manager)")
        assert lsm_idx < hyd_idx, (
            "load_session_map() must run BEFORE hydrate_interactive_state() "
            "so window_states have session_id populated before hydrate's "
            "staleness check fires."
        )

    @pytest.mark.asyncio
    async def test_resolve_stale_ids_runs_before_hydrate(self):
        """v3 invariant: resolve_stale_ids must precede hydrate so
        window-id remaps are visible to hydrate's resolve_window_for_thread."""
        from pathlib import Path

        src = Path("src/cctelegram/bot.py").read_text()
        post_init_idx = src.index("async def post_init(")
        body = src[post_init_idx:]
        rsi_idx = body.index("await session_manager.resolve_stale_ids()")
        hyd_idx = body.index("hydrate_interactive_state(session_manager)")
        assert rsi_idx < hyd_idx


@pytest.mark.usefixtures("_isolated_interactive_state_file")
class TestClearInteractiveMsgTombstone:
    """``clear_interactive_msg(tombstone=True)`` edits the single card to a
    non-actionable tombstone instead of deleting it.

    Set up by ``status_polling`` when the pane-absent hysteresis fires:
    the user never picked an option (no Telegram callback consumed) but
    Claude Code moved past the AUQ on its own (e.g. bypassPermissions).
    Without the tombstone the user's chat would lose all record of the
    picker; the tombstone preserves the message as an explicit notice.
    """

    @pytest.mark.asyncio
    async def test_tombstone_edits_single_card_instead_of_deleting(self, monkeypatch):
        from cctelegram.handlers import interactive_ui as iui

        ikey = (1, 42)
        iui._interactive_msgs[ikey] = 12345
        iui._interactive_mode[ikey] = "@5"

        edits: list[dict] = []
        deletes: list[dict] = []

        async def _fake_edit(bot, **kwargs):
            edits.append(kwargs)
            return iui.TopicSendOutcome.OK

        async def _fake_delete(bot, **kwargs):
            deletes.append(kwargs)
            return iui.TopicSendOutcome.OK

        monkeypatch.setattr(iui, "topic_edit", _fake_edit)
        monkeypatch.setattr(iui, "topic_delete", _fake_delete)

        session_mgr = MagicMock()
        session_mgr.resolve_chat_id = MagicMock(return_value=-100123)

        await iui.clear_interactive_msg(
            user_id=1,
            bot=MagicMock(),
            thread_id=42,
            session_mgr=session_mgr,
            tombstone=True,
        )

        assert len(edits) == 1, edits
        assert len(deletes) == 0, deletes
        edit = edits[0]
        assert edit["message_id"] == 12345
        assert edit["window_id"] == "@5"
        assert edit["reply_markup"] is None
        assert "Telegram pick" in edit["text"]
        assert edit["plain"] is True

    @pytest.mark.asyncio
    async def test_tombstone_default_false_still_deletes(self, monkeypatch):
        from cctelegram.handlers import interactive_ui as iui

        ikey = (1, 42)
        iui._interactive_msgs[ikey] = 12345
        iui._interactive_mode[ikey] = "@5"

        edits: list[dict] = []
        deletes: list[dict] = []

        async def _fake_edit(bot, **kwargs):
            edits.append(kwargs)
            return iui.TopicSendOutcome.OK

        async def _fake_delete(bot, **kwargs):
            deletes.append(kwargs)
            return iui.TopicSendOutcome.OK

        monkeypatch.setattr(iui, "topic_edit", _fake_edit)
        monkeypatch.setattr(iui, "topic_delete", _fake_delete)

        session_mgr = MagicMock()
        session_mgr.resolve_chat_id = MagicMock(return_value=-100123)

        await iui.clear_interactive_msg(
            user_id=1,
            bot=MagicMock(),
            thread_id=42,
            session_mgr=session_mgr,
        )

        assert len(edits) == 0, edits
        assert len(deletes) == 1, deletes
        # The window_id quirk fix: cleared_window_id is propagated.
        assert deletes[0]["window_id"] == "@5"
        assert deletes[0]["message_id"] == 12345


@pytest.mark.usefixtures("_isolated_interactive_state_file")
class TestAuqContextMsgRecordPersistence:
    """``_auq_context_msgs`` round-trips through interactive_state.json."""

    def test_record_persists_through_persist_then_load(
        self, _isolated_interactive_state_file
    ):
        import json as _json

        from cctelegram.handlers import interactive_ui as iui

        iui._auq_context_msgs["@5"] = iui._ContextMsgRecord(
            message_ids=(42, 43),
            source="form",
            dedup_key="form:abc123",
            tool_use_id=None,
            render_sha1="deadbeef",
            user_id=1,
            chat_id=-100,
            thread_id=378,
            session_id="sess-1",
            created_at="2026-05-25T07:00:00+00:00",
        )
        iui._persist_interactive_state()

        data = _json.loads(_isolated_interactive_state_file.read_text())
        assert "auq_context_msgs" in data
        assert "@5" in data["auq_context_msgs"]
        payload = data["auq_context_msgs"]["@5"]
        assert payload["message_ids"] == [42, 43]
        assert payload["source"] == "form"
        assert payload["dedup_key"] == "form:abc123"
        assert payload["tool_use_id"] is None

        rec = iui._ContextMsgRecord.from_dict(payload)
        assert rec is not None
        assert rec.message_ids == (42, 43)
        assert rec.source == "form"


@pytest.mark.usefixtures("_isolated_interactive_state_file")
class TestMaybeUpgradeAuqContextMessage:
    """``maybe_upgrade_auq_context_message`` upgrades a form-source post
    to dict-source by editing the existing Telegram message(s) in place.

    Covers the descriptions-missing bug: live AUQs render only labels
    from the pane form (commit 603c6bc), and the rich JSONL dict
    arrives later (when Claude flushes after answer). This test pins
    the contract: when the dict arrives, edit the message to include
    descriptions.
    """

    @pytest.mark.asyncio
    async def test_upgrade_edits_form_source_to_dict(self, monkeypatch):
        from cctelegram.handlers import interactive_ui as iui

        # Seed: form-source record (label-only) is persisted.
        iui._auq_context_msgs["@5"] = iui._ContextMsgRecord(
            message_ids=(101,),
            source="form",
            dedup_key="form:abc",
            tool_use_id=None,
            render_sha1="form-only-sha",
            user_id=1,
            chat_id=-100,
            thread_id=42,
            session_id="sess-1",
            created_at="2026-05-25T07:00:00+00:00",
        )

        # JSONL dict with rich descriptions is now cached.
        iui._last_completed_ask_tool_input["@5"] = {
            "questions": [
                {
                    "question": "Pick scope",
                    "options": [
                        {
                            "label": "All tabs",
                            "description": "Patch every extraction tab.",
                        },
                        {
                            "label": "Just one",
                            "description": "Limit to the visible tab.",
                        },
                    ],
                }
            ]
        }
        iui._last_auq_tool_use_id["@5"] = "toolu_01XYZ"

        edits: list[dict] = []
        sends: list[dict] = []

        async def _fake_edit(bot, **kwargs):
            edits.append(kwargs)
            return iui.TopicSendOutcome.OK

        async def _fake_send(bot, **kwargs):
            sends.append(kwargs)
            msg = MagicMock()
            msg.message_id = 999
            return msg, iui.TopicSendOutcome.OK

        monkeypatch.setattr(iui, "topic_edit", _fake_edit)
        monkeypatch.setattr(iui, "topic_send", _fake_send)

        result = await iui.maybe_upgrade_auq_context_message(
            bot=MagicMock(), window_id="@5"
        )

        assert result is True
        assert len(edits) >= 1
        # The first edit targets the existing message_id.
        assert edits[0]["message_id"] == 101
        # The edit text now contains a description fragment.
        edited_text = edits[0]["text"]
        assert "Patch every extraction tab." in edited_text

        # Record has been flipped to dict source.
        rec = iui._auq_context_msgs["@5"]
        assert rec.source == "dict"
        assert rec.tool_use_id == "toolu_01XYZ"
        assert rec.message_ids[0] == 101

    @pytest.mark.asyncio
    async def test_upgrade_is_noop_when_already_dict(self, monkeypatch):
        from cctelegram.handlers import interactive_ui as iui

        iui._auq_context_msgs["@5"] = iui._ContextMsgRecord(
            message_ids=(101,),
            source="dict",
            dedup_key="toolu_01XYZ",
            tool_use_id="toolu_01XYZ",
            render_sha1="any",
            user_id=1,
            chat_id=-100,
            thread_id=42,
            session_id="sess-1",
            created_at="2026-05-25T07:00:00+00:00",
        )

        called = []

        async def _fake_edit(bot, **kwargs):
            called.append("edit")
            return iui.TopicSendOutcome.OK

        monkeypatch.setattr(iui, "topic_edit", _fake_edit)

        result = await iui.maybe_upgrade_auq_context_message(
            bot=MagicMock(), window_id="@5"
        )

        assert result is False
        assert called == []

    @pytest.mark.asyncio
    async def test_upgrade_is_noop_when_no_record(self, monkeypatch):
        from cctelegram.handlers import interactive_ui as iui

        async def _fake_edit(bot, **kwargs):  # pragma: no cover
            raise AssertionError("edit must not be called")

        monkeypatch.setattr(iui, "topic_edit", _fake_edit)
        result = await iui.maybe_upgrade_auq_context_message(
            bot=MagicMock(), window_id="@unknown"
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_upgrade_no_op_when_render_identical(self, monkeypatch):
        """Form-source and dict-source happened to render identically
        (e.g. descriptions empty in JSONL too). Skip the API call but
        flip the source so a future call short-circuits."""
        from cctelegram.handlers import interactive_ui as iui

        # Prime a dict with no descriptions — renders identically to
        # what a form with the same labels would render.
        tool_input = {
            "questions": [
                {
                    "question": "Pick",
                    "options": [
                        {"label": "A"},
                        {"label": "B"},
                    ],
                }
            ]
        }
        rendered = iui._format_auq_context_message(tool_input)
        import hashlib as _hashlib

        identical_sha = _hashlib.sha1(rendered.encode("utf-8")).hexdigest()

        iui._auq_context_msgs["@5"] = iui._ContextMsgRecord(
            message_ids=(101,),
            source="form",
            dedup_key="form:abc",
            tool_use_id=None,
            render_sha1=identical_sha,
            user_id=1,
            chat_id=-100,
            thread_id=42,
            session_id="sess-1",
            created_at="2026-05-25T07:00:00+00:00",
        )
        iui._last_completed_ask_tool_input["@5"] = tool_input
        iui._last_auq_tool_use_id["@5"] = "toolu_01XYZ"

        called: list[str] = []

        async def _fake_edit(bot, **kwargs):  # pragma: no cover
            called.append("edit")
            return iui.TopicSendOutcome.OK

        monkeypatch.setattr(iui, "topic_edit", _fake_edit)

        result = await iui.maybe_upgrade_auq_context_message(
            bot=MagicMock(), window_id="@5"
        )

        # No-op upgrade returns False but flips the source.
        assert result is False
        assert called == []
        rec = iui._auq_context_msgs["@5"]
        assert rec.source == "dict"
        assert rec.tool_use_id == "toolu_01XYZ"


@pytest.mark.usefixtures("_isolated_interactive_state_file")
class TestCodexP2Fixes:
    """Codex review (2026-05-25) flagged two correctness gaps in the
    initial edit-on-upgrade impl. Both fixed; these tests pin them."""

    @pytest.mark.asyncio
    async def test_upgrade_does_not_commit_on_edit_failure(self, monkeypatch):
        """P2 #1: if topic_edit returns a non-OK outcome (TOPIC_CLOSED,
        FORBIDDEN, OTHER, …) the record must NOT flip to source="dict".
        Earlier code appended msg_id before the outcome check, leaving
        future calls short-circuiting as "already upgraded" while the
        Telegram message still showed the form-only render."""
        from cctelegram.handlers import interactive_ui as iui

        iui._auq_context_msgs["@5"] = iui._ContextMsgRecord(
            message_ids=(101,),
            source="form",
            dedup_key="form:abc",
            tool_use_id=None,
            render_sha1="form-only-sha",
            user_id=1,
            chat_id=-100,
            thread_id=42,
            session_id="sess-1",
            created_at="2026-05-25T07:00:00+00:00",
        )
        iui._last_completed_ask_tool_input["@5"] = {
            "questions": [
                {
                    "question": "Pick scope",
                    "options": [
                        {
                            "label": "All tabs",
                            "description": "Patch every extraction tab.",
                        }
                    ],
                }
            ]
        }
        iui._last_auq_tool_use_id["@5"] = "toolu_01XYZ"

        async def _fake_edit_topic_closed(bot, **kwargs):
            return iui.TopicSendOutcome.TOPIC_CLOSED

        monkeypatch.setattr(iui, "topic_edit", _fake_edit_topic_closed)

        result = await iui.maybe_upgrade_auq_context_message(
            bot=MagicMock(), window_id="@5"
        )

        # Edit failed → no upgrade — record stays form-source so a
        # future call can retry.
        assert result is False
        rec = iui._auq_context_msgs["@5"]
        assert rec.source == "form", (
            "edit failure must not flip source to 'dict' — that would "
            "permanently suppress retries"
        )
        assert rec.message_ids == (101,)

    def test_hydrate_remaps_auq_context_msgs_window_id(self, monkeypatch):
        """P2 #2: tmux server restart can renumber @12 → @13. The
        interactive_msgs loop already remaps. The auq_context_msgs
        sidecar must follow the same remap, not get pruned."""
        from cctelegram.handlers import interactive_ui as iui

        # Persisted state: AUQ context msg was posted under @12 (which
        # the new tmux server has renumbered to @13 with same session).
        old_window = "@12"
        new_window = "@13"
        session_id = "sess-1"

        state = {
            "interactive_msgs": {
                "1:42": {
                    "msg_id": 12345,
                    "window_id": old_window,
                    "session_id": session_id,
                    "tool_use_id": None,
                    "created_at": "2026-05-25T07:00:00+00:00",
                },
            },
            "auq_context_posted": {},
            "auq_context_msgs": {
                old_window: {
                    "message_ids": [201, 202],
                    "source": "form",
                    "dedup_key": "form:abc",
                    "tool_use_id": None,
                    "render_sha1": "deadbeef",
                    "user_id": 1,
                    "chat_id": -100,
                    "thread_id": 42,
                    "session_id": session_id,
                    "created_at": "2026-05-25T07:00:00+00:00",
                }
            },
        }
        import json as _json

        path = iui._interactive_state_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps(state))

        # SessionManager surface that the hydrate path consults.
        session_mgr = MagicMock()
        session_mgr.window_states = {new_window: MagicMock(session_id=session_id)}
        session_mgr.resolve_window_for_thread = MagicMock(return_value=new_window)

        # session_id_for_window is module-level — patch it for the
        # remap check.
        monkeypatch.setattr(
            iui,
            "session_id_for_window",
            lambda wid: session_id if wid == new_window else None,
        )

        iui.hydrate_interactive_state(session_mgr)

        # The interactive_msgs entry remaps from @12 → @13.
        assert iui._interactive_msgs[(1, 42)] == 12345
        assert iui._interactive_msg_meta[(1, 42)].window_id == new_window

        # The auq_context_msgs record MUST follow the same remap, not
        # get pruned because @12 isn't in known_windows anymore.
        assert new_window in iui._auq_context_msgs, (
            "auq_context_msgs must be remapped from @12 to @13, not pruned"
        )
        assert old_window not in iui._auq_context_msgs
        rec = iui._auq_context_msgs[new_window]
        assert rec.message_ids == (201, 202)
        assert rec.source == "form"

    @pytest.mark.asyncio
    async def test_upgrade_does_not_commit_on_partial_multi_chunk_failure(
        self, monkeypatch
    ):
        """Codex round 2 P2: form-source record has 2 chunks. First
        edit succeeds, second fails (e.g. TOPIC_CLOSED). Must NOT
        commit source='dict' — that would leave chunk 2 permanently
        stuck on form-source text with no retry path."""
        from cctelegram.handlers import interactive_ui as iui

        iui._auq_context_msgs["@5"] = iui._ContextMsgRecord(
            message_ids=(101, 102),  # two chunks
            source="form",
            dedup_key="form:abc",
            tool_use_id=None,
            render_sha1="old-sha",
            user_id=1,
            chat_id=-100,
            thread_id=42,
            session_id="sess-1",
            created_at="2026-05-25T07:00:00+00:00",
        )
        # Make a dict source that renders into ≥2 chunks (long descriptions)
        big = "X" * 2500
        iui._last_completed_ask_tool_input["@5"] = {
            "questions": [
                {
                    "question": "Pick scope",
                    "options": [
                        {"label": "A", "description": big},
                        {"label": "B", "description": big},
                    ],
                }
            ]
        }
        iui._last_auq_tool_use_id["@5"] = "toolu_01XYZ"

        call_count = {"n": 0}

        async def _fake_edit(bot, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return iui.TopicSendOutcome.OK
            return iui.TopicSendOutcome.TOPIC_CLOSED

        monkeypatch.setattr(iui, "topic_edit", _fake_edit)

        result = await iui.maybe_upgrade_auq_context_message(
            bot=MagicMock(), window_id="@5"
        )

        assert result is False
        rec = iui._auq_context_msgs["@5"]
        # Must NOT have committed — source stays "form" for retry.
        assert rec.source == "form"
        assert rec.message_ids == (101, 102)

    def test_remember_clears_auq_context_msgs_on_tool_use_id_rotation(self):
        """Codex round 3 P2 #2: when remember_ask_tool_input sees a
        NEW tool_use_id for the same window, it clears _auq_context_posted
        AND _auq_context_msgs. Without clearing _auq_context_msgs, the
        next AUQ's dict arrival would trigger maybe_upgrade which would
        edit the OLD lifecycle's message_ids with the NEW question's
        text — permanently wrong content."""
        from cctelegram.handlers import interactive_ui as iui

        # Lifecycle 1: window @5 has a form-source record + an active
        # tool_use_id from JSONL hydrate.
        iui._last_auq_tool_use_id["@5"] = "toolu_OLD"
        iui._auq_context_posted["@5"] = "form:abc"
        iui._auq_context_msgs["@5"] = iui._ContextMsgRecord(
            message_ids=(101,),
            source="form",
            dedup_key="form:abc",
            tool_use_id="toolu_OLD",
            render_sha1="sha-old",
            user_id=1,
            chat_id=-100,
            thread_id=42,
            session_id="sess-1",
            created_at="2026-05-25T07:00:00+00:00",
        )

        # Lifecycle 2: a NEW AUQ arrives in the same window before
        # tool_result fires for the old one.
        iui.remember_ask_tool_input(
            "@5",
            {"questions": [{"question": "New Q?"}]},
            "toolu_NEW",
        )

        # All three records for the old lifecycle MUST be cleared.
        assert "@5" not in iui._auq_context_posted, (
            "_auq_context_posted must clear on tool_use_id rotation"
        )
        assert "@5" not in iui._auq_context_msgs, (
            "_auq_context_msgs must clear on tool_use_id rotation — "
            "leaving the stale record would let maybe_upgrade edit "
            "the OLD message_ids with the NEW question's text"
        )
        # The new lifecycle's cache is in place.
        assert iui._last_auq_tool_use_id["@5"] == "toolu_NEW"

    def test_hydrate_prunes_auq_context_msgs_on_session_mismatch(self, monkeypatch):
        """Codex round 4 P2 #1: a persisted auq_context_msgs record
        for window @5 with session sess-OLD must be pruned at hydrate
        when @5 now binds to sess-NEW (e.g. /clear). Otherwise
        maybe_upgrade would edit OLD message ids with NEW question text."""
        from cctelegram.handlers import interactive_ui as iui

        state = {
            "interactive_msgs": {},
            "auq_context_posted": {},
            "auq_context_msgs": {
                "@5": {
                    "message_ids": [201],
                    "source": "form",
                    "dedup_key": "form:abc",
                    "tool_use_id": None,
                    "render_sha1": "deadbeef",
                    "user_id": 1,
                    "chat_id": -100,
                    "thread_id": 42,
                    "session_id": "sess-OLD",
                    "created_at": "2026-05-25T07:00:00+00:00",
                }
            },
        }
        import json as _json

        path = iui._interactive_state_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps(state))

        session_mgr = MagicMock()
        session_mgr.window_states = {"@5": MagicMock(session_id="sess-NEW")}
        session_mgr.resolve_window_for_thread = MagicMock(return_value=None)
        monkeypatch.setattr(
            iui,
            "session_id_for_window",
            lambda wid: "sess-NEW" if wid == "@5" else None,
        )

        iui.hydrate_interactive_state(session_mgr)

        # The record was pruned — session mismatch.
        assert "@5" not in iui._auq_context_msgs

    @pytest.mark.asyncio
    async def test_upgrade_preserves_appended_ids_on_partial_append(self, monkeypatch):
        """Codex round 4 P2 #2: partial append must persist the
        already-landed appended_ids on the record so the next retry
        doesn't duplicate them. Form-source render had 1 chunk, dict
        needs 3 chunks. Edit chunk 1 OK; append chunk 2 OK; append
        chunk 3 fails. Record must update to message_ids=(101, 202),
        source still 'form' for retry."""
        from cctelegram.handlers import interactive_ui as iui

        iui._auq_context_msgs["@5"] = iui._ContextMsgRecord(
            message_ids=(101,),
            source="form",
            dedup_key="form:abc",
            tool_use_id=None,
            render_sha1="form-only-sha",
            user_id=1,
            chat_id=-100,
            thread_id=42,
            session_id="sess-1",
            created_at="2026-05-25T07:00:00+00:00",
        )
        # Long descriptions → multiple dict chunks
        big = "Y" * 2500
        iui._last_completed_ask_tool_input["@5"] = {
            "questions": [
                {
                    "question": "Pick scope",
                    "options": [
                        {"label": "A", "description": big},
                        {"label": "B", "description": big},
                        {"label": "C", "description": big},
                    ],
                }
            ]
        }
        iui._last_auq_tool_use_id["@5"] = "toolu_01XYZ"

        async def _fake_edit(bot, **kwargs):
            return iui.TopicSendOutcome.OK

        append_call = {"n": 0}

        async def _fake_send(bot, **kwargs):
            append_call["n"] += 1
            if append_call["n"] == 1:
                msg = MagicMock()
                msg.message_id = 202
                return msg, iui.TopicSendOutcome.OK
            raise RuntimeError("transient failure on chunk 3")

        monkeypatch.setattr(iui, "topic_edit", _fake_edit)
        monkeypatch.setattr(iui, "topic_send", _fake_send)

        result = await iui.maybe_upgrade_auq_context_message(
            bot=MagicMock(), window_id="@5"
        )

        assert result is False
        rec = iui._auq_context_msgs["@5"]
        # source stays "form" — upgrade incomplete.
        assert rec.source == "form"
        # message_ids updated to include the appended chunk that DID land.
        assert rec.message_ids == (101, 202), (
            f"expected (101, 202), got {rec.message_ids}"
        )


# ── AUQ PreToolUse-hook reader (chunk 3) ──────────────────────────────────


import json as _json  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

from cctelegram.handlers.interactive_ui import (  # noqa: E402
    PreToolAskRecord,
    _PRETOOL_SCHEMA_VERSION,
    _PRETOOL_TTL_SECONDS,
    _labels_are_subsequence,
    _pretool_ask_records,
    _read_pretool_side_file,
    _record_consistent_with_pane,
    _resolve_pretool_record,
)
from cctelegram.terminal_parser import (  # noqa: E402,F811
    questions_content_digest,
    questions_content_pairs_from_tool_input,
)


def _write_pretool_side_file(
    tmp_path: _Path,
    *,
    session_id: str = "550e8400-e29b-41d4-a716-446655440000",
    tool_use_id: str = "toolu_017abcdef01234567890ab",
    questions: list[dict] | None = None,
    written_at: float | None = None,
    schema_version: int = 1,
) -> _Path:
    """Write a PreToolUse side file under tmp_path/auq_pending/.

    Returns the path. Tests pass tmp_path as the cc-telegram dir via
    CC_TELEGRAM_DIR env var so app_dir() resolves to it.
    """
    if questions is None:
        questions = [
            {
                "question": "Pick a fruit",
                "options": [
                    {"label": "Apple", "description": "red"},
                    {"label": "Banana", "description": "yellow"},
                ],
            }
        ]
    tool_input = {"questions": questions}
    pairs = questions_content_pairs_from_tool_input(tool_input)
    assert pairs is not None
    record = {
        "schema_version": schema_version,
        "session_id": session_id,
        "tool_use_id": tool_use_id,
        "tool_input": tool_input,
        "written_at": written_at if written_at is not None else time.time(),
        "input_fingerprint": questions_content_digest(pairs),
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/tmp/cwd",
    }
    pending_dir = tmp_path / "auq_pending"
    pending_dir.mkdir(exist_ok=True)
    target = pending_dir / f"{session_id}.json"
    target.write_text(_json.dumps(record))
    return target


def _make_form_single_question(
    title: str, labels: list[str], *, current_tab_inferred: bool = True
) -> AskUserQuestionForm:
    """Build an AskUserQuestionForm representing a single-question pane parse."""
    options = tuple(
        AskOption(label=lab, recommended=False, cursor=(i == 0), number=i + 1)
        for i, lab in enumerate(labels)
    )
    return AskUserQuestionForm(
        options=options,
        current_question_title=title,
        current_tab_inferred=current_tab_inferred,
    )


@pytest.fixture
def _cc_telegram_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
    # Clear in-memory cache before AND after — keeps tests isolated.
    _pretool_ask_records.clear()
    yield tmp_path
    _pretool_ask_records.clear()


class TestReadPretoolSideFile:
    def test_returns_none_when_file_missing(self, _cc_telegram_dir):
        assert _read_pretool_side_file("not-a-session") is None

    def test_reads_well_formed_file(self, _cc_telegram_dir):
        _write_pretool_side_file(_cc_telegram_dir)
        rec = _read_pretool_side_file("550e8400-e29b-41d4-a716-446655440000")
        assert rec is not None
        assert rec.tool_use_id == "toolu_017abcdef01234567890ab"
        assert rec.tool_input["questions"][0]["question"] == "Pick a fruit"
        assert len(rec.input_fingerprint) == 12

    def test_rejects_unknown_schema_version(self, _cc_telegram_dir):
        _write_pretool_side_file(_cc_telegram_dir, schema_version=999)
        rec = _read_pretool_side_file("550e8400-e29b-41d4-a716-446655440000")
        assert rec is None

    def test_rejects_malformed_json(self, _cc_telegram_dir):
        pending_dir = _cc_telegram_dir / "auq_pending"
        pending_dir.mkdir()
        sid = "550e8400-e29b-41d4-a716-446655440000"
        (pending_dir / f"{sid}.json").write_text("{not valid json")
        assert _read_pretool_side_file(sid) is None

    def test_rejects_non_dict_top_level(self, _cc_telegram_dir):
        pending_dir = _cc_telegram_dir / "auq_pending"
        pending_dir.mkdir()
        sid = "550e8400-e29b-41d4-a716-446655440000"
        (pending_dir / f"{sid}.json").write_text("[1, 2, 3]")
        assert _read_pretool_side_file(sid) is None

    def test_rejects_non_dict_tool_input(self, _cc_telegram_dir):
        pending_dir = _cc_telegram_dir / "auq_pending"
        pending_dir.mkdir()
        sid = "550e8400-e29b-41d4-a716-446655440000"
        (pending_dir / f"{sid}.json").write_text(
            _json.dumps(
                {"schema_version": _PRETOOL_SCHEMA_VERSION, "tool_input": "nope"}
            )
        )
        assert _read_pretool_side_file(sid) is None


class TestLabelsAreSubsequence:
    def test_empty_visible_false(self):
        assert _labels_are_subsequence((), ("A", "B")) is False

    def test_full_match(self):
        assert _labels_are_subsequence(("A", "B"), ("A", "B")) is True

    def test_visible_longer_than_full_false(self):
        assert _labels_are_subsequence(("A", "B", "C"), ("A", "B")) is False

    def test_visible_is_prefix(self):
        assert _labels_are_subsequence(("A",), ("A", "B", "C")) is True

    def test_visible_is_suffix(self):
        assert _labels_are_subsequence(("B", "C"), ("A", "B", "C")) is True

    def test_visible_in_middle(self):
        assert _labels_are_subsequence(("B", "C"), ("A", "B", "C", "D")) is True

    def test_non_contiguous_rejected(self):
        # A and C visible but not B → must reject (the contract is
        # contiguous subsequence, not subset).
        assert _labels_are_subsequence(("A", "C"), ("A", "B", "C")) is False

    def test_different_label_rejected(self):
        assert _labels_are_subsequence(("X",), ("A", "B")) is False


class TestRecordConsistentWithPane:
    def _single_q_record(self, labels: list[str], title: str = "Q"):
        tool_input = {
            "questions": [
                {
                    "question": title,
                    "options": [{"label": lab, "description": "d"} for lab in labels],
                }
            ]
        }
        pairs = questions_content_pairs_from_tool_input(tool_input)
        assert pairs is not None
        return PreToolAskRecord(
            tool_input=tool_input,
            session_id="sess",
            tool_use_id="tu",
            written_at=time.time(),
            input_fingerprint=questions_content_digest(pairs),
        )

    def test_no_pane_form(self):
        rec = self._single_q_record(["A", "B"])
        ok, reason = _record_consistent_with_pane(rec, None)
        assert ok is False
        assert reason == "no_pane_form"

    def test_empty_pane_options(self):
        rec = self._single_q_record(["A", "B"])
        form = AskUserQuestionForm()  # no options
        ok, reason = _record_consistent_with_pane(rec, form)
        assert ok is False
        assert reason == "no_pane_form"

    def test_single_question_full_match_accepted(self):
        rec = self._single_q_record(["Apple", "Banana"])
        form = _make_form_single_question("Q", ["Apple", "Banana"])
        ok, reason = _record_consistent_with_pane(rec, form)
        assert ok is True
        assert reason == "ok"

    def test_pane_label_mismatch_rejected(self):
        rec = self._single_q_record(["Apple", "Banana"])
        form = _make_form_single_question("Q", ["Apple", "Cherry"])
        ok, reason = _record_consistent_with_pane(rec, form)
        assert ok is False
        assert reason == "label_mismatch"

    def test_pane_title_missing_accepted_when_labels_match(self):
        # Hermes edge case: long descriptions push title off-pane.
        # Reader must STILL accept the record if labels match.
        rec = self._single_q_record(["Apple", "Banana"], title="Pick a fruit")
        form = _make_form_single_question("", ["Apple", "Banana"])
        ok, reason = _record_consistent_with_pane(rec, form)
        assert ok is True
        assert reason == "ok"

    def test_pane_shows_subsequence_of_options_accepted(self):
        # Pane scrolled — only options 2..N visible, option 1 off-screen.
        rec = self._single_q_record(["Apple", "Banana", "Cherry"])
        form = _make_form_single_question("Q", ["Banana", "Cherry"])
        ok, reason = _record_consistent_with_pane(rec, form)
        assert ok is True
        assert reason == "ok"

    def test_pane_title_prefix_match_accepted(self):
        # Pane truncates long question title; record carries full string.
        # Each must be a prefix of the other.
        rec = self._single_q_record(
            ["A"], title="A long question that the pane truncated"
        )
        form = _make_form_single_question("A long question that the pane", ["A"])
        ok, reason = _record_consistent_with_pane(rec, form)
        assert ok is True
        assert reason == "ok"

    def test_pane_title_differs_substantively_rejected(self):
        rec = self._single_q_record(["A"], title="Pick a fruit")
        form = _make_form_single_question("Choose your favorite color", ["A"])
        ok, reason = _record_consistent_with_pane(rec, form)
        assert ok is False
        assert reason == "title_mismatch"

    def test_multi_question_current_tab_inferred_match(self):
        # Multi-tab record + reliable tab → match by title.
        tool_input = {
            "questions": [
                {"question": "Q1: fruit", "options": [{"label": "A"}, {"label": "B"}]},
                {
                    "question": "Q2: color",
                    "options": [{"label": "Red"}, {"label": "Blue"}],
                },
            ]
        }
        pairs = questions_content_pairs_from_tool_input(tool_input)
        assert pairs is not None
        rec = PreToolAskRecord(
            tool_input=tool_input,
            session_id="sess",
            tool_use_id="tu",
            written_at=time.time(),
            input_fingerprint=questions_content_digest(pairs),
        )
        # Pane is on Q2.
        form = _make_form_single_question(
            "Q2: color", ["Red", "Blue"], current_tab_inferred=True
        )
        ok, reason = _record_consistent_with_pane(rec, form)
        assert ok is True
        assert reason == "ok"

    def test_multi_question_uninferred_tab_falls_through_to_label_match(self):
        # current_tab_inferred=False → predicate accepts any question
        # whose labels match the visible labels.
        tool_input = {
            "questions": [
                {"question": "Q1", "options": [{"label": "A"}, {"label": "B"}]},
                {"question": "Q2", "options": [{"label": "Red"}, {"label": "Blue"}]},
            ]
        }
        pairs = questions_content_pairs_from_tool_input(tool_input)
        assert pairs is not None
        rec = PreToolAskRecord(
            tool_input=tool_input,
            session_id="sess",
            tool_use_id="tu",
            written_at=time.time(),
            input_fingerprint=questions_content_digest(pairs),
        )
        form = _make_form_single_question(
            "", ["Red", "Blue"], current_tab_inferred=False
        )
        ok, reason = _record_consistent_with_pane(rec, form)
        assert ok is True

    def test_does_not_use_form_fingerprint_for_acceptance(self):
        # Cursor on different option ⇒ form.fingerprint() differs but
        # _record_consistent_with_pane must still accept (Codex R3 ask:
        # NEVER use AskUserQuestionForm.fingerprint() in the predicate).
        rec = self._single_q_record(["Apple", "Banana"])
        form_cur_a = _make_form_single_question("Q", ["Apple", "Banana"])
        form_cur_b = AskUserQuestionForm(
            options=(
                AskOption(label="Apple", recommended=False, cursor=False, number=1),
                AskOption(label="Banana", recommended=False, cursor=True, number=2),
            ),
            current_question_title="Q",
            current_tab_inferred=True,
        )
        assert form_cur_a.fingerprint() != form_cur_b.fingerprint()
        ok_a, _ = _record_consistent_with_pane(rec, form_cur_a)
        ok_b, _ = _record_consistent_with_pane(rec, form_cur_b)
        assert ok_a is True and ok_b is True


class TestResolvePretoolRecord:
    def _bind_window_to_session(self, window_id: str, session_id: str):
        # Drive session_manager state so session_id_for_window resolves.
        from cctelegram.session import session_manager

        session_manager.window_states.setdefault(
            window_id,
            type(
                session_manager.window_states.get(window_id, None)
                or type("WS", (), {})()
            )(),
        )
        # Easier: use the public API to ensure mapping.
        # session_manager has a set_session_id or similar — fall back to
        # touching window_states directly.
        ws = session_manager.window_states.get(window_id)
        if ws is None:
            from cctelegram.session import WindowState

            ws = WindowState(cwd="/tmp/cwd", session_id=session_id)
            session_manager.window_states[window_id] = ws
        else:
            object.__setattr__(ws, "session_id", session_id)

    def test_returns_none_when_no_session_mapping(self, _cc_telegram_dir):
        # No window→session map → reader returns None.
        form = _make_form_single_question("Q", ["A"])
        # Use a window_id that isn't in session_manager.
        assert _resolve_pretool_record("@no-such-window", form) is None

    def test_returns_none_when_side_file_missing(self, _cc_telegram_dir):
        self._bind_window_to_session("@9001", "11111111-1111-1111-1111-111111111111")
        form = _make_form_single_question("Q", ["A"])
        assert _resolve_pretool_record("@9001", form) is None

    def test_happy_path_returns_record(self, _cc_telegram_dir):
        sid = "550e8400-e29b-41d4-a716-446655440000"
        self._bind_window_to_session("@9002", sid)
        _write_pretool_side_file(_cc_telegram_dir, session_id=sid)
        form = _make_form_single_question("Pick a fruit", ["Apple", "Banana"])
        rec = _resolve_pretool_record("@9002", form)
        assert rec is not None
        assert rec.tool_use_id == "toolu_017abcdef01234567890ab"

    def test_caches_after_first_resolve(self, _cc_telegram_dir):
        sid = "550e8400-e29b-41d4-a716-446655440000"
        self._bind_window_to_session("@9003", sid)
        _write_pretool_side_file(_cc_telegram_dir, session_id=sid)
        form = _make_form_single_question("Pick a fruit", ["Apple", "Banana"])
        _resolve_pretool_record("@9003", form)
        assert "@9003" in _pretool_ask_records

    def test_ttl_expiry_evicts_and_returns_none(self, _cc_telegram_dir):
        sid = "550e8400-e29b-41d4-a716-446655440000"
        self._bind_window_to_session("@9004", sid)
        _write_pretool_side_file(
            _cc_telegram_dir,
            session_id=sid,
            written_at=time.time() - _PRETOOL_TTL_SECONDS - 1,
        )
        form = _make_form_single_question("Pick a fruit", ["Apple", "Banana"])
        assert _resolve_pretool_record("@9004", form) is None
        assert "@9004" not in _pretool_ask_records

    def test_pane_drift_evicts_cached_record(self, _cc_telegram_dir):
        # Cache invariant: a cached record that no longer matches the
        # live pane is evicted on next call, not stale-served.
        sid = "550e8400-e29b-41d4-a716-446655440000"
        self._bind_window_to_session("@9005", sid)
        _write_pretool_side_file(_cc_telegram_dir, session_id=sid)
        form_initial = _make_form_single_question("Pick a fruit", ["Apple", "Banana"])
        rec1 = _resolve_pretool_record("@9005", form_initial)
        assert rec1 is not None
        # Pane drifts to a different label set (user moved on).
        form_drifted = _make_form_single_question("Other Q", ["Cherry", "Date"])
        rec2 = _resolve_pretool_record("@9005", form_drifted)
        assert rec2 is None
        assert "@9005" not in _pretool_ask_records

    def test_corrupt_file_evicts_cached_record(self, _cc_telegram_dir):
        sid = "550e8400-e29b-41d4-a716-446655440000"
        self._bind_window_to_session("@9006", sid)
        _write_pretool_side_file(_cc_telegram_dir, session_id=sid)
        form = _make_form_single_question("Pick a fruit", ["Apple", "Banana"])
        rec1 = _resolve_pretool_record("@9006", form)
        assert rec1 is not None
        # Now corrupt the file on disk.
        (_cc_telegram_dir / "auq_pending" / f"{sid}.json").write_text("{garbage")
        rec2 = _resolve_pretool_record("@9006", form)
        assert rec2 is None
        assert "@9006" not in _pretool_ask_records

    def test_future_written_at_evicts_record(self, _cc_telegram_dir):
        # Codex chunk-3 P1: a side file with written_at in the future
        # (clock skew or tampering) MUST be rejected, not stay valid
        # indefinitely. The reader uses a -30s skew window.
        from cctelegram.handlers.interactive_ui import (
            _PRETOOL_FUTURE_SKEW_SECONDS,
        )

        sid = "550e8400-e29b-41d4-a716-446655440000"
        # Bind window. Use the real session_manager (NOT iui's patch).
        from cctelegram.session import WindowState, session_manager

        session_manager.window_states["@skew1"] = WindowState(
            cwd="/tmp/cwd", session_id=sid
        )
        try:
            _write_pretool_side_file(
                _cc_telegram_dir,
                session_id=sid,
                written_at=time.time() + _PRETOOL_FUTURE_SKEW_SECONDS + 60,
            )
            form = _make_form_single_question("Pick a fruit", ["Apple", "Banana"])
            assert _resolve_pretool_record("@skew1", form) is None
        finally:
            session_manager.window_states.pop("@skew1", None)

    def test_recent_future_within_skew_window_accepted(self, _cc_telegram_dir):
        # A tiny clock drift within the skew window must still accept
        # (NTP can momentarily produce 1-2s of future-drift).
        sid = "550e8400-e29b-41d4-a716-446655440000"
        from cctelegram.session import WindowState, session_manager

        session_manager.window_states["@skew2"] = WindowState(
            cwd="/tmp/cwd", session_id=sid
        )
        try:
            _write_pretool_side_file(
                _cc_telegram_dir, session_id=sid, written_at=time.time() + 2
            )
            form = _make_form_single_question("Pick a fruit", ["Apple", "Banana"])
            assert _resolve_pretool_record("@skew2", form) is not None
        finally:
            session_manager.window_states.pop("@skew2", None)

    def test_non_uuid_session_id_refused(self, _cc_telegram_dir, caplog):
        # Codex chunk-3 P2: defense-in-depth against a corrupt
        # session_map storing a non-UUID session_id that could escape
        # auq_pending/ via path traversal.
        import logging as _logging

        from cctelegram.handlers.interactive_ui import _read_pretool_side_file

        with caplog.at_level(
            _logging.WARNING, logger="cctelegram.handlers.interactive_ui"
        ):
            assert _read_pretool_side_file("../etc/passwd") is None
        assert any(
            "refusing to resolve non-UUID" in r.getMessage() for r in caplog.records
        )

    def test_fingerprint_is_recomputed_not_trusted_from_file(
        self, _cc_telegram_dir, tmp_path
    ):
        # Codex chunk-3 P1: an attacker (or a malformed write) could
        # poison the stored input_fingerprint with question text. The
        # reader MUST recompute the fingerprint from the validated
        # tool_input and never trust the stored value.
        sid = "550e8400-e29b-41d4-a716-446655440000"
        # Hand-craft a file with a poisoned input_fingerprint.
        pending_dir = tmp_path / "auq_pending"
        pending_dir.mkdir(exist_ok=True)
        tool_input = {
            "questions": [
                {
                    "question": "Q",
                    "options": [{"label": "A"}, {"label": "B"}],
                }
            ]
        }
        rec = {
            "schema_version": _PRETOOL_SCHEMA_VERSION,
            "session_id": sid,
            "tool_use_id": "tu",
            "tool_input": tool_input,
            "written_at": time.time(),
            "input_fingerprint": "SECRET_QUESTION_TEXT_LEAK",
        }
        (pending_dir / f"{sid}.json").write_text(_json.dumps(rec))
        from cctelegram.handlers.interactive_ui import _read_pretool_side_file

        loaded = _read_pretool_side_file(sid)
        assert loaded is not None
        # Recomputed → strict 12-hex, NOT the poisoned value.
        assert loaded.input_fingerprint != "SECRET_QUESTION_TEXT_LEAK"
        assert len(loaded.input_fingerprint) == 12
        assert all(c in "0123456789abcdef" for c in loaded.input_fingerprint)

    def test_peek_does_not_create_window_state(self):
        # Codex chunk-3 P2: peek must NOT auto-create a WindowState
        # for an unknown window. _resolve_pretool_record uses peek
        # so probing for an unknown window doesn't mutate state.
        from cctelegram.session import peek_session_id_for_window, session_manager

        bogus = "@no-such-window-test"
        assert bogus not in session_manager.window_states
        assert peek_session_id_for_window(bogus) is None
        # Crucially: peek did NOT create an entry.
        assert bogus not in session_manager.window_states

    def test_rejection_log_omits_question_text(self, _cc_telegram_dir, caplog):
        # Privacy: rejection reason logs must NOT include question/option
        # text. Trigger a pane-mismatch rejection and verify the log only
        # has the reason code + fingerprint.
        import logging as _logging

        sid = "550e8400-e29b-41d4-a716-446655440000"
        self._bind_window_to_session("@9007", sid)
        _write_pretool_side_file(
            _cc_telegram_dir,
            session_id=sid,
            questions=[
                {
                    "question": "ULTRA_SECRET_QUESTION_TEXT",
                    "options": [
                        {"label": "ULTRA_SECRET_LABEL_1"},
                        {"label": "ULTRA_SECRET_LABEL_2"},
                    ],
                }
            ],
        )
        form = _make_form_single_question("Different Q", ["Different Label"])
        with caplog.at_level(
            _logging.DEBUG, logger="cctelegram.handlers.interactive_ui"
        ):
            rec = _resolve_pretool_record("@9007", form)
        assert rec is None
        for record in caplog.records:
            assert "ULTRA_SECRET" not in record.getMessage()


# ── Gate routing (chunk 4): the R2 P1 fix verification ───────────────────


@pytest.fixture
def _pretool_gate_setup(tmp_path, monkeypatch):
    """Set up CC_TELEGRAM_DIR + clean caches before each gate test."""
    from cctelegram.handlers import interactive_ui as iui

    monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
    iui._pretool_ask_records.clear()
    iui._last_completed_ask_tool_input.clear()
    iui._last_auq_tool_use_id.clear()
    iui._auq_context_posted.clear()
    iui._auq_context_msgs.clear()
    iui._interactive_msgs.clear()
    iui._interactive_mode.clear()
    yield tmp_path
    iui._pretool_ask_records.clear()
    iui._last_completed_ask_tool_input.clear()
    iui._last_auq_tool_use_id.clear()
    iui._auq_context_posted.clear()
    iui._auq_context_msgs.clear()
    iui._interactive_msgs.clear()
    iui._interactive_mode.clear()


def _auq_pane_text(*, title: str, labels: list[str]) -> str:
    """Render a numbered-options AUQ pane (no box-drawing) that the
    real parse_ask_user_question accepts."""
    lines = [title, ""]
    for i, lab in enumerate(labels, start=1):
        cursor = "❯" if i == 1 else " "
        lines.append(f"{cursor} {i}. {lab}")
    lines.append("")
    lines.append("Enter to select · ↑/↓ to navigate · Esc to cancel")
    return "\n".join(lines) + "\n"


def _bind_window(window_id: str, session_id: str, cwd: str = "/tmp/cwd") -> None:
    """Bind window_id → session_id in session_manager (sync API)."""
    from cctelegram.session import WindowState, session_manager

    ws = session_manager.window_states.get(window_id)
    if ws is None:
        ws = WindowState(cwd=cwd, session_id=session_id)
        session_manager.window_states[window_id] = ws
    else:
        object.__setattr__(ws, "session_id", session_id)


def _extract_gate_source_tag(caplog) -> str | None:
    """Pull the ``ctx_source=...`` value from the latest gate-eval log."""
    for record in reversed(caplog.records):
        msg = record.getMessage()
        if "AUQ context gate eval" not in msg:
            continue
        marker = "ctx_source="
        idx = msg.find(marker)
        if idx == -1:
            continue
        rest = msg[idx + len(marker) :]
        end = rest.find(" ")
        return rest[:end] if end != -1 else rest
    return None


@pytest.mark.usefixtures("_clear_interactive_state")
class TestContextGateRouting:
    """The R2 P1 fix: when a PreToolUse-hook record is present for a
    live AUQ that hasn't flushed tool_use to JSONL yet, the gate must
    route ctx_source to ``dict_via_hook`` — not fall through to form.
    """

    @pytest.mark.asyncio
    async def test_gate_routes_dict_via_hook_when_pretool_present_no_tool_use_id(
        self, mock_bot: AsyncMock, _pretool_gate_setup, caplog
    ):
        import logging as _logging

        from cctelegram.handlers import interactive_ui as iui

        window_id = "@h1"
        session_id = "550e8400-e29b-41d4-a716-446655440000"
        labels = ["Apple", "Banana", "Cherry"]
        title = "Pick a fruit"
        _bind_window(window_id, session_id)
        _write_pretool_side_file(
            _pretool_gate_setup,
            session_id=session_id,
            questions=[
                {
                    "question": title,
                    "options": [
                        {"label": lab, "description": f"about {lab}"} for lab in labels
                    ],
                }
            ],
        )

        pane_text = _auq_pane_text(title=title, labels=labels)
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch.object(iui, "tmux_manager") as mock_tmux,
            patch.object(iui, "session_manager") as mock_sm_iu,
            patch("cctelegram.handlers.attention.session_manager") as mock_sm_att,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=pane_text)
            mock_sm_iu.resolve_chat_id.return_value = 100
            mock_sm_iu.window_states = {
                window_id: type(
                    "WS",
                    (),
                    {
                        "window_id": window_id,
                        "session_id": session_id,
                        "cwd": "/tmp/cwd",
                    },
                )()
            }
            mock_sm_att.resolve_chat_id.return_value = 100
            mock_sm_att.get_display_name.return_value = "topic"

            with caplog.at_level(
                _logging.INFO, logger="cctelegram.handlers.interactive_ui"
            ):
                # NOTE: _last_auq_tool_use_id is INTENTIONALLY empty —
                # this is the R2 P1 scenario. Pre-fix, the gate would
                # have routed to "form" here.
                await handle_interactive_ui(
                    mock_bot, user_id=1, window_id=window_id, thread_id=42
                )

        source_tag = _extract_gate_source_tag(caplog)
        assert source_tag == "dict_via_hook", (
            f"expected dict_via_hook, got {source_tag!r}. "
            f"This is the R2 P1 regression test — failure means the gate "
            f"is silently falling back to form despite a valid hook record."
        )

    @pytest.mark.asyncio
    async def test_gate_routes_to_form_when_no_pretool_record(
        self, mock_bot: AsyncMock, _pretool_gate_setup, caplog
    ):
        # No side file written. Today's default behavior — form-source
        # fallback. Must still work post-patch (regression check).
        import logging as _logging

        from cctelegram.handlers import interactive_ui as iui

        window_id = "@h2"
        session_id = "550e8400-e29b-41d4-a716-446655440000"
        _bind_window(window_id, session_id)
        pane_text = _auq_pane_text(title="Q", labels=["A", "B"])

        mock_window = MagicMock()
        mock_window.window_id = window_id
        with (
            patch.object(iui, "tmux_manager") as mock_tmux,
            patch.object(iui, "session_manager") as mock_sm_iu,
            patch("cctelegram.handlers.attention.session_manager") as mock_sm_att,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=pane_text)
            mock_sm_iu.resolve_chat_id.return_value = 100
            mock_sm_iu.window_states = {}
            mock_sm_att.resolve_chat_id.return_value = 100
            mock_sm_att.get_display_name.return_value = "topic"
            with caplog.at_level(
                _logging.INFO, logger="cctelegram.handlers.interactive_ui"
            ):
                await handle_interactive_ui(
                    mock_bot, user_id=1, window_id=window_id, thread_id=42
                )
        source_tag = _extract_gate_source_tag(caplog)
        assert source_tag == "form"

    @pytest.mark.asyncio
    async def test_gate_routes_dict_via_jsonl_when_both_present(
        self, mock_bot: AsyncMock, _pretool_gate_setup, caplog
    ):
        # JSONL cache present + pretool record present → JSONL wins
        # (it's authoritative and carries the JSONL tool_use_id used by
        # downstream dedup/upgrade paths).
        import logging as _logging

        from cctelegram.handlers import interactive_ui as iui

        window_id = "@h3"
        session_id = "550e8400-e29b-41d4-a716-446655440000"
        _bind_window(window_id, session_id)
        tool_input = {
            "questions": [
                {
                    "question": "Q",
                    "options": [{"label": "A"}, {"label": "B"}],
                }
            ]
        }
        # Prime BOTH:
        iui._last_completed_ask_tool_input[window_id] = tool_input
        iui._last_auq_tool_use_id[window_id] = "toolu_jsonl_id"
        _write_pretool_side_file(
            _pretool_gate_setup,
            session_id=session_id,
            questions=tool_input["questions"],
        )
        pane_text = _auq_pane_text(title="Q", labels=["A", "B"])

        mock_window = MagicMock()
        mock_window.window_id = window_id
        with (
            patch.object(iui, "tmux_manager") as mock_tmux,
            patch.object(iui, "session_manager") as mock_sm_iu,
            patch("cctelegram.handlers.attention.session_manager") as mock_sm_att,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=pane_text)
            mock_sm_iu.resolve_chat_id.return_value = 100
            mock_sm_iu.window_states = {}
            mock_sm_att.resolve_chat_id.return_value = 100
            mock_sm_att.get_display_name.return_value = "topic"
            with caplog.at_level(
                _logging.INFO, logger="cctelegram.handlers.interactive_ui"
            ):
                await handle_interactive_ui(
                    mock_bot, user_id=1, window_id=window_id, thread_id=42
                )
        source_tag = _extract_gate_source_tag(caplog)
        assert source_tag == "dict_via_jsonl"

    @pytest.mark.asyncio
    async def test_gate_dedup_key_uses_pretool_fingerprint_when_no_tool_use_id(
        self, mock_bot: AsyncMock, _pretool_gate_setup, caplog
    ):
        # The dedup key when routed via hook: "pretool:<tool_use_id>" if
        # the hook payload carried tool_use_id, else "pretool:<fp>".
        # Verify the fingerprint variant.
        import logging as _logging

        from cctelegram.handlers import interactive_ui as iui

        window_id = "@h4"
        session_id = "550e8400-e29b-41d4-a716-446655440000"
        _bind_window(window_id, session_id)
        # Write side file with empty tool_use_id so the gate falls back
        # to fingerprint-based dedup key.
        _write_pretool_side_file(
            _pretool_gate_setup,
            session_id=session_id,
            tool_use_id="",
            questions=[
                {
                    "question": "Q",
                    "options": [{"label": "A"}, {"label": "B"}],
                }
            ],
        )
        pane_text = _auq_pane_text(title="Q", labels=["A", "B"])

        mock_window = MagicMock()
        mock_window.window_id = window_id
        with (
            patch.object(iui, "tmux_manager") as mock_tmux,
            patch.object(iui, "session_manager") as mock_sm_iu,
            patch("cctelegram.handlers.attention.session_manager") as mock_sm_att,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=pane_text)
            mock_sm_iu.resolve_chat_id.return_value = 100
            mock_sm_iu.window_states = {}
            mock_sm_att.resolve_chat_id.return_value = 100
            mock_sm_att.get_display_name.return_value = "topic"
            with caplog.at_level(
                _logging.INFO, logger="cctelegram.handlers.interactive_ui"
            ):
                await handle_interactive_ui(
                    mock_bot, user_id=1, window_id=window_id, thread_id=42
                )

        # Find the dedup_key value in the gate-eval log line.
        for record in reversed(caplog.records):
            msg = record.getMessage()
            if "AUQ context gate eval" not in msg:
                continue
            assert "dedup_key=pretool:" in msg, (
                f"expected dedup_key=pretool:<fp>, got log: {msg}"
            )
            break
        else:
            pytest.fail("Did not find AUQ context gate eval log line")


# ── Cleanup paths (chunk 5) ───────────────────────────────────────────────


class TestPretoolCleanup:
    """Cleanup of AUQ PreToolUse side files via the lifecycle hooks."""

    def _bind(self, window_id: str, session_id: str) -> None:
        from cctelegram.session import WindowState, session_manager

        session_manager.window_states[window_id] = WindowState(
            cwd="/tmp/cwd", session_id=session_id
        )

    def _unbind(self, window_id: str) -> None:
        from cctelegram.session import session_manager

        session_manager.window_states.pop(window_id, None)

    def test_forget_ask_tool_input_unlinks_side_file_for_current_session(
        self, _cc_telegram_dir
    ):
        from cctelegram.handlers.interactive_ui import forget_ask_tool_input

        sid = "550e8400-e29b-41d4-a716-446655440000"
        self._bind("@cleanup1", sid)
        try:
            target = _write_pretool_side_file(_cc_telegram_dir, session_id=sid)
            assert target.exists()
            forget_ask_tool_input("@cleanup1")
            assert not target.exists()
        finally:
            self._unbind("@cleanup1")

    def test_forget_ask_tool_input_clears_pretool_cache(self, _cc_telegram_dir):
        from cctelegram.handlers.interactive_ui import (
            _pretool_ask_records,
            _resolve_pretool_record,
            forget_ask_tool_input,
        )

        sid = "550e8400-e29b-41d4-a716-446655440000"
        self._bind("@cleanup2", sid)
        try:
            _write_pretool_side_file(_cc_telegram_dir, session_id=sid)
            form = _make_form_single_question("Pick a fruit", ["Apple", "Banana"])
            _resolve_pretool_record("@cleanup2", form)
            assert "@cleanup2" in _pretool_ask_records
            forget_ask_tool_input("@cleanup2")
            assert "@cleanup2" not in _pretool_ask_records
        finally:
            self._unbind("@cleanup2")

    def test_unlink_for_session_handles_non_uuid_session_id(self, _cc_telegram_dir):
        # Defense: a corrupt session_id should not raise — the helper
        # is best-effort and silently no-ops on non-UUID input.
        from cctelegram.handlers.interactive_ui import (
            _unlink_pretool_side_file_for_session,
        )

        # Should not raise.
        _unlink_pretool_side_file_for_session("../etc/passwd")
        _unlink_pretool_side_file_for_session("")

    def test_unlink_for_session_silently_skips_missing_file(self, _cc_telegram_dir):
        # No file written yet — unlink is silent.
        from cctelegram.handlers.interactive_ui import (
            _unlink_pretool_side_file_for_session,
        )

        sid = "550e8400-e29b-41d4-a716-446655440000"
        _unlink_pretool_side_file_for_session(sid)
        # No file created either.
        assert not (_cc_telegram_dir / "auq_pending" / f"{sid}.json").exists()


class TestPretoolStartupGC:
    """Bot-startup garbage collection of stale side files."""

    def test_deletes_files_older_than_1h(self, _cc_telegram_dir):
        from cctelegram.handlers.interactive_ui import (
            _PRETOOL_GC_AGE_SECONDS,
            gc_stale_pretool_side_files,
        )

        old_sid = "11111111-1111-1111-1111-111111111111"
        fresh_sid = "22222222-2222-2222-2222-222222222222"
        old_file = _write_pretool_side_file(_cc_telegram_dir, session_id=old_sid)
        fresh_file = _write_pretool_side_file(_cc_telegram_dir, session_id=fresh_sid)
        # Backdate the old file's mtime past the GC cutoff.
        old_mtime = time.time() - _PRETOOL_GC_AGE_SECONDS - 60
        import os as _os

        _os.utime(old_file, (old_mtime, old_mtime))

        deleted = gc_stale_pretool_side_files()
        assert deleted == 1
        assert not old_file.exists()
        assert fresh_file.exists()

    def test_no_dir_no_action(self, tmp_path, monkeypatch):
        # GC must not crash if auq_pending/ doesn't exist yet.
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        from cctelegram.handlers.interactive_ui import gc_stale_pretool_side_files

        assert gc_stale_pretool_side_files() == 0

    def test_gc_skips_unlink_when_file_replaced_during_scan(
        self, _cc_telegram_dir, monkeypatch
    ):
        # Codex P2 (chunk 5): GC re-stats mtime right before unlink to
        # close the TOCTOU window. Simulate the race by patching the
        # second stat to return a fresh mtime — file must survive.
        from pathlib import Path as _PPath

        from cctelegram.handlers.interactive_ui import (
            _PRETOOL_GC_AGE_SECONDS,
            gc_stale_pretool_side_files,
        )

        sid = "11111111-1111-1111-1111-111111111111"
        target = _write_pretool_side_file(_cc_telegram_dir, session_id=sid)
        import os as _os

        old_mtime = time.time() - _PRETOOL_GC_AGE_SECONDS - 60
        _os.utime(target, (old_mtime, old_mtime))

        real_stat = _PPath.stat
        call_count = {"n": 0}

        def flipping_stat(self, *args, **kwargs):
            res = real_stat(self, *args, **kwargs)
            if self.name == f"{sid}.json":
                call_count["n"] += 1
                if call_count["n"] >= 2:
                    # Second stat (re-check) sees a fresh mtime —
                    # GC must back off and leave the file.
                    return os.stat_result(
                        (
                            res.st_mode,
                            res.st_ino,
                            res.st_dev,
                            res.st_nlink,
                            res.st_uid,
                            res.st_gid,
                            res.st_size,
                            res.st_atime,
                            time.time(),  # fresh mtime
                            res.st_ctime,
                        )
                    )
            return res

        import os

        monkeypatch.setattr(_PPath, "stat", flipping_stat)
        deleted = gc_stale_pretool_side_files()
        assert deleted == 0
        assert target.exists()

    def test_ignores_non_uuid_filenames(self, _cc_telegram_dir):
        # An entry that doesn't match <uuid>.json (e.g. a leftover
        # temp file) is left alone, even if older than the cutoff.
        from cctelegram.handlers.interactive_ui import (
            _PRETOOL_GC_AGE_SECONDS,
            gc_stale_pretool_side_files,
        )

        pending_dir = _cc_telegram_dir / "auq_pending"
        pending_dir.mkdir(exist_ok=True)
        non_uuid = pending_dir / "not-a-uuid.json"
        non_uuid.write_text("{}")
        old_mtime = time.time() - _PRETOOL_GC_AGE_SECONDS - 60
        import os as _os

        _os.utime(non_uuid, (old_mtime, old_mtime))

        gc_stale_pretool_side_files()
        # Non-UUID file untouched.
        assert non_uuid.exists()


class TestPretoolMissingHookWarning:
    """Bot-startup warning when PreToolUse hook entry is missing."""

    def test_warns_when_settings_file_missing(self, tmp_path, caplog):
        import logging as _logging

        from cctelegram.handlers.interactive_ui import (
            warn_if_pre_tool_use_hook_missing,
        )

        with caplog.at_level(
            _logging.WARNING, logger="cctelegram.handlers.interactive_ui"
        ):
            warned = warn_if_pre_tool_use_hook_missing(
                tmp_path / "missing-settings.json"
            )
        assert warned is True
        assert any(
            "cc-telegram hook --install" in r.getMessage() for r in caplog.records
        )

    def test_warns_when_pretool_entry_missing(self, tmp_path, caplog):
        import logging as _logging

        from cctelegram.handlers.interactive_ui import (
            warn_if_pre_tool_use_hook_missing,
        )

        settings = {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "cc-telegram hook"}]}
                ]
            }
        }
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(_json.dumps(settings))
        with caplog.at_level(
            _logging.WARNING, logger="cctelegram.handlers.interactive_ui"
        ):
            warned = warn_if_pre_tool_use_hook_missing(settings_file)
        assert warned is True
        assert any(
            "cc-telegram hook --install" in r.getMessage() for r in caplog.records
        )

    def test_no_warn_when_pretool_entry_present(self, tmp_path, caplog):
        import logging as _logging

        from cctelegram.handlers.interactive_ui import (
            warn_if_pre_tool_use_hook_missing,
        )

        settings = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "AskUserQuestion",
                        "hooks": [{"type": "command", "command": "cc-telegram hook"}],
                    }
                ]
            }
        }
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(_json.dumps(settings))
        with caplog.at_level(
            _logging.WARNING, logger="cctelegram.handlers.interactive_ui"
        ):
            warned = warn_if_pre_tool_use_hook_missing(settings_file)
        assert warned is False


class TestSessionMonitorClearRace:
    """Verify the /clear cleanup unlinks the OLD session's side file
    (codex R2 finding: forget_ask_tool_input runs after session_id swap
    and would otherwise miss the file)."""

    def test_session_change_unlinks_old_side_file(self, _cc_telegram_dir):
        # Simulate the cleanup path directly. session_monitor calls
        # _unlink_pretool_side_file_for_session(old_sid) BEFORE
        # forget_ask_tool_input(window_id) so the OLD session's file
        # gets cleaned even though the window's WindowState now points
        # to the new session.
        from cctelegram.handlers.interactive_ui import (
            _unlink_pretool_side_file_for_session,
        )

        old_sid = "11111111-1111-1111-1111-111111111111"
        new_sid = "22222222-2222-2222-2222-222222222222"
        old_file = _write_pretool_side_file(_cc_telegram_dir, session_id=old_sid)
        # Bind window to NEW session (simulating /clear swap).
        from cctelegram.session import WindowState, session_manager

        session_manager.window_states["@race1"] = WindowState(
            cwd="/tmp/cwd", session_id=new_sid
        )
        try:
            assert old_file.exists()
            _unlink_pretool_side_file_for_session(old_sid)
            assert not old_file.exists()
        finally:
            session_manager.window_states.pop("@race1", None)


class TestGateDedupAcrossPretoolToJsonl:
    """Codex chunk-3+4 P2 delta: directly verify the gate's dedup-key
    transition. A `pretool:<fp>` claim must block a subsequent JSONL
    `tool_use_id` claim — no duplicate post."""

    def test_pretool_claim_blocks_subsequent_jsonl_claim(self, _cc_telegram_dir):
        from cctelegram.handlers.interactive_ui import (
            _auq_context_posted,
            claim_auq_context_post,
        )

        window_id = "@dedup1"
        _auq_context_posted.pop(window_id, None)
        # First: pretool claim with fingerprint-based key.
        assert claim_auq_context_post(window_id, "pretool:abc123") is True
        # Second: JSONL tool_use_id arrives (different key) → blocked.
        assert claim_auq_context_post(window_id, "toolu_jsonl_id") is False
        _auq_context_posted.pop(window_id, None)
