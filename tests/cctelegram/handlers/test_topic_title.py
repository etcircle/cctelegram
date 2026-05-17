"""Tests for the compact token formatters used by the message footer."""

from __future__ import annotations

from cctelegram.handlers import topic_title


def test_format_tokens_under_1m() -> None:
    assert topic_title.format_tokens(0) == "0k"
    assert topic_title.format_tokens(500) == "0k"  # rounds down
    assert topic_title.format_tokens(113_000) == "113k"
    assert topic_title.format_tokens(999_999) == "1000k"  # boundary, still k


def test_format_tokens_over_1m() -> None:
    assert topic_title.format_tokens(1_000_000) == "1M"
    assert topic_title.format_tokens(1_200_000) == "1.2M"
    assert topic_title.format_tokens(1_500_000) == "1.5M"


def test_format_max() -> None:
    assert topic_title.format_max(200_000) == "200k"
    assert topic_title.format_max(1_000_000) == "1M"
