"""Unit tests for handlers/late_answer.py (Wave A AFK auto-resolve adaptation).

Covers the two-factor ``is_afk_auto_resolve`` detection contract (plan §A2)
pinned RED-first against the REAL captured JSONL (the 2026-07-02 A7 gate
capture + the 2026-07-01 692f0990 rig lines, promoted verbatim to
``tests/cctelegram/fixtures/afk_auto_resolve_v2.1.198.jsonl``), and the
in-memory late-answer card registry state machine (plan §A5).

The detection tests construct ``msg.text`` exactly the way the monitor path
does — ``TranscriptParser._format_tool_result_text`` wraps the raw content in
``EXPANDABLE_QUOTE_START/END`` sentinels (AskUserQuestion has no tool-specific
stats branch, so the default expandable-quote branch applies).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cctelegram.handlers import late_answer
from cctelegram.transcript_parser import TranscriptParser

FIXTURE = Path(__file__).parent.parent / "fixtures" / "afk_auto_resolve_v2.1.198.jsonl"


@pytest.fixture(autouse=True)
def _reset_late_answer():
    late_answer.reset_for_tests()
    yield
    late_answer.reset_for_tests()


def _fixture_tool_results() -> list[tuple[str, Any]]:
    """Return (content, entry_level_toolUseResult) for each tool_result line.

    ``toolUseResult`` is coerced exactly like ``transcript_parser`` does for
    ``ParsedEntry.tool_result_meta``: a non-dict (the Esc-rejection line
    carries a plain string) becomes None.
    """
    out: list[tuple[str, Any]] = []
    for line in FIXTURE.read_text().splitlines():
        d = json.loads(line)
        if d.get("type") != "user":
            continue
        block = d["message"]["content"][0]
        assert block["type"] == "tool_result"
        raw_meta = d.get("toolUseResult")
        meta = raw_meta if isinstance(raw_meta, dict) else None
        out.append((block["content"], meta))
    return out


def _monitor_text(content: str) -> str:
    """Render the raw tool_result content the way the monitor emit path does."""
    return TranscriptParser._format_tool_result_text(content, "AskUserQuestion", None)


def _afk_cases() -> list[tuple[str, Any]]:
    return [
        (content, meta)
        for content, meta in _fixture_tool_results()
        if content.startswith("No response after")
    ]


# ── Detection: fixture-pinned positives ──────────────────────────────────


def test_is_afk_auto_resolve_matches_fixture() -> None:
    """All three real AFK captures (fresh gate + 2x 692f0990 rig) detect True.

    Every captured AFK resolve carries the entry-level ``toolUseResult`` with
    ``answers == {}`` — the meta-PRESENT path where the unanchored regex
    decides.
    """
    cases = _afk_cases()
    assert len(cases) == 3, "fixture must pin all three captured AFK resolves"
    for content, meta in cases:
        assert isinstance(meta, dict) and meta.get("answers") == {}
        assert late_answer.is_afk_auto_resolve(_monitor_text(content), meta) is True


def test_afk_fixture_preserves_afk_timeout_ms_discriminator() -> None:
    """The observed-but-unused ``afkTimeoutMs`` candidate discriminator is
    preserved in the fixture for future work (NOT part of the detection
    contract — see the module docstring)."""
    fresh_gate_metas = [
        meta
        for content, meta in _afk_cases()
        if isinstance(meta, dict) and "afkTimeoutMs" in meta
    ]
    assert fresh_gate_metas, "fixture lost the afkTimeoutMs-carrying capture"
    assert fresh_gate_metas[0]["afkTimeoutMs"] == 60000


def test_afk_summary_prefixed_text_detects_with_meta_present() -> None:
    """The monitor's pending-tool shape prefixes ``**AskUserQuestion**(…)``
    before the expandable quote; with the meta present (the real detection
    path) the unanchored regex still hits."""
    content, meta = _afk_cases()[0]
    text = "**AskUserQuestion**(Session focus)\n" + _monitor_text(content)
    assert late_answer.is_afk_auto_resolve(text, meta) is True


# ── Detection: fixture-pinned negatives ──────────────────────────────────


def test_genuine_answer_false() -> None:
    """The captured genuine answer (non-empty ``answers``) never detects."""
    cases = [
        (content, meta)
        for content, meta in _fixture_tool_results()
        if content.startswith("Your questions have been answered")
    ]
    assert cases, "fixture must pin a genuine-answer tool_result"
    for content, meta in cases:
        assert isinstance(meta, dict) and meta["answers"]
        assert late_answer.is_afk_auto_resolve(_monitor_text(content), meta) is False


def test_esc_rejection_false() -> None:
    """The captured Esc-rejection line — whose entry-level ``toolUseResult``
    is a plain STRING, coerced to meta=None by the parser — never detects
    (negative wrapper + no anchored AFK start)."""
    cases = [
        (content, meta)
        for content, meta in _fixture_tool_results()
        if content.startswith("The user doesn't want to proceed")
    ]
    assert cases, "fixture must pin the Esc-rejection tool_result"
    for content, meta in cases:
        assert meta is None  # str toolUseResult coerces to None
        assert late_answer.is_afk_auto_resolve(_monitor_text(content), meta) is False


def test_free_text_echo_with_answers_false() -> None:
    """Factor 2 is authoritative: a genuine free-text answer ECHOING the AFK
    phrase (regex hit) with NON-EMPTY ``answers`` returns False."""
    content = (
        'Your questions have been answered: "What happened?"="No response after '
        '60s — I was away from keyboard". You can now continue with these '
        "answers in mind."
    )
    meta = {"answers": {"What happened?": "No response after 60s — I was away"}}
    assert late_answer.is_afk_auto_resolve(_monitor_text(content), meta) is False


# ── Detection: hardened meta-absent rule ([R1] both reviewers P2) ─────────


def test_meta_absent_anchored_start_required() -> None:
    """Meta None: an AFK-phrase echo MID-content must not detect — the
    stripped content must BEGIN with the phrase."""
    content = (
        "I decided to proceed on my own. Note that the earlier prompt said "
        "No response after 60s — the user may be away from keyboard."
    )
    assert late_answer.is_afk_auto_resolve(_monitor_text(content), None) is False


def test_meta_absent_genuine_wrapper_rejected() -> None:
    """Meta None: the negative wrappers reject FIRST, even when the stripped
    content BEGINS with the AFK phrase (order pin: (b) before (c))."""
    content = (
        "No response after 60s — but wait: "
        'Your questions have been answered: "Q"="A". You can now continue.'
    )
    assert late_answer.is_afk_auto_resolve(_monitor_text(content), None) is False
    esc_content = (
        "No response after 60s — but The user doesn't want to proceed with "
        "this tool use."
    )
    assert late_answer.is_afk_auto_resolve(_monitor_text(esc_content), None) is False


def test_meta_absent_true_afk_matches() -> None:
    """Meta None with the REAL AFK content in the NO-summary shape (the
    restart/hydrate case where the ``**AskUserQuestion**(…)`` prefix is
    absent) detects True."""
    content, _meta = _afk_cases()[0]
    assert late_answer.is_afk_auto_resolve(_monitor_text(content), None) is True


def test_meta_absent_summary_prefix_is_safe_false_negative() -> None:
    """Documented [R2 Hermes P3] residual: with meta None AND the pending-tool
    ``**AskUserQuestion**(…)`` summary prefix, the anchored match false-
    NEGATIVES (safe direction — today's teardown). The meta-PRESENT path is
    the real detection path."""
    content, _meta = _afk_cases()[0]
    text = "**AskUserQuestion**(Session focus)\n" + _monitor_text(content)
    assert late_answer.is_afk_auto_resolve(text, None) is False


def test_meta_present_empty_answers_unanchored_regex_decides() -> None:
    """Meta present with empty answers → the unanchored regex decides
    (drift-tolerant units too)."""
    for unit_text in ("60s", "60 seconds", "2m", "90 secs"):
        text = _monitor_text(
            f"No response after {unit_text} — the user may be away from keyboard."
        )
        assert late_answer.is_afk_auto_resolve(text, {"answers": {}}) is True
    # Unrelated tool_result with empty answers → regex miss → False.
    assert (
        late_answer.is_afk_auto_resolve(_monitor_text("All done."), {"answers": {}})
        is False
    )


def test_meta_present_malformed_answers_routes_through_hardened_rule() -> None:
    """[Codex P2 + Hermes P2, converged — D5 REJECTED] a PRESENT meta dict
    whose ``answers`` is NOT a dict (None / missing / list / str / …) must
    NOT fall open to the unanchored regex — it routes through the SAME
    hardened meta-absent rule (sentinel-strip → negative wrappers reject
    first → anchored-start match). Only the observed exact shapes decide via
    the unanchored regex (non-empty dict → False; empty dict → regex)."""
    genuine_echo = (
        'Your questions have been answered: "What happened?"="No response '
        'after 60s — I was away". You can now continue with these answers '
        "in mind."
    )
    true_afk = (
        "No response after 60s — the user may be away from keyboard. Proceed "
        "using your best judgment based on the context so far."
    )
    mid_echo = (
        "I decided to proceed on my own. Note that the earlier prompt said "
        "No response after 60s — the user may be away from keyboard."
    )
    # answers=["x"] + a genuine wrapper echoing the AFK phrase → False (the
    # unanchored regex would have said True — the D5 fall-open hole).
    assert (
        late_answer.is_afk_auto_resolve(_monitor_text(genuine_echo), {"answers": ["x"]})
        is False
    )
    # answers="garbage" + true AFK content (anchored start) → True.
    assert (
        late_answer.is_afk_auto_resolve(_monitor_text(true_afk), {"answers": "garbage"})
        is True
    )
    # answers=None + true AFK content → True.
    assert (
        late_answer.is_afk_auto_resolve(_monitor_text(true_afk), {"answers": None})
        is True
    )
    # answers=None + a MID-content AFK echo → False (anchored start required).
    assert (
        late_answer.is_afk_auto_resolve(_monitor_text(mid_echo), {"answers": None})
        is False
    )
    # answers key missing entirely → same hardened routing.
    assert late_answer.is_afk_auto_resolve(_monitor_text(true_afk), {}) is True
    assert late_answer.is_afk_auto_resolve(_monitor_text(mid_echo), {}) is False


def test_sentinel_constants_match_transcript_parser() -> None:
    """Drift guard: the sentinels duplicated into the leaf must stay byte-
    identical to ``TranscriptParser``'s (late_answer must not import the
    parser — it would drag ``config`` into the leaf)."""
    assert (
        late_answer._EXPANDABLE_QUOTE_START == TranscriptParser.EXPANDABLE_QUOTE_START
    )
    assert late_answer._EXPANDABLE_QUOTE_END == TranscriptParser.EXPANDABLE_QUOTE_END


# ── Registry state machine (§A5) ─────────────────────────────────────────


def _mint(**overrides: Any) -> str:
    kwargs: dict[str, Any] = dict(
        owner_id=12345,
        thread_id=42,
        window_id="@7",
        msg_id=999,
        question="Which lane?",
        labels={1: "Lane A", 2: "Lane B"},
    )
    kwargs.update(overrides)
    return late_answer.mint_card(**kwargs)


def test_mint_lookup_roundtrip() -> None:
    token = _mint()
    row = late_answer.lookup(token)
    assert row is not None
    assert (row.owner_id, row.thread_id, row.window_id, row.msg_id) == (
        12345,
        42,
        "@7",
        999,
    )
    assert row.question == "Which lane?"
    assert row.labels == {1: "Lane A", 2: "Lane B"}
    assert row.state == "live"
    assert late_answer.lookup("no-such-token") is None


def test_begin_send_single_use() -> None:
    token = _mint()
    assert late_answer.begin_send(token) is True
    row = late_answer.lookup(token)
    assert row is not None and row.state == "in_flight"
    # Second tap while in flight → False.
    assert late_answer.begin_send(token) is False
    # Unknown token → False.
    assert late_answer.begin_send("nope") is False


def test_finish_send_success_consumes() -> None:
    token = _mint()
    assert late_answer.begin_send(token) is True
    late_answer.finish_send(token, True)
    row = late_answer.lookup(token)
    assert row is not None and row.state == "consumed"
    assert late_answer.begin_send(token) is False


def test_finish_send_failure_resets_to_live() -> None:
    token = _mint()
    assert late_answer.begin_send(token) is True
    late_answer.finish_send(token, False)
    row = late_answer.lookup(token)
    assert row is not None and row.state == "live"
    # The single-use gate re-arms for the retry tap.
    assert late_answer.begin_send(token) is True


def test_invalidate_window_scoped() -> None:
    token_a = _mint(window_id="@7")
    token_b = _mint(window_id="@8")
    late_answer.invalidate_window("@7")
    assert late_answer.lookup(token_a) is None
    assert late_answer.lookup(token_b) is not None


def test_reset_for_tests_clears_registry() -> None:
    token = _mint()
    late_answer.reset_for_tests()
    assert late_answer.lookup(token) is None


def test_mint_clips_labels_to_64_chars() -> None:
    long_label = "x" * 100
    token = _mint(labels={1: long_label})
    row = late_answer.lookup(token)
    assert row is not None
    assert len(row.labels[1]) <= 64


# ── Card text + correction message templates (§A4 / §A5) ─────────────────


def test_card_text_keyboard_shape() -> None:
    text = late_answer.card_text("Which lane?", with_keyboard=True)
    assert text.splitlines() == [
        "⏰ Claude proceeded after ~60s (no response).",
        "Question: Which lane?",
        "Tap an option to send a correction:",
    ]


def test_card_text_text_only_shape() -> None:
    text = late_answer.card_text("Which lane?", with_keyboard=False)
    assert text.splitlines() == [
        "⏰ Claude proceeded after ~60s (no response).",
        "Question: Which lane?",
        "Reply in text to send a correction.",
    ]


def test_card_text_no_snapshot_omits_question() -> None:
    text = late_answer.card_text(None, with_keyboard=False)
    assert text.splitlines() == [
        "⏰ Claude proceeded after ~60s (no response).",
        "Reply in text to send a correction.",
    ]
    assert "Question:" not in text


def test_correction_message_template_single_line() -> None:
    msg = late_answer.correction_message("Which\nlane\tnow?", "Lane  A\nfast")
    assert msg == (
        'Re your earlier question "Which lane now?" (it auto-resolved after '
        '60s while I was away): my answer is "Lane A fast". '
        "Please course-correct based on this."
    )
    assert "\n" not in msg
