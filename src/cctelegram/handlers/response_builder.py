"""Response message building for Telegram delivery.

Builds paginated response messages from Claude Code output:
  - Handles different content types (text, thinking, tool_use, tool_result)
  - Splits long messages into pages within Telegram's 4096 char limit
  - Truncates thinking content to keep messages compact

Markdown conversion is NOT done here — the send layer (message_sender,
message_queue) handles convert_markdown() so each message is converted
exactly once.

Key function:
  - build_response_parts: Build paginated response messages
"""

import re

from ..markdown_v2 import convert_markdown_tables
from ..telegram_sender import split_message
from ..transcript_parser import TranscriptParser

_TASK_NOTIF_RE = re.compile(
    r"\A<task-notification>(.*?)</task-notification>\s*\Z", re.DOTALL
)
_TASK_NOTIF_TAG_RE = re.compile(
    r"<(?P<tag>task-id|summary|event)>(?P<body>.*?)</(?P=tag)>", re.DOTALL
)


def is_task_notification(text: str) -> bool:
    """True when the text is an external ``<task-notification>`` envelope.

    Public predicate (plan v4 / codex r2 P3-5): the per-user echo gate in
    ``bot.handle_new_message`` must EXEMPT these system events from user-echo
    suppression without duplicating the envelope regexes.
    """
    return _TASK_NOTIF_RE.match(text or "") is not None


def extract_task_notification_task_id(text: str) -> str | None:
    """Extract ``<task-id>`` from a ``<task-notification>`` envelope.

    Public extractor beside the predicate (GH #44, codex r3 P3-1 — the
    predicate alone returns bool). For a background async agent the task-id
    IS the agent key (== the sidechain ``agent-<id>.jsonl`` stem minus the
    prefix; fixture-verified). ``None`` when the text is not a recognizable
    envelope or carries no task-id.
    """
    m = _TASK_NOTIF_RE.match(text or "")
    if not m:
        return None
    for tm in _TASK_NOTIF_TAG_RE.finditer(m.group(1)):
        if tm.group("tag") == "task-id":
            body = tm.group("body").strip()
            return body or None
    return None


# The async-launch background discriminator (GH #44 §3.2a). Anchored on the
# STRUCTURED ``agentId: <id>`` line — the surrounding success sentence is
# diagnostic/fixture coverage only, never load-bearing (codex r3 + hermes
# §9-2: TUI prose drifts across Claude Code versions; the id line is the
# stable part). Callers scope it to Agent/Task tool_result text.
# Leading whitespace tolerated: the transcript parser renders tool_result
# content indented under the "⎿" marker.
_ASYNC_LAUNCH_AGENT_ID_RE = re.compile(
    r"^\s*agentId:\s*([0-9a-fA-F]{6,})\b", re.MULTILINE
)


def extract_async_agent_launch_id(text: str) -> str | None:
    """Extract the ``agentId`` from an async-Agent-launch ``tool_result``.

    Returns the raw id (no ``agent-`` prefix — normalize with
    ``route_runtime.normalize_background_agent_key`` before keying) or
    ``None`` when no ``agentId:`` line is present. Synchronous agents never
    produce one (their tool_result is the agent's final report).
    """
    if not text:
        return None
    m = _ASYNC_LAUNCH_AGENT_ID_RE.search(text)
    return m.group(1) if m else None


def _render_task_notification(text: str) -> str | None:
    """Render an external `<task-notification>` envelope as a clean card.

    Returns None if the text isn't a recognizable task-notification, in
    which case the caller falls back to the default rendering path.
    """
    m = _TASK_NOTIF_RE.match(text)
    if not m:
        return None

    task_id: str | None = None
    summary: str | None = None
    events: list[str] = []
    for tm in _TASK_NOTIF_TAG_RE.finditer(m.group(1)):
        tag = tm.group("tag")
        body = tm.group("body").strip()
        if not body:
            continue
        if tag == "task-id" and task_id is None:
            task_id = body
        elif tag == "summary" and summary is None:
            summary = body
        elif tag == "event":
            events.append(body)

    if not (task_id or summary or events):
        return None

    header = f"🔔 *Task* `{task_id}`" if task_id else "🔔 *Task notification*"
    lines = [header]
    if summary:
        lines.append(summary)
    head = "\n".join(lines)
    if events:
        events_block = "\n".join(events)
        return head + "\n\n" + TranscriptParser._format_expandable_quote(events_block)
    return head


def build_response_parts(
    text: str,
    content_type: str = "text",
    role: str = "assistant",
) -> list[str]:
    """Build paginated response messages for Telegram.

    Returns a list of raw markdown strings, each within Telegram's 4096 char limit.
    Multi-part messages get a [1/N] suffix.
    Markdown-to-MarkdownV2 conversion is done by the send layer, not here.
    """
    text = text.strip()

    # External `<task-notification>` envelopes (injected by hooks / external
    # agents as user-role prompts) get a custom card instead of the raw
    # 👤 echo — they're system events, not "the user said X".
    if role == "user":
        rendered = _render_task_notification(text)
        if rendered is not None:
            return [rendered]

    # User messages: add emoji prefix (no newline)
    if role == "user":
        prefix = "👤 "
        separator = ""
        # User messages are typically short, no special processing needed
        if len(text) > 3000:
            text = text[:3000] + "…"
        return [f"{prefix}{text}"]

    # Truncate thinking content to keep it compact
    if content_type == "thinking":
        start_tag = TranscriptParser.EXPANDABLE_QUOTE_START
        end_tag = TranscriptParser.EXPANDABLE_QUOTE_END
        max_thinking = 500
        if start_tag in text and end_tag in text:
            inner = text[text.index(start_tag) + len(start_tag) : text.index(end_tag)]
            if len(inner) > max_thinking:
                inner = inner[:max_thinking] + "\n\n… (thinking truncated)"
            text = start_tag + inner + end_tag
        elif len(text) > max_thinking:
            text = text[:max_thinking] + "\n\n… (thinking truncated)"

    # Format based on content type
    if content_type == "thinking":
        # Thinking: prefix with "∴ Thinking…" and single newline
        prefix = "∴ Thinking…"
        separator = "\n"
    else:
        # Plain text: no prefix
        prefix = ""
        separator = ""

    # If text contains expandable quote sentinels, don't split —
    # the quote must stay atomic. Truncation is handled by
    # _render_expandable_quote in markdown_v2.py.
    if TranscriptParser.EXPANDABLE_QUOTE_START in text:
        if prefix:
            return [f"{prefix}{separator}{text}"]
        return [text]

    # Convert tables to card-style before splitting so tables aren't broken
    # across messages. The send layer's convert_markdown() call is idempotent.
    text = convert_markdown_tables(text)

    # Split first, then assemble each chunk.
    # Use conservative max to leave room for MarkdownV2 expansion at send layer.
    max_text = 3000 - len(prefix) - len(separator)

    text_chunks = split_message(text, max_length=max_text)
    total = len(text_chunks)

    if total == 1:
        if prefix:
            return [f"{prefix}{separator}{text_chunks[0]}"]
        return [text_chunks[0]]

    parts = []
    for i, chunk in enumerate(text_chunks, 1):
        if prefix:
            parts.append(f"{prefix}{separator}{chunk}\n\n[{i}/{total}]")
        else:
            parts.append(f"{chunk}\n\n[{i}/{total}]")
    return parts
