"""Compact token formatters used by the per-message context footer.

The forum-topic-rename behavior this module originally implemented was
reverted on user request. Only the formatting helpers survive — they're
shared by ``bot._build_context_footer`` (and were previously by the
``/context`` slash command, also removed).
"""

from __future__ import annotations


def format_tokens(tokens: int) -> str:
    """Render a token count compactly: ``113k``, ``324k``, ``1.2M``."""
    if tokens >= 1_000_000:
        m = tokens / 1_000_000
        s = f"{m:.1f}".rstrip("0").rstrip(".")
        return f"{s}M"
    return f"{round(tokens / 1000)}k"


def format_max(max_tokens: int) -> str:
    """Render the cap label: ``200k`` or ``1M``."""
    if max_tokens >= 1_000_000:
        return "1M"
    return f"{max_tokens // 1000}k"
