"""Tests for response_builder.build_response_parts."""

from cctelegram.handlers.response_builder import build_response_parts
from cctelegram.transcript_parser import TranscriptParser

EXP_START = TranscriptParser.EXPANDABLE_QUOTE_START
EXP_END = TranscriptParser.EXPANDABLE_QUOTE_END


class TestBuildResponseParts:
    def test_user_message_has_emoji_prefix(self):
        parts = build_response_parts("hello", role="user")
        assert len(parts) == 1
        assert "\U0001f464" in parts[0]

    def test_user_message_truncated_at_3000_chars(self):
        long_text = "a" * 4000
        parts = build_response_parts(long_text, role="user")
        assert len(parts) == 1
        short_parts = build_response_parts("b" * 100, role="user")
        assert len(parts[0]) < len(long_text)
        assert len(short_parts[0]) < len(parts[0])

    def test_thinking_content_truncated_at_500_chars(self):
        inner = "x" * 800
        text = f"{EXP_START}{inner}{EXP_END}"
        parts = build_response_parts(text, content_type="thinking")
        assert len(parts) == 1
        assert "truncated" in parts[0].lower()

    def test_plain_text_single_part(self):
        parts = build_response_parts("short text")
        assert len(parts) == 1

    def test_plain_text_multi_part_has_page_suffix(self):
        long_text = "\n".join(f"line {i} " + "padding" * 50 for i in range(200))
        parts = build_response_parts(long_text)
        assert len(parts) > 1
        assert "1/" in parts[0]

    def test_expandable_quote_stays_atomic(self):
        inner = "thought " * 100
        text = f"{EXP_START}{inner}{EXP_END}"
        parts = build_response_parts(text, content_type="thinking")
        assert len(parts) == 1

    def test_thinking_has_prefix(self):
        parts = build_response_parts("some thought", content_type="thinking")
        assert len(parts) == 1
        assert "Thinking" in parts[0]

    def test_assistant_text_no_prefix(self):
        parts = build_response_parts(
            "hello world", content_type="text", role="assistant"
        )
        assert len(parts) == 1
        assert "\U0001f464" not in parts[0]
        assert "Thinking" not in parts[0]


class TestTaskNotification:
    def test_full_envelope_renders_as_card(self):
        text = (
            "<task-notification>"
            "<task-id>bfxtsefjq</task-id>"
            '<summary>Monitor event: "Stream Phase 0.5"</summary>'
            "<event>2026-05-03 08:24:24 [info] phase0_skipped doc=foo.pdf</event>"
            "</task-notification>"
        )
        parts = build_response_parts(text, role="user")
        assert len(parts) == 1
        out = parts[0]
        # No raw 👤 user-echo prefix
        assert "\U0001f464" not in out
        # Header with task id and bell icon
        assert "🔔" in out
        assert "bfxtsefjq" in out
        assert 'Monitor event: "Stream Phase 0.5"' in out
        # Event body wrapped in expandable quote sentinels
        assert EXP_START in out and EXP_END in out
        assert "phase0_skipped" in out
        # XML tags should not survive
        assert "<task-id>" not in out
        assert "<event>" not in out
        assert "</task-notification>" not in out

    def test_multiple_events_joined(self):
        text = (
            "<task-notification>"
            "<summary>x</summary>"
            "<event>line one</event>"
            "<event>line two</event>"
            "</task-notification>"
        )
        parts = build_response_parts(text, role="user")
        assert len(parts) == 1
        assert "line one" in parts[0]
        assert "line two" in parts[0]

    def test_summary_only_no_events_block(self):
        text = "<task-notification><summary>only summary</summary></task-notification>"
        parts = build_response_parts(text, role="user")
        assert len(parts) == 1
        assert "only summary" in parts[0]
        assert EXP_START not in parts[0]

    def test_empty_envelope_falls_back(self):
        text = "<task-notification></task-notification>"
        parts = build_response_parts(text, role="user")
        # No recognizable fields → falls back to user-message rendering
        assert "\U0001f464" in parts[0]

    def test_assistant_role_not_intercepted(self):
        # Assistant text mentioning the tag literally must not be hijacked.
        text = "<task-notification><summary>x</summary></task-notification>"
        parts = build_response_parts(text, role="assistant")
        assert "🔔" not in parts[0]

    def test_non_envelope_user_text_unchanged(self):
        parts = build_response_parts("just a normal message", role="user")
        assert "\U0001f464" in parts[0]
        assert "🔔" not in parts[0]

    def test_envelope_with_surrounding_whitespace(self):
        text = (
            "\n  <task-notification>"
            "<task-id>abc</task-id>"
            "<summary>s</summary>"
            "</task-notification>  \n"
        )
        parts = build_response_parts(text, role="user")
        assert "abc" in parts[0]
        assert "🔔" in parts[0]
