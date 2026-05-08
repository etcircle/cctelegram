"""JSONL transcript parser for Claude Code session files.

Parses Claude Code session JSONL files and extracts structured messages.
Handles: text, thinking, tool_use, tool_result, local_command, and user messages.
Tool pairing: tool_use blocks in assistant messages are matched with
tool_result blocks in subsequent user messages via tool_use_id.

Shared by both session.py (history) and session_monitor.py (real-time).
Format reference: https://github.com/desis123/claude-code-viewer

Key classes: TranscriptParser (static methods), ParsedEntry, ParsedMessage, PendingToolInfo.
"""

import base64
import difflib
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Literal

from .config import config

logger = logging.getLogger(__name__)

# Mirrors handlers.busy_indicator._TURN_END_REASONS so the lifecycle-only
# end-of-turn marker emitted in ``parse_entries`` is keyed off the same
# stop_reason set the indicator's transition table reads. Duplicated rather
# than imported to keep the parser dependency-free.
_TURN_END_REASONS_LITERAL = frozenset({"end_turn", "stop_sequence"})


@dataclass
class ParsedMessage:
    """Parsed message from a transcript."""

    message_type: str  # "user", "assistant", "tool_use", "tool_result", etc.
    text: str  # Extracted text content
    tool_name: str | None = None  # For tool_use messages


@dataclass
class ParsedEntry:
    """A single parsed message entry ready for display."""

    role: Literal["user", "assistant"]
    text: str  # Already formatted text
    content_type: Literal[
        "text", "thinking", "tool_use", "tool_result", "local_command"
    ]
    tool_use_id: str | None = None
    timestamp: str | None = None  # ISO timestamp from JSONL
    tool_name: str | None = (
        None  # For tool_use entries, the tool name (e.g. "AskUserQuestion")
    )
    image_data: list[tuple[str, bytes]] | None = (
        None  # For tool_result entries with images: (media_type, raw_bytes)
    )
    # JSONL `message.stop_reason`, propagated from the raw assistant
    # message to every entry derived from it. None for user-role entries
    # (including tool_result-bearing ones) — JSONL doesn't carry it there.
    stop_reason: str | None = None
    # Raw tool_use input dict, populated for Edit/Write/Agent/Task so
    # downstream renderers (e.g. §2.7 Agent prominence) can extract
    # description / subagent_type / prompt without re-parsing JSONL.
    tool_input: dict[str, Any] | None = None
    uuid: str | None = None
    # When True, this entry exists only to drive run-state transitions in
    # the busy indicator and must NOT be rendered in Telegram. Emitted for
    # raw JSONL events that lack visible content (empty tool_result, an
    # end_turn assistant message with no text/thinking) but still matter
    # to lifecycle bookkeeping.
    lifecycle_only: bool = False


@dataclass
class PendingToolInfo:
    """Information about a pending tool_use waiting for its tool_result."""

    summary: str  # Formatted tool summary (e.g. "**Read**(file.py)")
    tool_name: str  # Tool name (e.g. "Read", "Edit")
    input_data: Any = None  # Tool input parameters (for Edit to generate diff)
    uuid: str | None = None


class TranscriptParser:
    """Parser for Claude Code JSONL session files.

    Expected JSONL entry structure:
    - type: "user" | "assistant" | "summary" | "file-history-snapshot" | ...
    - message.content: list of blocks (text, tool_use, tool_result, thinking)
    - sessionId, cwd, timestamp, uuid: metadata fields

    Tool pairing model: tool_use blocks appear in assistant messages,
    matching tool_result blocks appear in the next user message (keyed by tool_use_id).
    """

    # Magic string constants
    _NO_CONTENT_PLACEHOLDER = "(no content)"
    _INTERRUPTED_TEXT = "[Request interrupted by user for tool use]"

    @staticmethod
    def parse_line(line: str) -> dict | None:
        """Parse a single JSONL line.

        Args:
            line: A single line from the JSONL file

        Returns:
            Parsed dict or None if line is empty/invalid
        """
        line = line.strip()
        if not line:
            return None

        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return None

    @staticmethod
    def get_message_type(data: dict) -> str | None:
        """Get the message type from parsed data.

        Returns:
            Message type: "user", "assistant", "file-history-snapshot", etc.
        """
        return data.get("type")

    @staticmethod
    def is_user_message(data: dict) -> bool:
        """Check if this is a user message."""
        return data.get("type") == "user"

    @staticmethod
    def extract_text_only(content_list: list[Any]) -> str:
        """Extract only text content from structured content.

        This is used for Telegram notifications where we only want
        the actual text response, not tool calls or thinking.

        Args:
            content_list: List of content blocks

        Returns:
            Combined text content only
        """
        if not isinstance(content_list, list):
            if isinstance(content_list, str):
                return content_list
            return ""

        texts = []
        for item in content_list:
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    text = item.get("text", "")
                    if text:
                        texts.append(text)

        return "\n".join(texts)

    _RE_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

    _RE_COMMAND_NAME = re.compile(r"<command-name>(.*?)</command-name>")
    _RE_LOCAL_STDOUT = re.compile(
        r"<local-command-stdout>(.*?)</local-command-stdout>", re.DOTALL
    )
    _RE_SYSTEM_TAGS = re.compile(
        r"<(bash-input|bash-stdout|bash-stderr|local-command-caveat|system-reminder)"
    )

    @staticmethod
    def _format_edit_diff(old_string: str, new_string: str) -> str:
        """Generate a compact unified diff between old_string and new_string."""
        old_lines = old_string.splitlines(keepends=True)
        new_lines = new_string.splitlines(keepends=True)
        diff = difflib.unified_diff(old_lines, new_lines, lineterm="")
        # Skip the --- / +++ header lines
        result_lines: list[str] = []
        for line in diff:
            if line.startswith("---") or line.startswith("+++"):
                continue
            # Strip trailing newline for clean display
            result_lines.append(line.rstrip("\n"))
        return "\n".join(result_lines)

    @classmethod
    def format_tool_use_summary(cls, name: str, input_data: dict | Any) -> str:
        """Format a tool_use block into a brief summary line.

        Args:
            name: Tool name (e.g. "Read", "Write", "Bash")
            input_data: The tool input dict

        Returns:
            Formatted string like "**Read**(file.py)"
        """
        if not isinstance(input_data, dict):
            return f"**{name}**"

        # Pick a meaningful short summary based on tool name
        summary = ""
        if name in ("Read", "Glob"):
            summary = input_data.get("file_path") or input_data.get("pattern", "")
        elif name == "Write":
            summary = input_data.get("file_path", "")
        elif name in ("Edit", "NotebookEdit"):
            summary = input_data.get("file_path") or input_data.get("notebook_path", "")
            # Note: Edit/Update diff and stats are generated in tool_result stage,
            # not here. We just show the tool name and file path.
        elif name == "Bash":
            summary = input_data.get("command", "")
        elif name == "Grep":
            summary = input_data.get("pattern", "")
        elif name in ("Agent", "Task"):
            # Both legacy "Task" and current "Agent" are subagent invocations
            # (Anthropic renamed it). Prefer the human-written description;
            # fall back to the subagent_type slug if that's all we have.
            summary = (
                input_data.get("description") or input_data.get("subagent_type") or ""
            )
        elif name == "WebFetch":
            summary = input_data.get("url", "")
        elif name == "WebSearch":
            summary = input_data.get("query", "")
        elif name == "TodoWrite":
            todos = input_data.get("todos", [])
            if isinstance(todos, list):
                summary = f"{len(todos)} item(s)"
        elif name == "TodoRead":
            summary = ""
        elif name == "AskUserQuestion":
            questions = input_data.get("questions", [])
            if isinstance(questions, list) and questions:
                q = questions[0]
                if isinstance(q, dict):
                    summary = q.get("question", "")
        elif name == "ExitPlanMode":
            summary = ""
        elif name == "Skill":
            summary = input_data.get("skill", "")
        else:
            # Generic: show first string value
            for v in input_data.values():
                if isinstance(v, str) and v:
                    summary = v
                    break

        if summary:
            max_chars = config.tool_summary_max_chars
            if len(summary) > max_chars:
                summary = summary[:max_chars] + "…"
            return f"**{name}**({summary})"
        return f"**{name}**"

    @staticmethod
    def extract_tool_result_text(content: list | Any) -> str:
        """Extract text from a tool_result content block."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    t = item.get("text", "")
                    if t:
                        parts.append(t)
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts)
        return ""

    @staticmethod
    def extract_tool_result_images(
        content: list | Any,
    ) -> list[tuple[str, bytes]] | None:
        """Extract base64-encoded images from a tool_result content block.

        Returns list of (media_type, raw_bytes) tuples, or None if no images found.
        """
        if not isinstance(content, list):
            return None
        images: list[tuple[str, bytes]] = []
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "image":
                continue
            source = item.get("source")
            if not isinstance(source, dict) or source.get("type") != "base64":
                continue
            media_type = source.get("media_type", "image/png")
            data_str = source.get("data", "")
            if not data_str:
                continue
            try:
                raw_bytes = base64.b64decode(data_str)
                images.append((media_type, raw_bytes))
            except Exception:
                logger.debug("Failed to decode base64 image in tool_result")
        return images if images else None

    @classmethod
    def parse_message(cls, data: dict) -> ParsedMessage | None:
        """Parse a message entry from the JSONL data.

        Args:
            data: Parsed JSON dict from a JSONL line

        Returns:
            ParsedMessage or None if not a parseable message
        """
        msg_type = cls.get_message_type(data)

        if msg_type not in ("user", "assistant"):
            return None

        message = data.get("message")
        if not isinstance(message, dict):
            return None
        content = message.get("content", "")

        if isinstance(content, list):
            text = cls.extract_text_only(content)
        else:
            text = str(content) if content else ""
        text = cls._RE_ANSI_ESCAPE.sub("", text)

        # Detect local command responses in user messages.
        # These are rendered as bot replies: "❯ /cmd\n  ⎿  output"
        if msg_type == "user" and text:
            stdout_match = cls._RE_LOCAL_STDOUT.search(text)
            if stdout_match:
                stdout = stdout_match.group(1).strip()
                cmd_match = cls._RE_COMMAND_NAME.search(text)
                cmd = cmd_match.group(1) if cmd_match else None
                return ParsedMessage(
                    message_type="local_command",
                    text=stdout,
                    tool_name=cmd,  # reuse field for command name
                )
            # Pure command invocation (no stdout) — carry command name
            cmd_match = cls._RE_COMMAND_NAME.search(text)
            if cmd_match:
                return ParsedMessage(
                    message_type="local_command_invoke",
                    text="",
                    tool_name=cmd_match.group(1),
                )

        return ParsedMessage(
            message_type=msg_type,
            text=text,
        )

    @staticmethod
    def get_timestamp(data: dict) -> str | None:
        """Extract timestamp from message data."""
        return data.get("timestamp")

    EXPANDABLE_QUOTE_START = "\x02EXPQUOTE_START\x02"
    EXPANDABLE_QUOTE_END = "\x02EXPQUOTE_END\x02"

    @classmethod
    def _format_expandable_quote(cls, text: str) -> str:
        """Format text as a Telegram expandable blockquote.

        Wraps text with sentinel markers. The actual MarkdownV2 formatting
        (> prefix, || suffix, escaping) is done in convert_markdown() after
        telegramify processes the surrounding content.
        """
        return f"{cls.EXPANDABLE_QUOTE_START}{text}{cls.EXPANDABLE_QUOTE_END}"

    @classmethod
    def _format_tool_result_text(
        cls,
        text: str,
        tool_name: str | None = None,
        tool_input_data: dict | None = None,
    ) -> str:
        """Format tool result text with statistics summary.

        Shows relevant statistics for each tool type, with expandable quote for full content.

        No truncation here — per project principles, truncation is handled
        only at the send layer (split_message / _render_expandable_quote).
        """
        if not text:
            return ""

        line_count = text.count("\n") + 1 if text else 0

        # Tool-specific statistics
        if tool_name == "Read":
            # Read: show line count instead of full content
            return f"  ⎿  Read {line_count} lines"

        elif tool_name == "Write":
            # Write: prefer the input content, but fall back to the result text
            # for tests/history entries that do not carry tool input data.
            written = tool_input_data.get("content", "") if tool_input_data else text
            written_lines = written.count("\n") + (0 if written.endswith("\n") else 1)
            return f"  ⎿  Wrote {written_lines} lines"

        elif tool_name == "Bash":
            # Bash: show output line count
            if line_count > 0:
                stats = f"  ⎿  Output {line_count} lines"
                return stats + "\n" + cls._format_expandable_quote(text)
            return cls._format_expandable_quote(text)

        elif tool_name == "Grep":
            # Grep: show match count (count non-empty lines)
            matches = len([line for line in text.split("\n") if line.strip()])
            stats = f"  ⎿  Found {matches} matches"
            return stats + "\n" + cls._format_expandable_quote(text)

        elif tool_name == "Glob":
            # Glob: show file count
            files = len([line for line in text.split("\n") if line.strip()])
            stats = f"  ⎿  Found {files} files"
            return stats + "\n" + cls._format_expandable_quote(text)

        elif tool_name == "Task":
            # Task: show output length
            if line_count > 0:
                stats = f"  ⎿  Agent output {line_count} lines"
                return stats + "\n" + cls._format_expandable_quote(text)
            return cls._format_expandable_quote(text)

        elif tool_name == "WebFetch":
            # WebFetch: show content length
            char_count = len(text)
            stats = f"  ⎿  Fetched {char_count} characters"
            return stats + "\n" + cls._format_expandable_quote(text)

        elif tool_name == "WebSearch":
            # WebSearch: show results count (estimate by sections)
            results = text.count("\n\n") + 1 if text else 0
            stats = f"  ⎿  {results} search results"
            return stats + "\n" + cls._format_expandable_quote(text)

        # Default: expandable quote without stats
        return cls._format_expandable_quote(text)

    @classmethod
    def parse_entries(
        cls,
        entries: list[dict],
        pending_tools: dict[str, PendingToolInfo] | None = None,
    ) -> tuple[list[ParsedEntry], dict[str, PendingToolInfo]]:
        """Parse a list of JSONL entries into a flat list of display-ready messages.

        This is the shared core logic used by both get_recent_messages (history)
        and check_for_updates (monitor).

        Args:
            entries: List of parsed JSONL dicts (already filtered through parse_line)
            pending_tools: Optional carry-over pending tool_use state from a
                previous call (tool_use_id -> formatted summary). Used by the
                monitor to handle tool_use and tool_result arriving in separate
                poll cycles.

        Returns:
            Tuple of (parsed entries, remaining pending_tools state)
        """
        result: list[ParsedEntry] = []
        last_cmd_name: str | None = None
        # Pending tool_use blocks keyed by id
        _carry_over = pending_tools is not None
        if pending_tools is None:
            pending_tools = {}
        else:
            pending_tools = dict(pending_tools)  # don't mutate caller's dict

        # Empty-turn detection within this batch. Tracks "did the most recent
        # user prompt produce any assistant output before the turn ended."
        # Reset on each user-text entry; flipped True on any assistant
        # text/thinking/tool_use; consulted when a system ``turn_duration``
        # entry is seen. A definitive empty turn (Claude received the prompt,
        # ran the model, emitted nothing) otherwise looks identical to "still
        # working" to the user — no message reaches the topic.
        seen_user_prompt = False
        assistant_emitted_after_prompt = False

        for data in entries:
            msg_type = cls.get_message_type(data)
            # System ``turn_duration`` is the only definitive end-of-turn
            # signal Claude writes when the assistant message is absent
            # entirely (model returned nothing). It's the marker we use to
            # diagnose empty turns and to drive busy_indicator to IDLE in
            # cases where no assistant ``stop_reason=end_turn`` ever lands.
            if msg_type == "system" and data.get("subtype") == "turn_duration":
                if seen_user_prompt and not assistant_emitted_after_prompt:
                    ts = cls.get_timestamp(data)
                    raw_uuid = data.get("uuid")
                    eu = raw_uuid if isinstance(raw_uuid, str) else None
                    duration_ms = data.get("durationMs")
                    suffix = ""
                    if isinstance(duration_ms, int | float):
                        suffix = f" (turn took {duration_ms / 1000:.1f}s)"
                    warn_text = (
                        "⚠️ Claude finished the turn without responding"
                        f"{suffix}. Try resending — this often happens when "
                        "replying to a message from a /clear-ed session."
                    )
                    result.append(
                        ParsedEntry(
                            role="assistant",
                            text=warn_text,
                            content_type="text",
                            timestamp=ts,
                            uuid=eu,
                        )
                    )
                    # Synthetic end-of-turn lifecycle marker so the
                    # busy_indicator transitions to IDLE_RECENT even though
                    # no assistant text/thinking with stop_reason=end_turn
                    # was emitted by Claude itself.
                    result.append(
                        ParsedEntry(
                            role="assistant",
                            text="",
                            content_type="text",
                            timestamp=ts,
                            stop_reason="end_turn",
                            uuid=eu,
                            lifecycle_only=True,
                        )
                    )
                seen_user_prompt = False
                assistant_emitted_after_prompt = False
                continue
            if msg_type not in ("user", "assistant"):
                continue

            # Extract timestamp for this entry
            entry_timestamp = cls.get_timestamp(data)
            raw_uuid = data.get("uuid")
            entry_uuid = raw_uuid if isinstance(raw_uuid, str) else None

            message = data.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content", "")
            if not isinstance(content, list):
                content = [{"type": "text", "text": str(content)}] if content else []

            # `stop_reason` lives at message level on assistant turns only;
            # user-role messages (including tool_result carriers) have no
            # stop_reason in JSONL.
            entry_stop_reason = (
                message.get("stop_reason") if msg_type == "assistant" else None
            )

            parsed = cls.parse_message(data)

            # Handle local command messages first
            if parsed:
                if parsed.message_type == "local_command_invoke":
                    last_cmd_name = parsed.tool_name
                    continue
                if parsed.message_type == "local_command":
                    cmd = parsed.tool_name or last_cmd_name or ""
                    text = parsed.text
                    if cmd:
                        if "\n" in text:
                            formatted = f"❯ `{cmd}`\n```\n{text}\n```"
                        else:
                            formatted = f"❯ `{cmd}`\n`{text}`"
                    else:
                        if "\n" in text:
                            formatted = f"```\n{text}\n```"
                        else:
                            formatted = f"`{text}`"
                    result.append(
                        ParsedEntry(
                            role="assistant",
                            text=formatted,
                            content_type="local_command",
                            timestamp=entry_timestamp,
                            uuid=entry_uuid,
                        )
                    )
                    last_cmd_name = None
                    continue
            last_cmd_name = None

            if msg_type == "assistant":
                # Any assistant turn — even one whose blocks the parser
                # ultimately filters — counts as Claude responding to the
                # prompt. Empty-turn detection fires only when no assistant
                # entry at all lands between the user prompt and the
                # ``turn_duration`` system marker.
                assistant_emitted_after_prompt = True
                # Process content blocks
                has_text = False
                _result_len_before = len(result)
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")

                    if btype == "text":
                        t = block.get("text", "").strip()
                        if t and t != cls._NO_CONTENT_PLACEHOLDER:
                            result.append(
                                ParsedEntry(
                                    role="assistant",
                                    text=t,
                                    content_type="text",
                                    timestamp=entry_timestamp,
                                    stop_reason=entry_stop_reason,
                                    uuid=entry_uuid,
                                )
                            )
                            has_text = True

                    elif btype == "tool_use":
                        tool_id = block.get("id", "")
                        name = block.get("name", "unknown")
                        inp = block.get("input", {})
                        summary = cls.format_tool_use_summary(name, inp)

                        # ExitPlanMode: emit plan content as text before tool_use entry
                        if name == "ExitPlanMode" and isinstance(inp, dict):
                            plan = inp.get("plan", "")
                            if plan:
                                result.append(
                                    ParsedEntry(
                                        role="assistant",
                                        text=plan,
                                        content_type="text",
                                        timestamp=entry_timestamp,
                                        stop_reason=entry_stop_reason,
                                        uuid=entry_uuid,
                                    )
                                )
                        if tool_id:
                            # Store tool info for later tool_result formatting.
                            # Edit needs input_data for diff generation;
                            # Agent/Task carry it so §2.7's top-level render
                            # can show description / subagent_type / prompt.
                            input_data = (
                                inp
                                if name
                                in ("Edit", "NotebookEdit", "Write", "Agent", "Task")
                                else None
                            )
                            pending_tools[tool_id] = PendingToolInfo(
                                summary=summary,
                                tool_name=name,
                                input_data=input_data,
                                uuid=entry_uuid,
                            )
                            # Also emit tool_use entry with tool_name for immediate handling
                            result.append(
                                ParsedEntry(
                                    role="assistant",
                                    text=summary,
                                    content_type="tool_use",
                                    tool_use_id=tool_id,
                                    timestamp=entry_timestamp,
                                    tool_name=name,
                                    stop_reason=entry_stop_reason,
                                    tool_input=inp if isinstance(inp, dict) else None,
                                    uuid=entry_uuid,
                                )
                            )
                        else:
                            result.append(
                                ParsedEntry(
                                    role="assistant",
                                    text=summary,
                                    content_type="tool_use",
                                    tool_use_id=tool_id or None,
                                    timestamp=entry_timestamp,
                                    tool_name=name,
                                    stop_reason=entry_stop_reason,
                                    uuid=entry_uuid,
                                )
                            )

                    elif btype == "thinking":
                        thinking_text = block.get("thinking", "")
                        if thinking_text:
                            quoted = cls._format_expandable_quote(thinking_text)
                            result.append(
                                ParsedEntry(
                                    role="assistant",
                                    text=quoted,
                                    content_type="thinking",
                                    timestamp=entry_timestamp,
                                    stop_reason=entry_stop_reason,
                                    uuid=entry_uuid,
                                )
                            )
                        elif not has_text:
                            result.append(
                                ParsedEntry(
                                    role="assistant",
                                    text="(thinking)",
                                    content_type="thinking",
                                    timestamp=entry_timestamp,
                                    stop_reason=entry_stop_reason,
                                    uuid=entry_uuid,
                                )
                            )

                # Lifecycle-only end-of-turn marker.
                #
                # If this assistant message terminated cleanly (end_turn /
                # stop_sequence) but produced no visible text/thinking entry,
                # the busy indicator would never see the end-of-turn signal
                # and stay stuck in RUNNING. Emit a content-less entry so
                # ``_apply_event`` can drive the route to IDLE_RECENT. The
                # session_monitor honors ``lifecycle_only`` by dispatching a
                # TranscriptEvent without enqueueing a Telegram message.
                if (
                    entry_stop_reason in _TURN_END_REASONS_LITERAL
                    and len(result) == _result_len_before
                ):
                    result.append(
                        ParsedEntry(
                            role="assistant",
                            text="",
                            content_type="text",
                            timestamp=entry_timestamp,
                            stop_reason=entry_stop_reason,
                            uuid=entry_uuid,
                            lifecycle_only=True,
                        )
                    )

            elif msg_type == "user":
                # Check for tool_result blocks and merge with pending tools
                user_text_parts: list[str] = []

                for block in content:
                    if not isinstance(block, dict):
                        if isinstance(block, str) and block.strip():
                            user_text_parts.append(block.strip())
                        continue
                    btype = block.get("type", "")

                    if btype == "tool_result":
                        tool_use_id = block.get("tool_use_id", "")
                        result_content = block.get("content", "")
                        result_text = cls.extract_tool_result_text(result_content)
                        result_images = cls.extract_tool_result_images(result_content)
                        is_error = block.get("is_error", False)
                        is_interrupted = result_text == cls._INTERRUPTED_TEXT
                        tool_info = pending_tools.pop(tool_use_id, None)
                        _tuid = tool_use_id or None

                        # Extract tool info from PendingToolInfo object
                        if tool_info is None:
                            tool_summary = None
                            tool_name = None
                            tool_input_data = None
                        else:
                            tool_summary = tool_info.summary
                            tool_name = tool_info.tool_name
                            tool_input_data = tool_info.input_data

                        if is_interrupted:
                            # Show interruption inline with tool summary
                            entry_text = tool_summary or ""
                            if entry_text:
                                entry_text += "\n⏹ Interrupted"
                            else:
                                entry_text = "⏹ Interrupted"
                            result.append(
                                ParsedEntry(
                                    role="assistant",
                                    text=entry_text,
                                    content_type="tool_result",
                                    tool_use_id=_tuid,
                                    timestamp=entry_timestamp,
                                    tool_input=tool_input_data
                                    if isinstance(tool_input_data, dict)
                                    else None,
                                    uuid=entry_uuid,
                                )
                            )
                        elif is_error:
                            # Show error in stats line
                            if tool_summary:
                                entry_text = tool_summary
                            else:
                                entry_text = "**Error**"
                            # Add error message in stats format
                            if result_text:
                                # Take first line of error as summary
                                error_summary = result_text.split("\n")[0]
                                if len(error_summary) > 100:
                                    error_summary = error_summary[:100] + "…"
                                entry_text += f"\n  ⎿  Error: {error_summary}"
                                # If multi-line error, add expandable quote
                                if "\n" in result_text:
                                    entry_text += "\n" + cls._format_expandable_quote(
                                        result_text
                                    )
                            else:
                                entry_text += "\n  ⎿  Error"
                            result.append(
                                ParsedEntry(
                                    role="assistant",
                                    text=entry_text,
                                    content_type="tool_result",
                                    tool_use_id=_tuid,
                                    timestamp=entry_timestamp,
                                    image_data=result_images,
                                    tool_input=tool_input_data
                                    if isinstance(tool_input_data, dict)
                                    else None,
                                    uuid=entry_uuid,
                                )
                            )
                        elif tool_summary:
                            entry_text = tool_summary
                            # For Edit tool, generate diff stats and expandable quote
                            if tool_name == "Edit" and tool_input_data and result_text:
                                old_s = tool_input_data.get("old_string", "")
                                new_s = tool_input_data.get("new_string", "")
                                if old_s and new_s:
                                    diff_text = cls._format_edit_diff(old_s, new_s)
                                    if diff_text:
                                        added = sum(
                                            1
                                            for line in diff_text.split("\n")
                                            if line.startswith("+")
                                            and not line.startswith("+++")
                                        )
                                        removed = sum(
                                            1
                                            for line in diff_text.split("\n")
                                            if line.startswith("-")
                                            and not line.startswith("---")
                                        )
                                        stats = f"  ⎿  Added {added} lines, removed {removed} lines"
                                        entry_text += (
                                            "\n"
                                            + stats
                                            + "\n"
                                            + cls._format_expandable_quote(diff_text)
                                        )
                            # For other tools, append formatted result text
                            elif (
                                result_text
                                and cls.EXPANDABLE_QUOTE_START not in tool_summary
                            ):
                                entry_text += "\n" + cls._format_tool_result_text(
                                    result_text, tool_name, tool_input_data
                                )
                            result.append(
                                ParsedEntry(
                                    role="assistant",
                                    text=entry_text,
                                    content_type="tool_result",
                                    tool_use_id=_tuid,
                                    timestamp=entry_timestamp,
                                    image_data=result_images,
                                    tool_input=tool_input_data
                                    if isinstance(tool_input_data, dict)
                                    else None,
                                    uuid=entry_uuid,
                                )
                            )
                        elif result_text or result_images:
                            result.append(
                                ParsedEntry(
                                    role="assistant",
                                    text=cls._format_tool_result_text(
                                        result_text, tool_name, tool_input_data
                                    )
                                    if result_text
                                    else (tool_summary or ""),
                                    content_type="tool_result",
                                    tool_use_id=_tuid,
                                    timestamp=entry_timestamp,
                                    image_data=result_images,
                                    tool_input=tool_input_data
                                    if isinstance(tool_input_data, dict)
                                    else None,
                                    uuid=entry_uuid,
                                )
                            )
                        elif tool_use_id:
                            # No visible content (no summary, no result text /
                            # image, not an error / interruption) but the raw
                            # JSONL block has a tool_use_id. After a bot
                            # restart this is the common shape — the parser's
                            # in-memory ``pending_tools`` no longer remembers
                            # the matching tool_use, so a perfectly normal
                            # quiet tool_result becomes invisible to the
                            # busy indicator and the route stays stuck in
                            # RUNNING_TOOL. Emit a lifecycle-only entry so
                            # ``_apply_event`` can close the open tool slot.
                            result.append(
                                ParsedEntry(
                                    role="assistant",
                                    text="",
                                    content_type="tool_result",
                                    tool_use_id=_tuid,
                                    timestamp=entry_timestamp,
                                    uuid=entry_uuid,
                                    lifecycle_only=True,
                                )
                            )

                    elif btype == "text":
                        t = block.get("text", "").strip()
                        if t and not cls._RE_SYSTEM_TAGS.search(t):
                            user_text_parts.append(t)

                # Add user text if present (skip if message was only tool_results)
                if user_text_parts:
                    combined = "\n".join(user_text_parts)
                    # Skip if it looks like local command XML
                    if not cls._RE_LOCAL_STDOUT.search(
                        combined
                    ) and not cls._RE_COMMAND_NAME.search(combined):
                        result.append(
                            ParsedEntry(
                                role="user",
                                text=combined,
                                content_type="text",
                                timestamp=entry_timestamp,
                                uuid=entry_uuid,
                            )
                        )
                        # Empty-turn tracking — a real user prompt resets the
                        # window. Tool_result-only user messages never reach
                        # this branch (they short-circuit above), so we won't
                        # falsely re-arm the warning mid-tool-loop.
                        seen_user_prompt = True
                        assistant_emitted_after_prompt = False

        # Flush remaining pending tools at end.
        # In carry-over mode (monitor), keep them pending for the next call
        # without emitting entries. In one-shot mode (history), emit them.
        remaining_pending = dict(pending_tools)
        if not _carry_over:
            for tool_id, tool_info in pending_tools.items():
                result.append(
                    ParsedEntry(
                        role="assistant",
                        text=tool_info.summary,
                        content_type="tool_use",
                        tool_use_id=tool_id,
                        uuid=tool_info.uuid,
                    )
                )

        # Strip whitespace
        for entry in result:
            entry.text = entry.text.strip()

        return result, remaining_pending


# ── Latest-usage lookup (for context-window indicator) ────────────────────


@dataclass(frozen=True)
class LatestUsage:
    """Snapshot of the latest assistant turn's token + model usage."""

    tokens: int  # input + cache_read + cache_creation (== next-turn ctx size)
    model: str  # raw model id from JSONL, e.g. "claude-opus-4-7"


# Cached by (jsonl_path, mtime, size) so a status-poller hitting this every
# second only re-reads when the file actually changed. Tracking size as well
# as mtime guards against same-second appends that wouldn't bump mtime on
# coarse-resolution filesystems. The cache is bounded by the number of
# distinct sessions; old entries can be evicted by clear_usage_cache.
_latest_usage_cache: dict[str, tuple[tuple[float, int], LatestUsage | None]] = {}


def clear_usage_cache(jsonl_path: str | None = None) -> None:
    """Drop the cached latest-usage for a path, or all paths if None."""
    if jsonl_path is None:
        _latest_usage_cache.clear()
    else:
        _latest_usage_cache.pop(jsonl_path, None)


def read_latest_usage(jsonl_path: str) -> LatestUsage | None:
    """Return the most recent assistant message's usage + model, or None.

    Reads the file from end → beginning, line-buffered, stopping at the
    first assistant entry that carries a ``message.usage`` block. Sums
    ``input_tokens + cache_read_input_tokens + cache_creation_input_tokens``
    — that's the size of the prompt the next turn would re-feed, i.e.
    "what's currently in context." ``output_tokens`` is excluded because
    next turn it shows up inside ``cache_read_input_tokens``.

    Sidechain entries (``isSidechain=true``) are skipped: they belong to
    sub-agents and have their own context budget.

    Caches by (path, mtime) so a 1Hz poller doesn't re-read unchanged files.
    """
    try:
        st = os.stat(jsonl_path)
    except OSError:
        return None
    cache_key = (st.st_mtime, st.st_size)

    cached = _latest_usage_cache.get(jsonl_path)
    if cached is not None and cached[0] == cache_key:
        return cached[1]

    result: LatestUsage | None = None
    try:
        # Tail-first scan — most JSONLs are small enough that reading the
        # whole file is fine, and we get correctness without seek juggling.
        # If profiling ever shows a hotspot we can switch to a reverse-line
        # iterator with a 64KB tail buffer.
        with open(jsonl_path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        _latest_usage_cache[jsonl_path] = (cache_key, None)
        return None

    for raw_line in reversed(lines):
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "assistant":
            continue
        if entry.get("isSidechain"):
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue

        def _int(v: Any) -> int:
            return v if isinstance(v, int) and v >= 0 else 0

        tokens = (
            _int(usage.get("input_tokens"))
            + _int(usage.get("cache_read_input_tokens"))
            + _int(usage.get("cache_creation_input_tokens"))
        )
        model_raw = message.get("model")
        model = model_raw if isinstance(model_raw, str) else ""
        if tokens <= 0 or not model:
            continue
        result = LatestUsage(tokens=tokens, model=model)
        break

    _latest_usage_cache[jsonl_path] = (cache_key, result)
    return result
