"""Interactive UI handling for Claude Code prompts.

Handles interactive terminal UIs displayed by Claude Code:
  - AskUserQuestion: Multi-choice question prompts (single + multi-tab)
  - ExitPlanMode: Plan mode exit confirmation
  - Permission Prompt: Tool permission requests
  - RestoreCheckpoint: Checkpoint restoration selection

Provides:
  - Keyboard navigation (up/down/left/right/enter/esc)
  - Terminal capture and display
  - Interactive mode tracking per user and thread
  - Multi-tab AskUserQuestion state machine: one Telegram card per
    question, edit on tab advance, generation-guarded cleanup with
    orphan rollback.

State dicts are keyed by (user_id, thread_id_or_0) for Telegram topic support.
"""

import asyncio
import hashlib
import json
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters

from ..callback_dispatcher import checked_callback_data
from ..config import config
from ..session import session_id_for_window, session_manager
from ..terminal_parser import (
    AskUserQuestionForm,
    build_form_from_tool_input,
    extract_interactive_content,
    parse_ask_user_question,
    resolve_ask_form,
    visible_pane_liveness,
)
from ..tmux_manager import tmux_manager
from ..utils import atomic_write_json
from . import attention
from .callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_PICK,
    CB_ASK_REFRESH,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_UP,
)
from .message_sender import (
    NO_LINK_PREVIEW,
    TopicSendOutcome,
    safe_answer,
    topic_delete,
    topic_edit,
    topic_edit_reply_markup,
    topic_send,
)

logger = logging.getLogger(__name__)

# Tool names that trigger interactive UI via JSONL (terminal capture + inline keyboard)
INTERACTIVE_TOOL_NAMES = frozenset({"AskUserQuestion", "ExitPlanMode"})

# Track interactive UI message IDs: (user_id, thread_id_or_0) -> message_id
_interactive_msgs: dict[tuple[int, int], int] = {}

# Track interactive mode: (user_id, thread_id_or_0) -> window_id
_interactive_mode: dict[tuple[int, int], str] = {}

# Replay cache of the last completed/JSONL-visible AskUserQuestion
# ``tool_use.input`` payload keyed by window_id. This is NOT a live source
# for a currently pending AUQ: Claude Code writes the AUQ ``tool_use`` line
# to JSONL only after the user answers. The active picker's source of truth
# is the tmux pane. This cache is only an enhancement for explicit JSONL
# dispatches and narrow restart/replay hydration cases where a completed
# AUQ payload is available but the Telegram card still needs rendering.
_last_completed_ask_tool_input: dict[str, dict] = {}

# Companion to ``_last_completed_ask_tool_input``: tracks the JSONL
# ``tool_use.id`` for the currently-cached AUQ. Used by the AUQ context
# message gate (``claim_auq_context_post``) to dedup per-AUQ posts.
# Separate dict (rather than extending the cache value) so existing
# readers of ``_last_completed_ask_tool_input`` stay unchanged.
_last_auq_tool_use_id: dict[str, str] = {}

# Per-window record of the ``tool_use.id`` whose context message has
# already been posted. Compared against ``_last_auq_tool_use_id`` to
# decide whether the next ``handle_interactive_ui`` invocation should
# post a fresh context message or skip (already done for this AUQ).
# Cleared by ``forget_ask_tool_input`` so the next AUQ in the same
# window starts with a clean slate. Persisted alongside
# ``_interactive_msg_meta`` to survive ``launchctl kickstart``.
_auq_context_posted: dict[str, str] = {}


@dataclass(frozen=True)
class _InteractiveMsgMeta:
    """Sidecar metadata for a persisted ``_interactive_msgs`` entry.

    Carries the route-anchored bindings (``window_id`` + ``session_id``)
    that ``hydrate_interactive_state`` needs to validate the entry on
    startup. ``_interactive_msgs`` keeps the bare ``int`` shape for
    existing readers; this dict is the persist source of truth.
    """

    msg_id: int
    window_id: str
    session_id: str
    tool_use_id: str | None
    created_at: str  # ISO 8601 UTC

    def to_dict(self) -> dict[str, str | int | None]:
        return {
            "msg_id": self.msg_id,
            "window_id": self.window_id,
            "session_id": self.session_id,
            "tool_use_id": self.tool_use_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "_InteractiveMsgMeta | None":
        """Build a record from JSON-decoded payload; return None on corruption.

        Rejects msg_id <= 0 and empty/non-string window_id so downstream
        readers (topic_edit / topic_delete) can't send malformed args
        to Telegram. Other fields fall back to "" / None on missing
        keys.
        """
        try:
            msg_id_raw = d["msg_id"]
            window_id_raw = d["window_id"]
        except (KeyError, TypeError):
            return None
        try:
            msg_id = int(msg_id_raw)
        except (TypeError, ValueError):
            return None
        if msg_id <= 0:
            return None
        if not isinstance(window_id_raw, str) or not window_id_raw.strip():
            return None
        tool_use_id_raw = d.get("tool_use_id")
        tool_use_id = str(tool_use_id_raw) if isinstance(tool_use_id_raw, str) else None
        return cls(
            msg_id=msg_id,
            window_id=window_id_raw,
            session_id=str(d.get("session_id") or ""),
            tool_use_id=tool_use_id,
            created_at=str(d.get("created_at", "")),
        )


# Sidecar to ``_interactive_msgs``: full metadata for persistence +
# staleness validation. Every mutation of ``_interactive_msgs`` goes
# through ``_set_interactive_msg`` / ``_clear_interactive_msg`` so the
# two dicts stay in sync. On-disk shape is the union of both dicts at
# ``~/.cc-telegram/interactive_state.json``.
_interactive_msg_meta: dict[tuple[int, int], _InteractiveMsgMeta] = {}


class _ContextSendResult(Enum):
    """Outcome of ``_send_auq_context_message``.

    The caller in ``handle_interactive_ui`` uses this to decide
    whether to roll back the ``claim_auq_context_post`` claim:
    NONE_SENT means no chunks landed → rolling back is safe.
    PARTIAL_SENT means chunk 1 (at least) landed → rolling back
    would re-send chunk 1 on the next render and duplicate it.
    FULL_SENT means all chunks landed → no rollback needed.
    """

    FULL_SENT = "full_sent"
    NONE_SENT = "none_sent"
    PARTIAL_SENT = "partial_sent"


def remember_ask_tool_input(
    window_id: str,
    tool_input: dict | None,
    tool_use_id: str | None = None,
) -> None:
    """Store the latest AskUserQuestion ``tool_use.input`` for a window.

    ``tool_use_id`` (the JSONL ``tool_use.id``) is optional for
    backward compat with call sites that don't have it (e.g. tests
    that only care about the input dict), but production callers
    in ``bot.py`` and ``session_monitor._hydrate_ask_tool_input_cache``
    pass it so the AUQ context-message gate can dedup per AUQ.
    """
    if isinstance(tool_input, dict):
        _last_completed_ask_tool_input[window_id] = tool_input
        if isinstance(tool_use_id, str):
            _last_auq_tool_use_id[window_id] = tool_use_id
        else:
            # Caller doesn't have a tool_use_id (test helper or legacy
            # path). Drop any stale ID + posted state so the context
            # gate's "missing id blocks claim" guarantee holds even if
            # an earlier remember left state behind. Hermes P3 hardening,
            # 2026-05-22 diff review.
            _last_auq_tool_use_id.pop(window_id, None)
            _auq_context_posted.pop(window_id, None)


def forget_ask_tool_input(window_id: str) -> None:
    """Drop the cached AskUserQuestion input for a window (e.g. on tool_result).

    Also persists the cleared ``_auq_context_posted`` state so a
    subsequent restart doesn't carry forward a stale claim marker.
    """
    _last_completed_ask_tool_input.pop(window_id, None)
    _last_auq_tool_use_id.pop(window_id, None)
    had_marker = _auq_context_posted.pop(window_id, None) is not None
    if had_marker:
        _persist_interactive_state()


def claim_auq_context_post(window_id: str) -> bool:
    """Atomic check-and-set for the AUQ context-message gate.

    Returns ``True`` iff the caller owns the right to send the AUQ
    context message (i.e. an AUQ is cached for this window AND its
    ``tool_use.id`` has not yet been context-posted). On ``True``,
    the slot is immediately claimed so concurrent callers see
    ``False`` and skip the duplicate post.

    Synchronous — relies on asyncio's single-thread semantics for
    atomicity between the read and the write. The function does not
    cross an ``await`` boundary, so two coroutines for the same
    window cannot interleave between the check and the set.

    Persists the claim to ``interactive_state.json`` (write-through)
    so a ``launchctl kickstart`` between claim and the next render
    doesn't re-fire the context message for an already-posted AUQ.
    """
    current_id = _last_auq_tool_use_id.get(window_id)
    if current_id is None:
        return False
    if _auq_context_posted.get(window_id) == current_id:
        return False
    _auq_context_posted[window_id] = current_id
    _persist_interactive_state()
    return True


# ── Persistence + hydrate (Wave A, Bug A) ────────────────────────────────
#
# ``_interactive_msgs`` and ``_auq_context_posted`` live in process
# memory; without persistence they wipe on every ``launchctl kickstart``,
# producing the duplicate-picker bug the 2026-05-22 fix addresses. The
# persistence layer is a write-through to ``interactive_state.json``
# (separate from ``state.json`` to avoid coupling with SessionManager's
# save path). Hydrate runs once in ``bot.post_init`` AFTER
# ``resolve_stale_ids()`` and ``load_session_map()`` so window_id remaps
# and session_id bindings are both available to the staleness check.


def _interactive_state_file_path() -> Path:
    """Resolve the on-disk persistence path for interactive UI state."""
    return Path(config.state_file).parent / "interactive_state.json"


def _persist_interactive_state() -> None:
    """Atomic write of ``_interactive_msg_meta`` + ``_auq_context_posted``.

    Called from inside the route-lock-held section, immediately after
    the in-memory mutation it persists. Sync — atomic_write_json is
    blocking but the file is < 10 KB in practice and we don't yield
    the event loop. Errors are logged at WARNING but not raised — a
    disk-full / read-only fs must not bring down the bot. Next
    mutation retries the persist.
    """
    path = _interactive_state_file_path()
    try:
        data: dict[str, dict] = {
            "interactive_msgs": {
                f"{u}:{t}": rec.to_dict()
                for (u, t), rec in _interactive_msg_meta.items()
            },
            "auq_context_posted": dict(_auq_context_posted),
        }
        atomic_write_json(path, data)
    except OSError as exc:
        logger.warning("Failed to persist interactive_state.json: %s", exc)


def _set_interactive_msg(
    ikey: tuple[int, int],
    msg_id: int,
    window_id: str,
    session_id: str,
    tool_use_id: str | None,
) -> None:
    """Write-through: update ``_interactive_msgs`` + sidecar + persist.

    Call inside the route-lock-held section. ``session_id`` may be ""
    (e.g., SessionStart hook hasn't fired yet for a new window);
    hydrate normalizes None vs "" on read.
    """
    _interactive_msgs[ikey] = msg_id
    _interactive_msg_meta[ikey] = _InteractiveMsgMeta(
        msg_id=msg_id,
        window_id=window_id,
        session_id=session_id,
        tool_use_id=tool_use_id,
        created_at=datetime.now(UTC).isoformat(),
    )
    _persist_interactive_state()


def _clear_interactive_msg(ikey: tuple[int, int]) -> int | None:
    """Pop both ``_interactive_msgs`` and the sidecar, persist, return prior msg_id."""
    msg_id = _interactive_msgs.pop(ikey, None)
    _interactive_msg_meta.pop(ikey, None)
    _persist_interactive_state()
    return msg_id


def _refresh_interactive_msg_meta(
    ikey: tuple[int, int],
    msg_id: int,
    window_id: str,
    session_id: str,
    tool_use_id: str | None,
) -> None:
    """Refresh sidecar metadata without resetting ``created_at``.

    Called from the edit-success branch (OK / MESSAGE_NOT_MODIFIED) so
    metadata stays current after a window-id remap, a delayed
    SessionStart hook fire, or a first-time ``tool_use_id`` reveal on
    a previously pane-only render. ``created_at`` is preserved when
    the sidecar entry already exists (the same card is being
    refreshed, not freshly sent).
    """
    existing = _interactive_msg_meta.get(ikey)
    created_at = existing.created_at if existing else datetime.now(UTC).isoformat()
    _interactive_msgs[ikey] = msg_id
    _interactive_msg_meta[ikey] = _InteractiveMsgMeta(
        msg_id=msg_id,
        window_id=window_id,
        session_id=session_id,
        tool_use_id=tool_use_id,
        created_at=created_at,
    )
    _persist_interactive_state()


def hydrate_interactive_state(session_mgr) -> None:
    """Restore ``_interactive_msgs`` + ``_auq_context_posted`` from disk.

    Called ONCE during bot startup from ``bot.post_init`` IMMEDIATELY
    AFTER ``await session_manager.resolve_stale_ids()`` AND
    ``await session_manager.load_session_map()``, and BEFORE
    ``monitor = SessionMonitor()``. Sync — no asyncio.Lock creation,
    no await.

    Per-entry decision tree:
      1. Look up the route's current window via
         ``session_mgr.resolve_window_for_thread(user_id, thread_id)``.
      2. If no current window (unbound route), drop the entry. The
         persisted msg_id has no live owner. Do NOT delete the orphan
         card — it may belong to legitimate user history.
      3. If current window's session_id matches the persisted
         ``rec.session_id``, keep the entry; rewrite ``rec.window_id``
         if the route was remapped (e.g., @12 → @13 across tmux server
         restart).
      4. If current window's session_id mismatches, drop. The route
         was rebound or the session was cleared; no fallback to the
         persisted window_id (would mis-attribute the msg_id to a
         route that no longer owns the card).

    Session-id comparisons normalize None vs "": session_id_for_window
    returns None for windows with no recorded session, persisted
    entries may carry "" for the same condition.

    ``_auq_context_posted`` markers are loaded into a local dict FIRST
    so the meta loop's remap mirror can read and update them; markers
    whose window is unknown to session_mgr are pruned; the local
    dict is then committed to the module-level ``_auq_context_posted``.
    """
    path = _interactive_state_file_path()
    if not path.exists():
        return
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Interactive state hydrate read failed: %s", exc)
        return
    if not isinstance(data, dict):
        return

    def _norm(s: str | None) -> str:
        return s or ""

    state_mutated_any = False
    pruned_ctx_any = False

    raw_ctx_for_load = data.get("auq_context_posted")
    ctx_markers: dict[str, str] = {}
    if isinstance(raw_ctx_for_load, dict):
        for wid, tuid in raw_ctx_for_load.items():
            if isinstance(wid, str) and isinstance(tuid, str):
                ctx_markers[wid] = tuid

    raw = data.get("interactive_msgs")
    if isinstance(raw, dict):
        for key_str, payload in raw.items():
            if not isinstance(payload, dict) or not isinstance(key_str, str):
                continue
            try:
                u_str, t_str = key_str.split(":")
                user_id = int(u_str)
                thread_id = int(t_str)
            except ValueError:
                continue
            rec = _InteractiveMsgMeta.from_dict(payload)
            if rec is None:
                continue

            current_window = session_mgr.resolve_window_for_thread(
                user_id, thread_id if thread_id else None
            )
            if current_window is None:
                logger.info(
                    "AUQ hydrate: dropping unowned interactive_msg "
                    "(user=%d thread=%d persisted_window=%s "
                    "persisted_session=%s)",
                    user_id,
                    thread_id,
                    rec.window_id,
                    (rec.session_id[:8] if rec.session_id else "<empty>"),
                )
                state_mutated_any = True
                continue

            cur_session = session_id_for_window(current_window)
            if _norm(cur_session) != _norm(rec.session_id):
                logger.info(
                    "AUQ hydrate: dropping stale interactive_msg "
                    "(user=%d thread=%d persisted_window=%s "
                    "current_window=%s persisted_session=%s "
                    "current_session=%s)",
                    user_id,
                    thread_id,
                    rec.window_id,
                    current_window,
                    (rec.session_id[:8] if rec.session_id else "<empty>"),
                    (cur_session[:8] if cur_session else "<none>"),
                )
                state_mutated_any = True
                continue

            if current_window != rec.window_id:
                logger.info(
                    "AUQ hydrate: remapping persisted window %s → %s "
                    "(user=%d thread=%d session matched)",
                    rec.window_id,
                    current_window,
                    user_id,
                    thread_id,
                )
                old_window_id = rec.window_id
                rec = _InteractiveMsgMeta(
                    msg_id=rec.msg_id,
                    window_id=current_window,
                    session_id=rec.session_id,
                    tool_use_id=rec.tool_use_id,
                    created_at=rec.created_at,
                )
                state_mutated_any = True
                old_marker = ctx_markers.get(old_window_id)
                if (
                    old_marker is not None
                    and rec.tool_use_id is not None
                    and old_marker == rec.tool_use_id
                ):
                    ctx_markers.pop(old_window_id, None)
                    ctx_markers[current_window] = old_marker
                    logger.info(
                        "AUQ hydrate: also remapped context marker %s → %s",
                        old_window_id,
                        current_window,
                    )

            ikey = (user_id, thread_id)
            _interactive_msgs[ikey] = rec.msg_id
            _interactive_msg_meta[ikey] = rec

    known_windows = set(session_mgr.window_states.keys())
    for wid, tuid in ctx_markers.items():
        if wid not in known_windows:
            logger.debug(
                "AUQ hydrate: pruning stale context-posted marker for "
                "unknown window %s",
                wid,
            )
            pruned_ctx_any = True
            continue
        _auq_context_posted[wid] = tuid

    if isinstance(raw_ctx_for_load, dict) and len(_auq_context_posted) != len(
        raw_ctx_for_load
    ):
        pruned_ctx_any = True

    logger.info(
        "AUQ hydrate: %d interactive_msg entries, %d context-posted markers",
        len(_interactive_msgs),
        len(_auq_context_posted),
    )

    if state_mutated_any or pruned_ctx_any:
        _persist_interactive_state()


def _resolve_ask_tool_input(window_id: str, explicit: dict | None) -> dict | None:
    """Pick the freshest tool_input available for an AskUserQuestion render."""
    if explicit is not None:
        return explicit
    return _last_completed_ask_tool_input.get(window_id)


# Public sibling-imported alias for use by ``bot.py`` callback handlers.
# ``_resolve_ask_tool_input`` is module-internal by convention (underscore
# prefix), but the pick-token callback at ``bot.py:2896`` needs the same
# cache to achieve fingerprint parity between render and validate (FA4 in
# the plan). Exposing it under a public name keeps the import boundary
# honest.
def resolve_ask_tool_input(window_id: str) -> dict | None:
    """Return the cached AskUserQuestion ``tool_use.input`` for ``window_id``.

    Used by the pick-token callback validator in ``bot.py`` to feed
    ``resolve_ask_form`` the same JSONL payload the render path saw, so
    the two paths produce byte-identical fingerprints.
    """
    return _last_completed_ask_tool_input.get(window_id)


# ── PR 3: rerender_guard sentinel + digest helper ────────────────────────
#
# Distinct sentinel object so callers can pass ``None`` to mean "guard
# against a present-tool-input that gets cleared" (the callback path
# captures the digest before releasing the lock; if cache is later cleared
# or replaced, the digest mismatch aborts the re-render). ``_NO_GUARD``
# means "don't guard, just render" — used by the monitor / JSONL dispatch
# paths where there's no prior snapshot.
_NO_GUARD: object = object()


def _ask_tool_input_digest(payload: dict | None) -> str | None:
    """Stable content digest of a cached AskUserQuestion ``tool_use.input``.

    Comparison must be content-based (not object identity) because the
    cache may return structurally-equal-but-distinct dicts across calls.
    Used by the ``rerender_guard`` mechanism in ``handle_interactive_ui``
    to detect "cache cleared" or "replaced with a new prompt" between
    pick-token callback exit and re-render entry.

    Returns ``None`` when the input is ``None`` so callers can distinguish
    "cache was cleared" (digest is None) from "cache held this payload"
    (digest is a hex string).
    """
    if payload is None:
        return None
    try:
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        # The cache stores parsed JSON, so this branch should not fire in
        # practice. Treat unserializable input as "no useful digest" so
        # the guard at least doesn't crash.
        return None
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()


# ── Per-route asyncio.Lock ───────────────────────────────────────────────
#
# Lock contract (rewritten for P2.1 — honest version after multi-tab was
# disabled in PR #14; the original aspirational contract claimed coverage
# the actual single-card code never honoured):
#
#   ACTUALLY PROTECTED by ``_get_route_lock`` today:
#     * ``_multi_tab_sessions`` — dormant infra (multi-tab dispatch is
#       gate-off in ``handle_interactive_ui``). The lock matters here only
#       because in-flight render coroutines re-check generation under it.
#     * ``_pick_token_cache`` / ``_pick_tokens`` — pruned under the lock in
#       ``clear_interactive_msg`` (P2.2) so a concurrent ``handle_interactive_ui``
#       (which awaits between pane capture and mint) can't post a card whose
#       tokens point at a cache row the cleanup just dropped.
#
#   NOT PROTECTED today (single-producer in practice, single-event-loop
#   means sync dict writes don't interleave with other coroutines'
#   sync dict writes):
#     * ``_interactive_msgs`` — written by ``handle_interactive_ui`` and
#       ``clear_interactive_msg``; the read-then-write inside
#       ``handle_interactive_ui`` happens after the last await, so a
#       concurrent clear can't tear it.
#     * ``_interactive_mode`` — same shape as ``_interactive_msgs``.
#     * ``_last_completed_ask_tool_input`` — replay cache written by
#       ``session_monitor`` (single writer) and read everywhere; dict ops
#       are atomic enough. It is not a live pending-AUQ source.
#
#   RELEASED across Telegram I/O awaits — serializing those would stall
#   multi-route concurrency.
#
#   Non-reentrant: the pick-token callback handler MUST NOT hold the lock
#   across ``await handle_interactive_ui(...)``. Validate, release, then
#   call.
#
# TO RE-ENABLE MULTI-TAB: re-wrap ``_interactive_msgs`` / ``_interactive_mode``
# reads/writes in this lock — single-producer assumption breaks once a form
# has N cards across N tabs in flight. The original contract above is the
# target shape; git blame this comment for the pre-PR-#14 history.
#
# ``_route_locks`` are created on demand and never cleaned up. The keyspace
# is bounded by (user_id × thread_id) pairs the bot has seen — small in
# practice, and the lock objects are tiny.
_route_locks: dict[tuple[int, int], asyncio.Lock] = {}


def _get_route_lock(user_id: int, thread_id: int | None) -> asyncio.Lock:
    """Get or create the per-route lock used by the multi-tab state machine."""
    key = (user_id, thread_id or 0)
    if key not in _route_locks:
        _route_locks[key] = asyncio.Lock()
    return _route_locks[key]


@dataclass
class _MultiTabSession:
    """In-memory state for a live multi-question AskUserQuestion form.

    One session per route. Mutually exclusive with ``_interactive_msgs[key]``
    — the first-time multi-tab render clears the single-card entry, and
    cleanup drops both maps atomically.

    ``message_ids`` is a fixed-size list aligned with the questions
    matrix: ``message_ids[i]`` is the Telegram message_id for tab ``i``,
    or ``None`` when that tab's card failed to send (partial bundle).
    PR 3.2 changed this from append-only to fixed-size to fix the
    index-misalignment bug: if Q1's send failed and Q2's succeeded,
    the append-only list put Q2's id at message_ids[0], and
    ``edit_advance`` then targeted the wrong message.
    """

    window_id: str
    shape_digest: str  # sha1 over titles + ordered labels + counts
    message_ids: list[int | None] = field(default_factory=list)
    current_tab_idx: int = 0
    # Incremented by cleanup. In-flight render/edit coroutines re-acquire
    # the lock, compare their captured ``generation_at_entry`` against the
    # current value, and roll back any side-effects they produced on
    # mismatch.
    generation: int = 0


_multi_tab_sessions: dict[tuple[int, int], _MultiTabSession] = {}


class _RenderCancelled(Exception):
    """Raised inside a multi-tab render when the route's generation has
    been bumped (i.e. cleanup fired). Caller catches and rolls back any
    orphan message IDs produced before the cancellation point.
    """


def has_interactive_surface(user_id: int, thread_id: int | None) -> bool:
    """True if the route currently owns interactive cards (single OR multi-tab).

    Callers in ``bot.py`` (tool_result path) and ``status_polling.py``
    (UI-gone path) gate cleanup on this predicate instead of
    ``get_interactive_msg_id`` alone, which would miss multi-tab sessions
    (those don't populate ``_interactive_msgs``).
    """
    key = (user_id, thread_id or 0)
    return key in _interactive_msgs or key in _multi_tab_sessions


# ── PR 2b: structured option-pick callback tokens ────────────────────────
#
# When ``handle_interactive_ui`` lands a structured AskUserQuestion card, it
# mints one callback token per option button. The token resolves server-side
# (via ``_pick_tokens``) to the (window, fingerprint, option_number,
# option_label) bound at mint time. On click, the callback handler:
#
#   1. Looks up the token. Missing / expired → "Card expired, refresh".
#   2. Re-captures the pane and re-runs the parser. None → "Form gone".
#   3. Compares ``form.fingerprint()`` to the token's pinned value. Mismatch
#      → "Form changed, refreshing" + repaint the card. Do NOT dispatch
#      the key — that's the load-bearing staleness check Hermes flagged.
#   4. Sends the literal digit via tmux_manager.send_keys(literal=True,
#      enter=False). Marks the token used (single-use).
#
# Token lifetime is short (5 minutes) because the form is interactive and
# the user will either resolve or abandon it within minutes. No daily GC
# needed; ``_prune_expired_pick_tokens`` runs on every mint so the map
# stays bounded.

# Conservative TTL — Claude Code's AskUserQuestion picker stays open at most
# a few minutes in practice. 300s is comfortably longer than the slowest
# turnaround but short enough that a forgotten token can't pile up.
_PICK_TOKEN_TTL_SECONDS = 300.0


@dataclass(frozen=True)
class _PickTokenEntry:
    """Server-side state bound to a single option-button click.

    Frozen because once minted, the entry must not mutate (the staleness
    check compares the *minted* fingerprint against the *current* parse).
    Marking entries used is done by popping from the map, not flipping a
    field, so single-use semantics are enforced by ``consume_pick_token``.
    """

    window_id: str
    user_id: int
    thread_id: int | None
    fingerprint: str  # form.fingerprint() at the moment the keyboard rendered
    option_number: int  # the numeric shortcut to send (1-9)
    option_label: str  # human label, used for log messages + sanity
    is_review_submit: bool  # True iff this click should submit the review screen
    expires_at: float  # monotonic clock deadline


_pick_tokens: dict[str, _PickTokenEntry] = {}

# Stable per-route cache so a re-render of the same form (same fingerprint)
# reuses the same callback tokens. Without this, every status-polling tick
# would mint fresh random tokens, the reply_markup would never match the
# previous edit, Telegram would never return MESSAGE_NOT_MODIFIED, and the
# bot would re-edit the card every poll cycle while the user is reading it.
# Hermes peer review flagged this as a no-ship before fix.
#
# Key: (user_id, thread_id_or_0, window_id, fingerprint)
# Value: list[token] — one token per option button, in the order the
#        keyboard builder emitted them.
_pick_token_cache: dict[tuple[int, int, str, str], list[str]] = {}


def _prune_expired_pick_tokens(now: float | None = None) -> None:
    """Drop expired tokens from the in-memory map.

    Runs on every mint — the map is small (≤ #options per active picker, so
    typically ≤ 10) so the O(n) scan is cheap. Cache entries pointing at
    expired tokens are pruned too so a stale fingerprint can't pin a dead
    token list.
    """
    if now is None:
        now = time.monotonic()
    stale = [tok for tok, e in _pick_tokens.items() if e.expires_at <= now]
    for tok in stale:
        _pick_tokens.pop(tok, None)
    if stale:
        stale_set = set(stale)
        for cache_key, tokens in list(_pick_token_cache.items()):
            if any(t in stale_set for t in tokens):
                _pick_token_cache.pop(cache_key, None)


def _mint_pick_token(entry: _PickTokenEntry) -> str:
    """Register a token for an option button. Returns the token id.

    Token is 12 hex chars from ``secrets.token_hex(6)``. The full callback
    payload is ``aqp:<token>`` → 17 chars total, well under Telegram's
    64-byte cap.
    """
    _prune_expired_pick_tokens()
    # 6 bytes = 12 hex chars. Collision space ~2^48; with at most a few
    # tokens live at any moment, accidental clash is astronomically
    # unlikely. Loop on the off chance.
    for _ in range(8):
        token = secrets.token_hex(6)
        if token not in _pick_tokens:
            _pick_tokens[token] = entry
            return token
    # Pathological — shouldn't happen, but signal loudly rather than
    # silently overwrite an existing token.
    raise RuntimeError("Unable to mint a unique pick token")


def peek_pick_token(token: str) -> _PickTokenEntry | None:
    """Look up a token WITHOUT consuming it. Returns the entry or None.

    P1.5/CB3: callbacks MUST validate ``entry.user_id`` against the click
    sender's ID before consuming. Looking up + consuming in one step (the
    old ``consume_pick_token``-only API) made it possible for a wrong user
    to click another user's button, hit the "not your card" reject, and
    still burn the token + its sibling cache row. The legitimate owner's
    next click then 404'd with "Card expired, refreshing."

    Use this for the validate phase; call ``consume_pick_token`` only after
    user/window/fingerprint checks pass.

    Expired tokens are pruned as a side effect so the caller can treat a
    None return as "definitely gone" without re-checking expiry.
    """
    _prune_expired_pick_tokens()
    return _pick_tokens.get(token)


def consume_pick_token(token: str) -> _PickTokenEntry | None:
    """Pop a token (single-use). Returns the entry or None if missing/expired.

    Also drops the cache entry for the form generation this token belonged
    to: once a click lands, the form is about to advance to the next tab /
    question / review screen, and the next render needs fresh tokens
    against the new fingerprint anyway. Leaving the cache populated would
    keep handing out the just-consumed token (which would then 404 on
    click).

    SECURITY (CB3): this mutates state. Callers MUST validate ownership via
    ``peek_pick_token`` first before calling this — otherwise a wrong-user
    click destroys the legitimate owner's token + sibling cache row.
    """
    _prune_expired_pick_tokens()
    entry = _pick_tokens.pop(token, None)
    if entry is not None:
        cache_key = (
            entry.user_id,
            entry.thread_id or 0,
            entry.window_id,
            entry.fingerprint,
        )
        # Remove the cache row AND drop every sibling token belonging to
        # that row — the whole generation is dead now that the user has
        # acted on one of its buttons.
        sibling_tokens = _pick_token_cache.pop(cache_key, None)
        if sibling_tokens:
            for sib in sibling_tokens:
                if sib != token:
                    _pick_tokens.pop(sib, None)
    return entry


def reset_pick_tokens_for_tests() -> None:
    """Clear the pick-token map. Test-only helper."""
    _pick_tokens.clear()
    _pick_token_cache.clear()


# Cross-module emergency DM cooldown lives in ``handlers.attention``
# (``attention.should_emit_emergency_dm``). The interactive-UI surface and
# the assistant-text surface in ``handlers.message_queue`` share that fence
# so a single broken-topic episode cannot fire two DMs from two surfaces.


def get_interactive_window(user_id: int, thread_id: int | None = None) -> str | None:
    """Get the window_id for user's interactive mode."""
    return _interactive_mode.get((user_id, thread_id or 0))


def set_interactive_mode(
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
) -> None:
    """Set interactive mode for a user."""
    logger.debug(
        "Set interactive mode: user=%d, window_id=%s, thread=%s",
        user_id,
        window_id,
        thread_id,
    )
    _interactive_mode[(user_id, thread_id or 0)] = window_id


# Callback registry: fired when an interactive lifecycle ends, so per-lifecycle
# state owned by other modules (e.g. ``status_polling._absent_streak``) can be
# dropped synchronously instead of waiting for a later poll to notice the
# transition. Codex P2 (2026-05-20): without this hook, an external clear via
# the JSONL tool_result handler in ``bot.handle_new_message`` can land between
# polls and leave the next lifecycle inheriting a stale counter, defeating the
# hysteresis that protects the live picker. Callback signature:
# ``(user_id, thread_id_or_0, cleared_window_id_or_none)``.
ClearCallback = Callable[[int, int, str | None], None]
_clear_callbacks: list[ClearCallback] = []


def register_clear_callback(callback: ClearCallback) -> None:
    """Register a synchronous callback fired when an interactive lifecycle
    ends (both ``clear_interactive_mode`` and ``clear_interactive_msg``).

    Registrations are process-lifetime; identity dedupe guards against
    accidental double-registration on bot reload. Exceptions in one callback
    do not prevent the next from running.
    """
    if callback in _clear_callbacks:
        return
    _clear_callbacks.append(callback)


def _fire_clear(user_id: int, thread_id: int, window_id: str | None) -> None:
    """Notify all registered clear callbacks for a route lifecycle end."""
    for cb in list(_clear_callbacks):
        try:
            cb(user_id, thread_id, window_id)
        except Exception as e:
            logger.error(
                "clear callback error user=%d thread=%d window=%s: %s",
                user_id,
                thread_id,
                window_id,
                e,
            )


def clear_interactive_mode(user_id: int, thread_id: int | None = None) -> None:
    """Clear interactive mode for a user (without deleting message)."""
    logger.debug("Clear interactive mode: user=%d, thread=%s", user_id, thread_id)
    cleared_window_id = _interactive_mode.pop((user_id, thread_id or 0), None)
    _fire_clear(user_id, thread_id or 0, cleared_window_id)


# Sentinel returned by ``assert_nav_dispatchable`` for the ESC-on-stale branch:
# ESC must still call ``clear_interactive_msg`` even when the picker is gone;
# all other nav callbacks just short-circuit. Use a Literal so pyright can
# narrow ``w is None`` / ``w == NAV_ESC_CLEAR`` cleanly in callers.
from typing import Literal, overload  # noqa: E402

from ..tmux_manager import TmuxWindow  # noqa: E402

NAV_ESC_CLEAR: Literal["__esc_clear__"] = "__esc_clear__"


@overload
async def assert_nav_dispatchable(
    query,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    *,
    tmux_mgr=None,
    is_esc: Literal[False] = False,
) -> TmuxWindow | None: ...


@overload
async def assert_nav_dispatchable(
    query,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    *,
    tmux_mgr=None,
    is_esc: Literal[True],
) -> TmuxWindow | Literal["__esc_clear__"] | None: ...


async def assert_nav_dispatchable(
    query,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    *,
    tmux_mgr=None,
    is_esc: bool = False,
) -> TmuxWindow | Literal["__esc_clear__"] | None:
    """Guard a nav-keystroke callback before it dispatches keys to tmux.

    P1.1 + P1.3 + CB1 + CB5 + F1 + F2 + F3, all collapsed into one helper
    called from every CB_ASK_* nav callback in ``bot.py``. Returns:

      * a live ``tmux.Window`` — caller proceeds with ``send_keys`` /
        ``handle_interactive_ui``.
      * ``NAV_ESC_CLEAR`` — ESC carve-out (F2): the picker is closed but
        ESC was tapped; the user wants the card gone. Caller runs
        ``clear_interactive_msg`` to reap the Telegram artefact.
      * ``None`` — non-ESC nav callback against a non-live surface, or
        any guard failure. Caller has already had a ``query.answer``
        explanation; just ``return``.

    Guards, in order (cheapest first; short-circuit on first failure):
      1. Route owns an interactive surface (``has_interactive_surface``).
      2. The callback's window matches this route's active interactive
         window (``get_interactive_window``).
      3. ``find_window_by_id`` resolves to a live tmux window.
      4. **Visible-only** capture (scrollback=0) → three-state liveness
         (``visible_pane_liveness``). PRESENT proceeds; ABSENT short-
         circuits; UNKNOWN proceeds (CB1: empty/mid-redraw capture must
         NOT destructively clear a live picker).

    The visible-only capture is critical (P1.3): scrollback can contain
    stale historical pickers that match ``is_interactive_ui``, so a
    scrollback-fed liveness check returns True even when the user is
    back at the shell. CB5 (long-question case) is handled inside
    ``visible_pane_liveness`` via the picker-anchor fallback.
    """
    if tmux_mgr is None:
        tmux_mgr = tmux_manager
    if not has_interactive_surface(user_id, thread_id):
        if is_esc:
            # Cleanup is idempotent and what ESC wants.
            return NAV_ESC_CLEAR
        await safe_answer(query, "No live interactive UI")
        return None
    if get_interactive_window(user_id, thread_id) != window_id:
        if is_esc:
            return NAV_ESC_CLEAR
        await safe_answer(query, "Window changed")
        return None
    w = await tmux_mgr.find_window_by_id(window_id)
    if w is None:
        if is_esc:
            return NAV_ESC_CLEAR
        await safe_answer(query, "Window not found")
        return None
    visible = await tmux_mgr.capture_pane(w.window_id, scrollback_lines=0)
    state = visible_pane_liveness(visible)
    if state == "absent":
        if is_esc:
            return NAV_ESC_CLEAR
        await safe_answer(query, "Picker closed, refreshing")
        return None
    # PRESENT or UNKNOWN: proceed. UNKNOWN explicitly continues per CB1.
    return w


def get_interactive_msg_id(user_id: int, thread_id: int | None = None) -> int | None:
    """Get the interactive message ID for a user."""
    return _interactive_msgs.get((user_id, thread_id or 0))


def _topic_link(chat_id: int, thread_id: int | None) -> str | None:
    """Build a best-effort Telegram private supergroup topic link."""
    if thread_id is None:
        return None
    chat = str(chat_id)
    if not chat.startswith("-100"):
        return None
    return f"https://t.me/c/{chat[4:]}/{thread_id}"


async def _notify_waiting_dm(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None,
    prompt_text: str,
    session_mgr,
) -> None:
    """Emergency-only DM fallback when the topic-first attention card fails.

    The normal path is ``attention.notify_waiting`` (in-topic card). This DM
    is reached only when the topic itself cannot be written to (deleted,
    closed, forbidden) so the user still gets a signal that Claude is blocked.

    Cooldown is owned by ``attention.should_emit_emergency_dm`` so repeated
    waiting episodes for the same route don't stack DMs.
    """
    if not attention.should_emit_emergency_dm(user_id, thread_id, window_id):
        logger.debug(
            "Skipping interactive waiting DM due to shared cooldown "
            "user=%d thread=%s window=%s",
            user_id,
            thread_id,
            window_id,
        )
        return

    display = session_mgr.get_display_name(window_id) or window_id
    chat_id = session_mgr.resolve_chat_id(user_id, thread_id)
    link = _topic_link(chat_id, thread_id)
    message = f"🔔 Claude is waiting for input in {display}"
    if link:
        message += f"\n{link}"
    try:
        await bot.send_message(
            chat_id=user_id,
            text=message,
            link_preview_options=NO_LINK_PREVIEW,
        )
        logger.info(
            "Interactive waiting DM sent to user=%d thread=%s window=%s",
            user_id,
            thread_id,
            window_id,
        )
    except Exception as e:
        # Non-fatal: the in-topic interactive UI still exists. This commonly
        # fails if the user has not opened a DM with the bot.
        logger.debug("Failed to send interactive waiting DM to %d: %s", user_id, e)


# Per-option description cap. The plan (v5 §Card layout) sets 250 chars so
# the worst-case 6-option × 6-tab body stays comfortably under 4096 even
# with header / labels / footer. Truncation is hard with a trailing
# ellipsis so the reader knows there's more.
_DESCRIPTION_CHAR_CAP = 250

# Hard cap on rendered card body. Matches the message_queue.py merge limit
# so the renderer can never produce a body that the send layer would have
# to split (we don't split interactive cards — splitting breaks the
# message_ids list invariant the multi-tab state machine relies on).
_CARD_BODY_CHAR_CAP = 3800


def _truncate_description(description: str) -> str:
    """Shorten a per-option description for inline display.

    Hard cap at ``_DESCRIPTION_CHAR_CAP`` chars; collapse multi-line
    descriptions to a single line so the cap is meaningful. Returns an
    empty string for empty input so callers can skip the indent line.
    """
    if not description:
        return ""
    # Collapse internal newlines + runs of whitespace so the cap counts
    # against visible characters, not layout noise.
    flat = " ".join(description.split())
    if len(flat) <= _DESCRIPTION_CHAR_CAP:
        return flat
    return flat[: _DESCRIPTION_CHAR_CAP - 1].rstrip() + "…"


def _should_post_auq_context(tool_input: dict | None) -> bool:
    """True iff the AUQ has at least one question with renderable text.

    User invariant 2026-05-22: always post a separate "📋 AskUserQuestion
    — full details" info message alongside the picker for every AUQ
    that has any content to show. The gate aligns with what
    ``_format_auq_context_message`` actually renders — the formatter
    skips the whole question when ``question``/``header`` text is
    empty, so a gate firing on label-only forms would consume the
    claim and post a header-only message (the "convergent
    overengineering" risk Codex flagged on v2).

    Returns False only for malformed input (not a dict, no questions
    list, every question missing both ``question`` and ``header`` text).
    """
    if not isinstance(tool_input, dict):
        return False
    questions = tool_input.get("questions")
    if not isinstance(questions, list):
        return False
    for q in questions:
        if not isinstance(q, dict):
            continue
        question_text = q.get("question") or q.get("header")
        if isinstance(question_text, str) and question_text.strip():
            return True
    return False


def _format_auq_context_message(tool_input: dict) -> str:
    """Render the JSONL AUQ ``tool_use.input`` as a readable context dump.

    Output shape:

        📋 AskUserQuestion — full details
        (Picker below answers each question one at a time.)  ← multi-Q only

        Q1. <question>

        1. <label>
           <full description, paragraph as-is>

        2. <label>
           <full description>

        Q2. <question>  ← only when len(questions) > 1
        …

    Plain text only — no markdown to convert later. The send layer's
    ``build_response_parts`` chunks on the 3000-char boundary and adds
    ``[i/N]`` markers when the message exceeds the limit.
    """
    questions_raw = tool_input.get("questions") or []
    questions = [q for q in questions_raw if isinstance(q, dict)]
    parts: list[str] = ["📋 AskUserQuestion — full details"]
    if len(questions) > 1:
        parts.append("(Picker below answers each question one at a time.)")
    parts.append("")
    for q_idx, q in enumerate(questions, start=1):
        question_text = (q.get("question") or q.get("header") or "").strip()
        if not question_text:
            continue
        if len(questions) > 1:
            parts.append(f"Q{q_idx}. {question_text}")
        else:
            parts.append(question_text)
        parts.append("")
        options_raw = q.get("options") or []
        # Filter to options with a non-empty label BEFORE enumerating so
        # the displayed numbering stays 1..N without gaps when the JSONL
        # contains malformed/empty option entries.
        labeled = []
        for o in options_raw:
            if not isinstance(o, dict):
                continue
            label = (o.get("label") or "").strip()
            if not label:
                continue
            description = (o.get("description") or "").strip()
            labeled.append((label, description))
        for opt_idx, (label, description) in enumerate(labeled, start=1):
            parts.append(f"{opt_idx}. {label}")
            if description:
                for line in description.splitlines() or [description]:
                    parts.append(f"   {line}")
            parts.append("")
    return "\n".join(parts).rstrip()


async def _send_auq_context_message(
    bot: Bot,
    *,
    user_id: int,
    thread_id: int | None,
    chat_id: int,
    window_id: str,
    tool_input: dict,
) -> _ContextSendResult:
    """Format and send the AUQ context message (multi-part if needed).

    Returns a tri-state outcome (Wave A, Codex v2→v3 P2 #3):
      * ``FULL_SENT`` — every chunk landed.
      * ``NONE_SENT`` — zero chunks landed (pre-loop no-op exit, or the
        first chunk failed). Caller may safely roll back the
        ``claim_auq_context_post`` claim so the next render re-tries.
      * ``PARTIAL_SENT`` — chunk 1 (at least) landed but a later chunk
        failed. Caller MUST keep the claim — rolling back and
        re-sending would duplicate the chunks that already reached
        Telegram.

    Anchors the first chunk to the user's last prompt via
    ``peek_route_last_user_message`` (non-consuming). Subsequent chunks
    land unanchored.

    ``RetryAfter`` from python-telegram-bot's flood control IS re-raised
    so the caller's flood-control contract is honored (the route lock
    holds, the picker render inherits the back-off). Other exceptions
    are caught and mapped to NONE_SENT/PARTIAL_SENT based on whether
    any chunk landed (Hermes v3→v4 P2 #2 — preserve the existing
    defensive catch).
    """
    from telegram.error import RetryAfter

    from .message_queue import peek_route_last_user_message
    from .response_builder import build_response_parts

    text = _format_auq_context_message(tool_input)
    if not text.strip():
        # Codex v4→v5 P2 #2: explicit NONE_SENT so caller's
        # ``if result is NONE_SENT`` rollback branch fires.
        return _ContextSendResult.NONE_SENT
    parts = build_response_parts(text, content_type="text", role="assistant")
    if not parts:
        return _ContextSendResult.NONE_SENT

    anchor: ReplyParameters | None = None
    if config.reply_context_enabled:
        anchor_id = peek_route_last_user_message(user_id, thread_id, window_id)
        if anchor_id is not None:
            anchor = ReplyParameters(message_id=anchor_id)

    session_id = session_id_for_window(window_id)
    total = len(parts)
    sent_any = False
    for idx, chunk in enumerate(parts, start=1):
        send_kwargs: dict = dict(
            op="interactive",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            window_id=window_id,
            text=chunk,
            role="assistant",
            content_type="text",
            session_id=session_id,
            part_index=idx if total > 1 else 0,
        )
        if idx == 1 and anchor is not None:
            send_kwargs["reply_parameters"] = anchor
        try:
            sent, _outcome = await topic_send(bot, **send_kwargs)
        except RetryAfter:
            raise
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "AUQ context message send raised (window=%s, part %d/%d): %s",
                window_id,
                idx,
                total,
                exc,
            )
            return (
                _ContextSendResult.PARTIAL_SENT
                if sent_any
                else _ContextSendResult.NONE_SENT
            )
        if sent is None:
            logger.warning(
                "AUQ context message chunk dropped (window=%s, part %d/%d) — "
                "stopping mid-sequence to avoid a [i/N] gap",
                window_id,
                idx,
                total,
            )
            return (
                _ContextSendResult.PARTIAL_SENT
                if sent_any
                else _ContextSendResult.NONE_SENT
            )
        sent_any = True
    return _ContextSendResult.FULL_SENT


def _clip_card_body(body: str) -> str:
    """Hard-clip rendered card body to ``_CARD_BODY_CHAR_CAP`` chars.

    Defense in depth: ``_truncate_description`` keeps individual options
    short, but a question with many options + very long question text
    could still push the body over the cap. We clip on a line boundary
    so the truncation doesn't land mid-sentence; final line marks the cut.
    """
    if len(body) <= _CARD_BODY_CHAR_CAP:
        return body
    # Reserve room for a "[…body truncated]" marker.
    marker = "\n\n[…body truncated; use keystroke nav to scroll the terminal]"
    budget = _CARD_BODY_CHAR_CAP - len(marker)
    if budget <= 0:
        return body[:_CARD_BODY_CHAR_CAP]
    clipped = body[:budget]
    cut = clipped.rfind("\n")
    if cut > 0:
        clipped = clipped[:cut]
    return clipped + marker


def _render_ask_user_question(form: AskUserQuestionForm) -> str:
    """Render a structured AskUserQuestion form into Telegram-friendly text.

    The body produced here replaces the raw pane excerpt for picker variants
    that ``parse_ask_user_question`` understands. Two layout modes:

    * ``is_review_screen`` → render the summary header + the resolved answers,
      then the Submit / Cancel choice. This is the screen the user lands on
      after answering every tab.
    * Otherwise → render the tab strip with state glyphs, the current
      question title (if any), and the numbered options below.

    Output is plain text (no markdown conversion downstream) so terminal
    glyphs like ``☒`` / ``☐`` / ``✔`` survive verbatim. The caller still
    sends with ``plain=True``.
    """
    lines: list[str] = []

    if form.is_review_screen:
        lines.append("✔ Review your answers")
        # Tab strip with resolved markers; tabs are in the order they appeared
        # in the picker. Skip the synthetic submit cell — the prompt and
        # button row below carry that information already.
        content_tabs = [t for t in form.tabs if not t.is_submit]
        if content_tabs:
            lines.append("")
            for t in content_tabs:
                glyph = "☒" if t.answered else "☐"
                lines.append(f"  {glyph} {t.label}".rstrip())
        if form.options:
            lines.append("")
            lines.append("Ready to submit your answers?")
            lines.append("")
            for opt in form.options:
                cursor = "❯ " if opt.cursor else "  "
                rec = " (Recommended)" if opt.recommended else ""
                lines.append(f"{cursor}{opt.number}. {opt.label}{rec}")
        return _clip_card_body("\n".join(lines).rstrip())

    # Picker layout — tabs (if any) → question title → options
    if form.tabs:
        cells: list[str] = []
        for t in form.tabs:
            if t.is_submit:
                cells.append("✔")
            else:
                glyph = "☒" if t.answered else "☐"
                label = t.label or ""
                cells.append(f"{glyph} {label}".rstrip())
        lines.append("  ".join(cells))
        lines.append("")

    # ``current_question_title`` is the JSONL-authoritative title (used
    # in the fingerprint + ``_strong_match``). ``pane_walkback_title`` is
    # the pane-only walk-back fallback (display only) — important for
    # fresh single-tab pickers that Claude Code hasn't flushed to JSONL
    # yet (2026-05-21 D5 incident). The renderer prefers the
    # authoritative title and falls through to the walk-back guess.
    title = form.current_question_title or form.pane_walkback_title
    if title:
        lines.append(title)
        lines.append("")

    if form.options:
        for opt in form.options:
            cursor = "❯ " if opt.cursor else "  "
            rec = " (Recommended)" if opt.recommended else ""
            lines.append(f"{cursor}{opt.number}. {opt.label}{rec}")
            # PR 2: inline per-option reasoning text from the JSONL payload
            # when available. The pane parser doesn't populate
            # ``description`` (it can't reliably attribute description
            # lines to specific options), so this branch only fires for
            # forms that came from ``resolve_ask_form`` with a JSONL
            # overlay. Capped at 250 chars per option; collapses
            # multi-line descriptions.
            desc = _truncate_description(opt.description)
            if desc:
                lines.append(f"    {desc}")
        if form.is_free_text:
            lines.append("")
            lines.append("  (Type something — send a regular message to free-text)")
        lines.append("")
        lines.append("Enter to select · Tab/Arrow keys to navigate · Esc to cancel")
        return _clip_card_body("\n".join(lines).rstrip())

    # No options extracted (mid-redraw, or a layout the parser only partially
    # recognized). Caller falls back to the raw pane excerpt — return an
    # empty string to signal "no structured render available".
    return ""


def _build_pick_button_rows(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    form: AskUserQuestionForm,
) -> list[list[InlineKeyboardButton]]:
    """Build inline-keyboard rows of option-pick buttons for a parsed form.

    One button per option; max 5 per row. Each button mints a single-use
    token bound to ``(window, fingerprint, option_number, option_label)``
    so the callback handler can detect a "form changed under us" race
    before dispatching the keystroke.

    Review-screen Submit/Cancel rows are rendered here too. The Submit
    button is flagged ``is_review_submit=True`` so the callback handler
    can apply a tighter guard (must still be on the review screen) before
    sending Enter / digit 1.

    Returns an empty list when the form has no options — caller drops the
    structured-pick row and falls back to the keystroke keyboard only.

    PR 3 gates (plan v5):
      * FA5+ safety — for multi-tab forms (``len(form.questions) > 1``),
        return [] when ``form.current_tab_inferred == False``. The
        keystroke fallback still lets the user navigate; we MUST NOT
        mint pick buttons because the dispatched digit could answer the
        wrong tab in the live TUI.
      * 1-9 cap — only options with ``number <= 9`` get a button.
        Sending literal ``"10"`` would type ``1`` then ``0`` and submit
        option 1 plus add a zero to the next picker. Options 10+ stay
        visible in the body but are picked via keystroke nav.
    """
    if not form.options:
        logger.info(
            "_build_pick_button_rows SUPPRESSED gate=no_options questions=%d "
            "current_tab_inferred=%s is_review_screen=%s question_title=%r",
            len(form.questions),
            form.current_tab_inferred,
            form.is_review_screen,
            (form.current_question_title or "<none>")[:80],
        )
        return []

    if not form.options_contiguous_from_one():
        logger.info(
            "_build_pick_button_rows SUPPRESSED gate=non_contiguous_from_one "
            "questions=%d options=%d numbers=%r is_review_screen=%s question_title=%r",
            len(form.questions),
            len(form.options),
            [opt.number for opt in form.options],
            form.is_review_screen,
            (form.current_question_title or "<none>")[:80],
        )
        return []

    # FA5+: multi-tab form without confirmed current-tab inference.
    # Suppress pick buttons entirely — keystroke nav remains.
    # Exception: review screens (Submit/Cancel confirmation) are
    # pane-authoritative — the options come directly from the live
    # pane (resolver returns ``pane_form.options`` at
    # ``terminal_parser.py`` multi-q review branch), so labels and
    # dispatch numbers agree with what the user sees on screen even
    # though ``current_tab_inferred`` is False (no tab inference
    # happens on a review screen). Suppressing here was hiding the
    # Submit answers / Cancel buttons mid-AUQ workflow.
    if (
        len(form.questions) > 1
        and not form.current_tab_inferred
        and not form.is_review_screen
    ):
        logger.info(
            "_build_pick_button_rows SUPPRESSED gate=fa5_guard questions=%d "
            "options=%d is_review_screen=%s question_title=%r",
            len(form.questions),
            len(form.options),
            form.is_review_screen,
            (form.current_question_title or "<none>")[:80],
        )
        return []

    fingerprint = form.fingerprint()
    deadline = time.monotonic() + _PICK_TOKEN_TTL_SECONDS

    # Filter to options that can be dispatched via literal-N. Tokens are
    # only allocated for these; the keystroke fallback still reaches the
    # rest. 1-9 cap applies here.
    pickable = [
        opt for opt in form.options if opt.number is not None and 1 <= opt.number <= 9
    ]
    if not pickable:
        logger.info(
            "_build_pick_button_rows SUPPRESSED gate=no_pickable questions=%d "
            "options=%d numbers=%r is_review_screen=%s question_title=%r",
            len(form.questions),
            len(form.options),
            [opt.number for opt in form.options],
            form.is_review_screen,
            (form.current_question_title or "<none>")[:80],
        )
        return []

    cache_key = (user_id, thread_id or 0, window_id, fingerprint)

    def _mint(opt_number: int, label: str, is_submit: bool) -> str:
        return _mint_pick_token(
            _PickTokenEntry(
                window_id=window_id,
                user_id=user_id,
                thread_id=thread_id,
                fingerprint=fingerprint,
                option_number=opt_number,
                option_label=label,
                is_review_submit=is_submit,
                expires_at=deadline,
            )
        )

    # Token-reuse path: if we already minted tokens for this exact form
    # generation (matching fingerprint), re-emit the same callback_data so
    # the rendered reply_markup is byte-identical and Telegram returns
    # MESSAGE_NOT_MODIFIED on the next edit. The cache row is wiped on
    # consume + on fingerprint change, so this can't hand out a stale
    # token bound to a different form.
    cached = _pick_token_cache.get(cache_key)
    if cached is not None and len(cached) == len(pickable):
        # Double-check that every cached token is still alive — TTL eviction
        # may have dropped some out from under us. If any are missing, fall
        # through to fresh-mint so callbacks don't 404 immediately.
        if all(t in _pick_tokens for t in cached):
            tokens: list[str] = cached
        else:
            _pick_token_cache.pop(cache_key, None)
            tokens = [
                _mint(
                    opt.number or 0,
                    opt.label,
                    form.is_review_screen and opt.cursor and opt.number == 1,
                )
                for opt in pickable
            ]
            _pick_token_cache[cache_key] = tokens
    else:
        tokens = [
            _mint(
                opt.number or 0,
                opt.label,
                form.is_review_screen and opt.cursor and opt.number == 1,
            )
            for opt in pickable
        ]
        _pick_token_cache[cache_key] = tokens

    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    # Telegram tolerates more than 5 buttons per row, but on a phone the
    # text gets clipped after ~5. Cap conservatively.
    width = 5
    for opt, token in zip(pickable, tokens):
        # ``opt.number is None`` was filtered above, but reassure the type
        # checker.
        assert opt.number is not None
        is_submit = form.is_review_screen and opt.cursor and opt.number == 1
        # Button text: number + truncated label + recommended star
        prefix = "✅ " if is_submit else f"{opt.number}. "
        # Cap label so the whole button stays under Telegram's tap-target
        # readable width. 24 chars before truncation keeps "C — Parallel
        # tracks…" visible. Recommended star adds 1 char.
        max_label = 24
        truncated = (
            opt.label if len(opt.label) <= max_label else opt.label[:max_label] + "…"
        )
        star = " ★" if opt.recommended else ""
        text = f"{prefix}{truncated}{star}"
        row.append(InlineKeyboardButton(text, callback_data=f"{CB_ASK_PICK}{token}"))
        if len(row) >= width:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def _build_interactive_keyboard(
    window_id: str,
    ui_name: str = "",
    pick_rows: list[list[InlineKeyboardButton]] | None = None,
) -> InlineKeyboardMarkup:
    """Build keyboard for interactive UI navigation.

    ``ui_name`` controls the layout: ``RestoreCheckpoint`` omits ←/→ keys
    since only vertical selection is needed.

    ``pick_rows`` is the optional output of ``_build_pick_button_rows`` —
    when present, the structured pick row(s) are placed at the top of the
    keyboard, above the keystroke navigation. The keystroke row stays even
    when pick buttons are available so the user can still pick a free-text
    "Type something" option, dismiss with Esc, or refresh.
    """
    vertical_only = ui_name == "RestoreCheckpoint"

    rows: list[list[InlineKeyboardButton]] = []
    if pick_rows:
        rows.extend(pick_rows)
    # Row 1: directional keys
    rows.append(
        [
            InlineKeyboardButton(
                "␣ Space",
                callback_data=checked_callback_data(f"{CB_ASK_SPACE}{window_id}"),
            ),
            InlineKeyboardButton(
                "↑", callback_data=checked_callback_data(f"{CB_ASK_UP}{window_id}")
            ),
            InlineKeyboardButton(
                "⇥ Tab", callback_data=checked_callback_data(f"{CB_ASK_TAB}{window_id}")
            ),
        ]
    )
    if vertical_only:
        rows.append(
            [
                InlineKeyboardButton(
                    "↓",
                    callback_data=checked_callback_data(f"{CB_ASK_DOWN}{window_id}"),
                ),
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    "←",
                    callback_data=checked_callback_data(f"{CB_ASK_LEFT}{window_id}"),
                ),
                InlineKeyboardButton(
                    "↓",
                    callback_data=checked_callback_data(f"{CB_ASK_DOWN}{window_id}"),
                ),
                InlineKeyboardButton(
                    "→",
                    callback_data=checked_callback_data(f"{CB_ASK_RIGHT}{window_id}"),
                ),
            ]
        )
    # Row 2: action keys
    rows.append(
        [
            InlineKeyboardButton(
                "⎋ Esc", callback_data=checked_callback_data(f"{CB_ASK_ESC}{window_id}")
            ),
            InlineKeyboardButton(
                "🔄",
                callback_data=checked_callback_data(f"{CB_ASK_REFRESH}{window_id}"),
            ),
            InlineKeyboardButton(
                "⏎ Enter",
                callback_data=checked_callback_data(f"{CB_ASK_ENTER}{window_id}"),
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


# ── PR 3: multi-tab AskUserQuestion state machine ───────────────────────


def _render_multi_tab_card_body(
    form: AskUserQuestionForm,
    tab_idx: int,
    tab_count: int,
) -> str:
    """Render one tab's card body for a multi-question form.

    Builds a synthetic single-question AskUserQuestionForm holding just
    this tab's data (title, options with descriptions) and feeds it
    through ``_render_ask_user_question`` so the layout matches the
    single-card path. A "Qi / N · title" header is prepended so the user
    can correlate cards.
    """
    if tab_idx < 0 or tab_idx >= len(form.questions):
        return ""
    q = form.questions[tab_idx]
    synthetic = AskUserQuestionForm(
        tabs=(),
        current_question_title=q.title or None,
        options=q.options,
        is_review_screen=False,
        is_free_text=False,
        pane_excerpt="",
        # Empty questions tuple = single-card render path for the layout
        # (no QS:/INF: fingerprint lines on the synthetic form; we don't
        # use its fingerprint anyway — the parent multi-tab form's
        # fingerprint is what binds the pick tokens).
        questions=(),
    )
    body = _render_ask_user_question(synthetic)
    if not body:
        return ""
    header = f"Q{tab_idx + 1} / {tab_count}"
    return f"{header}\n\n{body}"


async def _multi_tab_teardown(
    bot: Bot,
    *,
    user_id: int,
    chat_id: int,
    thread_id: int | None,
    window_id: str,
    session: _MultiTabSession,
) -> None:
    """Best-effort delete every message in a multi-tab session.

    Called by ``clear_interactive_msg`` and by the post-N path when a
    shape mutation forces teardown. Failures are logged but never
    raised — a Telegram outage during cleanup leaves visible orphans
    that the user can dismiss manually; the in-memory state is already
    correct.
    """
    for msg_id in session.message_ids:
        if msg_id is None:
            continue  # PR 3.2: tabs that failed to post leave None slots
        try:
            await topic_delete(
                bot,
                op="interactive",
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                window_id=window_id,
                message_id=msg_id,
            )
        except Exception as exc:
            logger.warning(
                "Multi-tab teardown: topic_delete failed for msg=%d (user=%d window=%s): %s",
                msg_id,
                user_id,
                window_id,
                exc,
            )


async def _handle_multi_tab_ask(
    bot: Bot,
    *,
    user_id: int,
    thread_id: int | None,
    chat_id: int,
    window_id: str,
    form: AskUserQuestionForm,
    ui_name: str,
    rerender_guard: object,
) -> bool:
    """State machine for multi-question AskUserQuestion forms.

    Behaviour by entry state:

      * No existing session (first render): post one card per tab; only
        the inferred current tab carries the option-pick keyboard +
        keystroke nav. If ``current_tab_inferred`` is False, no card
        carries pick buttons — keystroke nav only on card 0.
      * Existing session, same shape_digest (tab advance): edit existing
        cards in place via ``topic_edit_reply_markup`` (markup-only,
        body unchanged). Strip keyboard from old current card, attach to
        new current card.
      * Existing session, different shape_digest (Claude redrew with new
        questions): teardown old cards, post fresh N.

    Lock contract: state mutations under ``_get_route_lock``; Telegram
    I/O outside the lock. Generation captured on entry; mismatch on
    reacquire = ``_RenderCancelled`` → roll back orphan message IDs.
    """
    from ..terminal_parser import _questions_digest

    ikey = (user_id, thread_id or 0)
    lock = _get_route_lock(user_id, thread_id)
    shape_digest = _questions_digest(form.questions)
    tab_count = len(form.questions)
    current_idx = _find_current_tab_idx(form)

    # ── Phase 1: decide action under lock ────────────────────────────
    teardown_session: _MultiTabSession | None = None
    teardown_single_msg_id: int | None = None
    action: str  # "post_n" or "edit_advance" or "noop"
    session: _MultiTabSession
    generation_at_entry: int

    async with lock:
        # Re-render guard: if the JSONL cache moved on between caller's
        # release and our entry, abort. Single point that distinguishes
        # the callback path from the monitor path.
        if rerender_guard is not _NO_GUARD:
            current_digest = _ask_tool_input_digest(
                _last_completed_ask_tool_input.get(window_id)
            )
            if current_digest != rerender_guard:
                logger.info(
                    "Multi-tab re-render guard tripped (user=%d window=%s): cache changed since callback exit",
                    user_id,
                    window_id,
                )
                return False

        existing = _multi_tab_sessions.get(ikey)
        if existing is None:
            # First-time multi-tab render. If a single-card session exists
            # for this route, schedule it for deletion (mutual exclusion
            # — at most one of _interactive_msgs/_multi_tab_sessions is
            # set per route).
            teardown_single_msg_id = _clear_interactive_msg(ikey)
            # PR 3.2: pre-size message_ids to tab_count so indices align
            # with the questions matrix (was: append-only, broke on
            # partial bundles).
            session = _MultiTabSession(
                window_id=window_id,
                shape_digest=shape_digest,
                message_ids=[None] * tab_count,
                current_tab_idx=current_idx,
                generation=0,
            )
            _multi_tab_sessions[ikey] = session
            generation_at_entry = session.generation
            action = "post_n"
        elif existing.shape_digest != shape_digest:
            # Shape mutation. Tear down old cards and post fresh N.
            teardown_session = existing
            session = _MultiTabSession(
                window_id=window_id,
                shape_digest=shape_digest,
                message_ids=[None] * tab_count,
                current_tab_idx=current_idx,
                # Bump generation so any in-flight edit on the old session
                # rolls back its work.
                generation=existing.generation + 1,
            )
            _multi_tab_sessions[ikey] = session
            generation_at_entry = session.generation
            action = "post_n"
        elif existing.current_tab_idx == current_idx:
            # Same form, same tab — nothing to do.
            session = existing
            generation_at_entry = existing.generation
            action = "noop"
        else:
            # Same form, tab advance.
            session = existing
            generation_at_entry = existing.generation
            action = "edit_advance"

        _interactive_mode[ikey] = window_id

    # ── Phase 2: do Telegram I/O outside the lock ────────────────────
    if action == "noop":
        return True

    if teardown_single_msg_id is not None:
        try:
            await topic_delete(
                bot,
                op="interactive",
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                window_id=window_id,
                message_id=teardown_single_msg_id,
            )
        except Exception as exc:
            logger.debug("Single-card teardown failed (benign): %s", exc)

    if teardown_session is not None:
        await _multi_tab_teardown(
            bot,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            window_id=window_id,
            session=teardown_session,
        )

    if action == "post_n":
        return await _multi_tab_post_n(
            bot,
            user_id=user_id,
            thread_id=thread_id,
            chat_id=chat_id,
            window_id=window_id,
            form=form,
            ui_name=ui_name,
            current_idx=current_idx,
            tab_count=tab_count,
            generation_at_entry=generation_at_entry,
        )
    # action == "edit_advance"
    return await _multi_tab_edit_advance(
        bot,
        user_id=user_id,
        thread_id=thread_id,
        chat_id=chat_id,
        window_id=window_id,
        form=form,
        ui_name=ui_name,
        new_current_idx=current_idx,
        generation_at_entry=generation_at_entry,
    )


def _find_current_tab_idx(form: AskUserQuestionForm) -> int:
    """The current-tab index a multi-tab form is presenting.

    ``resolve_ask_form`` sets the form's ``current_question_title`` /
    ``options`` to match the inferred current tab. Recover the index by
    matching the title against the questions matrix; fall through to 0
    when matching fails (defensive — matches the FA5+ default behaviour).
    """
    title = (form.current_question_title or "").strip()
    if title:
        for i, q in enumerate(form.questions):
            if q.title.strip() == title:
                return i
    return 0


async def _multi_tab_post_n(
    bot: Bot,
    *,
    user_id: int,
    thread_id: int | None,
    chat_id: int,
    window_id: str,
    form: AskUserQuestionForm,
    ui_name: str,
    current_idx: int,
    tab_count: int,
    generation_at_entry: int,
) -> bool:
    """Post one Telegram card per tab; record message_ids in the session.

    Pick buttons attach to the current tab's card only — and only when
    ``form.current_tab_inferred`` is True (FA5+ safety). On generation
    mismatch (cleanup fired mid-post), raise ``_RenderCancelled`` and
    roll back orphan message IDs.
    """
    ikey = (user_id, thread_id or 0)
    lock = _get_route_lock(user_id, thread_id)
    orphans: list[int] = []

    # Anchor the first card to the user's prompt message when available.
    anchor: ReplyParameters | None = None
    if config.reply_context_enabled:
        from .message_queue import peek_route_last_user_message

        anchor_id = peek_route_last_user_message(user_id, thread_id, window_id)
        if anchor_id is not None:
            anchor = ReplyParameters(message_id=anchor_id)
    interactive_session_id = session_id_for_window(window_id)

    # Partial-bundle policy (PR 3.2, revised after live testing exposed
    # PR 3.1 regressions):
    #
    # The append-only message_ids list in PR 3.1 broke index alignment:
    # if Q1's send failed and Q2's succeeded, message_ids became [Q2_id]
    # and edit_advance treated index 0 as Q1, dispatching keystrokes to
    # the wrong card. PR 3.2 makes message_ids fixed-size and writes
    # by tab index, preserving alignment under partial bundles.
    #
    # The current-tab card is special: if its send fails, NO card has
    # the pick keyboard and the user can't click anything. Tear down
    # the session in that case so the next render starts clean. Other
    # tabs' failures are non-fatal — the user can still click the
    # current tab's button and proceed.
    current_tab_failed = False
    try:
        for idx in range(tab_count):
            body = _render_multi_tab_card_body(form, idx, tab_count)
            if not body:
                continue

            # Pick keyboard only on current tab, and only if inference
            # succeeded (FA5+). Other tabs render with no markup.
            if idx == current_idx:
                if form.current_tab_inferred:
                    pick_rows = _build_pick_button_rows(
                        user_id, thread_id, window_id, form
                    )
                else:
                    pick_rows = None
                keyboard = _build_interactive_keyboard(
                    window_id, ui_name=ui_name, pick_rows=pick_rows
                )
            else:
                keyboard = None

            send_kwargs: dict = dict(
                op="interactive",
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                window_id=window_id,
                text=body,
                plain=True,
                reply_markup=keyboard,
                role="tool",
                content_type="tool_use",
                session_id=interactive_session_id,
            )
            # Anchor only on the very first card.
            if idx == 0 and anchor is not None:
                send_kwargs["reply_parameters"] = anchor

            sent, _outcome = await topic_send(bot, **send_kwargs)
            if sent is None:
                if idx == current_idx:
                    logger.warning(
                        "Multi-tab CURRENT-TAB card send failed at idx=%d (user=%d window=%s); tearing down — no card has the keyboard",
                        idx,
                        user_id,
                        window_id,
                    )
                    current_tab_failed = True
                    raise _RenderCancelled()
                logger.warning(
                    "Multi-tab non-current card send failed at idx=%d (user=%d window=%s); leaving slot empty",
                    idx,
                    user_id,
                    window_id,
                )
                continue

            # Commit the message_id under the lock, with the generation
            # re-check. If cleanup bumped the generation between the
            # send and this point, this id is an orphan. Write by index
            # — message_ids is pre-sized so this preserves alignment
            # with the questions matrix.
            async with lock:
                current = _multi_tab_sessions.get(ikey)
                if current is None or current.generation != generation_at_entry:
                    orphans.append(sent.message_id)
                    raise _RenderCancelled()
                current.message_ids[idx] = sent.message_id
    except _RenderCancelled:
        # Generation guard fired OR current-tab failed. Roll back any
        # orphans (committed-then-stranded message_ids from the slots
        # we did write) AND any non-current cards we already posted in
        # this bundle (they belong to a torn-down session).
        all_to_delete = list(orphans)
        async with lock:
            current = _multi_tab_sessions.get(ikey)
            if current is not None and current.generation == generation_at_entry:
                # Collect what we did write before the failure.
                for mid in current.message_ids:
                    if mid is not None:
                        all_to_delete.append(mid)
                # Drop the session — caller should not see a half-built
                # state as a live surface.
                _multi_tab_sessions.pop(ikey, None)
        for msg_id in all_to_delete:
            try:
                await topic_delete(
                    bot,
                    op="interactive",
                    user_id=user_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    window_id=window_id,
                    message_id=msg_id,
                )
            except Exception as exc:
                logger.debug("Orphan rollback delete failed (benign): %s", exc)
        return False

    # Total failure (every send returned None somehow without raising)?
    # Drop the empty session.
    async with lock:
        current = _multi_tab_sessions.get(ikey)
        landed = (
            sum(1 for m in current.message_ids if m is not None)
            if current is not None
            else 0
        )
        if landed == 0:
            existing_for_cleanup = _multi_tab_sessions.get(ikey)
            if (
                existing_for_cleanup is not None
                and existing_for_cleanup.generation == generation_at_entry
            ):
                _multi_tab_sessions.pop(ikey, None)
            return False
    _ = current_tab_failed  # reserved for diagnostic surfacing later
    return True


async def _multi_tab_edit_advance(
    bot: Bot,
    *,
    user_id: int,
    thread_id: int | None,
    chat_id: int,
    window_id: str,
    form: AskUserQuestionForm,
    ui_name: str,
    new_current_idx: int,
    generation_at_entry: int,
) -> bool:
    """Move the option-pick keyboard between per-tab cards on a tab advance.

    Body text is unchanged (each card already shows its own question's
    full content); only ``reply_markup`` moves. ``topic_edit_reply_markup``
    is the markup-only edit API so we never trigger MESSAGE_NOT_MODIFIED
    on the body.
    """
    ikey = (user_id, thread_id or 0)
    lock = _get_route_lock(user_id, thread_id)

    # Snapshot the old current index under the lock. PR 3.2: message_ids
    # is now a fixed-size list[int | None] aligned with the questions
    # matrix; indices are stable, None means that tab's card failed to
    # send during post_n.
    async with lock:
        session = _multi_tab_sessions.get(ikey)
        if session is None or session.generation != generation_at_entry:
            return False
        old_idx = session.current_tab_idx
        old_msg_id = (
            session.message_ids[old_idx]
            if 0 <= old_idx < len(session.message_ids)
            else None
        )
        new_msg_id = (
            session.message_ids[new_current_idx]
            if 0 <= new_current_idx < len(session.message_ids)
            else None
        )

    if old_msg_id is None or new_msg_id is None:
        # Partial bundle (some cards failed to post). Session is still
        # live; keystroke nav continues to work. Return True so the
        # caller doesn't tear down the surviving cards via
        # ``has_interactive_surface`` cleanup chain.
        logger.warning(
            "Multi-tab edit_advance: missing message_ids (old=%s new=%s); keeping partial session live",
            old_msg_id,
            new_msg_id,
        )
        return True

    # Build the new keyboard for the new current tab (outside the lock).
    if form.current_tab_inferred:
        pick_rows = _build_pick_button_rows(user_id, thread_id, window_id, form)
    else:
        pick_rows = None
    new_keyboard = _build_interactive_keyboard(
        window_id, ui_name=ui_name, pick_rows=pick_rows
    )

    # Strip the old current card's keyboard first, then attach the new one.
    await topic_edit_reply_markup(
        bot,
        op="interactive",
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        window_id=window_id,
        message_id=old_msg_id,
        reply_markup=None,
    )
    await topic_edit_reply_markup(
        bot,
        op="interactive",
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        window_id=window_id,
        message_id=new_msg_id,
        reply_markup=new_keyboard,
    )

    # Commit new index.
    async with lock:
        session = _multi_tab_sessions.get(ikey)
        if session is None or session.generation != generation_at_entry:
            return False
        session.current_tab_idx = new_current_idx

    return True


async def handle_interactive_ui(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
    *,
    tool_input: dict | None = None,
    rerender_guard: object = _NO_GUARD,
    from_poller: bool = False,
    tmux_mgr=None,
    session_mgr=None,
) -> bool:
    """Capture terminal and send interactive UI content to user.

    Handles AskUserQuestion, ExitPlanMode, Permission Prompt, and
    RestoreCheckpoint UIs. Returns True if UI was detected and sent,
    False otherwise.

    ``tool_input`` is the raw JSONL ``tool_use.input`` dict when explicitly
    available from JSONL dispatch/replay. For a live pending AskUserQuestion,
    JSONL is not authoritative because Claude Code buffers the ``tool_use``
    line until the user answers; the tmux pane is the active source of truth.
    The pane is captured for structured AUQ parsing, verbatim text excerpt,
    and the keystroke fallback path.

    ``rerender_guard`` (PR 3, plan v5 §Re-render guard) is a content digest
    snapshot of ``_last_completed_ask_tool_input[window_id]`` taken before the
    caller (the pick-token callback handler) released the route lock. The
    handler compares it against the current cache value; if the cache was
    cleared or replaced between callback exit and re-render entry, abort
    the re-render — the world has moved on. Default ``_NO_GUARD`` means
    "always render" (monitor / JSONL dispatch path).
    """
    if tmux_mgr is None:
        tmux_mgr = tmux_manager
    if session_mgr is None:
        session_mgr = session_manager
    ikey = (user_id, thread_id or 0)
    w = await tmux_mgr.find_window_by_id(window_id)
    if not w:
        return False

    # P1.3 two-phase capture: liveness uses visible-only (no scrollback) so
    # historical pickers still sitting in the buffer can't fake a live UI.
    # Once the visible pane confirms a picker IS on screen, capture again
    # WITH scrollback for the structured parse — long AskUserQuestion text
    # can push early options out of the visible region, and the structured
    # parser needs them.
    #
    # CB1: empty/whitespace visible capture is UNKNOWN, not ABSENT —
    # ``visible_pane_liveness`` distinguishes the two. UNKNOWN here means
    # "tmux probably mid-redraw"; bail without rendering so we don't post
    # a partial card, but ALSO don't destructively clear (callers gate
    # cleanup on ``has_interactive_surface``, which we don't touch).
    visible = await tmux_mgr.capture_pane(w.window_id, scrollback_lines=0)
    state = visible_pane_liveness(visible)
    if state != "present":
        logger.debug(
            "Interactive UI liveness=%s for window_id %s (last 3 visible: %s)",
            state,
            window_id,
            (visible or "").strip().split("\n")[-3:],
        )
        return False

    # Picker confirmed live. Now capture with scrollback for the structured
    # parse — long AskUserQuestion text pushes early options off the top of
    # the visible pane, and the parser needs them.
    pane_text = await tmux_mgr.capture_pane(w.window_id, scrollback_lines=500)
    if not pane_text:
        logger.debug("No pane text captured for window_id %s", window_id)
        return False

    # Extract content between separators
    content = extract_interactive_content(pane_text)
    if not content:
        return False

    # For AskUserQuestion specifically, try the structured renderer first.
    # ``parse_ask_user_question`` is strict-or-None: it only returns a form
    # when it can produce a clean structured view. On a non-empty render we
    # use it; otherwise we fall back to the raw pane excerpt (the legacy
    # behavior for every other interactive UI).
    #
    # PR 2b: when the form carries numeric options, also mint a row of
    # option-pick buttons. The keystroke keyboard stays underneath so the
    # user can still navigate manually, dismiss with Esc, or write a free-
    # text reply.
    text = content.content
    pick_rows: list[list[InlineKeyboardButton]] | None = None
    if content.name == "AskUserQuestion":
        # Unified resolver (PR 1) feeds both render and validate paths the
        # same form. Combines JSONL tool_input (full option list with
        # descriptions, plus the multi-question matrix) with pane state
        # (cursor / free-text / review-screen flags + current-tab
        # inference). On multi-tab forms the resolver tracks
        # ``current_tab_inferred``; if False, PR 3 will gate pick buttons.
        # For PR 2 the render path is still single-card only, so the flag
        # is informational here — multi-question pick buttons stay
        # disabled until PR 3 ships the multi-tab state machine.
        resolved_input = _resolve_ask_tool_input(window_id, tool_input)
        form: AskUserQuestionForm | None = resolve_ask_form(resolved_input, pane_text)
        if form is None:
            # Belt-and-braces fallback. resolve_ask_form already tries
            # pane parse internally; this only fires when both inputs
            # are useless.
            form = build_form_from_tool_input(resolved_input)
            if form is None:
                form = parse_ask_user_question(pane_text)

        # Active AUQs do not have a live JSONL payload: Claude Code buffers
        # the AskUserQuestion ``tool_use`` line until the user answers. The
        # pane parse is therefore authoritative while the picker is pending.
        # If the pane starts at option >1, earlier choices are scrolled off;
        # render the visible structured text immediately, suppress unsafe
        # pick buttons, and tell the user to use manual navigation/text.
        p14_suppress_picks = False
        partial_options_notice: str | None = None
        if form is not None and form.options:
            first_num = form.options[0].number or 0
            last_num = form.options[-1].number or first_num
            partial_pane = first_num > 1
            if partial_pane:
                p14_suppress_picks = True
                partial_options_notice = (
                    f"Only options {first_num}-{last_num} are visible; "
                    "use ↑/↓/Tab below or send your answer as text."
                )
                logger.info(
                    "AskUserQuestion partial pane for window %s — visible "
                    "options %d-%d; suppressing pick buttons",
                    window_id,
                    first_num,
                    last_num,
                )
            # Stale replay cache (``form._meta["stale_fallback"] == "1"``)
            # is no longer treated as a pick-suppression condition: the
            # resolver returns ``pane_form`` in that branch (pane-derived
            # labels), the contiguous-from-1 gate in
            # ``_build_pick_button_rows`` is the actual defense against
            # wrong-action dispatches, and dispatch is a literal digit
            # keystroke against the live pane — so labels and dispatch
            # agree regardless of cache freshness. Suppressing on
            # stale_fallback alone dropped buttons on legitimate complete
            # contiguous pickers when an earlier AUQ sat in the cache.
        # Multi-tab dispatch DISABLED at user request (2026-05-15):
        # PRs #11/12/13 shipped a per-tab card state machine. Live
        # testing surfaced enough rough edges (timeout cascades,
        # fingerprint drift, partial-bundle alignment) that the user
        # explicitly preferred the legacy single-card behaviour, which
        # they confirmed "works much better" — one rolling card that
        # updates body+keyboard as the picker advances tab-by-tab.
        #
        # The multi-tab state-machine code (``_handle_multi_tab_ask``
        # and friends) stays in place as dormant infrastructure but is
        # never reached. Single-card flow below handles every variant,
        # including multi-question forms: ``resolve_ask_form`` populates
        # ``current_question_title`` + ``options`` from the inferred
        # current tab, ``_render_ask_user_question`` renders that tab's
        # descriptions, and pick buttons + keystroke nav let the user
        # walk through tabs. To re-enable multi-tab, restore the
        # ``_handle_multi_tab_ask`` dispatch here after addressing the
        # live-testing issues.
        if form is not None:
            structured = _render_ask_user_question(form)
            if structured:
                text = structured
            if partial_options_notice:
                text = f"{text}\n\n{partial_options_notice}"
            if not p14_suppress_picks:
                built = _build_pick_button_rows(user_id, thread_id, window_id, form)
                if built:
                    pick_rows = built

    # Build message with navigation keyboard (structured rows on top when
    # available, keystroke nav row below for free-text / manual paths).
    keyboard = _build_interactive_keyboard(
        window_id, ui_name=content.name, pick_rows=pick_rows
    )

    chat_id = session_mgr.resolve_chat_id(user_id, thread_id)

    # PR 3: if we're rendering a single card but an active multi-tab
    # session exists for this route, the form has changed shape (e.g.
    # the user reached the review screen, or Claude redrew with a single
    # question after a multi-tab phase). Tear down the multi-tab cards
    # before posting / editing the single card so the user doesn't see
    # both surfaces at once.
    multi_tab_session_to_clear: _MultiTabSession | None = None
    lock = _get_route_lock(user_id, thread_id)
    async with lock:
        existing_multi = _multi_tab_sessions.pop(ikey, None)
        if existing_multi is not None:
            existing_multi.generation += 1  # cancel any in-flight edits
            multi_tab_session_to_clear = existing_multi
    if multi_tab_session_to_clear is not None:
        await _multi_tab_teardown(
            bot,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            window_id=window_id,
            session=multi_tab_session_to_clear,
        )

    # AUQ context message — posted ONCE per (window_id, tool_use_id)
    # when at least one option description would be truncated by the
    # picker card's per-option cap. Held under the same per-route lock
    # as the picker send/edit below so concurrent bot.py + status_polling
    # callers serialize on this route: the first claims the context-post
    # slot via ``claim_auq_context_post``, posts context, then sends the
    # picker; the second skips context (already posted) and edits the
    # existing picker. Without the lock, the picker could land before
    # the context message in the chat order (race flagged by hermes
    # P1 on the 2026-05-22 design review). The lock is per-route so
    # this does not stall other routes.
    async with lock:
        if content.name == "AskUserQuestion":
            ctx_input = _resolve_ask_tool_input(window_id, tool_input)
            logger.info(
                "AUQ context gate eval: window=%s from_poller=%s "
                "explicit_input=%s cached_input=%s tool_use_id=%s "
                "should_post=%s already_posted=%s",
                window_id,
                from_poller,
                tool_input is not None,
                _last_completed_ask_tool_input.get(window_id) is not None,
                _last_auq_tool_use_id.get(window_id),
                _should_post_auq_context(ctx_input),
                _auq_context_posted.get(window_id) is not None,
            )
            if _should_post_auq_context(ctx_input) and claim_auq_context_post(
                window_id
            ):
                ctx_result = await _send_auq_context_message(
                    bot,
                    user_id=user_id,
                    thread_id=thread_id,
                    chat_id=chat_id,
                    window_id=window_id,
                    tool_input=ctx_input,  # type: ignore[arg-type]
                )
                # Codex v2→v3 P2 #3: rollback the claim ONLY when nothing
                # landed. PARTIAL_SENT keeps the claim — re-sending would
                # duplicate the chunks that already reached Telegram.
                if ctx_result is _ContextSendResult.NONE_SENT:
                    _auq_context_posted.pop(window_id, None)
                    _persist_interactive_state()

        # Staleness gate (Wave A, Bug A): if the persisted msg id was
        # for a different session, treat the entry as stale and re-send.
        # Do NOT delete the orphan card — it may belong to legitimate
        # user history. Normalize None vs "" so a None-returning
        # session_id_for_window doesn't falsely drop entries with an
        # empty stored session_id.
        meta = _interactive_msg_meta.get(ikey)
        if meta is not None:
            current_session = session_id_for_window(window_id)
            if (current_session or "") != (meta.session_id or ""):
                _interactive_msgs.pop(ikey, None)
                _interactive_msg_meta.pop(ikey, None)
                _persist_interactive_state()
                logger.info(
                    "AUQ session-id mismatch — dropping stale persisted "
                    "msg %d (user=%d thread=%s window=%s "
                    "persisted_session=%s current_session=%s)",
                    meta.msg_id,
                    user_id,
                    thread_id,
                    window_id,
                    (meta.session_id or "<empty>")[:8],
                    (current_session or "<none>")[:8],
                )

        # Check if we have an existing interactive message to edit
        existing_msg_id = _interactive_msgs.get(ikey)
        if existing_msg_id:
            edit_outcome = await topic_edit(
                bot,
                op="interactive",
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                window_id=window_id,
                message_id=existing_msg_id,
                text=text,
                plain=True,
                reply_markup=keyboard,
            )
            # MESSAGE_NOT_MODIFIED means Claude redrew an identical UI;
            # treating it as success keeps the same Telegram message in
            # place (no fresh card, no delete-then-resend churn).
            if edit_outcome in (
                TopicSendOutcome.OK,
                TopicSendOutcome.MESSAGE_NOT_MODIFIED,
            ):
                _interactive_mode[ikey] = window_id
                # Hermes v2→v3 P2 #3: refresh sidecar so metadata stays
                # current after window-id remaps, delayed SessionStart
                # hook fires, or a first-time tool_use_id reveal on a
                # previously pane-only render. created_at is preserved
                # by the helper.
                _refresh_interactive_msg_meta(
                    ikey,
                    msg_id=existing_msg_id,
                    window_id=window_id,
                    session_id=session_id_for_window(window_id) or "",
                    tool_use_id=_last_auq_tool_use_id.get(window_id),
                )
                # The interactive card edit landed in the topic. The
                # separate "🔔 waiting for input" attention card is
                # suppressed here: it was a duplicate of the same
                # content, in the same topic, with a self-pointing link.
                # Telegram's own notification on the edited card already
                # covers the "ping the user" use case; the attention
                # card is reserved for the topic-send-failed branch
                # below where the user genuinely doesn't see the card.
                return True
            # Edit failed — fall through to fresh send while keeping
            # the old id so we can delete it after a new one lands.

        # Send new message (plain text — terminal content is not
        # markdown). §2.5.2: anchor the interactive card to the user's
        # prompt that triggered the tool, when we know it. ``peek``
        # (not consume) so the same anchor still applies when Claude
        # follows up with assistant text after the user resolves the
        # interactive card — both the card and the trailing text are
        # responses to the same user prompt. The text-side
        # ``_process_content_task`` is the canonical owner of the
        # anchor's lifecycle (it pops on first-part send).
        logger.info(
            "Sending interactive UI to user %d for window_id %s",
            user_id,
            window_id,
        )
        anchor: ReplyParameters | None = None
        if config.reply_context_enabled:
            from .message_queue import peek_route_last_user_message

            anchor_id = peek_route_last_user_message(user_id, thread_id, window_id)
            if anchor_id is not None:
                anchor = ReplyParameters(message_id=anchor_id)
        interactive_session_id = session_id_for_window(window_id)
        if anchor is not None:
            sent, send_outcome = await topic_send(
                bot,
                op="interactive",
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                window_id=window_id,
                text=text,
                plain=True,
                reply_markup=keyboard,
                reply_parameters=anchor,
                role="tool",
                content_type="tool_use",
                session_id=interactive_session_id,
            )
        else:
            sent, send_outcome = await topic_send(
                bot,
                op="interactive",
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                window_id=window_id,
                text=text,
                plain=True,
                reply_markup=keyboard,
                role="tool",
                content_type="tool_use",
                session_id=interactive_session_id,
            )
        if sent is None:
            # Topic send failed — still mark interactive mode (prevents
            # per-poll retry spam) and try the topic-first attention
            # card. If that also cannot reach the topic, emergency-fall
            # back to a direct DM.
            _interactive_mode[ikey] = window_id
            # Ensure the sidecar doesn't carry a stale entry forward
            # (Hermes v1→v2 hardening).
            if _interactive_msg_meta.pop(ikey, None) is not None:
                _interactive_msgs.pop(ikey, None)
                _persist_interactive_state()
            outcome = await attention.notify_waiting(
                bot,
                user_id=user_id,
                thread_id=thread_id,
                window_id=window_id,
                prompt_text=text,
                kind="interactive_ui",
            )
            if outcome is not TopicSendOutcome.OK and send_outcome in (
                TopicSendOutcome.TOPIC_NOT_FOUND,
                TopicSendOutcome.TOPIC_CLOSED,
                TopicSendOutcome.FORBIDDEN,
            ):
                await _notify_waiting_dm(
                    bot, user_id, window_id, thread_id, text, session_mgr
                )
            return False
        _set_interactive_msg(
            ikey,
            msg_id=sent.message_id,
            window_id=window_id,
            session_id=interactive_session_id or "",
            tool_use_id=_last_auq_tool_use_id.get(window_id),
        )
        _interactive_mode[ikey] = window_id
        # See note above: the interactive card landed in the topic, so
        # the duplicate "🔔 waiting for input" attention card is
        # suppressed. The send-failed branch still fires notify_waiting
        # because that's the only signal the user gets when the
        # topic-send couldn't deliver.
        # New message sent successfully — now safe to delete the old one.
        if existing_msg_id:
            # Codex defensive (v2 P2): topic_delete failure leaves the
            # old card orphaned (we already overwrote _interactive_msgs
            # with the new id, so the lifecycle is structurally correct
            # — only the user's visible state has a stray card).
            try:
                await topic_delete(
                    bot,
                    op="interactive",
                    user_id=user_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    window_id=window_id,
                    message_id=existing_msg_id,
                )
            except Exception as exc:
                logger.warning(
                    "topic_delete of old interactive msg=%d failed: %s",
                    existing_msg_id,
                    exc,
                )
        return True


async def clear_interactive_msg(
    user_id: int,
    bot: Bot | None = None,
    thread_id: int | None = None,
    *,
    session_mgr=None,
) -> None:
    """Clear tracked interactive surfaces (single card + multi-tab session).

    PR 3: walks both ``_interactive_msgs`` and ``_multi_tab_sessions``.
    State mutations under the route lock (snapshot + drop + bump
    generation), Telegram deletes outside the lock.
    """
    if session_mgr is None:
        session_mgr = session_manager
    ikey = (user_id, thread_id or 0)

    # ── Phase 1: snapshot + drop state under lock ──────────────────
    lock = _get_route_lock(user_id, thread_id)
    async with lock:
        single_msg_id = _clear_interactive_msg(ikey)
        # P2.2: capture the active window for this route BEFORE popping
        # ``_interactive_mode`` so we can scope token pruning correctly.
        # Multiple windows can never share a route (1 topic = 1 window),
        # but the pruning still wants to scope to the cleared window to
        # avoid touching unrelated routes that share a (user, thread)
        # key in ``_pick_tokens``.
        cleared_window_id = _interactive_mode.pop(ikey, None)
        multi_session = _multi_tab_sessions.pop(ikey, None)
        if multi_session is not None:
            # Bump generation so any in-flight render coroutine fails its
            # re-check and rolls back its orphans.
            multi_session.generation += 1
            multi_msg_ids = list(multi_session.message_ids)
        else:
            multi_msg_ids = []

        # P2.2: prune pick-tokens for this route. Without this, a deleted
        # interactive card leaves its tokens live until the 5-minute TTL,
        # which combined with stale-scrollback liveness checks (P1.3)
        # would let a stale callback validate against a closed picker.
        # Scope by (user_id, thread_id, window_id) so concurrent
        # interactive surfaces on other routes are untouched. The token
        # entries carry the route fields directly, so we can match
        # cheaply by iterating the small dict.
        if cleared_window_id is not None:
            stale_tokens = [
                tok
                for tok, e in _pick_tokens.items()
                if e.user_id == user_id
                and (e.thread_id or 0) == (thread_id or 0)
                and e.window_id == cleared_window_id
            ]
            for tok in stale_tokens:
                _pick_tokens.pop(tok, None)
            # Cache rows for this route point at the same fingerprint
            # set; drop them so a future mint doesn't reuse a row whose
            # tokens we just invalidated.
            stale_cache_keys = [
                key
                for key in _pick_token_cache
                if key[0] == user_id
                and key[1] == (thread_id or 0)
                and key[2] == cleared_window_id
            ]
            for key in stale_cache_keys:
                _pick_token_cache.pop(key, None)

    logger.debug(
        "Clear interactive: user=%d thread=%s single=%s multi_count=%d",
        user_id,
        thread_id,
        single_msg_id,
        len(multi_msg_ids),
    )

    # Fire lifecycle-end hooks BEFORE Telegram I/O so subscribers (e.g.
    # ``status_polling._absent_streak``) drop their per-lifecycle state even
    # if the bot.delete_message call below fails. Lock has already released
    # so callbacks can re-enter interactive_ui safely.
    _fire_clear(user_id, thread_id or 0, cleared_window_id)

    if bot is None:
        return

    chat_id = session_mgr.resolve_chat_id(user_id, thread_id)

    # ── Phase 2: Telegram I/O outside the lock ─────────────────────
    if single_msg_id is not None:
        await topic_delete(
            bot,
            op="interactive",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            window_id=None,
            message_id=single_msg_id,
        )

    for msg_id in multi_msg_ids:
        if msg_id is None:
            continue  # PR 3.2: tabs that failed to post leave None slots
        try:
            await topic_delete(
                bot,
                op="interactive",
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                window_id=None,
                message_id=msg_id,
            )
        except Exception as exc:
            logger.warning(
                "clear_interactive_msg: topic_delete failed for msg=%d (user=%d): %s",
                msg_id,
                user_id,
                exc,
            )

    await attention.dismiss(bot, user_id=user_id, thread_id=thread_id)
