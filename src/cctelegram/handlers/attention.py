"""Topic-first attention card.

One bold, audible message per ``(user_id, thread_id)`` route that says
"Claude is waiting for you". Drives an idle↔waiting state machine so the
notification fires exactly once per attention episode and silent edits keep
the card current without spamming the user.

State machine:

    IDLE ── notify_waiting ──▶ WAITING   (fresh ``topic_send`` — pushes a notification)
    WAITING ── notify_waiting (same fingerprint) ──▶ WAITING (no-op, silent)
    WAITING ── notify_waiting (new fingerprint) ──▶ WAITING (edit-only, silent)
    WAITING ── dismiss ──▶ IDLE          (edit card to acknowledged trailer)
    *       ── clear  ──▶ IDLE           (hard reset — used by topic teardown)

DM is *not* the primary surface. ``notify_waiting`` only emits a topic
message; emergency DM fallback is owned by the queue's broken-topic path.

Public surface:
  - ``notify_waiting(bot, user_id, thread_id, window_id, prompt_text)`` —
    used by ``handle_interactive_ui`` as a fallback when the structured
    interactive card cannot be delivered to the topic.
  - ``dismiss(bot, user_id, thread_id)``
  - ``clear(user_id, thread_id)`` — synchronous teardown for ``cleanup``
  - ``is_waiting(user_id, thread_id)`` — for digest integration
  - ``is_attention_request(text)`` — heuristic shared with the queue worker

The legacy §2.6 / §2.9 "end-of-turn-question" card with Yes / No / Type-in
inline-keyboard buttons was removed 2026-05-17. It fired on any assistant
turn that ended with a ``?`` and presumed a yes/no answer shape, which was
misleading for list-selection questions. Real ``AskUserQuestion`` tool calls
surface as the structured picker via ``handle_interactive_ui``; plain-text
questions no longer mint a half-card.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Literal

from telegram import Bot

from ..session import session_id_for_window, session_manager
from .message_sender import (
    TopicSendOutcome,
    strip_sentinels,
    topic_edit,
    topic_send,
)

logger = logging.getLogger(__name__)


# Minimum seconds between two fresh attention-card sends for the same
# (user, thread). Within this window the same fingerprint is silently
# ignored and a different fingerprint becomes an edit, never a fresh send.
# Mitigates "thinking↔waiting flapping" pointed out in the plan §5.
ATTENTION_REPEAT_DWELL_SECONDS = 30

# Cap the prompt preview embedded in the card body so we don't ship a 4K wall.
PROMPT_PREVIEW_LIMIT = 600

# Ack trailer text on dismiss. Kept short: the user already saw the prompt.
DISMISS_TRAILER = "✅ Acknowledged — Claude is no longer waiting."


@dataclass
class AttentionState:
    """One per ``(user_id, thread_id)``: tracks the live attention card."""

    message_id: int
    window_id: str
    last_fingerprint: str
    generation: int
    state: Literal["idle", "waiting"]
    last_send_at: float
    kind: str


# Keyed by ``(user_id, thread_id_or_0)`` so DM-only routes (thread_id is None)
# still get a stable card slot.
_attention_state: dict[tuple[int, int], AttentionState] = {}


# Monotonic per-route card generation. Separate from ``_attention_state`` so a
# stale state replaced by a clear / dismiss cycle cannot be silently mistaken
# for the live state by an in-flight check.
_attention_generations: dict[tuple[int, int], int] = {}


# Cross-module fence for emergency "Claude is waiting" DMs. interactive_ui
# can decide to DM the user when the topic itself is broken; the cooldown
# stops a single waiting episode from producing repeated DMs. Keyed by
# ``(user_id, thread_id_or_0, window_id)`` so unrelated routes don't share
# a fence.
EMERGENCY_DM_COOLDOWN_SECONDS = 300
_emergency_dm_last_sent: dict[tuple[int, int, str], float] = {}


def should_emit_emergency_dm(
    user_id: int,
    thread_id: int | None,
    window_id: str,
) -> bool:
    """Return True iff an emergency waiting-DM may be sent for this episode.

    Marks the slot as recently-sent on a True return so subsequent calls
    inside ``EMERGENCY_DM_COOLDOWN_SECONDS`` are denied.
    ``interactive_ui._notify_waiting_dm`` routes through this fence so a
    single waiting episode never produces repeated DMs.
    """
    fence_key = (user_id, thread_id or 0, window_id or "")
    now = time.monotonic()
    last = _emergency_dm_last_sent.get(fence_key)
    if last is not None and now - last < EMERGENCY_DM_COOLDOWN_SECONDS:
        return False
    _emergency_dm_last_sent[fence_key] = now
    return True


# ── Heuristic ──────────────────────────────────────────────────────────────


_ATTENTION_CUES: tuple[str, ...] = (
    "tell me which",
    "tell me your pick",
    "tell me your picks",
    "tell me your choice",
    "which option",
    "which approach",
    "which one",
    "do you want me to",
    "want me to proceed",
    "want me to continue",
    "ok to proceed",
    "okay to proceed",
    "ok unless you",
    "unless you object",
    "please confirm",
    "confirm before",
    "before i write",
    "before i proceed",
    "owner decision",
    "owner decisions",
    "your recommendation",
    "go with recommendations",
    "go with your recommendations",
)


def is_attention_request(text: str) -> bool:
    """Heuristic: final assistant text that is probably waiting for user input.

    Lifted unchanged from the legacy ``_looks_like_attention_request`` in
    ``message_queue`` so both call sites share the same definition.
    """
    cleaned = strip_sentinels(text or "").strip()
    if not cleaned:
        return False
    lower = " ".join(cleaned.lower().split())
    if any(cue in lower for cue in _ATTENTION_CUES):
        return True
    # A direct final question is usually attention-worthy; avoid tiny greetings.
    return cleaned.endswith("?") and len(cleaned) > 80


# ── Internals ──────────────────────────────────────────────────────────────


def _key(user_id: int, thread_id: int | None) -> tuple[int, int]:
    return (user_id, thread_id or 0)


def _fingerprint(window_id: str, kind: str, prompt_text: str) -> str:
    body = f"{window_id}\0{kind}\0{prompt_text[:1000]}"
    return hashlib.sha1(body.encode("utf-8", "replace")).hexdigest()


def _bump_attention_generation(key: tuple[int, int]) -> int:
    """Advance and return the route-local attention generation."""
    generation = _attention_generations.get(key, 0) + 1
    _attention_generations[key] = generation
    return generation


def _display_name(window_id: str) -> str:
    return session_manager.get_display_name(window_id) or window_id or "Claude"


def _topic_link(chat_id: int, thread_id: int | None) -> str | None:
    if thread_id is None:
        return None
    chat = str(chat_id)
    if not chat.startswith("-100"):
        return None
    return f"https://t.me/c/{chat[4:]}/{thread_id}"


def _render_card(
    *,
    window_id: str,
    chat_id: int,
    thread_id: int | None,
    kind: str,
    prompt_text: str,
) -> str:
    """Render the attention card body for the interactive-UI fallback path."""
    display = _display_name(window_id)
    preview = strip_sentinels(prompt_text or "").strip()
    if len(preview) > PROMPT_PREVIEW_LIMIT:
        preview = preview[: PROMPT_PREVIEW_LIMIT - 1].rstrip() + "…"
    if kind == "interactive_ui":
        header = f"🔔 Claude is waiting for input — {display}"
    else:
        header = f"🔔 Claude needs a decision — {display}"
    lines: list[str] = [header, "Tap to open the topic and respond."]
    link = _topic_link(chat_id, thread_id)
    if link:
        lines.append(link)
    if preview:
        lines.append("")
        lines.append(preview)
    return "\n".join(lines)


# ── Public API ─────────────────────────────────────────────────────────────


def is_waiting(user_id: int, thread_id: int | None) -> bool:
    """Return True if there is a live (waiting) attention card for this route."""
    state = _attention_state.get(_key(user_id, thread_id))
    return bool(state and state.state == "waiting")


async def notify_waiting(
    bot: Bot,
    *,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    prompt_text: str,
    kind: str = "interactive_ui",
) -> TopicSendOutcome:
    """Idle→waiting sends a fresh card; waiting→waiting edits in place.

    Returns the ``TopicSendOutcome`` of the underlying topic operation so the
    caller can route to repair (Stage 3) on ``TOPIC_NOT_FOUND``/
    ``TOPIC_CLOSED``. Same-fingerprint repeats are silently treated as ``OK``
    (no Telegram call made).

    Only caller today is ``handle_interactive_ui``'s topic-send-failed fallback
    (``kind="interactive_ui"``). The historical ``end_of_turn_question`` kind
    with Yes/No/Type-in inline buttons was removed 2026-05-17.
    """
    key = _key(user_id, thread_id)
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    text = _render_card(
        window_id=window_id,
        chat_id=chat_id,
        thread_id=thread_id,
        kind=kind,
        prompt_text=prompt_text,
    )
    fingerprint = _fingerprint(window_id, kind, prompt_text)
    now = time.monotonic()
    existing = _attention_state.get(key)
    within_dwell = (
        existing is not None
        and existing.window_id == window_id
        and (now - existing.last_send_at) < ATTENTION_REPEAT_DWELL_SECONDS
    )

    # WAITING + same fingerprint: silent no-op.
    if (
        existing is not None
        and existing.state == "waiting"
        and existing.window_id == window_id
        and existing.last_fingerprint == fingerprint
    ):
        logger.debug(
            "attention noop user=%d thread=%s window=%s kind=%s fingerprint=%s",
            user_id,
            thread_id,
            window_id,
            kind,
            fingerprint,
        )
        return TopicSendOutcome.OK

    # Anti-flap guard: if a card was sent very recently (within dwell) for
    # this window — even if the state is now ``idle`` because the user
    # already replied or the worker dismissed — prefer editing the existing
    # message over emitting a fresh audible card.
    if (
        existing is not None
        and existing.state == "idle"
        and within_dwell
        and existing.message_id
    ):
        outcome = await topic_edit(
            bot,
            op="attention",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            window_id=window_id,
            message_id=existing.message_id,
            text=text,
        )
        if outcome in (TopicSendOutcome.OK, TopicSendOutcome.MESSAGE_NOT_MODIFIED):
            existing.last_fingerprint = fingerprint
            existing.generation = _bump_attention_generation(key)
            existing.kind = kind
            existing.state = "waiting"
            return TopicSendOutcome.OK
        # Edit failed — drop slot and fall through to fresh send so the
        # signal is not silently lost.
        logger.debug(
            "attention anti-flap edit failed user=%d thread=%s window=%s outcome=%s",
            user_id,
            thread_id,
            window_id,
            outcome.value,
        )
        _attention_state.pop(key, None)

    # WAITING (any fingerprint): edit the existing card silently.
    if (
        existing is not None
        and existing.state == "waiting"
        and existing.window_id == window_id
        and existing.message_id
    ):
        outcome = await topic_edit(
            bot,
            op="attention",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            window_id=window_id,
            message_id=existing.message_id,
            text=text,
        )
        # MESSAGE_NOT_MODIFIED means Telegram refused a no-op edit — the
        # rendered card already matches what we wanted to write. Treat it as
        # success so we do not "fall through to fresh send" and push an
        # audible duplicate of a card the user is already looking at.
        if outcome in (TopicSendOutcome.OK, TopicSendOutcome.MESSAGE_NOT_MODIFIED):
            existing.last_fingerprint = fingerprint
            existing.generation = _bump_attention_generation(key)
            existing.kind = kind
            return TopicSendOutcome.OK
        # Edit failed (message gone, topic shifted, etc.). Fall through to
        # send a fresh card below — that is closer to the user's intent than
        # silently dropping the update.
        logger.debug(
            "attention edit failed user=%d thread=%s window=%s outcome=%s — sending fresh card",
            user_id,
            thread_id,
            window_id,
            outcome.value,
        )
        _attention_state.pop(key, None)

    pending_generation = _bump_attention_generation(key)

    # IDLE → WAITING: send a fresh, audible card. §2.5.5: attention cards
    # are bot UI, not Claude output — write role="activity" so a quote-reply
    # renders with the UI-noise header instead of being treated as
    # load-bearing assistant conversation.
    sent, outcome = await topic_send(
        bot,
        op="attention",
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        window_id=window_id,
        text=text,
        disable_notification=False,
        role="activity",
        content_type="activity",
        session_id=session_id_for_window(window_id),
    )
    if sent is None:
        # Don't mark waiting if the send failed — caller will route to repair
        # (Stage 3) and may retry, in which case we want to act like idle.
        return outcome

    _attention_state[key] = AttentionState(
        message_id=sent.message_id,
        window_id=window_id,
        last_fingerprint=fingerprint,
        generation=pending_generation,
        state="waiting",
        last_send_at=now,
        kind=kind,
    )
    return TopicSendOutcome.OK


async def dismiss(
    bot: Bot,
    *,
    user_id: int,
    thread_id: int | None,
) -> None:
    """Mark the attention card as acknowledged.

    Edits the card to the ack trailer and flips state back to idle. Safe to
    call when no card exists (no-op). Never DMs.
    """
    key = _key(user_id, thread_id)
    state = _attention_state.get(key)
    if state is None or state.state != "waiting":
        # Idle already — nothing to do. Keep state slot so subsequent
        # notify_waiting still hits the IDLE→WAITING branch cleanly.
        if state is not None:
            state.state = "idle"
        return

    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    if state.message_id:
        outcome = await topic_edit(
            bot,
            op="attention",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            window_id=state.window_id,
            message_id=state.message_id,
            text=DISMISS_TRAILER,
        )
        if outcome is not TopicSendOutcome.OK:
            logger.debug(
                "attention dismiss edit non-OK user=%d thread=%s window=%s outcome=%s",
                user_id,
                thread_id,
                state.window_id,
                outcome.value,
            )
    state.state = "idle"


def clear(user_id: int, thread_id: int | None) -> None:
    """Hard-clear attention state for a route (no Telegram I/O).

    Used by topic teardown (``cleanup.clear_topic_state``) and on session
    rotation so a fresh attention episode never inherits a stale fingerprint.
    """
    _attention_state.pop(_key(user_id, thread_id), None)


def reset_for_tests() -> None:
    """Test-only: drop all attention state."""
    _attention_state.clear()
    _attention_generations.clear()
    _emergency_dm_last_sent.clear()
