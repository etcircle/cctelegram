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
  - ``notify_waiting(bot, user_id, thread_id, window_id, prompt_text, *, kind)``
  - ``dismiss(bot, user_id, thread_id)``
  - ``clear(user_id, thread_id)`` — synchronous teardown for ``cleanup``
  - ``is_waiting(user_id, thread_id)`` — for digest integration
  - ``is_attention_request(text)`` — heuristic shared with the queue worker
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from ..config import config
from ..session import session_id_for_window, session_manager
from .inbound_aggregator import Route
from .message_sender import (
    TopicSendOutcome,
    strip_sentinels,
    topic_edit,
    topic_send,
)

if TYPE_CHECKING:
    from ..session_monitor import TranscriptEvent
    from .busy_indicator import RunState

logger = logging.getLogger(__name__)


# Minimum seconds between two fresh attention-card sends for the same
# (user, thread). Within this window the same fingerprint is silently
# ignored and a different fingerprint becomes an edit, never a fresh send.
# Mitigates "thinking↔waiting flapping" pointed out in the plan §5.
ATTENTION_REPEAT_DWELL_SECONDS = 30

# Cap the prompt preview embedded in the card body so we don't ship a 4K wall.
PROMPT_PREVIEW_LIMIT = 600

# §2.6 narrow trigger: characters of the final paragraph excerpt embedded in
# the "Awaiting your reply" card. Kept short — the user is meant to scan the
# question and tap into the topic for the full context.
ATTENTION_QUESTION_PREVIEW_CHARS = 200

# Markdown punctuation we strip from the right of the final paragraph before
# checking for a trailing "?" — Claude often closes a question with bold or
# italic emphasis ("**Want me to do X?**"), and trailing markup must not hide
# the question mark from the predicate.
_TRAILING_MARKDOWN_CHARS = ".!*_~`)]}>"

# Ack trailer text on dismiss. Kept short: the user already saw the prompt.
DISMISS_TRAILER = "✅ Acknowledged — Claude is no longer waiting."

# §2.9 Inline-keyboard buttons on end-of-turn-question attention cards.
# Default 24h matches the configured ``attention_button_ttl_seconds`` default;
# kept as a module constant so tests can patch it without forcing a Config
# rebuild.
ATTENTION_BUTTON_TTL_SECONDS = 86400

# Trailer appended to the card body when the user clicks one of the §2.9
# buttons. The buttons are removed from the message in the same edit; this
# trailer is the only audit signal that the card was answered.
VERB_LABELS: dict[str, str] = {
    "yes": "✅ Replied: yes",
    "no": "❌ Replied: no",
    "type": "💬 Reply in chat",
}


@dataclass
class AttentionState:
    """One per ``(user_id, thread_id)``: tracks the live attention card."""

    message_id: int
    window_id: str
    last_fingerprint: str
    state: Literal["idle", "waiting"]
    last_send_at: float
    kind: str


@dataclass
class _AttentionCallbackEntry:
    """Per-token state for a §2.9 attention-card inline-keyboard.

    ``rendered_text`` is the body that was passed to ``topic_send`` so the
    callback handler can reconstruct the original message verbatim on edit
    (Telegram's ``query.message.text`` returns the rendered plain view with
    MarkdownV2 entities stripped, which would corrupt formatting on a naive
    re-edit). ``parse_mode`` is the parse_mode the original send used so the
    edit goes back through the same formatter.
    """

    route: Route
    created_at: float
    rendered_text: str
    parse_mode: str | None


# Keyed by ``(user_id, thread_id_or_0)`` so DM-only routes (thread_id is None)
# still get a stable card slot.
_attention_state: dict[tuple[int, int], AttentionState] = {}


# §2.9: short-lived token → ``_AttentionCallbackEntry`` map. The token is
# encoded into the inline-keyboard ``callback_data`` so we don't have to pack
# the (user_id, thread_id, window_id) route into Telegram's 64-byte
# callback_data cap. Single-use: the callback handler pops the entry on
# click. Stale entries are pruned by ``prune_expired_attention_tokens`` on
# the daily GC tick.
_attention_callback_routes: dict[str, _AttentionCallbackEntry] = {}


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


def final_paragraph_ends_with_question_mark(text: str) -> bool:
    """True when the last paragraph of ``text`` ends with a "?".

    "Paragraph" = blocks separated by a blank line. Trailing markdown emphasis
    (``**...**``, ``*...*``, code ticks, bracketing punctuation) is stripped
    before the check so questions that close with bold or italics aren't
    silently rejected.
    """
    cleaned = strip_sentinels(text or "").rstrip()
    if not cleaned:
        return False
    paragraphs = [p for p in cleaned.split("\n\n") if p.strip()]
    if not paragraphs:
        return False
    last = paragraphs[-1].rstrip()
    last = last.rstrip(_TRAILING_MARKDOWN_CHARS).rstrip()
    return last.endswith("?")


def is_end_of_turn_question(
    event: "TranscriptEvent",
    run_state: "RunState",
) -> bool:
    """§2.6 narrow trigger: only end-of-turn final-act questions raise a card.

    Returns True iff the assistant's text block ended its turn with a
    final-paragraph question that also trips the broader attention heuristic,
    AND the route isn't already showing an interactive-tool card. Mid-turn
    questions and generic ``?``-bearing statements never trip this — the
    surrounding stage gates filter them out before this point.
    """
    # Late import to break circularity (busy_indicator → attention via tests).
    from .busy_indicator import RunState

    if event.role != "assistant" or event.block_type != "text":
        return False
    if event.stop_reason not in {"end_turn", "stop_sequence"}:
        return False
    if run_state is RunState.WAITING_ON_USER:
        return False
    text = event.text or ""
    if not final_paragraph_ends_with_question_mark(text):
        return False
    return is_attention_request(text)


def final_paragraph(text: str) -> str:
    """Return the last paragraph of ``text``, stripped, for card excerpts."""
    cleaned = strip_sentinels(text or "").strip()
    if not cleaned:
        return ""
    paragraphs = [p for p in cleaned.split("\n\n") if p.strip()]
    if not paragraphs:
        return ""
    return paragraphs[-1].strip()


# ── Internals ──────────────────────────────────────────────────────────────


def _key(user_id: int, thread_id: int | None) -> tuple[int, int]:
    return (user_id, thread_id or 0)


def _fingerprint(window_id: str, kind: str, prompt_text: str) -> str:
    body = f"{window_id}\0{kind}\0{prompt_text[:1000]}"
    return hashlib.sha1(body.encode("utf-8", "replace")).hexdigest()


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
    """Render the attention card body."""
    display = _display_name(window_id)
    # The §2.6 end-of-turn trigger pre-formats its own prefix
    # ('🔔 Awaiting your reply — <display>\n"<excerpt>"') so the body is
    # passed through verbatim. Other kinds get the legacy header + preview.
    if kind == "end_of_turn_question":
        body = strip_sentinels(prompt_text or "").rstrip()
        link = _topic_link(chat_id, thread_id)
        if link:
            body = f"{body}\n{link}"
        return body
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
    kind: Literal["interactive_ui", "end_of_turn_question"] = "interactive_ui",
) -> TopicSendOutcome:
    """Idle→waiting sends a fresh card; waiting→waiting edits in place.

    Returns the ``TopicSendOutcome`` of the underlying topic operation so the
    caller can route to repair (Stage 3) on ``TOPIC_NOT_FOUND``/
    ``TOPIC_CLOSED``. Same-fingerprint repeats inside the dwell window are
    silently treated as ``OK`` (no Telegram call made).
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

    # WAITING + same fingerprint within dwell: silent no-op.
    if (
        existing is not None
        and existing.state == "waiting"
        and existing.window_id == window_id
        and existing.last_fingerprint == fingerprint
        and (now - existing.last_send_at) < ATTENTION_REPEAT_DWELL_SECONDS
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
    # message over emitting a fresh audible card. This catches the
    # "user reply → dismiss → handle_interactive_ui re-fires notify_waiting"
    # ping-pong that would otherwise push a duplicate notification.
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

    # §2.9: only end-of-turn-question cards carry the inline-keyboard. Other
    # ``kind`` values (interactive_ui) keep their existing surface untouched —
    # interactive_ui already renders its own keyboards via
    # ``interactive_ui.py``.
    reply_markup: InlineKeyboardMarkup | None = None
    pending_token: str | None = None
    if kind == "end_of_turn_question" and getattr(config, "attention_buttons", True):
        pending_token = _make_attention_callback_token()
        reply_markup = _build_attention_keyboard(pending_token)
        # Bug 2: register the token BEFORE ``topic_send`` so concurrent
        # ``notify_waiting`` calls cannot race against a late registration
        # while topic_send retries through its MarkdownV2 → plain-text
        # fallback (each retry is an ``await``). On send failure we pop the
        # orphan below so it cannot accumulate.
        _attention_callback_routes[pending_token] = _AttentionCallbackEntry(
            route=(user_id, thread_id or 0, window_id),
            created_at=time.monotonic(),
            rendered_text=text,
            parse_mode="MarkdownV2",
        )

    # IDLE → WAITING: send a fresh, audible card. ``reply_markup`` is the
    # one optional kwarg; pass it as a real keyword so pyright keeps strong
    # types for the rest of ``topic_send``'s signature.
    try:
        if reply_markup is not None:
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
                reply_markup=reply_markup,
            )
        else:
            sent, outcome = await topic_send(
                bot,
                op="attention",
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                window_id=window_id,
                text=text,
                disable_notification=False,
                # §2.5.5: attention cards are bot UI, not Claude output —
                # write role="activity" so a quote-reply renders with the
                # UI-noise header instead of being treated as load-bearing
                # assistant conversation.
                role="activity",
                content_type="activity",
                session_id=session_id_for_window(window_id),
            )
    except Exception:
        # Bug 2: if the send raises, don't leave the pre-registered token
        # orphaned in the map.
        if pending_token is not None:
            _attention_callback_routes.pop(pending_token, None)
        raise
    if sent is None:
        # Don't mark waiting if the send failed — caller will route to repair
        # (Stage 3) and may retry, in which case we want to act like idle.
        # Bug 2: also release the pre-registered token; no buttons reached
        # Telegram so it can never be redeemed.
        if pending_token is not None:
            _attention_callback_routes.pop(pending_token, None)
        return outcome

    _attention_state[key] = AttentionState(
        message_id=sent.message_id,
        window_id=window_id,
        last_fingerprint=fingerprint,
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
    _emergency_dm_last_sent.clear()
    _attention_callback_routes.clear()


# ── §2.9 attention-button callback tokens ─────────────────────────────────


def _make_attention_callback_token() -> str:
    """Return a short URL-safe token for use in ``attn:<verb>:<token>``.

    ``secrets.token_urlsafe(8)`` yields ~11 characters, leaving the full
    ``attn:<verb>:<token>`` payload comfortably under Telegram's 64-byte
    callback_data cap even with the longest verb (``type`` → 21 bytes).
    """
    return secrets.token_urlsafe(8)


def _build_attention_keyboard(token: str) -> InlineKeyboardMarkup:
    """Build the §2.9 three-button row attached to end-of-turn-question cards."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Yes", callback_data=f"attn:yes:{token}"),
                InlineKeyboardButton("❌ No", callback_data=f"attn:no:{token}"),
                InlineKeyboardButton(
                    "💬 Type in chat", callback_data=f"attn:type:{token}"
                ),
            ]
        ]
    )


def consume_attention_token(token: str) -> _AttentionCallbackEntry | None:
    """Pop and return the entry bound to ``token``, or ``None`` if unknown.

    Single-use: a second click with the same token returns ``None`` so the
    callback handler can render the "already answered or expired" alert.

    The full ``_AttentionCallbackEntry`` is returned (not just the route) so
    the caller can re-render the original card body verbatim on edit and
    re-bind the token via ``rebind_attention_token`` if a downstream
    aggregator step fails (Bug 3).
    """
    return _attention_callback_routes.pop(token, None)


def rebind_attention_token(token: str, entry: _AttentionCallbackEntry) -> None:
    """Re-insert ``entry`` under ``token`` after a failed consume-and-act.

    Bug 3 (atomic consume): ``consume_attention_token`` pops eagerly so a
    second concurrent click is rejected with "already answered or expired".
    If the downstream aggregator step then raises, the user would otherwise
    be stuck — clicking again hits the expired path and they have no other
    affordance. The callback handler calls this to put the entry back so the
    user can retry the click.
    """
    _attention_callback_routes[token] = entry


def prune_expired_attention_tokens() -> int:
    """Drop token entries older than ``config.attention_button_ttl_seconds``.

    Returns the count of entries dropped so the GC loop can log a summary.
    Called once per day from the same loop that prunes ``message_refs``.
    """
    ttl = float(
        getattr(config, "attention_button_ttl_seconds", ATTENTION_BUTTON_TTL_SECONDS)
    )
    now = time.monotonic()
    expired: list[str] = [
        token
        for token, entry in _attention_callback_routes.items()
        if (now - entry.created_at) > ttl
    ]
    for token in expired:
        _attention_callback_routes.pop(token, None)
    return len(expired)
