"""AFK auto-resolve detection + late-answer card registry (Wave A, plan §A2/§A5).

On Claude Code >=2.1.198 an unanswered AskUserQuestion self-resolves after ~60s
with a synthetic "No response after 60s …" ``tool_result`` (undocumented, no
knob — GH #30740 closed not-planned). This leaf owns the two primitives the
bridge adapts with:

  - ``is_afk_auto_resolve()`` — the two-factor detection contract: an
    unanchored regex over the rendered tool_result text (Factor 1, primary) +
    the authoritative entry-level ``toolUseResult.answers`` emptiness qualifier
    (Factor 2), with the hardened meta-absent rule (sentinel-strip → negative
    wrappers reject FIRST → anchored-start match). False negative = today's
    silent teardown (the safe direction).
  - The in-memory ``aql:`` late-answer card registry — ``token →
    LateAnswerCard`` with the ``live → in_flight → consumed`` single-use state
    machine behind the buttons minted when the picker card is converted to the
    "⏰ Claude proceeded" card, plus the shared card-text / correction-message
    templates.

Observed candidate FUTURE discriminator (2026-07-02 A7 gate capture, 2.1.198):
the AFK resolve's entry-level ``toolUseResult`` also carries ``afkTimeoutMs``
(60000; observed keys: afkTimeoutMs / annotations / answers / questions). It is
deliberately NOT part of the review-converged detection contract (regex +
answers-emptiness + the hardened meta-absent rule) and is preserved verbatim in
``tests/cctelegram/fixtures/afk_auto_resolve_v2.1.198.jsonl`` for future work.

Leaf module: stdlib + ``callback_data`` helpers only. The registry is
deliberately NOT persisted (restart wipes it — the ``aql:`` callback answers a
graceful "expired" modal), NOT a route_runtime field, and registers no
observers (c313657 stays forbidden).
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any

from .callback_data import CB_ASK_LATE, checked_callback_data

logger = logging.getLogger(__name__)

# ── Detection (plan §A2) ──────────────────────────────────────────────────

# Sentinels duplicated from ``transcript_parser.TranscriptParser`` — importing
# the parser would drag ``config`` into this leaf. Drift-guarded by
# ``test_late_answer.py::test_sentinel_constants_match_transcript_parser``.
_EXPANDABLE_QUOTE_START = "\x02EXPQUOTE_START\x02"
_EXPANDABLE_QUOTE_END = "\x02EXPQUOTE_END\x02"

# Factor 1 (primary): unanchored, drift-tolerant on the number/unit — NOT
# full-string equality. Searched over ``msg.text``, which for an AUQ
# tool_result is the raw content wrapped in EXPANDABLE_QUOTE sentinels
# (``transcript_parser._format_tool_result_text`` default branch), so the
# search must be (and is) unanchored.
_AFK_PHRASE = r"No response after \d+\s*(?:s|secs?|seconds?|m|mins?|minutes?)\b"
_AFK_AUTO_RESOLVE_RE = re.compile(_AFK_PHRASE, re.IGNORECASE)
# Meta-absent rule (c): the STRIPPED content must BEGIN with the AFK phrase —
# the CLI's AFK text starts the content; an echo inside a genuine answer never
# does.
_AFK_ANCHORED_RE = re.compile(r"^" + _AFK_PHRASE, re.IGNORECASE)

# Meta-absent rule (b): negative wrappers reject FIRST (genuine answer /
# Esc-rejection shapes observed in the rig transcripts).
_NEGATIVE_WRAPPERS = (
    "Your questions have been answered:",
    "The user doesn't want to proceed",
)


def _strip_sentinels_and_lead(text: str) -> str:
    """Meta-absent rule (a): drop the expandable-quote sentinels + leading ws."""
    stripped = text.replace(_EXPANDABLE_QUOTE_START, "").replace(
        _EXPANDABLE_QUOTE_END, ""
    )
    return stripped.lstrip()


def is_afk_auto_resolve(text: str, tool_result_meta: dict[str, Any] | None) -> bool:
    """True iff an AskUserQuestion tool_result is the ~60s AFK auto-resolve.

    Two-factor (plan §A2, review-converged):

    - **Factor 2 (authoritative):** a dict ``tool_result_meta`` whose
      ``answers`` is a NON-EMPTY dict → False regardless of the regex (a
      genuine free-text answer may ECHO the AFK phrase).
    - **Meta present, answers empty/absent:** the unanchored Factor-1 regex
      decides — the observed AFK shape is exactly ``answers: {}`` + the
      phrase.
    - **Meta absent (None — older/drifted JSONL, or a non-dict entry-level
      ``toolUseResult``):** the regex must NOT decide alone. Hardened rule:
      (a) strip the expandable-quote sentinels + leading whitespace; (b)
      negative wrappers reject FIRST; (c) require the stripped content to
      BEGIN with the AFK phrase. Best-effort by design ([R2 Hermes P3]): the
      monitor's pending-tool ``**AskUserQuestion**(…)`` summary prefix makes
      the anchored match false-NEGATIVE — the safe direction (today's
      teardown); the meta-PRESENT path is the real detection path.
    """
    if isinstance(tool_result_meta, dict):
        answers = tool_result_meta.get("answers")
        if isinstance(answers, dict) and answers:
            return False  # genuine answer — authoritative, regardless of regex
        return bool(_AFK_AUTO_RESOLVE_RE.search(text))
    stripped = _strip_sentinels_and_lead(text)
    for wrapper in _NEGATIVE_WRAPPERS:
        if wrapper in stripped:
            return False
    return bool(_AFK_ANCHORED_RE.match(stripped))


# ── Late-answer card registry (plan §A5) ──────────────────────────────────

STATE_LIVE = "live"
STATE_IN_FLIGHT = "in_flight"
STATE_CONSUMED = "consumed"

# One token per CARD (shared by its option buttons — single-use is per card).
_TOKEN_BYTES = 9  # 12 urlsafe chars; aql:<wid>:<n>:<token> ≈ 22 bytes ≪ 64.

_BUTTON_LABEL_MAX_CHARS = 64


@dataclass
class LateAnswerCard:
    """One converted "⏰ Claude proceeded" card's registry row."""

    owner_id: int
    thread_id: int
    window_id: str
    msg_id: int
    question: str
    labels: dict[int, str]
    state: str = STATE_LIVE


_cards: dict[str, LateAnswerCard] = {}


def clip_label(label: str) -> str:
    """Clip an option label for buttons / edits / the correction message."""
    label = collapse_whitespace(label)
    if len(label) <= _BUTTON_LABEL_MAX_CHARS:
        return label
    return label[: _BUTTON_LABEL_MAX_CHARS - 1].rstrip() + "…"


def mint_card(
    *,
    owner_id: int,
    thread_id: int,
    window_id: str,
    msg_id: int,
    question: str,
    labels: dict[int, str],
) -> str:
    """Register a converted card and return its (single, card-wide) token.

    Labels are stored CLIPPED (≤64 chars, plan §A4) so the buttons, the
    ⏳/✅ edits, the failure-retry keyboard rebuild, and the correction
    message all render the SAME string (disclosed residual A10.5 — the full
    descriptions live in the still-standing 📋 details message).
    """
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    _cards[token] = LateAnswerCard(
        owner_id=owner_id,
        thread_id=thread_id,
        window_id=window_id,
        msg_id=msg_id,
        question=question,
        labels={n: clip_label(label) for n, label in labels.items()},
    )
    return token


def lookup(token: str) -> LateAnswerCard | None:
    """Return the registry row for ``token`` (None post-restart / invalidated)."""
    return _cards.get(token)


def begin_send(token: str) -> bool:
    """Single-use gate: ``live → in_flight``. False when not live/unknown."""
    row = _cards.get(token)
    if row is None or row.state != STATE_LIVE:
        return False
    row.state = STATE_IN_FLIGHT
    return True


def finish_send(token: str, ok: bool) -> None:
    """Resolve an in-flight send: ok → ``consumed``; failure → back to ``live``
    (the retry tap re-arms — plan §A5 step 8's failure branch)."""
    row = _cards.get(token)
    if row is None:
        return
    row.state = STATE_CONSUMED if ok else STATE_LIVE


def invalidate_window(window_id: str) -> None:
    """Drop every card minted for ``window_id``.

    Wired at (a) ``interactive_ui.forget_ask_tool_input`` (the next AUQ's
    tool_result, /clear / session replacement, the generic surface clear) and
    (b) ``remember_ask_tool_input``'s tool_use_id-rotation branch (a BACKSTOP
    only — the real protection against a late tap into a newer live prompt is
    the executor's freshness guards).
    """
    stale = [token for token, row in _cards.items() if row.window_id == window_id]
    for token in stale:
        del _cards[token]


def invalidate_topic(owner_id: int, thread_id: int) -> None:
    """Drop every card for ``(owner_id, thread_id)`` — lifecycle seam (c),
    topic close via ``handlers/cleanup.clear_topic_state``.

    Topic-keyed rather than window-keyed because ``clear_topic_state``'s
    per-route loop only enumerates QUEUED routes (``routes_for_topic`` reads
    ``message_queue._route_queues``) — a queue-less route's window would
    never be visited, stranding its card (the same gap that gave
    ``route_runtime`` its own ``clear_routes_for_topic`` seam; hermes
    round-2 P2 precedent). The registry rows carry (owner, thread) so the
    sweep is exact.
    """
    stale = [
        token
        for token, row in _cards.items()
        if row.owner_id == owner_id and row.thread_id == thread_id
    ]
    for token in stale:
        del _cards[token]


def reset_for_tests() -> None:
    """Test-only: drop all registry state (R3 reset-seam contract)."""
    _cards.clear()


# ── Card text + correction-message templates (plan §A4 / §A5) ─────────────

AFK_CARD_HEADER = "⏰ Claude proceeded after ~60s (no response)."
_TAP_PROMPT = "Tap an option to send a correction:"
_TEXT_ONLY_PROMPT = "Reply in text to send a correction."


def card_text(question: str | None, *, with_keyboard: bool) -> str:
    """Render the converted card's plain-text body (plan §A4).

    ``question`` is the ALREADY-CLIPPED question (``_clip_card_title`` at the
    conversion seam); None (no trusted snapshot) omits the Question line.
    """
    lines = [AFK_CARD_HEADER]
    if question:
        lines.append(f"Question: {question}")
    lines.append(_TAP_PROMPT if with_keyboard else _TEXT_ONLY_PROMPT)
    return "\n".join(lines)


def keyboard_rows(
    window_id: str, labels: dict[int, str], token: str
) -> list[tuple[str, str]]:
    """Build (button_label, callback_data) rows for the ``aql:`` keyboard.

    Returns plain tuples (one per row — plan §A4) so this leaf never imports
    telegram; callers wrap them in ``InlineKeyboardButton``. Used by both the
    conversion seam (first render) and the executor's failure branch
    (re-attach the ORIGINAL keyboard for the retry tap).
    ``checked_callback_data`` enforces Telegram's 64-byte cap.
    """
    return [
        (
            clip_label(label),
            checked_callback_data(f"{CB_ASK_LATE}{window_id}:{n}:{token}"),
        )
        for n, label in sorted(labels.items())
    ]


def collapse_whitespace(text: str) -> str:
    """Collapse ALL whitespace runs (incl. newlines) to single spaces.

    ``send_to_window`` sends literal text then Enter — an embedded newline
    would submit early (plan §A5 message-text rule).
    """
    return " ".join(text.split())


def correction_message(question: str, label: str) -> str:
    """The single-line late-answer user-turn text (plan §A5, exact template)."""
    q = collapse_whitespace(question)
    lbl = collapse_whitespace(label)
    return (
        f'Re your earlier question "{q}" (it auto-resolved after 60s while I '
        f'was away): my answer is "{lbl}". Please course-correct based on this.'
    )
