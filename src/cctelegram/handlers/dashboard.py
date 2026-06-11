"""Cross-topic dashboard — one owner+chat-scoped overview message per (chat, owner).

Owns the ``/dashboard`` command and the pull-only refresh driver for the Wave C
busy-signal dashboard: a single passive text message listing every topic the
invoking user has bound, grouped needs-attention-first (🔔 waiting on you /
🟡 running / ⚪ idle), repainted by the 1s status poller on content change.

Core responsibilities:
  - ``dashboard_command``: gate on the allowlist, reject DM/General, claim the
    invoking topic as the user's dashboard host (re-running elsewhere MOVES it;
    ``/dashboard pin`` opt-in pins the existing message — never automatic).
    It NEVER writes ``set_group_chat_id`` (hermes R2 P1): thread ids are
    chat-local, so a host claim in chat B would poison the mapping of a bound
    topic with the same thread number in chat A and leak it onto chat B's
    dashboard. ``group_chat_ids`` is written only by the genuine bound-topic
    message seams; the dashboard carries its OWN chat (the command's
    ``effective_chat`` at claim time, the record key afterwards) explicitly
    through every ``topic_send``/``topic_edit``/``topic_delete``.
  - ``render_dashboard``: pure renderer over ``session_manager`` bindings +
    ``route_runtime.snapshot`` per route — scoped to (owner, chat): only
    bindings whose ``group_chat_ids`` mapping resolves to the dashboard's own
    chat render; an unresolvable chat is EXCLUDED (fail closed — a dashboard
    never lists another forum's topics). 🔔 = ``WAITING_ON_USER`` OR an idle
    route whose ``last_assistant_turn_ended_at > last_user_turn_at`` (both
    non-None — after a restart the wall-clock stamps are gone and the
    dashboard renders state-only). Ages are minute-coarse from the monotonic
    ``last_event_at`` so the content hash doesn't churn every second.
  - ``maybe_refresh_dashboards``: called once per status-poll sweep. Edits
    only when the rendered-content hash changed (covers run-state transitions
    AND bind/unbind/rename without one); ``MESSAGE_NOT_MODIFIED`` is success;
    ``MESSAGE_NOT_FOUND`` (the message is provably deleted) self-heals
    (re-send + ``update_dashboard_msg_id``) — a generic ``OTHER`` failure
    only logs and retries next sweep (re-sending on a transient would orphan
    the still-live message); a topic-shaped failure clears the record (no
    self-heal loop into a dead topic — the user re-runs ``/dashboard``
    elsewhere).
  - A per-(chat_id, owner_id) ``asyncio.Lock`` serializes the whole
    Telegram-I/O-spanning claim/move/self-heal flow (pre-C fix 1), with a
    post-send re-read + loser cleanup.
  - ``clear_dashboards_in_thread``: the (chat-scoped) topic-teardown seam,
    called from ``cleanup.clear_topic_state`` and the no-binding branch of
    ``bot.topic_closed_handler`` (pre-C fix 3 + review P2-3/P2-4).

Boundary contract (architecture.md): this module reads
``route_runtime.snapshot`` + ``session_manager`` and sends via the
message-sender helpers ONLY. It never enqueues status updates, never touches
send-layer caches of other modules, never mutates route_runtime, and registers
no observer/callback anywhere (pull-only; c313657 stays forbidden).

Visibility note (honest): the dashboard is owner-FILTERED, not private — any
member of the shared forum can read the posted message.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time

from telegram import Bot, Update
from telegram.ext import ContextTypes

from .. import route_runtime
from ..config import config
from ..route_runtime import RunState
from ..session import session_manager
from . import pane_signals
from .message_sender import (
    TopicSendOutcome,
    safe_reply,
    topic_delete,
    topic_edit,
    topic_send,
)

logger = logging.getLogger(__name__)

DASHBOARD_HEADER = "📊 Sessions"
EMPTY_STATE_TEXT = "No bound topics."

# Topic-shaped outcomes that mean the host topic is gone/unusable. A local
# mirror of the send-layer classification (message_sender owns the enum) — we
# deliberately do NOT import the message-queue module's private set.
_TOPIC_BROKEN_OUTCOMES: frozenset[TopicSendOutcome] = frozenset(
    {
        TopicSendOutcome.TOPIC_NOT_FOUND,
        TopicSendOutcome.TOPIC_CLOSED,
        TopicSendOutcome.FORBIDDEN,
    }
)

_OK_OUTCOMES: frozenset[TopicSendOutcome] = frozenset(
    {TopicSendOutcome.OK, TopicSendOutcome.MESSAGE_NOT_MODIFIED}
)

# Per-(chat_id, owner_id) operation lock — serializes the WHOLE claim/move/
# self-heal flow including its Telegram I/O awaits (pre-C fix 1: the sync
# SessionManager methods alone can't prevent a concurrent double-/dashboard
# double-send under concurrent_updates(True)).
_dashboard_locks: dict[tuple[int, int], asyncio.Lock] = {}

# Per-(chat_id, owner_id) hash of the last successfully rendered+published
# body. The pull-only repaint dedup: state lines + display names + binding
# set all live in the rendered text, so one hash covers run-state
# transitions AND bind/unbind/rename; ages are minute-coarse so the hash is
# stable within the minute (the implicit 60s age-refresh tick).
_last_render_hash: dict[tuple[int, int], str] = {}


def is_user_allowed(user_id: int | None) -> bool:
    """Allowlist gate (module-level so tests can patch it like bot.py's)."""
    return user_id is not None and config.is_user_allowed(user_id)


def _lock_for(key: tuple[int, int]) -> asyncio.Lock:
    lock = _dashboard_locks.get(key)
    if lock is None:
        lock = _dashboard_locks.setdefault(key, asyncio.Lock())
    return lock


def _drop_caches(key: tuple[int, int]) -> None:
    _last_render_hash.pop(key, None)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── renderer (pure) ──────────────────────────────────────────────────────


def _fmt_age(seconds: float) -> str:
    """Minute-coarse age: <60m → "Nm", otherwise whole hours "Nh"."""
    minutes = max(0, int(seconds // 60))
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h"


def render_dashboard(
    owner_id: int, chat_id: int, *, now_mono: float | None = None
) -> str:
    """Render the owner+chat-scoped dashboard body (plain text, crash-proof).

    Reads ``session_manager.iter_thread_bindings()`` filtered to ``owner_id``
    AND to bindings whose topic lives in ``chat_id`` (hermes Wave C review
    P1: thread ids are chat-local and the posted message is visible to the
    whole forum, so a dashboard in chat A must never list chat B's topics).
    The chat of a binding is the persisted ``group_chat_ids`` mapping via
    ``session_manager.get_group_chat_id`` — FAIL CLOSED: a binding whose chat
    cannot be resolved is excluded from every dashboard, never leaked.
    Then ``window_display_names`` + ``route_runtime.snapshot`` per route.
    Groups needs-attention-first:

      🔔 <name> — waiting on you (Xm): ``WAITING_ON_USER``, or idle with
         ``last_assistant_turn_ended_at > last_user_turn_at`` (both non-None;
         either None → never classified unanswered — the restart degradation).
      🟡 <name> — running (tool · Xm): RUNNING / RUNNING_TOOL (the snapshot
         carries tool ids, not names, so RUNNING_TOOL renders a generic
         "tool" marker).
      ⚪ <name> — idle Xm/Xh: everything else.

    Ages come from the monotonic ``last_event_at`` (only the 🔔
    classification needs the wall-clock stamp pair) and are coarsened to
    whole minutes so the refresh driver's content hash doesn't churn every
    second. ``now_mono`` is injectable for deterministic tests.
    """
    now = time.monotonic() if now_mono is None else now_mono
    rows: list[tuple[int, str, str]] = []
    for user_id, thread_id, window_id in session_manager.iter_thread_bindings():
        if user_id != owner_id:
            continue
        # Chat scope (P1, fail closed): unresolvable chat ⇒ excluded.
        if session_manager.get_group_chat_id(user_id, thread_id) != chat_id:
            continue
        route: route_runtime.Route = (owner_id, thread_id or 0, window_id)
        snap = route_runtime.snapshot(route)
        name = session_manager.get_display_name(window_id)
        age = _fmt_age(now - snap.last_event_at) if snap.last_event_at > 0 else None

        is_active = snap.run_state in (RunState.RUNNING, RunState.RUNNING_TOOL)
        unanswered = (
            not is_active
            and snap.run_state is not RunState.WAITING_ON_USER
            and snap.last_assistant_turn_ended_at is not None
            and snap.last_user_turn_at is not None
            and snap.last_assistant_turn_ended_at > snap.last_user_turn_at
        )
        if snap.run_state is RunState.WAITING_ON_USER or unanswered:
            line = f"🔔 {name} — waiting on you" + (f" ({age})" if age else "")
            rows.append((0, name.lower(), line))
        elif is_active:
            if snap.run_state is RunState.RUNNING_TOOL:
                detail = f"(tool · {age})" if age else "(tool)"
            else:
                detail = f"({age})" if age else ""
            line = f"🟡 {name} — running" + (f" {detail}" if detail else "")
            rows.append((1, name.lower(), line))
        else:
            # GH #43: an idle route with fresh pane-reported background
            # shells shows ⏳ instead of ⚪ (a decoration only — 🔔 above
            # outranks it, the run-state is untouched, and a stale count
            # silently falls back to ⚪ via the peek's max_age).
            bg_jobs = pane_signals.peek_background_jobs(route, now=time.time())
            if bg_jobs:
                line = (
                    f"⏳ {name} — idle · {bg_jobs} background "
                    f"job{'s' if bg_jobs != 1 else ''}" + (f" {age}" if age else "")
                )
            else:
                line = f"⚪ {name} — idle" + (f" {age}" if age else "")
            rows.append((2, name.lower(), line))

    if not rows:
        return f"{DASHBOARD_HEADER}\n\n{EMPTY_STATE_TEXT}"
    rows.sort()
    return DASHBOARD_HEADER + "\n\n" + "\n".join(line for _, _, line in rows)


# ── /dashboard command ───────────────────────────────────────────────────


def _thread_id_of(update: Update) -> int | None:
    msg = update.message
    if msg is None:
        return None
    if not getattr(msg, "is_topic_message", False):
        return None
    tid = getattr(msg, "message_thread_id", None)
    if tid is None or tid == 1:  # 1 = General — topic-only architecture
        return None
    return tid


async def dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/dashboard`` — claim THIS topic as the user's dashboard host.

    ``/dashboard pin`` pins the existing dashboard message (opt-in only).
    Rejects DM / the General topic (the dashboard is a topic-hosted message;
    topic-only architecture stands).
    """
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    msg = update.message
    if msg is None:
        return

    thread_id = _thread_id_of(update)
    if thread_id is None:
        await safe_reply(
            msg, "❌ /dashboard works only inside a forum topic — run it there."
        )
        return

    chat_id = msg.chat_id
    # Deliberately NO set_group_chat_id here (hermes R2 P1): thread ids are
    # CHAT-LOCAL, so writing (user, thread)→chat for an UNBOUND dashboard host
    # topic in chat B would overwrite the mapping of a bound topic with the
    # same thread number in chat A — and render_dashboard's chat filter would
    # then leak chat A's binding onto chat B's dashboard. The mapping is
    # written ONLY by the genuine bound-topic message seams; the dashboard
    # carries its own chat (this command's chat_id at claim time, the record
    # key afterwards) through every send/edit/delete.

    parts = (msg.text or "").split()
    sub = parts[1].lower() if len(parts) > 1 else ""
    if sub == "pin":
        await _pin_dashboard(context.bot, msg, chat_id, user.id)
        return
    if sub:
        await safe_reply(
            msg, "❌ Unknown subcommand. Use /dashboard or /dashboard pin."
        )
        return

    await _claim_dashboard(context.bot, msg, chat_id, user.id, thread_id)


async def _claim_dashboard(
    bot: Bot, msg, chat_id: int, owner_id: int, thread_id: int
) -> None:
    """Claim/move/refresh the (chat, owner) dashboard under the operation lock."""
    key = (chat_id, owner_id)
    async with _lock_for(key):
        existing = session_manager.get_dashboard(chat_id, owner_id)
        text = render_dashboard(owner_id, chat_id)

        if existing is not None and existing["thread_id"] == thread_id:
            # Re-run in the host topic: refresh the existing message in place.
            outcome = await topic_edit(
                bot,
                op="dashboard",
                user_id=owner_id,
                chat_id=chat_id,
                thread_id=thread_id,
                window_id=None,
                message_id=existing["msg_id"],
                text=text,
                plain=True,
            )
            if outcome in _OK_OUTCOMES:
                _last_render_hash[key] = _hash(text)
                return
            if outcome in _TOPIC_BROKEN_OUTCOMES:
                session_manager.clear_dashboard(chat_id, owner_id)
                _drop_caches(key)
                await safe_reply(
                    msg, "❌ This topic looks broken — run /dashboard elsewhere."
                )
                return
            if outcome is not TopicSendOutcome.MESSAGE_NOT_FOUND:
                # Generic OTHER (timeout / unclassified transient): the old
                # message may still be live — re-sending would orphan it
                # forever (hermes review P2-2). Leave the persisted msg_id
                # and the render hash alone; the refresh sweep retries.
                logger.warning(
                    "dashboard rerun edit failed (chat=%d owner=%d thread=%d "
                    "outcome=%s) — keeping existing msg_id; retry next sweep",
                    chat_id,
                    owner_id,
                    thread_id,
                    outcome.value,
                )
                await safe_reply(
                    msg,
                    "❌ Could not refresh the dashboard — it will retry shortly.",
                )
                return
            # MESSAGE_NOT_FOUND: the message is provably gone — re-send.

        sent, outcome = await topic_send(
            bot,
            op="dashboard",
            user_id=owner_id,
            chat_id=chat_id,
            thread_id=thread_id,
            window_id=None,
            text=text,
            plain=True,
        )
        if sent is None:
            await safe_reply(msg, "❌ Could not post the dashboard here.")
            return

        # Move: best-effort delete of the old message in the previous host.
        if existing is not None and existing["thread_id"] != thread_id:
            await topic_delete(
                bot,
                op="dashboard_move",
                user_id=owner_id,
                chat_id=chat_id,
                thread_id=existing["thread_id"],
                window_id=None,
                message_id=existing["msg_id"],
            )

        # Loser cleanup (pre-C fix 1): re-read after the Telegram I/O. If a
        # concurrent winner persisted a DIFFERENT msg_id while our send was in
        # flight (cross-process / unexpected writer), delete our own message
        # and keep theirs.
        current = session_manager.get_dashboard(chat_id, owner_id)
        if (
            current is not None
            and current != existing
            and current["msg_id"] != sent.message_id
        ):
            await topic_delete(
                bot,
                op="dashboard_loser_cleanup",
                user_id=owner_id,
                chat_id=chat_id,
                thread_id=thread_id,
                window_id=None,
                message_id=sent.message_id,
            )
            return

        session_manager.set_dashboard(chat_id, owner_id, thread_id, sent.message_id)
        _last_render_hash[key] = _hash(text)


async def _pin_dashboard(bot: Bot, msg, chat_id: int, owner_id: int) -> None:
    """``/dashboard pin`` — opt-in pin of the existing dashboard message.

    ``set_dashboard_pinned`` is recorded ONLY on pin-API success; a failure
    (missing rights, etc.) gets a friendly error and no persist.
    """
    key = (chat_id, owner_id)
    async with _lock_for(key):
        rec = session_manager.get_dashboard(chat_id, owner_id)
        if rec is None:
            await safe_reply(msg, "❌ No dashboard yet — run /dashboard first.")
            return
        try:
            await bot.pin_chat_message(
                chat_id=chat_id,
                message_id=rec["msg_id"],
                disable_notification=True,
            )
        except Exception as e:  # noqa: BLE001 - pin APIs are permission-sensitive
            logger.warning(
                "dashboard pin failed (chat=%d owner=%d): %s", chat_id, owner_id, e
            )
            await safe_reply(
                msg,
                "❌ Could not pin the dashboard — check the bot's pin permission.",
            )
            return
        session_manager.set_dashboard_pinned(chat_id, owner_id, True)
        await safe_reply(msg, "📌 Dashboard pinned.")


# ── pull-only refresh driver ─────────────────────────────────────────────


async def maybe_refresh_dashboards(bot: Bot) -> None:
    """Repaint every persisted dashboard whose rendered content changed.

    Called once per status-poll sweep (NOT per binding) — pull-only, riding
    the existing 1s poller; no observer channel. Per dashboard: render →
    content hash → edit only on change, serialized through the same
    per-(chat, owner) lock as the claim flow. ``MESSAGE_NOT_MODIFIED`` is
    success (W8 precedent). ``MESSAGE_NOT_FOUND`` (message provably deleted)
    self-heals (re-send + ``update_dashboard_msg_id`` under the lock, with a
    loser-cleanup re-read); a generic ``OTHER`` failure logs and leaves the
    msg_id + hash alone so the next sweep retries the edit (review P2-2 — a
    re-send on a transient would orphan the still-live message); a
    topic-shaped failure clears the record so we never self-heal into a dead
    topic (pre-C fix 3). One dashboard's failure never aborts the sweep.
    """
    for chat_id, owner_id, _rec in list(session_manager.iter_dashboards()):
        key = (chat_id, owner_id)
        try:
            text = render_dashboard(owner_id, chat_id)
            h = _hash(text)
            if _last_render_hash.get(key) == h:
                continue
            async with _lock_for(key):
                rec = session_manager.get_dashboard(chat_id, owner_id)
                if rec is None:
                    _drop_caches(key)
                    continue
                outcome = await topic_edit(
                    bot,
                    op="dashboard_refresh",
                    user_id=owner_id,
                    chat_id=chat_id,
                    thread_id=rec["thread_id"],
                    window_id=None,
                    message_id=rec["msg_id"],
                    text=text,
                    plain=True,
                )
                if outcome in _OK_OUTCOMES:
                    _last_render_hash[key] = h
                    continue
                if outcome in _TOPIC_BROKEN_OUTCOMES:
                    logger.warning(
                        "dashboard host topic broken (chat=%d owner=%d thread=%d) "
                        "— clearing record; re-run /dashboard elsewhere",
                        chat_id,
                        owner_id,
                        rec["thread_id"],
                    )
                    session_manager.clear_dashboard(chat_id, owner_id)
                    _drop_caches(key)
                    continue
                if outcome is not TopicSendOutcome.MESSAGE_NOT_FOUND:
                    # Generic OTHER (timeout / unclassified transient): the
                    # message may still be live — re-sending here would orphan
                    # it forever, once per content-hash change (hermes review
                    # P2-2). Keep the persisted msg_id and do NOT advance the
                    # hash, so the next sweep retries the edit.
                    logger.warning(
                        "dashboard refresh edit failed (chat=%d owner=%d "
                        "thread=%d outcome=%s) — keeping msg_id; retry next sweep",
                        chat_id,
                        owner_id,
                        rec["thread_id"],
                        outcome.value,
                    )
                    continue
                # MESSAGE_NOT_FOUND — the message is provably deleted.
                # Self-heal: re-send into the same host topic.
                sent, send_outcome = await topic_send(
                    bot,
                    op="dashboard_self_heal",
                    user_id=owner_id,
                    chat_id=chat_id,
                    thread_id=rec["thread_id"],
                    window_id=None,
                    text=text,
                    plain=True,
                )
                if sent is None:
                    if send_outcome in _TOPIC_BROKEN_OUTCOMES:
                        session_manager.clear_dashboard(chat_id, owner_id)
                        _drop_caches(key)
                    continue
                # Loser-cleanup re-read before persisting the new msg_id.
                current = session_manager.get_dashboard(chat_id, owner_id)
                if current is None or current["msg_id"] != rec["msg_id"]:
                    await topic_delete(
                        bot,
                        op="dashboard_self_heal_loser",
                        user_id=owner_id,
                        chat_id=chat_id,
                        thread_id=rec["thread_id"],
                        window_id=None,
                        message_id=sent.message_id,
                    )
                    continue
                session_manager.update_dashboard_msg_id(
                    chat_id, owner_id, sent.message_id
                )
                _last_render_hash[key] = h
        except Exception as e:  # noqa: BLE001 - one dashboard never aborts the sweep
            logger.warning(
                "dashboard refresh failed (chat=%d owner=%d): %s", chat_id, owner_id, e
            )


# ── topic-teardown seam ──────────────────────────────────────────────────


def clear_dashboards_in_thread(thread_id: int, *, chat_id: int | None) -> None:
    """Drop the dashboard record(s) hosted in ``(chat_id, thread_id)`` (topic
    closed / deleted). Called from ``cleanup.clear_topic_state`` and the
    no-binding branch of ``bot.topic_closed_handler`` so a dead host topic
    never traps the self-heal in a resend loop (pre-C fix 3); the user
    re-runs ``/dashboard`` elsewhere.

    Thread ids are CHAT-LOCAL (hermes review P2-3): chat A's topic 7 closing
    must not delete a valid dashboard in chat B's topic 7, so the clear is
    scoped to ``chat_id``. ``chat_id=None`` (the caller genuinely could not
    resolve the topic's chat) falls back to the old all-chats sweep WITH a
    warning — never strand a dead-topic record silently."""
    if chat_id is None:
        logger.warning(
            "clear_dashboards_in_thread: chat unresolvable for thread %d — "
            "falling back to clearing across ALL chats",
            thread_id,
        )
    for rec_chat_id, owner_id, rec in list(session_manager.iter_dashboards()):
        if rec["thread_id"] != thread_id:
            continue
        if chat_id is not None and rec_chat_id != chat_id:
            continue
        session_manager.clear_dashboard(rec_chat_id, owner_id)
        _drop_caches((rec_chat_id, owner_id))
        logger.info(
            "dashboard cleared with its host topic (chat=%d owner=%d thread=%d)",
            rec_chat_id,
            owner_id,
            thread_id,
        )


def reset_for_tests() -> None:
    """Test-only: drop the per-(chat, owner) locks and render-hash cache."""
    _dashboard_locks.clear()
    _last_render_hash.clear()
