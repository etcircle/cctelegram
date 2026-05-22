"""Tests for cctelegram.transcript_parser — pure logic, no I/O."""

import json
from pathlib import Path

import pytest

from cctelegram.transcript_parser import (
    ParsedMessage,
    TranscriptParser,
    clear_usage_cache,
    read_latest_usage,
)

EXPQUOTE_START = TranscriptParser.EXPANDABLE_QUOTE_START
EXPQUOTE_END = TranscriptParser.EXPANDABLE_QUOTE_END


# ── parse_line ───────────────────────────────────────────────────────────


class TestParseLine:
    @pytest.mark.parametrize(
        "line, expected",
        [
            ('{"type": "user"}', {"type": "user"}),
            ("not-json", None),
            ("", None),
            ("   \t  ", None),
        ],
        ids=["valid_json", "invalid_json", "empty", "whitespace"],
    )
    def test_parse_line(self, line: str, expected: dict | None):
        assert TranscriptParser.parse_line(line) == expected


# ── extract_text_only ────────────────────────────────────────────────────


class TestExtractTextOnly:
    @pytest.mark.parametrize(
        "content, expected",
        [
            ("plain string", "plain string"),
            (
                [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}],
                "hello\nworld",
            ),
            (
                [
                    {"type": "text", "text": "keep"},
                    {"type": "tool_use", "name": "Read"},
                ],
                "keep",
            ),
            ([], ""),
            (42, ""),
        ],
        ids=["string", "text_blocks", "mixed", "empty_list", "non_list_non_string"],
    )
    def test_extract_text_only(self, content: list | str | int, expected: str):
        assert TranscriptParser.extract_text_only(content) == expected


# ── format_tool_use_summary ──────────────────────────────────────────────


class TestFormatToolUseSummary:
    @pytest.mark.parametrize(
        "name, input_data, expected",
        [
            ("Read", {"file_path": "src/main.py"}, "**Read**(src/main.py)"),
            ("Write", {"file_path": "out.txt"}, "**Write**(out.txt)"),
            ("Bash", {"command": "ls -la"}, "**Bash**(ls -la)"),
            ("Grep", {"pattern": "TODO"}, "**Grep**(TODO)"),
            ("Glob", {"pattern": "*.py"}, "**Glob**(*.py)"),
            ("Task", {"description": "analyze code"}, "**Task**(analyze code)"),
            (
                "WebFetch",
                {"url": "https://example.com"},
                "**WebFetch**(https://example.com)",
            ),
            ("WebSearch", {"query": "python async"}, "**WebSearch**(python async)"),
            ("TodoWrite", {"todos": [1, 2, 3]}, "**TodoWrite**(3 item(s))"),
            ("TodoRead", {}, "**TodoRead**"),
            (
                "AskUserQuestion",
                {"questions": [{"question": "Continue?"}]},
                "**AskUserQuestion**(Continue?)",
            ),
            ("ExitPlanMode", {}, "**ExitPlanMode**"),
            ("Skill", {"skill": "code-review"}, "**Skill**(code-review)"),
            (
                "CustomTool",
                {"first_key": "value1"},
                "**CustomTool**(value1)",
            ),
        ],
        ids=[
            "Read",
            "Write",
            "Bash",
            "Grep",
            "Glob",
            "Task",
            "WebFetch",
            "WebSearch",
            "TodoWrite",
            "TodoRead",
            "AskUserQuestion",
            "ExitPlanMode",
            "Skill",
            "unknown_tool",
        ],
    )
    def test_tool_summary(self, name: str, input_data: dict, expected: str):
        assert TranscriptParser.format_tool_use_summary(name, input_data) == expected

    def test_non_dict_input(self):
        assert (
            TranscriptParser.format_tool_use_summary("Read", "not a dict") == "**Read**"
        )

    def test_truncation_at_max_summary_length(self):
        from cctelegram.config import config

        cap = config.tool_summary_max_chars
        long_value = "x" * (cap + 50)
        result = TranscriptParser.format_tool_use_summary(
            "Bash", {"command": long_value}
        )
        assert len(long_value) > cap
        assert result == f"**Bash**({'x' * cap}…)"


# ── extract_tool_result_text ─────────────────────────────────────────────


class TestExtractToolResultText:
    @pytest.mark.parametrize(
        "content, expected",
        [
            ("raw string", "raw string"),
            (
                [{"type": "text", "text": "line1"}, {"type": "text", "text": "line2"}],
                "line1\nline2",
            ),
            (
                [{"type": "text", "text": "keep"}, {"type": "image", "data": "..."}],
                "keep",
            ),
            (None, ""),
        ],
        ids=["string", "text_blocks", "mixed", "none"],
    )
    def test_extract_tool_result_text(self, content: str | list | None, expected: str):
        assert TranscriptParser.extract_tool_result_text(content) == expected


# ── parse_message ────────────────────────────────────────────────────────


class TestParseMessage:
    def test_user_text(self):
        data = {
            "type": "user",
            "message": {"content": [{"type": "text", "text": "hello"}]},
        }
        result = TranscriptParser.parse_message(data)
        assert result == ParsedMessage(message_type="user", text="hello")

    def test_assistant_text(self):
        data = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "hi there"}]},
        }
        result = TranscriptParser.parse_message(data)
        assert result == ParsedMessage(message_type="assistant", text="hi there")

    def test_local_command_with_stdout(self):
        data = {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "<command-name>/help</command-name>"
                            "<local-command-stdout>Available commands</local-command-stdout>"
                        ),
                    }
                ]
            },
        }
        result = TranscriptParser.parse_message(data)
        assert result is not None
        assert result.message_type == "local_command"
        assert result.text == "Available commands"
        assert result.tool_name == "/help"

    def test_local_command_invoke(self):
        data = {
            "type": "user",
            "message": {
                "content": [
                    {"type": "text", "text": "<command-name>/clear</command-name>"}
                ]
            },
        }
        result = TranscriptParser.parse_message(data)
        assert result is not None
        assert result.message_type == "local_command_invoke"
        assert result.text == ""
        assert result.tool_name == "/clear"

    def test_non_user_assistant_returns_none(self):
        data = {
            "type": "summary",
            "message": {"content": "summary text"},
        }
        assert TranscriptParser.parse_message(data) is None

    def test_string_content(self):
        data = {
            "type": "assistant",
            "message": {"content": "plain response"},
        }
        result = TranscriptParser.parse_message(data)
        assert result == ParsedMessage(message_type="assistant", text="plain response")


# ── _format_edit_diff ────────────────────────────────────────────────────


class TestFormatEditDiff:
    @pytest.mark.parametrize(
        "old, new, check",
        [
            (
                "hello",
                "world",
                lambda r: "-hello" in r and "+world" in r,
            ),
            (
                "line1\nline2\nline3",
                "line1\nchanged\nline3",
                lambda r: "-line2" in r and "+changed" in r,
            ),
            (
                "same",
                "same",
                lambda r: r == "",
            ),
        ],
        ids=["single_line", "multi_line", "identical"],
    )
    def test_format_edit_diff(self, old: str, new: str, check):
        result = TranscriptParser._format_edit_diff(old, new)
        assert check(result), f"Check failed for ({old!r}, {new!r}): {result!r}"


# ── _format_tool_result_text ─────────────────────────────────────────────


class TestFormatToolResultText:
    @pytest.mark.parametrize(
        "text, tool_name, check",
        [
            (
                "line1\nline2\nline3",
                "Read",
                lambda r: r == "  ⎿  Read 3 lines",
            ),
            (
                "line1\nline2",
                "Write",
                lambda r: r == "  ⎿  Wrote 2 lines",
            ),
            (
                "output line",
                "Bash",
                lambda r: (
                    r.startswith("  ⎿  Output 1 lines")
                    and EXPQUOTE_START in r
                    and EXPQUOTE_END in r
                ),
            ),
            (
                "file1.py\nfile2.py\n",
                "Grep",
                lambda r: "Found 2 matches" in r and EXPQUOTE_START in r,
            ),
            (
                "a.py\nb.py\nc.py",
                "Glob",
                lambda r: "Found 3 files" in r and EXPQUOTE_START in r,
            ),
            (
                "agent says hello",
                "Task",
                lambda r: "Agent output 1 lines" in r and EXPQUOTE_START in r,
            ),
            (
                "page content here",
                "WebFetch",
                lambda r: (
                    f"Fetched {len('page content here')} characters" in r
                    and EXPQUOTE_START in r
                ),
            ),
            (
                "",
                "Read",
                lambda r: r == "",
            ),
        ],
        ids=["Read", "Write", "Bash", "Grep", "Glob", "Task", "WebFetch", "empty"],
    )
    def test_format_tool_result_text(self, text: str, tool_name: str, check):
        result = TranscriptParser._format_tool_result_text(text, tool_name)
        assert check(result), f"Failed check for {tool_name!r}: {result!r}"

    # The Write branch resolves its line count from ``tool_input_data["content"]``
    # at runtime (the tool result is only "File created successfully at:…").
    # In replayed history and in unit tests there is no input_data, so the
    # branch falls back to the result text. These two tests pin both code
    # paths so a future "simplification" cannot silently regress to "Wrote 0
    # lines" for replayed history.

    def test_write_uses_tool_input_data_when_present(self):
        result = TranscriptParser._format_tool_result_text(
            text="File created successfully at: /tmp/x.py",
            tool_name="Write",
            tool_input_data={"content": "a\nb\nc"},
        )
        assert result == "  ⎿  Wrote 3 lines"

    def test_write_falls_back_to_result_text_without_input_data(self):
        result = TranscriptParser._format_tool_result_text(
            text="line1\nline2",
            tool_name="Write",
            tool_input_data=None,
        )
        assert result == "  ⎿  Wrote 2 lines"


# ── parse_entries ────────────────────────────────────────────────────────


class TestParseEntries:
    def test_assistant_text(self, make_jsonl_entry, make_text_block):
        entries = [make_jsonl_entry("assistant", [make_text_block("Hello!")])]
        result, pending = TranscriptParser.parse_entries(entries)
        assert len(result) == 1
        assert result[0].role == "assistant"
        assert result[0].text == "Hello!"
        assert result[0].content_type == "text"

    def test_user_text(self, make_jsonl_entry, make_text_block):
        entries = [make_jsonl_entry("user", [make_text_block("Hi bot")])]
        result, pending = TranscriptParser.parse_entries(entries)
        assert len(result) == 1
        assert result[0].role == "user"
        assert result[0].text == "Hi bot"

    def test_tool_use_and_result_pairing(
        self,
        make_jsonl_entry,
        make_text_block,
        make_tool_use_block,
        make_tool_result_block,
    ):
        entries = [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t1", "Read", {"file_path": "app.py"})],
            ),
            make_jsonl_entry(
                "user",
                [make_tool_result_block("t1", "file contents line1\nline2\nline3")],
            ),
        ]
        result, pending = TranscriptParser.parse_entries(entries)
        tool_use_entries = [e for e in result if e.content_type == "tool_use"]
        tool_result_entries = [e for e in result if e.content_type == "tool_result"]
        assert len(tool_use_entries) == 1
        assert tool_use_entries[0].tool_use_id == "t1"
        assert "**Read**" in tool_use_entries[0].text
        assert len(tool_result_entries) == 1
        assert tool_result_entries[0].tool_use_id == "t1"
        assert not pending

    def test_tool_result_carries_tool_name_from_pending_tools(
        self,
        make_jsonl_entry,
        make_tool_use_block,
        make_tool_result_block,
    ):
        """Regression — codex P1 (2026-05-21): ``tool_result`` ParsedEntry
        must carry ``tool_name`` so downstream callers
        (``bot.handle_new_message`` AUQ cache invalidation, future per-tool
        result classifiers) can identify which tool resolved without
        maintaining a separate id set. Pre-fix this field defaulted to None
        on every tool_result entry, and the bug-#2 AUQ cache leak fix
        silently no-op'd in production.

        AskUserQuestion is the load-bearing case: bug #2 ships an
        invalidation branch in ``bot.py`` keyed on
        ``msg.tool_name == "AskUserQuestion" and msg.content_type ==
        "tool_result"``. Without this propagation that branch never fires.
        """
        entries = [
            make_jsonl_entry(
                "assistant",
                [
                    make_tool_use_block(
                        "t-auq-1",
                        "AskUserQuestion",
                        {
                            "questions": [
                                {
                                    "question": "Pick one",
                                    "header": "Decide",
                                    "options": [{"label": "A"}, {"label": "B"}],
                                }
                            ]
                        },
                    )
                ],
            ),
            make_jsonl_entry(
                "user",
                [
                    make_tool_result_block(
                        "t-auq-1", "Your questions have been answered."
                    )
                ],
            ),
        ]
        result, pending = TranscriptParser.parse_entries(entries)
        tool_use_entries = [e for e in result if e.content_type == "tool_use"]
        tool_result_entries = [e for e in result if e.content_type == "tool_result"]
        assert len(tool_use_entries) == 1
        assert tool_use_entries[0].tool_name == "AskUserQuestion"
        assert len(tool_result_entries) == 1
        assert tool_result_entries[0].tool_use_id == "t-auq-1"
        assert tool_result_entries[0].tool_name == "AskUserQuestion", (
            "tool_result must inherit tool_name from the matching pending "
            "tool_use; without this the bot's AUQ cache invalidation branch "
            "(keyed on msg.tool_name) never fires in production and the "
            "cache leak from 2026-05-21 09:30:21 recurs"
        )

    def test_thinking_block(self, make_jsonl_entry, make_thinking_block):
        entries = [
            make_jsonl_entry("assistant", [make_thinking_block("reasoning here")])
        ]
        result, pending = TranscriptParser.parse_entries(entries)
        assert len(result) == 1
        assert result[0].content_type == "thinking"
        assert EXPQUOTE_START in result[0].text
        assert EXPQUOTE_END in result[0].text
        assert "reasoning here" in result[0].text

    def test_local_command_with_stdout(self, make_jsonl_entry, make_text_block):
        xml = (
            "<command-name>/status</command-name>"
            "<local-command-stdout>all good</local-command-stdout>"
        )
        entries = [make_jsonl_entry("user", [make_text_block(xml)])]
        result, pending = TranscriptParser.parse_entries(entries)
        assert len(result) == 1
        assert result[0].content_type == "local_command"
        assert "/status" in result[0].text
        assert "all good" in result[0].text

    def test_exit_plan_mode_emits_plan(self, make_jsonl_entry, make_tool_use_block):
        block = make_tool_use_block(
            "t1", "ExitPlanMode", {"plan": "Step 1: do X\nStep 2: do Y"}
        )
        entries = [make_jsonl_entry("assistant", [block])]
        result, pending = TranscriptParser.parse_entries(entries)
        texts = [e for e in result if e.content_type == "text"]
        tool_uses = [e for e in result if e.content_type == "tool_use"]
        assert len(texts) == 1
        assert "Step 1: do X" in texts[0].text
        assert len(tool_uses) >= 1

    def test_edit_tool_diff_stats(
        self,
        make_jsonl_entry,
        make_tool_use_block,
        make_tool_result_block,
    ):
        edit_input = {
            "file_path": "main.py",
            "old_string": "old line",
            "new_string": "new line",
        }
        entries = [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t1", "Edit", edit_input)],
            ),
            make_jsonl_entry(
                "user",
                [make_tool_result_block("t1", "OK")],
            ),
        ]
        result, pending = TranscriptParser.parse_entries(entries)
        tool_result_entries = [e for e in result if e.content_type == "tool_result"]
        assert len(tool_result_entries) == 1
        tr = tool_result_entries[0]
        assert "Added" in tr.text
        assert "removed" in tr.text
        assert EXPQUOTE_START in tr.text

    def test_error_tool_result(
        self,
        make_jsonl_entry,
        make_tool_use_block,
        make_tool_result_block,
    ):
        entries = [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t1", "Bash", {"command": "rm -rf /"})],
            ),
            make_jsonl_entry(
                "user",
                [make_tool_result_block("t1", "Permission denied", is_error=True)],
            ),
        ]
        result, pending = TranscriptParser.parse_entries(entries)
        tool_result_entries = [e for e in result if e.content_type == "tool_result"]
        assert len(tool_result_entries) == 1
        assert "Error: Permission denied" in tool_result_entries[0].text

    def test_interrupted_tool_result(
        self,
        make_jsonl_entry,
        make_tool_use_block,
        make_tool_result_block,
    ):
        entries = [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t1", "Read", {"file_path": "x.py"})],
            ),
            make_jsonl_entry(
                "user",
                [make_tool_result_block("t1", TranscriptParser._INTERRUPTED_TEXT)],
            ),
        ]
        result, pending = TranscriptParser.parse_entries(entries)
        tool_result_entries = [e for e in result if e.content_type == "tool_result"]
        assert len(tool_result_entries) == 1
        assert "Interrupted" in tool_result_entries[0].text

    def test_pending_tools_carry_over(self, make_jsonl_entry, make_tool_use_block):
        entries = [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t1", "Read", {"file_path": "a.py"})],
            ),
        ]
        result, pending = TranscriptParser.parse_entries(entries, pending_tools={})
        assert "t1" in pending
        flushed = [
            e for e in result if e.content_type == "tool_use" and e.tool_use_id == "t1"
        ]
        assert len(flushed) == 1

    def test_pending_tools_flushed_without_carry_over(
        self, make_jsonl_entry, make_tool_use_block
    ):
        entries = [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t1", "Read", {"file_path": "a.py"})],
            ),
        ]
        result, pending = TranscriptParser.parse_entries(entries, pending_tools=None)
        tool_entries = [e for e in result if e.tool_use_id == "t1"]
        assert len(tool_entries) == 2
        assert tool_entries[0].content_type == "tool_use"
        assert tool_entries[1].content_type == "tool_use"

    def test_system_tag_filtered(self, make_jsonl_entry, make_text_block):
        entries = [
            make_jsonl_entry(
                "user",
                [
                    make_text_block(
                        "<system-reminder>secret instructions</system-reminder>"
                    )
                ],
            ),
        ]
        result, pending = TranscriptParser.parse_entries(entries)
        user_entries = [e for e in result if e.role == "user"]
        assert len(user_entries) == 0


# ── stop_reason propagation ──────────────────────────────────────────────


class TestStopReasonPropagation:
    """Verify message.stop_reason is plumbed onto every assistant entry."""

    def _entry_with_stop_reason(
        self, msg_type: str, content: list, stop_reason: str | None
    ) -> dict:
        entry: dict = {
            "type": msg_type,
            "message": {"content": content},
            "sessionId": "sid",
            "cwd": "/tmp",
            "timestamp": "2026-05-02T00:00:00.000Z",
        }
        if stop_reason is not None:
            entry["message"]["stop_reason"] = stop_reason
        return entry

    def test_assistant_text_carries_stop_reason(self, make_text_block):
        entries = [
            self._entry_with_stop_reason(
                "assistant", [make_text_block("done!")], "end_turn"
            ),
        ]
        result, _ = TranscriptParser.parse_entries(entries)
        assert len(result) == 1
        assert result[0].stop_reason == "end_turn"

    def test_assistant_thinking_with_tool_use_carries_tool_use_stop_reason(
        self, make_thinking_block, make_tool_use_block
    ):
        entries = [
            self._entry_with_stop_reason(
                "assistant",
                [
                    make_thinking_block("reasoning"),
                    make_tool_use_block("t1", "Read", {"file_path": "x.py"}),
                ],
                "tool_use",
            ),
        ]
        # Use carry-over mode to avoid the end-of-loop flush re-emitting
        # an entry without stop_reason context.
        result, _ = TranscriptParser.parse_entries(entries, pending_tools={})
        thinking = [e for e in result if e.content_type == "thinking"]
        tool_uses = [e for e in result if e.content_type == "tool_use"]
        assert len(thinking) == 1
        assert thinking[0].stop_reason == "tool_use"
        assert len(tool_uses) == 1
        assert tool_uses[0].stop_reason == "tool_use"

    def test_assistant_no_stop_reason_field_stays_none(self, make_text_block):
        entries = [
            self._entry_with_stop_reason("assistant", [make_text_block("hi")], None),
        ]
        result, _ = TranscriptParser.parse_entries(entries)
        assert len(result) == 1
        assert result[0].stop_reason is None

    def test_user_text_has_no_stop_reason(self, make_text_block):
        entries = [
            self._entry_with_stop_reason("user", [make_text_block("hi bot")], None),
        ]
        result, _ = TranscriptParser.parse_entries(entries)
        user_entries = [e for e in result if e.role == "user"]
        assert len(user_entries) == 1
        assert user_entries[0].stop_reason is None

    def test_tool_result_user_message_entries_have_no_stop_reason(
        self, make_tool_use_block, make_tool_result_block
    ):
        # tool_result lives in user-role messages, which never carry
        # stop_reason in JSONL. The recomputation rejected by the plan
        # would have set is_complete=False here; the field should stay
        # None instead.
        entries = [
            self._entry_with_stop_reason(
                "assistant",
                [make_tool_use_block("t1", "Read", {"file_path": "x.py"})],
                "tool_use",
            ),
            self._entry_with_stop_reason(
                "user", [make_tool_result_block("t1", "ok")], None
            ),
        ]
        result, _ = TranscriptParser.parse_entries(entries)
        tool_results = [e for e in result if e.content_type == "tool_result"]
        assert len(tool_results) == 1
        assert tool_results[0].stop_reason is None
        tool_uses = [e for e in result if e.content_type == "tool_use"]
        assert tool_uses[0].stop_reason == "tool_use"


class TestUuidPropagation:
    """Verify the JSONL entry-level uuid is plumbed onto every ParsedEntry."""

    def test_uuid_round_trips_on_assistant_text(
        self, make_jsonl_entry, make_text_block
    ):
        entry = make_jsonl_entry("assistant", [make_text_block("hello")])
        entry["uuid"] = "abc-123"
        result, _ = TranscriptParser.parse_entries([entry])
        assert len(result) == 1
        assert result[0].uuid == "abc-123"

    def test_uuid_round_trips_on_user_tool_result(
        self, make_jsonl_entry, make_tool_use_block, make_tool_result_block
    ):
        assistant = make_jsonl_entry(
            "assistant", [make_tool_use_block("t1", "Read", {"file_path": "x.py"})]
        )
        assistant["uuid"] = "assistant-uuid"
        user = make_jsonl_entry("user", [make_tool_result_block("t1", "ok")])
        user["uuid"] = "user-uuid"
        result, _ = TranscriptParser.parse_entries([assistant, user])
        tool_results = [e for e in result if e.content_type == "tool_result"]
        assert len(tool_results) == 1
        assert tool_results[0].uuid == "user-uuid"

    def test_uuid_shared_across_multi_block_message(
        self, make_jsonl_entry, make_thinking_block, make_tool_use_block
    ):
        entry = make_jsonl_entry(
            "assistant",
            [
                make_thinking_block("reasoning"),
                make_tool_use_block("t1", "Read", {"file_path": "x.py"}),
            ],
        )
        entry["uuid"] = "shared-uuid"
        result, _ = TranscriptParser.parse_entries([entry], pending_tools={})
        assert len(result) >= 2
        assert all(e.uuid == "shared-uuid" for e in result)

    def test_uuid_missing_yields_none(self, make_jsonl_entry, make_text_block):
        entry = make_jsonl_entry("assistant", [make_text_block("hello")])
        entry.pop("uuid", None)
        result, _ = TranscriptParser.parse_entries([entry])
        assert len(result) == 1
        assert result[0].uuid is None


# ── read_latest_usage ────────────────────────────────────────────────────


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def _assistant(
    tokens_input: int, tokens_cache: int, model: str = "claude-opus-4-7"
) -> dict:
    return {
        "type": "assistant",
        "message": {
            "model": model,
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": tokens_input,
                "cache_read_input_tokens": tokens_cache,
                "cache_creation_input_tokens": 0,
                "output_tokens": 100,
            },
            "content": [{"type": "text", "text": "ok"}],
        },
    }


class TestReadLatestUsage:
    def setup_method(self):
        clear_usage_cache()

    def test_reads_latest_assistant_usage(self, tmp_path: Path):
        f = tmp_path / "s.jsonl"
        _write_jsonl(
            f,
            [
                _assistant(10, 1000),
                _assistant(20, 113000),
            ],
        )
        usage = read_latest_usage(str(f))
        assert usage is not None
        assert usage.tokens == 20 + 113000
        assert usage.model == "claude-opus-4-7"

    def test_skips_sidechain(self, tmp_path: Path):
        f = tmp_path / "s.jsonl"
        entries = [
            _assistant(1, 100),
            {**_assistant(99, 99999), "isSidechain": True},
        ]
        _write_jsonl(f, entries)
        usage = read_latest_usage(str(f))
        assert usage is not None
        assert usage.tokens == 1 + 100  # picked the non-sidechain entry

    def test_skips_user_entries(self, tmp_path: Path):
        f = tmp_path / "s.jsonl"
        _write_jsonl(
            f,
            [
                _assistant(5, 500),
                {"type": "user", "message": {"content": []}},
            ],
        )
        usage = read_latest_usage(str(f))
        assert usage is not None
        assert usage.tokens == 5 + 500

    def test_returns_none_on_no_assistant(self, tmp_path: Path):
        f = tmp_path / "s.jsonl"
        _write_jsonl(f, [{"type": "user", "message": {"content": []}}])
        assert read_latest_usage(str(f)) is None

    def test_returns_none_on_missing_file(self, tmp_path: Path):
        assert read_latest_usage(str(tmp_path / "nonexistent.jsonl")) is None

    def test_returns_none_on_malformed_lines(self, tmp_path: Path):
        f = tmp_path / "s.jsonl"
        f.write_text("not json\n{}\nbroken{\n")
        assert read_latest_usage(str(f)) is None

    def test_caches_by_mtime_and_size(self, tmp_path: Path):
        f = tmp_path / "s.jsonl"
        _write_jsonl(f, [_assistant(5, 500)])
        first = read_latest_usage(str(f))
        # Same mtime AND same size → cache hit even if content magically
        # differs (we simulate this by overwriting with same-length garbage
        # and pinning mtime). This pins the cache-key contract.
        import os

        st = f.stat()
        f.write_bytes(b"x" * st.st_size)
        os.utime(f, (st.st_atime, st.st_mtime))
        second = read_latest_usage(str(f))
        assert second == first  # cache hit

    def test_size_change_busts_cache(self, tmp_path: Path):
        f = tmp_path / "s.jsonl"
        _write_jsonl(f, [_assistant(5, 500)])
        first = read_latest_usage(str(f))
        # Append a new entry with different tokens; same-second mtime but
        # bigger size should still bust the cache.
        import os

        st_before = f.stat()
        with open(f, "a") as fp:
            fp.write(json.dumps(_assistant(7, 700)) + "\n")
        os.utime(f, (st_before.st_atime, st_before.st_mtime))
        second = read_latest_usage(str(f))
        assert second is not None and first is not None
        assert second.tokens == 7 + 700
        assert first.tokens == 5 + 500

    def test_skips_zero_token_entries(self, tmp_path: Path):
        f = tmp_path / "s.jsonl"
        # Latest entry has zero tokens — skip back to a usable one.
        zero = {
            "type": "assistant",
            "message": {
                "model": "claude-opus-4-7",
                "usage": {
                    "input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 0,
                },
            },
        }
        _write_jsonl(f, [_assistant(5, 500), zero])
        usage = read_latest_usage(str(f))
        assert usage is not None
        assert usage.tokens == 5 + 500
