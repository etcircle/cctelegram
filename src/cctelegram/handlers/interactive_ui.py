"""Interactive UI handling for Claude Code prompts.

Handles interactive terminal UIs displayed by Claude Code:
  - AskUserQuestion: Multi-choice question prompts (one rolling card per
    route; multi-question forms are walked tab-by-tab in the same card).
  - ExitPlanMode: Plan mode exit confirmation
  - Permission / Workflow approval gates: tool-permission prompts and the
    Workflow dynamic-workflow-launch approval — DISPLAY-ONLY in PR-1 (a labels
    card + the manual ↑/↓/⏎/Esc nav keyboard, no semantic option-pick button),
    behind the ``CC_TELEGRAM_PERMISSION_PROMPTS`` flag.
  - RestoreCheckpoint: Checkpoint restoration selection

Provides:
  - Keyboard navigation (up/down/left/right/enter/esc)
  - Terminal capture and display
  - Interactive mode tracking per user and thread

State dicts are keyed by (user_id, thread_id_or_0) for Telegram topic support.
"""

import asyncio
import hashlib
import json
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters

from ..config import config
from ..session import (
    peek_session_id_for_window,
    session_id_for_window,
    session_manager,
)
from ..terminal_parser import (
    REVIEW_SUBMIT_LABEL,
    AskUserQuestionForm,
    InteractiveUIContent,
    build_form_from_tool_input,
    extract_epm_plan_file_path,
    extract_interactive_content,
    parse_ask_user_question,
    visible_pane_liveness,
)
from .. import md_capture
from ..tmux_manager import tmux_manager
from ..utils import atomic_write_json
from . import attention, auq_source, pick_intent, pick_token
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
    CB_ASK_TOGGLE,
    CB_ASK_UP,
    checked_callback_data,
)
from .message_sender import (
    NO_LINK_PREVIEW,
    TopicSendOutcome,
    safe_answer,
    topic_delete,
    topic_edit,
    topic_send,
)

logger = logging.getLogger(__name__)

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
# message gate (``claim_auq_context_post_in_memory``) to dedup per-AUQ posts.
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
#
# Wave 1 (plan §5) made this dict write-after-send: ``commit_auq_context_post``
# writes here ONLY after at least one chunk lands on Telegram. The
# pre-Wave-1 ``claim_auq_context_post`` wrote here BEFORE any chunk
# landed, which made a crash between claim and the first chunk land
# permanently suppress the context message (the persisted marker
# survived restart even though Telegram never saw the post).
_auq_context_posted: dict[str, str] = {}


@dataclass(frozen=True)
class _AuqContextPendingClaim:
    """In-memory pending context-post claim — Wave 1 two-phase dedup.

    Lives only in ``_auq_context_post_pending``; NOT persisted to
    ``interactive_state.json``. A restart drops the pending claim by
    design so the next render re-attempts the context message, instead
    of carrying a stale persisted claim forward (the pre-Wave-1 bug).
    """

    dedup_key: str
    claim_token: str
    claimed_at: float  # monotonic seconds, read via ``_pending_claim_clock``


# Wave 1 (plan §5.1): in-memory pending claims for the two-phase
# context-post gate. ``claim_auq_context_post_in_memory`` writes here;
# ``commit_auq_context_post`` and ``rollback_auq_context_post`` consume.
# Never persisted — pending claims are process-lifetime-scoped on
# purpose: a restarted bot cannot know whether the in-flight chunk
# landed on Telegram, so re-rendering and re-posting is safer than
# carrying a stale claim forward.
_auq_context_post_pending: dict[str, _AuqContextPendingClaim] = {}


# Wave 1: TTL (seconds) for same-process abandoned-claim recovery. A
# pending claim older than this gets purged on the next
# ``claim_auq_context_post_in_memory`` call so a hung coroutine that
# never reached commit/rollback can't permanently block subsequent
# claims for the same window. This is NOT crash recovery — restart
# drops pending claims entirely (the dict is module-level state).
_PENDING_CLAIM_TTL_SECONDS = 60.0


def _pending_claim_clock() -> float:
    """Monotonic-clock hook for tests to override.

    Production reads ``time.monotonic()``; tests patch this module
    attribute to fast-forward the TTL without sleeping.
    """
    return time.monotonic()


@dataclass(frozen=True)
class _ContextMsgRecord:
    """Sidecar record for a posted AUQ context message.

    Tracks the chunked Telegram ``message_ids`` of a "📋 AskUserQuestion
    — full details" post so that a later upgrade pass (when JSONL
    finally flushes the rich dict source with per-option descriptions)
    can edit the existing message(s) in place rather than spawning a
    duplicate or leaving the form-source label-only render permanent.

    ``source`` is ``"form"`` for the pane-derived fallback render
    (commit 603c6bc) and ``"dict"`` for the rich JSONL render. Upgrade
    runs only when source is ``"form"`` and a dict source arrives.
    ``render_sha1`` is the SHA-1 of the rendered text (pre-chunking)
    used to short-circuit a no-op upgrade when the dict source happens
    to render identically to what's already on Telegram.
    """

    message_ids: tuple[int, ...]
    source: str  # "form" | "dict"
    dedup_key: str
    tool_use_id: str | None
    render_sha1: str
    user_id: int
    chat_id: int
    thread_id: int  # 0 ⇔ None (JSON-safe)
    session_id: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_ids": list(self.message_ids),
            "source": self.source,
            "dedup_key": self.dedup_key,
            "tool_use_id": self.tool_use_id,
            "render_sha1": self.render_sha1,
            "user_id": self.user_id,
            "chat_id": self.chat_id,
            "thread_id": self.thread_id,
            "session_id": self.session_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "_ContextMsgRecord | None":
        try:
            mids_raw = d["message_ids"]
            uid = int(d["user_id"])
            cid = int(d["chat_id"])
            tid = int(d.get("thread_id", 0) or 0)
        except (KeyError, TypeError, ValueError):
            return None
        if not isinstance(mids_raw, list) or not mids_raw:
            return None
        try:
            mids = tuple(int(m) for m in mids_raw)
        except (TypeError, ValueError):
            return None
        if any(m <= 0 for m in mids):
            return None
        src = d.get("source")
        if src not in ("form", "dict"):
            return None
        dk = d.get("dedup_key")
        if not isinstance(dk, str) or not dk:
            return None
        tuid_raw = d.get("tool_use_id")
        tuid = str(tuid_raw) if isinstance(tuid_raw, str) else None
        return cls(
            message_ids=mids,
            source=src,
            dedup_key=dk,
            tool_use_id=tuid,
            render_sha1=str(d.get("render_sha1") or ""),
            user_id=uid,
            chat_id=cid,
            thread_id=tid,
            session_id=str(d.get("session_id") or ""),
            created_at=str(d.get("created_at") or ""),
        )


# Sidecar to ``_auq_context_posted``: full record of every posted AUQ
# context message, keyed by window_id. Lets ``maybe_upgrade_auq_context_message``
# locate and edit the chunked posts when a richer JSONL source arrives.
# Cleared by ``forget_ask_tool_input`` together with ``_auq_context_posted``.
_auq_context_msgs: dict[str, _ContextMsgRecord] = {}


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

    Reported back to the caller in ``handle_interactive_ui`` for
    diagnostics only — Wave 1 made the send function itself
    responsible for settling the pending claim before returning
    (``rollback_auq_context_post`` on no-landing paths,
    ``commit_auq_context_post`` on any-chunk-landed paths). The
    caller no longer pops dedup state on NONE_SENT.

    Semantics:
      * NONE_SENT — no chunks landed; pending slot was rolled back;
        the next render claims again and re-posts.
      * PARTIAL_SENT — chunk 1 (at least) landed; commit ran with
        the truncated ``sent_msg_ids`` so a restart finds the
        chunked record and does NOT re-post.
      * FULL_SENT — every chunk landed; commit ran with all msg_ids.
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

    When a NEW ``tool_use_id`` replaces a different prior id for the
    same window, the context-post marker is also cleared: a new AUQ
    has started in the same window and the next ``claim`` must succeed.
    Under the presence-based dedup contract (the form-source v5 fix),
    leaving the prior marker in place would silently suppress the new
    AUQ's context message.
    """
    if isinstance(tool_input, dict):
        _last_completed_ask_tool_input[window_id] = tool_input
        if isinstance(tool_use_id, str):
            prior_id = _last_auq_tool_use_id.get(window_id)
            _last_auq_tool_use_id[window_id] = tool_use_id
            if prior_id is not None and prior_id != tool_use_id:
                # New AUQ id replaced an old one for the same window —
                # drop the stale context-post marker so the next claim
                # succeeds for the new AUQ.
                _auq_context_posted.pop(window_id, None)
                # Wave 1: also drop any in-flight pending claim. A
                # rotation under us means the claim_token in flight is
                # for the prior AUQ; rolling forward it would commit
                # the wrong dedup_key.
                _auq_context_post_pending.pop(window_id, None)
                # Codex P2 round 3 #2 (2026-05-25): also drop the
                # auq_context_msgs record from the prior lifecycle.
                # Without this, maybe_upgrade_auq_context_message
                # would later edit the OLD message_ids with the NEW
                # question's text (a stale record posing as if it
                # belongs to the current AUQ) and then mark them
                # "upgraded" — permanently wrong content.
                _auq_context_msgs.pop(window_id, None)
        else:
            # Caller doesn't have a tool_use_id (test helper or legacy
            # path). Drop any stale ID + posted state so the context
            # gate's "missing id blocks claim" guarantee holds even if
            # an earlier remember left state behind. Hermes P3 hardening,
            # 2026-05-22 diff review.
            _last_auq_tool_use_id.pop(window_id, None)
            _auq_context_posted.pop(window_id, None)
            _auq_context_post_pending.pop(window_id, None)
            _auq_context_msgs.pop(window_id, None)


def forget_ask_tool_input(window_id: str) -> None:
    """Drop the cached AskUserQuestion input for a window (e.g. on tool_result).

    Also persists the cleared ``_auq_context_posted`` /
    ``_auq_context_msgs`` state so a subsequent restart doesn't carry
    forward a stale claim marker or an upgrade record for a resolved
    AUQ.

    AUQ PreToolUse hook integration (v4 plan): the side-file half (in-memory
    pretool record cache + unlink the side file for the window's CURRENT
    session_id) is delegated to ``auq_source.forget_for_window``. The
    ``/clear`` race (where ``session_monitor._detect_and_cleanup_changes``
    runs BEFORE this and swaps the session_id under us) is handled separately
    in session_monitor — that path uses the OLD session_id via
    ``auq_source.unlink_for_session``, which is no longer reachable from here.
    """
    _last_completed_ask_tool_input.pop(window_id, None)
    _last_auq_tool_use_id.pop(window_id, None)
    auq_source.forget_for_window(window_id)
    # D2 restart-recovery: tomb this window's durable pick mint-intents on AUQ/EPM
    # resolution (the primary teardown seam). Orphan-safety is also provided by
    # recovery-time form/source re-validation + the 24h GC, but tombing here keeps
    # the store hygienic and prevents a resolved card's tokens from recovering.
    pick_intent.teardown_window(window_id)
    # NOTE (Wave 2 P1-1): the action-ledger `released` tombstone is NOT
    # written here. `forget_ask_tool_input` is a GENERIC teardown helper —
    # it also fires from `/clear`, session replacement
    # (session_monitor._detect_and_cleanup_changes) and the generic
    # interactive-surface clear in bot.handle_new_message, none of which
    # prove the AUQ instance reached its tool_result. Releasing here would
    # mask a genuinely dispatched-but-UNRESOLVED row and remove the durable
    # single-use brake. `auq_ledger.release_window` is called only at the
    # two positive-proof seams: the explicit AUQ ``tool_result`` branch in
    # ``bot.handle_new_message`` and the startup reconciler's
    # tool_result-proven block in ``session_monitor``.
    # Bug 2: tear down the MessageDisplay live-prose capture for this window's
    # CURRENT session on resolution. This is the primary teardown seam — it
    # fires for BOTH AUQ (the tool_result branch) and ExitPlanMode (via the
    # ``has_interactive_surface`` clear branch in ``bot.handle_new_message``).
    # The ``/clear`` race (session_id swapped under us) is covered separately in
    # ``session_monitor`` using the OLD session id, mirroring ``unlink_for_session``.
    _md_session = session_id_for_window(window_id)
    if _md_session:
        md_capture.teardown_session(_md_session)
    # Wave 1: drop any in-flight pending claim too. The AUQ lifecycle
    # is ending (tool_result arrived); a pending claim from this AUQ
    # is no longer valid and rolling it forward would commit a stale
    # dedup_key for whatever AUQ comes next. No persist needed —
    # pending is in-memory only.
    _auq_context_post_pending.pop(window_id, None)
    had_marker = _auq_context_posted.pop(window_id, None) is not None
    had_record = _auq_context_msgs.pop(window_id, None) is not None
    if had_marker or had_record:
        _persist_interactive_state()


_CLAUDE_SETTINGS_FILE_FOR_WARN = Path.home() / ".claude" / "settings.json"


def warn_if_pre_tool_use_hook_missing(
    settings_file: Path = _CLAUDE_SETTINGS_FILE_FOR_WARN,
) -> bool:
    """Warn (via log) if the PreToolUse hook entry is missing from
    Claude Code's settings.json.

    The bot will still work without it — AUQ context messages will
    fall back to form-source (labels only). But the user loses the
    descriptions-at-pick-time win that justifies this whole wave.
    Surfacing this at startup with the exact install command is the
    actionable nudge.

    Returns True if a warning was emitted, False if the hook is current.
    """
    if not settings_file.exists():
        logger.warning(
            "Claude Code settings file not found at %s — run "
            "'cc-telegram hook --install' to enable AUQ descriptions",
            settings_file,
        )
        return True
    try:
        settings = json.loads(settings_file.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(
            "Claude Code settings file unreadable (%s); "
            "AUQ descriptions may be disabled: %s",
            settings_file,
            e,
        )
        return True
    # Reuse the hook module's own check so we have a single source of
    # truth for "what counts as installed".
    from ..hook import _is_pre_tool_use_installed

    if _is_pre_tool_use_installed(settings) == "missing":
        logger.warning(
            "PreToolUse(AskUserQuestion) hook not registered in %s; "
            "AUQ descriptions will fall back to labels-only. "
            "Run 'cc-telegram hook --install' to enable.",
            settings_file,
        )
        return True
    return False


def warn_if_notification_hook_missing(
    settings_file: Path = _CLAUDE_SETTINGS_FILE_FOR_WARN,
) -> bool:
    """Warn (via log) if the Notification hook entry is missing from
    Claude Code's settings.json.

    The Wave B extension of the EXISTING bot-startup hook-health seam
    (plan v3 B-misc): without the Notification hook, Workflow / permission
    approval gates have no detection path and the topic shows "🟡 Busy"
    forever instead of "🔔 Waiting on you".

    Returns True if a warning was emitted, False if the hook is current.
    """
    if not settings_file.exists():
        logger.warning(
            "Claude Code settings file not found at %s — run "
            "'cc-telegram hook --install' to enable waiting-on-you "
            "notifications",
            settings_file,
        )
        return True
    try:
        settings = json.loads(settings_file.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(
            "Claude Code settings file unreadable (%s); "
            "waiting-on-you notifications may be disabled: %s",
            settings_file,
            e,
        )
        return True
    # Reuse the hook module's own check — single source of truth for
    # "what counts as installed".
    from ..hook import _is_notification_installed

    if _is_notification_installed(settings) == "missing":
        logger.warning(
            "Notification hook not registered in %s; Workflow/permission "
            "approval waits will not surface as '🔔 Waiting on you'. "
            "Run 'cc-telegram hook --install' to enable.",
            settings_file,
        )
        return True
    return False


def claim_auq_context_post_in_memory(window_id: str, dedup_key: str) -> str | None:
    """Two-phase context-post gate, phase 1 (Wave 1, plan §5.1).

    In-memory only — does NOT persist. Returns an opaque 16-hex-char
    ``claim_token`` on success, or ``None`` if the window already has
    a committed context message (persisted ``_auq_context_posted``
    marker) or a same-process pending claim still in flight (younger
    than ``_PENDING_CLAIM_TTL_SECONDS``).

    The caller MUST pair a successful claim with exactly one of:
      * ``commit_auq_context_post(window_id, claim_token, message_ids,
        ...)`` — after at least one chunk landed on Telegram. Persists
        ``_auq_context_posted[window_id] = dedup_key`` AND the chunked
        record in ``_auq_context_msgs[window_id]`` in a single atomic
        write; subsequent renders see the marker and skip.
      * ``rollback_auq_context_post(window_id, claim_token)`` — when
        zero chunks landed. Drops the pending entry; nothing hits
        disk, so the next render re-attempts the context message.

    Crash between claim and commit: the pending entry is in-memory
    only, so a restart drops it. The next render claims again and
    re-posts. This is intentional — without a chunk-landed record, the
    restarted bot has no idea whether the prior context message
    reached Telegram, so re-rendering is the safe default. Under the
    pre-Wave-1 single-phase ``claim_auq_context_post`` the persisted
    marker survived restart even when no chunk landed, silently
    suppressing the context message forever.

    Same-process abandoned-claim TTL: a pending claim older than
    ``_PENDING_CLAIM_TTL_SECONDS`` (default 60s) is purged on the next
    same-window claim attempt. This catches hung coroutines that
    never reached commit/rollback. Tests inject a faster clock via
    ``_pending_claim_clock``.

    Synchronous; relies on the per-route ``asyncio.Lock`` held by the
    caller in ``handle_interactive_ui`` (the ``async with lock:``
    region that wraps the AUQ context-post + picker render) for
    atomicity between the freshness check and the pending write
    within a route. Cross-route writes are key-disjoint by the
    topic-only invariant (1 topic = 1 window = 1 route).

    ``dedup_key`` must be a non-empty string. Pass the JSONL
    ``tool_use_id`` when available; ``pretool:<tool_use_id>`` /
    ``pretool:<input_fingerprint>`` for the PreToolUse-hook side-file
    source; ``form:<fingerprint>`` for the pane-derived fallback.
    """
    if not dedup_key:
        return None
    if _auq_context_posted.get(window_id) is not None:
        return None
    existing = _auq_context_post_pending.get(window_id)
    now = _pending_claim_clock()
    if existing is not None:
        if now - existing.claimed_at <= _PENDING_CLAIM_TTL_SECONDS:
            return None
        # Stale same-process pending — purge and continue. Only fires
        # if some prior coroutine left a claim hanging past TTL.
        _auq_context_post_pending.pop(window_id, None)
    claim_token = secrets.token_hex(8)  # 16 hex chars
    _auq_context_post_pending[window_id] = _AuqContextPendingClaim(
        dedup_key=dedup_key,
        claim_token=claim_token,
        claimed_at=now,
    )
    return claim_token


def commit_auq_context_post(
    window_id: str,
    claim_token: str,
    message_ids: tuple[int, ...],
    *,
    text: str,
    source: "dict | AskUserQuestionForm",
    user_id: int,
    chat_id: int,
    thread_id: int | None,
    session_id: str | None,
) -> bool:
    """Two-phase context-post gate, phase 2 (Wave 1, plan §5.1).

    Persists the dedup marker (``_auq_context_posted[window_id]``)
    AND the chunked-record sidecar (``_auq_context_msgs[window_id]``)
    in a single atomic write, then drops the in-memory pending entry.
    Called after at least one chunk landed on Telegram.

    Idempotency contract — **first-call-wins**: the first valid
    commit drains the pending entry; any subsequent call (with the
    same or any token) finds no pending and returns False without
    side effects. Plan v4 §5.1 sketched a "later calls overwrite
    message_ids" semantic, but ``_send_auq_context_message`` (the
    only production caller) invokes commit exactly once per claim
    (the central ``_settle_pending`` helper guarantees this), so the
    overwrite mode was never exercised; first-call-wins is simpler,
    visibly correct, and matches what callers actually do. If a
    future use case needs overwrite-mode, gate it on a fresh helper
    rather than reinterpreting this one.

    The returned bool distinguishes "wrote it" (True) from "no-op"
    (False) for the caller's diagnostic logging.

    ``claim_token`` must match the value returned by
    ``claim_auq_context_post_in_memory``. A stale or wrong token
    no-ops without side effects — defensive against test fixtures
    that may synthesize tokens without claiming first.

    Called from ``_send_auq_context_message`` after the chunk loop
    completes (FULL_SENT) or after one or more chunks landed before
    a later chunk failed (PARTIAL_SENT). On PARTIAL_SENT the
    truncated ``sent_msg_ids`` get persisted so a restart finds the
    record and does NOT re-post the context message — re-posting
    would duplicate the chunks already on Telegram.
    """
    pending = _auq_context_post_pending.get(window_id)
    if pending is None:
        return False
    if pending.claim_token != claim_token:
        return False
    dedup_key = pending.dedup_key
    _auq_context_posted[window_id] = dedup_key
    _auq_context_post_pending.pop(window_id, None)
    # ``_record_context_post`` writes ``_auq_context_msgs`` and calls
    # ``_persist_interactive_state``, so both dicts hit disk together.
    _record_context_post(
        window_id=window_id,
        text=text,
        source=source,
        dedup_key=dedup_key,
        message_ids=message_ids,
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        session_id=session_id,
    )
    return True


def rollback_auq_context_post(window_id: str, claim_token: str) -> bool:
    """Two-phase context-post gate, phase 3 (Wave 1, plan §5.1).

    Drops the in-memory pending entry when zero chunks landed on
    Telegram. No persistence happens — the dedup marker is never
    written, so the next render re-attempts the context message
    from scratch.

    Idempotent: no-op if the pending entry was already cleared
    (commit landed, TTL purged it, or forget_ask_tool_input ran).
    Returns True iff a pending entry was actually dropped — useful
    for diagnostic logging.

    Called from ``_send_auq_context_message`` on NONE_SENT outcomes
    (empty formatter output, ``build_response_parts`` returned empty,
    or the first chunk's ``topic_send`` failed without any prior
    chunk landing).
    """
    pending = _auq_context_post_pending.get(window_id)
    if pending is None:
        return False
    if pending.claim_token != claim_token:
        return False
    _auq_context_post_pending.pop(window_id, None)
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
        data: dict[str, Any] = {
            "interactive_msgs": {
                f"{u}:{t}": rec.to_dict()
                for (u, t), rec in _interactive_msg_meta.items()
            },
            "auq_context_posted": dict(_auq_context_posted),
            "auq_context_msgs": {
                wid: rec.to_dict() for wid, rec in _auq_context_msgs.items()
            },
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
    # Codex P2 #2 (2026-05-25): collect (old → new) window-id remaps
    # from the interactive_msgs loop and re-apply them when hydrating
    # auq_context_msgs. Without this, a tmux server restart that
    # renumbers @12 → @13 would prune the upgrade record (because the
    # old key isn't in known_windows) even though the owning picker
    # card was kept under the new window id.
    window_remaps: dict[str, str] = {}

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
                window_remaps[old_window_id] = current_window
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

    # Hydrate ``auq_context_msgs`` (chunked context-message records used
    # by ``maybe_upgrade_auq_context_message``). Reject records whose
    # window is unknown to session_mgr — the picker that anchored them
    # is gone, no upgrade is possible. Backward compat: missing key is
    # the pre-upgrade-feature shape; treat as empty.
    #
    # Codex P2 #2 (2026-05-25): re-apply window-id remaps collected
    # during the interactive_msgs loop. Without this, @12 → @13 on
    # tmux restart would prune the upgrade record by old key even
    # though the owning interactive card was kept under the new key.
    raw_ctx_msgs = data.get("auq_context_msgs")
    pruned_ctx_msg_any = False
    if isinstance(raw_ctx_msgs, dict):
        for wid, payload in raw_ctx_msgs.items():
            if not isinstance(wid, str) or not isinstance(payload, dict):
                pruned_ctx_msg_any = True
                continue
            remapped_wid = window_remaps.get(wid, wid)
            if remapped_wid not in known_windows:
                logger.debug(
                    "AUQ hydrate: pruning stale auq_context_msgs record "
                    "for unknown window %s (persisted_key=%s)",
                    remapped_wid,
                    wid,
                )
                pruned_ctx_msg_any = True
                continue
            rec = _ContextMsgRecord.from_dict(payload)
            if rec is None:
                pruned_ctx_msg_any = True
                continue
            # Codex P2 round 4 #1 (2026-05-25): apply the same
            # session_id staleness check the interactive_msg sidecar
            # already does (see line ~575). A window that still exists
            # but now belongs to a different session (e.g. /clear ran)
            # would otherwise carry the record forward, and a later
            # maybe_upgrade_auq_context_message call would edit the
            # OLD session's Telegram message ids with the NEW session's
            # AUQ text.
            cur_session_for_ctx = session_id_for_window(remapped_wid)
            if (cur_session_for_ctx or "") != (rec.session_id or ""):
                logger.info(
                    "AUQ hydrate: pruning auq_context_msgs record for "
                    "window %s on session mismatch "
                    "(persisted_session=%s current_session=%s)",
                    remapped_wid,
                    (rec.session_id[:8] if rec.session_id else "<empty>"),
                    (cur_session_for_ctx[:8] if cur_session_for_ctx else "<none>"),
                )
                pruned_ctx_msg_any = True
                continue
            if remapped_wid != wid:
                logger.info(
                    "AUQ hydrate: remapping auq_context_msgs key %s → %s",
                    wid,
                    remapped_wid,
                )
                pruned_ctx_msg_any = True  # forces re-persist with new key
            _auq_context_msgs[remapped_wid] = rec

    logger.info(
        "AUQ hydrate: %d interactive_msg entries, %d context-posted markers, "
        "%d context_msgs records",
        len(_interactive_msgs),
        len(_auq_context_posted),
        len(_auq_context_msgs),
    )

    if state_mutated_any or pruned_ctx_any or pruned_ctx_msg_any:
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


# ── Per-route asyncio.Lock ───────────────────────────────────────────────
#
# Lock contract — what the route lock actually does today:
#
#   PROTECTED by ``_get_route_lock`` (state held atomically across the
#   relevant work):
#     * The pick-token store (``pick_token``) — pruned via
#       ``pick_token.prune_for_route`` under the lock in
#       ``clear_interactive_msg``'s Phase 1 so a concurrent
#       ``handle_interactive_ui`` (which awaits between pane capture and
#       mint) can't post a card whose tokens point at a cache row the
#       cleanup just dropped.
#     * The AUQ render path in ``handle_interactive_ui`` — context-post +
#       picker send/edit + attention fallback all run inside the same
#       ``async with lock`` block. Holding the lock across this Telegram
#       I/O is intentional: it guarantees that on a single route the
#       context message lands before the picker, and that two concurrent
#       callers (status_polling tick vs JSONL-dispatched render) don't
#       interleave two pickers / two context posts. Cross-route concurrency
#       is preserved because the lock is per (user_id, thread_id_or_0).
#
#   NOT PROTECTED (single-event-loop means sync dict writes don't
#   interleave with other coroutines' sync dict writes):
#     * ``_interactive_mode`` — written by ``handle_interactive_ui`` and
#       ``clear_interactive_msg``; dict ops are atomic and the only
#       observable ordering is the one the lock above already enforces
#       for the AUQ render path.
#     * ``_last_completed_ask_tool_input`` — replay cache written by
#       ``session_monitor`` (single writer) and read everywhere; dict ops
#       are atomic enough. It is not a live pending-AUQ source.
#
#   RELEASED between phases:
#     * ``clear_interactive_msg`` runs Phase 1 (snapshot + drop state +
#       prune pick-tokens) inside the lock, then releases before Phase 2
#       (Telegram deletes / tombstone edit / attention dismiss). Phase 2
#       I/O failures can't strand in-memory state because the drop already
#       committed.
#
#   Non-reentrant: the pick-token callback handler MUST NOT hold the lock
#   across ``await handle_interactive_ui(...)``. Validate, release, then
#   call.
#
# ``_route_locks`` are created on demand and never cleaned up. The keyspace
# is bounded by (user_id × thread_id) pairs the bot has seen — small in
# practice, and the lock objects are tiny.
_route_locks: dict[tuple[int, int], asyncio.Lock] = {}


def _get_route_lock(user_id: int, thread_id: int | None) -> asyncio.Lock:
    """Get or create the per-route lock for AUQ context-post + cleanup ordering."""
    key = (user_id, thread_id or 0)
    if key not in _route_locks:
        _route_locks[key] = asyncio.Lock()
    return _route_locks[key]


def has_interactive_surface(user_id: int, thread_id: int | None) -> bool:
    """True if the route currently owns an interactive card.

    Callers in ``bot.py`` (tool_result path) and ``status_polling.py``
    (UI-gone path) gate cleanup on this predicate instead of
    ``get_interactive_msg_id`` alone, because ``get_interactive_msg_id``
    returns the raw int message_id (which would be a truthy int even if
    zero) while callers want a clean route-owns-surface predicate.
    """
    key = (user_id, thread_id or 0)
    return key in _interactive_msgs


# ── PR 2b: structured option-pick callback tokens ────────────────────────
#
# The pick-token store + the atomic ``validate_and_consume`` finalizer moved
# to ``handlers.pick_token`` (R4). ``interactive_ui`` is now a pure MINTER: it
# resolves the AUQ source via ``auq_source.resolve_auq_source`` and calls
# ``pick_token.mint_row(...)`` (which owns the cache-reuse logic + the
# generation counter); the callback validates/consumes via
# ``pick_token.validate_and_consume``. ``interactive_ui`` no longer holds the
# token dicts, the entry dataclass, the prune, or the consume.


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


# Hard cap on rendered card body. Matches the message_queue.py merge limit
# so the renderer can never produce a body that the send layer would have
# to split — interactive cards are one Telegram message per AUQ.
_CARD_BODY_CHAR_CAP = 3800


def _should_post_auq_context(source: dict | AskUserQuestionForm | None) -> bool:
    """True iff the AUQ source has at least one question with renderable text.

    User invariant 2026-05-22: always post a separate "📋 AskUserQuestion
    — full details" info message alongside the picker for every AUQ
    that has any content to show. The gate aligns with what
    ``_format_auq_context_message`` actually renders — the formatter
    skips the whole question when ``question``/``header`` text is
    empty, so a gate firing on label-only forms would consume the
    claim and post a header-only message (the "convergent
    overengineering" risk Codex flagged on v2).

    Accepts EITHER a JSONL ``tool_use.input`` dict OR an
    ``AskUserQuestionForm`` (the pane-derived fallback for live AUQs
    where Claude Code hasn't flushed the ``tool_use`` line yet). Returns
    False only for malformed input (not a dict/form, no usable
    title/header text, no labeled options).
    """
    if isinstance(source, AskUserQuestionForm):
        return _form_has_postable_content(source)
    if not isinstance(source, dict):
        return False
    questions = source.get("questions")
    if not isinstance(questions, list):
        return False
    for q in questions:
        if not isinstance(q, dict):
            continue
        question_text = q.get("question") or q.get("header")
        if isinstance(question_text, str) and question_text.strip():
            return True
    return False


def _form_has_postable_content(form: AskUserQuestionForm) -> bool:
    """Predicate for the form-source path of _should_post_auq_context.

    True when the formatter would produce more than just the header
    line — i.e. at least one of:
      * a non-empty multi-question matrix title/header,
      * a non-empty ``current_question_title`` / ``pane_walkback_title``,
      * a single labeled option (the form fallback is intentionally
        looser than the JSONL gate: pane parses frequently lack
        descriptions, so labels alone still carry real context value).
    """
    for q in form.questions:
        if (q.title or q.header or "").strip():
            return True
    if (form.current_question_title or form.pane_walkback_title or "").strip():
        return True
    for opt in form.options:
        if (opt.label or "").strip():
            return True
    return False


def _format_auq_context_message(source: dict | AskUserQuestionForm) -> str:
    """Render an AUQ source as a readable context dump.

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

    Accepts EITHER a JSONL ``tool_use.input`` dict (rich source: full
    multi-question matrix with per-option descriptions) OR an
    ``AskUserQuestionForm`` (pane fallback for live AUQs — title and
    visible option labels; descriptions usually empty because Claude
    Code only writes them to JSONL after the user answers).
    """
    if isinstance(source, AskUserQuestionForm):
        return _format_auq_context_message_from_form(source)
    questions_raw = source.get("questions") or []
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


def _format_auq_context_message_from_form(form: AskUserQuestionForm) -> str:
    """Render a pane-derived ``AskUserQuestionForm`` as a context dump.

    Output shape mirrors the dict path so users see consistent
    formatting whether JSONL or pane is the source. Differences from
    the dict path:

      * Per-option descriptions are usually empty (pane parses don't
        carry them) — the formatter omits the description indent line
        when missing.
      * Single-tab forms with only ``pane_walkback_title`` get rendered
        with that title as the question line (display-only fallback
        when ``current_question_title`` hasn't been pinned yet).
      * Multi-question matrix (rare for pure pane parse, possible when
        the resolver merged JSONL into the form) takes the multi-Q
        layout; single-tab takes the bare title layout.

    Returns ``""`` when the form has no renderable content
    (header-only would be misleading). ``_should_post_auq_context``
    callers already gate on this in practice.
    """
    parts: list[str] = ["📋 AskUserQuestion — full details"]
    multi_question = len(form.questions) > 1
    if multi_question:
        parts.append("(Picker below answers each question one at a time.)")
    parts.append("")
    if multi_question:
        for q_idx, q in enumerate(form.questions, start=1):
            qtext = (q.title or q.header or "").strip()
            if not qtext:
                continue
            parts.append(f"Q{q_idx}. {qtext}")
            parts.append("")
            for opt_idx, label, description in _labeled_options_from_ask(q.options):
                parts.append(f"{opt_idx}. {label}")
                if description:
                    for line in description.splitlines() or [description]:
                        parts.append(f"   {line}")
                parts.append("")
    else:
        title = (form.current_question_title or form.pane_walkback_title or "").strip()
        if title:
            parts.append(title)
            parts.append("")
        for opt_idx, label, description in _labeled_options_from_ask(form.options):
            parts.append(f"{opt_idx}. {label}")
            if description:
                for line in description.splitlines() or [description]:
                    parts.append(f"   {line}")
            parts.append("")
    rendered = "\n".join(parts).rstrip()
    # Header-only output (no questions, no title, no options) is
    # misleading — return empty so the send layer / gate caller skip.
    if rendered == "📋 AskUserQuestion — full details":
        return ""
    return rendered


def _labeled_options_from_ask(options) -> list[tuple[int, str, str]]:
    """Filter + re-number a tuple of ``AskOption`` for context rendering.

    Skips options with empty labels; re-enumerates the survivors
    starting from 1 so the context message shows a clean 1..N list
    even when the pane parse contains gap-numbered options. This is
    safe because the context message is informational — actionable
    numbers live on the picker card itself (which preserves the
    pane's original numbering).
    """
    labeled: list[tuple[int, str, str]] = []
    for opt in options:
        label = (getattr(opt, "label", "") or "").strip()
        if not label:
            continue
        description = (getattr(opt, "description", "") or "").strip()
        labeled.append((len(labeled) + 1, label, description))
    return labeled


async def _send_auq_context_message(
    bot: Bot,
    *,
    user_id: int,
    thread_id: int | None,
    chat_id: int,
    window_id: str,
    source: dict | AskUserQuestionForm,
    claim_token: str,
) -> _ContextSendResult:
    """Format and send the AUQ context message (multi-part if needed).

    ``source`` is either the JSONL ``tool_use.input`` dict (rich path)
    or the pane-derived ``AskUserQuestionForm`` (live-AUQ fallback,
    added by the v5 fix on 2026-05-24). The formatter dispatches on
    type.

    Wave 1 (plan §5.1): the function takes a ``claim_token`` returned
    by ``claim_auq_context_post_in_memory`` and is responsible for
    pairing the claim with exactly one ``commit_auq_context_post`` (on
    any chunk landing) or ``rollback_auq_context_post`` (on zero
    chunks landing) before returning. The caller MUST NOT manually
    pop ``_auq_context_posted`` / ``_auq_context_msgs`` on NONE_SENT
    — those dicts are only populated by commit, and rollback
    cleanly returns the pending slot.

    Returns a tri-state outcome (Wave A, Codex v2→v3 P2 #3):
      * ``FULL_SENT`` — every chunk landed; commit ran with all msg_ids.
      * ``NONE_SENT`` — zero chunks landed (pre-loop no-op exit, or
        the first chunk failed); rollback ran (no persistence).
      * ``PARTIAL_SENT`` — chunk 1 (at least) landed but a later chunk
        failed; commit ran with the truncated ``sent_msg_ids`` so a
        restart finds the chunked record and does NOT re-post.

    Anchors the first chunk to the user's last prompt via
    ``peek_route_last_user_message`` (non-consuming). Subsequent chunks
    land unanchored.

    ``RetryAfter`` from python-telegram-bot's flood control IS re-raised
    so the caller's flood-control contract is honored (the route lock
    holds, the picker render inherits the back-off). The pending claim
    is settled BEFORE the re-raise — commit on partial landing,
    rollback on no landing — so the in-memory slot doesn't sit until
    the 60s TTL while subsequent renders are blocked. Other
    exceptions are caught and mapped to NONE_SENT/PARTIAL_SENT based
    on whether any chunk landed (Hermes v3→v4 P2 #2 — preserve the
    existing defensive catch).
    """
    from telegram.error import RetryAfter

    from .message_queue import peek_route_last_user_message
    from .response_builder import build_response_parts

    text = _format_auq_context_message(source)
    if not text.strip():
        # Codex v4→v5 P2 #2: explicit NONE_SENT so caller's
        # rollback fires (Wave 1: handled inline here, not by caller).
        rollback_auq_context_post(window_id, claim_token)
        return _ContextSendResult.NONE_SENT
    parts = build_response_parts(text, content_type="text", role="assistant")
    if not parts:
        rollback_auq_context_post(window_id, claim_token)
        return _ContextSendResult.NONE_SENT

    anchor: ReplyParameters | None = None
    if config.reply_context_enabled:
        anchor_id = peek_route_last_user_message(user_id, thread_id, window_id)
        if anchor_id is not None:
            anchor = ReplyParameters(message_id=anchor_id)

    session_id = session_id_for_window(window_id)
    total = len(parts)
    sent_any = False
    sent_msg_ids: list[int] = []

    def _settle_pending(*, sent_any: bool) -> None:
        """Commit or rollback the pending claim based on landing state.

        Centralises the Wave 1 settlement so all three exit paths
        (exception, sent-is-None, end-of-loop) stay consistent. On
        ``sent_any`` True, commit with the (possibly truncated)
        ``sent_msg_ids`` — restart-safety: a restart sees the
        chunked record and does NOT re-post. On False, rollback —
        nothing landed, the next render re-attempts cleanly.
        """
        if sent_any:
            commit_auq_context_post(
                window_id,
                claim_token,
                tuple(sent_msg_ids),
                text=text,
                source=source,
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                session_id=session_id,
            )
        else:
            rollback_auq_context_post(window_id, claim_token)

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
            # Wave 1: settle the pending slot BEFORE re-raising so the
            # next render isn't blocked for the full 60s TTL while
            # AIORateLimiter's back-off runs upstream. Commit with the
            # partial sent_msg_ids if any landed; rollback otherwise.
            _settle_pending(sent_any=sent_any)
            raise
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "AUQ context message send raised (window=%s, part %d/%d): %s",
                window_id,
                idx,
                total,
                exc,
            )
            _settle_pending(sent_any=sent_any)
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
            _settle_pending(sent_any=sent_any)
            return (
                _ContextSendResult.PARTIAL_SENT
                if sent_any
                else _ContextSendResult.NONE_SENT
            )
        sent_any = True
        sent_msg_ids.append(int(sent.message_id))
    _settle_pending(sent_any=True)  # FULL_SENT path; sent_msg_ids is non-empty
    return _ContextSendResult.FULL_SENT


def _record_context_post(
    *,
    window_id: str,
    text: str,
    source: dict | AskUserQuestionForm,
    dedup_key: str,
    message_ids: tuple[int, ...],
    user_id: int,
    chat_id: int,
    thread_id: int | None,
    session_id: str | None,
) -> None:
    """Write ``_auq_context_msgs[window_id]`` after a context-message send.

    Captures everything ``maybe_upgrade_auq_context_message`` later
    needs: the chunked Telegram ids, the source kind (so the upgrade
    path knows whether a richer source can replace it), the rendered
    text's SHA-1 (for no-op upgrade detection), and the route bindings
    (so an upgrade fired from a non-route context — session_monitor
    poll loop — still knows where to edit).
    """
    src_kind = "dict" if isinstance(source, dict) else "form"
    render_sha1 = hashlib.sha1(text.encode("utf-8")).hexdigest()
    tool_use_id: str | None = None
    if isinstance(source, dict):
        # The dict came from JSONL via _resolve_ask_tool_input, which
        # doesn't preserve the tool_use_id alongside the input. Look
        # it up from the cache populated by remember_ask_tool_input.
        tool_use_id = _last_auq_tool_use_id.get(window_id)
    _auq_context_msgs[window_id] = _ContextMsgRecord(
        message_ids=message_ids,
        source=src_kind,
        dedup_key=dedup_key,
        tool_use_id=tool_use_id,
        render_sha1=render_sha1,
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id or 0,
        session_id=session_id or "",
        created_at=datetime.now(UTC).isoformat(),
    )
    _persist_interactive_state()


async def maybe_upgrade_auq_context_message(
    bot: Bot,
    window_id: str,
    *,
    session_mgr=None,
) -> bool:
    """If a form-source context message exists for ``window_id`` and the
    rich JSONL dict is now cached, edit the message(s) in place to add
    descriptions.

    Idempotent: returns ``False`` (no action) when:
      * no context record exists for the window,
      * the record is already ``source="dict"`` (upgrade ran),
      * no rich dict is cached in ``_last_completed_ask_tool_input``,
      * the rich render is byte-identical to the form render (no-op,
        but the record source is still flipped to ``"dict"`` so a
        future call short-circuits here).

    On real upgrade: re-renders the dict version, chunks it via
    ``build_response_parts``, edits each existing message_id in order,
    and appends new messages for any extra chunks (rich descriptions
    are much longer than form labels — extra chunks are the common
    case). Shorter-render edge case (rich is somehow shorter) does
    NOT trim trailing messages — the leftover chunk(s) just keep the
    form-source text and aren't strictly correct but also not
    misleading; pruning would require another pass and is low-value.

    Called from:
      * ``bot.handle_new_message`` immediately after
        ``remember_ask_tool_input`` succeeds with a dict input.
      * ``session_monitor._hydrate_ask_tool_input_cache`` after the
        same call (covers the case where the bot restarts before the
        first AUQ tool_result, hydration finds the buffered tool_use,
        and the originally form-rendered context message gets
        upgraded).

    Per-route lock serialises upgrade vs. any concurrent send/edit
    triggered by ``handle_interactive_ui`` for the same route. We can
    derive the route from the record we persisted at send time.
    """
    from telegram.error import RetryAfter

    from .response_builder import build_response_parts

    rec = _auq_context_msgs.get(window_id)
    if rec is None:
        return False
    if rec.source == "dict":
        return False

    tool_input = _last_completed_ask_tool_input.get(window_id)
    if not isinstance(tool_input, dict):
        return False

    new_text = _format_auq_context_message(tool_input)
    if not new_text.strip():
        return False
    new_sha1 = hashlib.sha1(new_text.encode("utf-8")).hexdigest()
    new_tool_use_id = _last_auq_tool_use_id.get(window_id)

    if session_mgr is None:
        session_mgr = session_manager
    thread_id_arg: int | None = rec.thread_id if rec.thread_id else None
    lock = _get_route_lock(rec.user_id, thread_id_arg)
    async with lock:
        # Re-check under lock — a concurrent send/clear may have raced.
        rec = _auq_context_msgs.get(window_id)
        if rec is None or rec.source == "dict":
            return False

        if new_sha1 == rec.render_sha1:
            # No-op upgrade (rich render byte-identical — e.g. JSONL
            # descriptions are also empty). Flip source/tool_use_id so
            # a future call short-circuits without re-rendering.
            _auq_context_msgs[window_id] = _ContextMsgRecord(
                message_ids=rec.message_ids,
                source="dict",
                dedup_key=new_tool_use_id or rec.dedup_key,
                tool_use_id=new_tool_use_id,
                render_sha1=rec.render_sha1,
                user_id=rec.user_id,
                chat_id=rec.chat_id,
                thread_id=rec.thread_id,
                session_id=rec.session_id,
                created_at=rec.created_at,
            )
            _persist_interactive_state()
            return False

        new_parts = build_response_parts(
            new_text, content_type="text", role="assistant"
        )
        if not new_parts:
            return False

        existing_ids = list(rec.message_ids)
        # Edit phase — overwrite chunks that already exist.
        # Codex P2 round 2 (2026-05-25): a partial edit failure (e.g.
        # chunk 1 edits OK, chunk 2 returns TOPIC_CLOSED / FORBIDDEN /
        # gets a transient exception) MUST NOT commit the upgrade.
        # Otherwise the user sees mixed chunks (chunk 1 with rich
        # text, chunk 2 with form-only text) while the record claims
        # source="dict" so no retry ever fires. Track an "all edits
        # succeeded" predicate: if False, abort without committing —
        # next poll re-attempts the full edit (already-edited chunks
        # short-circuit with MESSAGE_NOT_MODIFIED, which is harmless).
        expected_edits = min(len(existing_ids), len(new_parts))
        edited_ids: list[int] = []
        edit_partial = False
        for idx, (msg_id, chunk) in enumerate(zip(existing_ids, new_parts), start=1):
            try:
                outcome = await topic_edit(
                    bot,
                    op="interactive",
                    user_id=rec.user_id,
                    chat_id=rec.chat_id,
                    thread_id=thread_id_arg,
                    window_id=window_id,
                    message_id=msg_id,
                    text=chunk,
                    plain=True,
                )
            except RetryAfter:
                raise
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "AUQ context-message upgrade edit failed "
                    "(window=%s, part %d/%d): %s",
                    window_id,
                    idx,
                    len(new_parts),
                    exc,
                )
                edit_partial = True
                break
            # MESSAGE_NOT_MODIFIED is fine — count as a successful edit.
            # Codex P2 #1 (2026-05-25): outcome check MUST gate the
            # append. Otherwise TOPIC_CLOSED / TOPIC_NOT_FOUND /
            # FORBIDDEN / OTHER would still flip the record source to
            # ``"dict"`` (because ``final_ids`` is non-empty), and the
            # original form-source post would never get a retry.
            if outcome not in (
                TopicSendOutcome.OK,
                TopicSendOutcome.MESSAGE_NOT_MODIFIED,
            ):
                logger.warning(
                    "AUQ context-message upgrade edit unexpected outcome=%s "
                    "(window=%s, part %d/%d) — keeping form-source record "
                    "for retry",
                    outcome,
                    window_id,
                    idx,
                    len(new_parts),
                )
                edit_partial = True
                break
            edited_ids.append(msg_id)

        if edit_partial or len(edited_ids) < expected_edits:
            # Codex P2 round 2: at least one existing chunk still
            # shows form-source text. Do NOT commit — leave record as
            # source="form" so the next poll retries the full upgrade.
            # Already-edited chunks (with dict text) become a transient
            # mixed render until the retry succeeds; acceptable, since
            # the alternative is permanent stale chunks with no retry.
            logger.info(
                "AUQ context-message upgrade: partial edit "
                "(window=%s, edited %d/%d) — record unchanged for retry",
                window_id,
                len(edited_ids),
                expected_edits,
            )
            return False

        # Append phase — if the rich render is longer than the form
        # render, send the extra chunks. New chunks land at the end of
        # the chat (the picker card is in between, but Telegram doesn't
        # let us insert messages between existing ones, and the user's
        # view already follows the picker — extra chunks land below it,
        # which is the same shape as the original first-send layout
        # would have been for a longer text. The tradeoff is acceptable
        # given the alternative is no descriptions at all).
        #
        # Codex P2 round 3 #1 (2026-05-25): a partial append failure
        # must NOT commit. Same shape as the partial-edit case: if
        # we commit source="dict" with edited_ids + a few appended ids,
        # the tail chunks of the rich render will never get retried,
        # leaving the context message permanently truncated. Track
        # append_partial → abort without committing on partial.
        appended_ids: list[int] = []
        append_partial = False
        expected_appends = max(0, len(new_parts) - len(existing_ids))
        for idx, chunk in enumerate(
            new_parts[len(existing_ids) :], start=len(existing_ids) + 1
        ):
            try:
                sent, _outcome = await topic_send(
                    bot,
                    op="interactive",
                    user_id=rec.user_id,
                    chat_id=rec.chat_id,
                    thread_id=thread_id_arg,
                    window_id=window_id,
                    text=chunk,
                    role="assistant",
                    content_type="text",
                    session_id=rec.session_id or session_id_for_window(window_id),
                    part_index=idx if len(new_parts) > 1 else 0,
                )
            except RetryAfter:
                raise
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "AUQ context-message upgrade append failed "
                    "(window=%s, part %d/%d): %s",
                    window_id,
                    idx,
                    len(new_parts),
                    exc,
                )
                append_partial = True
                break
            if sent is None:
                logger.warning(
                    "AUQ context-message upgrade append chunk dropped "
                    "(window=%s, part %d/%d)",
                    window_id,
                    idx,
                    len(new_parts),
                )
                append_partial = True
                break
            appended_ids.append(int(sent.message_id))

        if append_partial or len(appended_ids) < expected_appends:
            # Codex P2 round 3 #1: partial append. Do NOT commit
            # source="dict" — that would short-circuit retries and
            # tail chunks would be lost forever.
            #
            # Codex P2 round 4 #2 (2026-05-25): if some appended_ids
            # DID land before the failure, persist them onto the
            # record (source still "form") so the next retry's edit
            # phase covers them idempotently (MESSAGE_NOT_MODIFIED on
            # already-correct text) and the append phase only re-runs
            # for the unlanded tail. Without this, the next retry
            # would re-append the already-landed chunk and the user
            # would see duplicate context chunks in the chat.
            if appended_ids:
                _auq_context_msgs[window_id] = _ContextMsgRecord(
                    message_ids=tuple(list(rec.message_ids) + appended_ids),
                    source="form",  # still incomplete — keep form for retry
                    dedup_key=rec.dedup_key,
                    tool_use_id=rec.tool_use_id,
                    render_sha1=rec.render_sha1,
                    user_id=rec.user_id,
                    chat_id=rec.chat_id,
                    thread_id=rec.thread_id,
                    session_id=rec.session_id,
                    created_at=rec.created_at,
                )
                _persist_interactive_state()
            logger.info(
                "AUQ context-message upgrade: partial append "
                "(window=%s, appended %d/%d) — record unchanged for retry "
                "(landed_ids preserved=%d)",
                window_id,
                len(appended_ids),
                expected_appends,
                len(appended_ids),
            )
            return False

        final_ids = tuple(edited_ids + appended_ids)
        if not final_ids:
            # Nothing landed — keep the existing record unchanged so a
            # future poll can retry.
            return False

        _auq_context_msgs[window_id] = _ContextMsgRecord(
            message_ids=final_ids,
            source="dict",
            dedup_key=new_tool_use_id or rec.dedup_key,
            tool_use_id=new_tool_use_id,
            render_sha1=new_sha1,
            user_id=rec.user_id,
            chat_id=rec.chat_id,
            thread_id=rec.thread_id,
            session_id=rec.session_id,
            created_at=rec.created_at,
        )
        _persist_interactive_state()
        logger.info(
            "AUQ context-message upgrade: window=%s edited=%d appended=%d "
            "tool_use_id=%s",
            window_id,
            len(edited_ids),
            len(appended_ids),
            (new_tool_use_id[:12] if new_tool_use_id else "<none>"),
        )
        return True


def _clip_card_body(body: str) -> str:
    """Hard-clip rendered card body to ``_CARD_BODY_CHAR_CAP`` chars.

    Defense in depth: the picker card renders option labels only (full
    descriptions live in the separate context message), but a question with
    many options + very long question text could still push the body over the
    cap. We clip on a line boundary so the truncation doesn't land
    mid-sentence; final line marks the cut.
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


# The selection (picker) card lists option LABELS only; the full question +
# per-option descriptions live in the separate "📋 full details" card. On the
# side-file/JSONL render path the ENTIRE ``questions[i].question`` string
# becomes ``form.current_question_title`` (terminal_parser.build_form_from_tool_input),
# so a multi-paragraph question would otherwise render verbatim above the
# options — pushing the tappable choices to the bottom and risking
# ``_clip_card_body``'s tail clip cutting the option lines off entirely. Cap
# the preamble shown in the picker card.
_SELCARD_TITLE_MAX_CHARS = 200


def _clip_card_title(title: str | None) -> str:
    """Clip the question/preamble shown in the SELECTION (picker) card.

    DISPLAY-only: this clips the LOCAL render string passed to the picker body.
    ``form.current_question_title`` is NEVER mutated, so the form fingerprint
    (``AskUserQuestionForm._canonical_repr`` → ``fingerprint``) stays
    byte-identical and tap-dispatch / ``pick_token`` mint+validate / the render
    dedup key are all unaffected. Clipping happens BEFORE the option lines are
    appended, so a long PREAMBLE can no longer be the reason the option lines
    get tail-clipped by ``_clip_card_body``. (NOT an absolute guarantee that
    options always survive: a pathological card with very many / very long
    OPTION labels, or a huge multi-question tab strip, can still exceed
    ``_CARD_BODY_CHAR_CAP`` and tail-clip — a separate, pre-existing
    ``_clip_card_body`` limit this change does not address.)

    A plain ellipsis (not a "see full details above" pointer) is used on
    purpose: the "📋 full details" card is suppressed on the ``bail_no_ctx``
    paths, where a pointer to a non-existent card would mislead — the full
    question still lives in that details card on the common paths and always on
    the tmux pane.
    """
    if not title:
        return title or ""
    if len(title) <= _SELCARD_TITLE_MAX_CHARS:
        return title
    head = title[:_SELCARD_TITLE_MAX_CHARS]
    # Prefer a word boundary in the last ~30% so the cut never lands mid-word;
    # fall back to a hard cut when there is no nearby space (e.g. a long token).
    cut = head.rfind(" ")
    if cut >= int(_SELCARD_TITLE_MAX_CHARS * 0.7):
        head = head[:cut]
    return head.rstrip() + "…"


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
        # Cap the preamble so the picker card stays short and a long question
        # no longer pushes the option lines off the bottom (DISPLAY-only — the
        # form is not mutated; the full question lives in the "📋 full details"
        # card).
        lines.append(_clip_card_title(title))
        lines.append("")

    if form.options:
        if form.select_mode == "multi":
            any_unknown = False
            for opt in form.options:
                if opt.selected is True:
                    glyph = "☑"
                elif opt.selected is False:
                    glyph = "☐"
                else:
                    glyph = "·"
                    any_unknown = True
                cursor = "❯ " if opt.cursor else "  "
                rec = " (Recommended)" if opt.recommended else ""
                lines.append(f"{cursor}{glyph} {opt.number}. {opt.label}{rec}")
                # Labels only — full per-option descriptions live in the
                # separate "📋 AskUserQuestion — full details" context message,
                # never inline in the picker card. Inlining them here
                # (multi-select only, added 2026-05-28, asymmetric with
                # single-select + the review screen) bloated the card and risked
                # _clip_card_body cutting later options off when descriptions
                # were long or numerous. Single-select renders labels only too.
            if form.is_free_text:
                lines.append("")
                lines.append("  (Type something — send a regular message to free-text)")
            lines.append("")
            if form.options_complete:
                lines.append(
                    "Tap a number to toggle · ⇥ Tab to review & submit · ⎋ Esc to cancel"
                )
            else:
                lines.append(
                    "full list unavailable; use ↑/↓ + Space then Tab in the keys below"
                )
            if any_unknown:
                lines.append(
                    "(· = selection state off-screen; tap to toggle, or scroll the tmux pane to confirm)"
                )
            return _clip_card_body("\n".join(lines).rstrip())

        for opt in form.options:
            cursor = "❯ " if opt.cursor else "  "
            rec = " (Recommended)" if opt.recommended else ""
            lines.append(f"{cursor}{opt.number}. {opt.label}{rec}")
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
    resolved_source: auq_source.ResolvedAuqSource,
) -> list[list[InlineKeyboardButton]]:
    """Build inline-keyboard rows of option-pick buttons for a parsed form.

    One button per option; max 5 per row. Each button mints a single-use
    token bound to ``(window, fingerprint, option_number, option_label)`` plus
    the minted ``(source_kind, source_fingerprint)`` from ``resolved_source``
    so ``pick_token.validate_and_consume`` can detect a "form changed under us"
    race AND a source drift before dispatching the keystroke.

    Review-screen Submit/Cancel rows are rendered here too. The Submit
    button is flagged ``is_review_submit=True`` so the callback handler
    can apply a tighter guard (must still be on the review screen) before
    sending Enter / digit 1.

    Returns an empty list when the form has no options — caller drops the
    structured-pick row and falls back to the keystroke keyboard only.

    Gates:
      * FA5+ safety — for multi-question forms (``len(form.questions) > 1``),
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

    # FA5+: multi-question form without confirmed current-tab inference.
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

    if form.select_mode == "unknown":
        logger.info("_build_pick_button_rows SUPPRESSED gate=select_mode_unknown")
        return []
    if form.select_mode == "multi" and len(form.questions) > 1:
        logger.info(
            "_build_pick_button_rows SUPPRESSED gate=multi_question_multi_select"
        )
        return []
    if form.select_mode == "multi" and not form.options_complete:
        logger.info("_build_pick_button_rows SUPPRESSED gate=incomplete_multi_select")
        return []
    if form.select_mode == "multi":
        return _build_multi_toggle_rows(
            user_id, thread_id, window_id, form, resolved_source
        )

    fingerprint = form.fingerprint()

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

    # Mint (or reuse) the sibling-token row via the pick_token store. mint_row
    # owns the cache-reuse logic (so ``_pick_token_cache`` stays private): a
    # FRESH mint of this form generation allocates the next module-global
    # generation and stamps it on the row + entries; an unchanged-form
    # re-render returns the SAME tokens with NO generation bump (preserving
    # Telegram MESSAGE_NOT_MODIFIED). Each entry records the resolved
    # ``(source_kind, source_fingerprint)`` so validate can measure source
    # parity.
    tokens, fresh = pick_token.mint_row(
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        fingerprint=fingerprint,
        source_kind=resolved_source.kind,
        source_fingerprint=resolved_source.source_fingerprint,
        specs=[
            pick_token._mint_spec(
                opt.number or 0,
                opt.label,
                bool(
                    form.is_review_screen
                    and opt.number == 1
                    and opt.label == REVIEW_SUBMIT_LABEL
                ),
            )
            for opt in pickable
        ],
    )

    # D2 restart-recovery: on a FRESH mint only, persist the per-token mint
    # intent so the callback handler can recover + re-dispatch the first
    # token-less tap after a bot restart (the in-memory store is wiped, but the
    # published card keeps its old keyboard). aqp: single-select + review-Submit
    # ONLY — the aqt: multi-toggle minter (``_build_multi_toggle_rows``)
    # deliberately does NOT persist (out of D2 scope). A byte-identical re-render
    # (``fresh=False``) reuses the same tokens whose intent already persists.
    if fresh:
        pick_intent.record_row(
            full_fingerprint=fingerprint,
            source_kind=resolved_source.kind,
            source_fingerprint=resolved_source.source_fingerprint,
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
            session_id=peek_session_id_for_window(window_id),
            minted_at=time.time(),
            token_specs=[
                pick_intent.TokenSpec(
                    token=token,
                    option_number=opt.number or 0,
                    option_label=opt.label,
                    is_review_submit=bool(
                        form.is_review_screen
                        and opt.number == 1
                        and opt.label == REVIEW_SUBMIT_LABEL
                    ),
                )
                for opt, token in zip(pickable, tokens)
            ],
        )

    # Wave 3: callback_data now carries (route_hash, fp8, opt, token) so
    # the restart-safe ledger can reconstruct the stable key without
    # needing the in-memory pick-token store to survive process restart.
    # ``fp8`` is an idempotency-key fragment, NOT a security primitive —
    # authorization comes from the in-memory token + owner check + live
    # pane revalidation in pick_token.validate_and_consume.
    from . import auq_ledger

    route_hash = auq_ledger.make_route_hash(user_id, thread_id, window_id)
    fp8 = fingerprint[:8]

    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    # Telegram tolerates more than 5 buttons per row, but on a phone the
    # text gets clipped after ~5. Cap conservatively.
    width = 5
    for opt, token in zip(pickable, tokens):
        # ``opt.number is None`` was filtered above, but reassure the type
        # checker.
        assert opt.number is not None
        is_submit = (
            form.is_review_screen
            and opt.number == 1
            and opt.label == REVIEW_SUBMIT_LABEL
        )
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
        callback_payload = f"{CB_ASK_PICK}{route_hash}:{fp8}:{opt.number}:{token}"
        row.append(
            InlineKeyboardButton(
                text, callback_data=checked_callback_data(callback_payload)
            )
        )
        if len(row) >= width:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def _build_multi_toggle_rows(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    form: AskUserQuestionForm,
    resolved_source: auq_source.ResolvedAuqSource,
) -> list[list[InlineKeyboardButton]]:
    """Build multi-select toggle buttons that dispatch a bare digit only."""
    assert form.select_mode == "multi"
    assert form.options_complete
    assert len(form.questions) <= 1

    fingerprint = form.fingerprint()
    pickable = [
        opt for opt in form.options if opt.number is not None and 1 <= opt.number <= 9
    ]
    if not pickable:
        return []

    # aqt: multi-select toggles are OUT of D2 restart-recovery scope, so this
    # minter discards the ``fresh`` flag and never persists a durable mint
    # intent (only the aqp: single-select / review-Submit minter does).
    tokens, _ = pick_token.mint_row(
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        fingerprint=fingerprint,
        source_kind=resolved_source.kind,
        source_fingerprint=resolved_source.source_fingerprint,
        specs=[
            pick_token._mint_spec(opt.number or 0, opt.label, False) for opt in pickable
        ],
    )

    from . import auq_ledger

    route_hash = auq_ledger.make_route_hash(user_id, thread_id, window_id)
    fp8 = fingerprint[:8]
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for opt, token in zip(pickable, tokens):
        assert opt.number is not None
        if opt.selected is True:
            glyph = "☑"
        elif opt.selected is False:
            glyph = "☐"
        else:
            glyph = "·"
        truncated = opt.label if len(opt.label) <= 24 else opt.label[:24] + "…"
        text = f"{glyph} {opt.number}. {truncated}"
        callback_payload = f"{CB_ASK_TOGGLE}{route_hash}:{fp8}:{opt.number}:{token}"
        row.append(
            InlineKeyboardButton(
                text, callback_data=checked_callback_data(callback_payload)
            )
        )
        if len(row) >= 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    # Optional discoverability button deliberately skipped: the existing ⇥ Tab
    # key already sends users to the review screen without adding a second alias.
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


# ── Interactive approval-gate (Permission / Workflow) display-only card ────
#
# PR-1 surfaces the gates with the EXISTING window-keyed manual ↑/↓/⏎/Esc nav
# keyboard and NO semantic option-pick buttons. The card honestly labels the
# nav controls as raw, un-cursor-verified live-pane keystrokes (Hermes P2-1):
# a ⏎/Esc tap sends a literal keystroke to whatever the live cursor is on,
# with no classify / verify / ledger (those are PR-2). The card body already
# carries the full pane region (extracted question + options + — for Workflow
# — phases + token warning), so the user reads the choices before acting.

_GATE_RENDER_NAMES: frozenset[str] = frozenset({"Permission", "Workflow"})

_GATE_NAV_NOTICE = (
    "⚠️ These controls type directly into the live terminal and are NOT "
    "double-checked against the cursor — read the options above, then tap "
    "↑/↓ to move and ⏎ Enter to confirm (or ⎋ Esc to decline)."
)


def _gate_card_text(content: InteractiveUIContent) -> str:
    """Compose the display-only gate card body: the extracted pane region
    (question + options, and for Workflow the phases + token-cost warning)
    followed by the honest un-verified-keystroke notice (P2-1).

    The raw extracted region (``content.content``) is the body — it already
    contains everything the user needs to read, including the Workflow phases
    and the "Dynamic workflows can use a lot of tokens" warning. PR-1 adds NO
    pick buttons; PR-2 will add a verified one-tap "Yes".
    """
    body = content.content.rstrip()
    return f"{body}\n\n{_GATE_NAV_NOTICE}"


# The per-tab card state machine (PRs #11/12/13) was retired in Wave 2 —
# git history pre-2026-05-26 has the deleted implementation. Multi-question
# AskUserQuestion forms are now handled by the single-card path below,
# which walks tabs in place by editing body+keyboard as the picker advances.


# Bug 2 live-prose: the bounded retry budget when the picker is DETECTED before
# the matching MessageDisplay ``final`` delta has landed. NOTE (PR-1): the prose
# finalizes a meaningful gap BEFORE the picker is detected (~5.44s idle, ~20.7s
# loaded — NOT the old inverted "~0.68s" claim), so in practice the final has
# landed by the time the picker is detected and the first read hits; this only
# covers the rare same-tick race. The freshness gate that recovers a large gap is
# the emission-anchor additive-OR (``select_fresh_prose``), NOT this retry. Held
# under the route lock, so it is intentionally short.
_LIVE_PROSE_RETRY_BUDGET_S = 0.25
_LIVE_PROSE_RETRY_STEP_S = 0.05
# Late-finalize fix: when the base budget above expires with no finalized prose
# AND a prose message is actively streaming (``md_capture.is_prose_streaming``),
# extend the wait ONCE by this much so a prose that finalizes mid-stream (the
# picker was detected before its final delta landed) still posts BEFORE the card
# instead of arriving after it via the post-resolution JSONL path. Triggers only
# on a real streaming signal (a prose-less picker bails at the base budget, zero
# added delay) and degrades to today's miss on expiry (never hangs). The detect-
# latency figure (~5-21s) is NOT this budget — the prose is already finalized
# during that lag (the first read hits); this only covers the genuine
# mid-stream-at-detection window. Held under the route lock (which no user-tap
# path contends on), so it is bounded.
_LIVE_PROSE_STREAM_WAIT_BUDGET_S = 3.0

# Conservative per-chunk cap for the pre-card live-prose split (< Telegram's 4096
# hard limit). ``split_message`` can return a chunk a few chars OVER its
# ``max_length`` when it auto-closes a fenced code block or wraps an expandable
# quote at the boundary (observed ~4097 at 4096), which would trip "Message is
# too long"; the headroom keeps every boundary chunk sendable.
_LIVE_PROSE_CHUNK_MAX = 4000


async def _read_epm_plan_file(footer_path: str | None) -> str | None:
    """Read the ExitPlanMode plan file referenced in the pane footer.

    Expands ``~`` and REQUIRES the resolved path to live under
    ``~/.claude/plans/`` (path-traversal guard). Reads off the event loop
    (``asyncio.to_thread``) since the plan can be multiple KB and this runs
    under the route lock. Returns the content, or None on any failure
    (missing/unreadable/outside-base/empty) — every failure degrades to
    today's behavior (the plan arrives post-resolution via JSONL), never
    raises."""
    if not footer_path:
        return None
    try:
        path = Path(footer_path).expanduser().resolve()
        base = (Path.home() / ".claude" / "plans").resolve()
        if not path.is_relative_to(base):
            logger.warning(
                "EPM plan path outside ~/.claude/plans/: %s — skipping", footer_path
            )
            return None
        content = await asyncio.to_thread(path.read_text, encoding="utf-8")
    except Exception as exc:  # FileNotFound / Permission / decode — degrade
        logger.debug("EPM plan file unreadable (%s): %s", footer_path, exc)
        return None
    return content if content.strip() else None


async def _maybe_post_epm_plan(
    bot: Bot,
    *,
    user_id: int,
    thread_id: int | None,
    chat_id: int,
    window_id: str,
    pane_text: str,
    tool_input: dict | None,
) -> None:
    """Post the ExitPlanMode plan BODY before the picker card, so the user does
    not approve blind. The plan text is the tool's ``input.plan``, buffered in
    JSONL until resolution — so for a LIVE pane-rendered card (``tool_input``
    None) it is read from the ``~/.claude/plans/<slug>.md`` file named in the
    pane footer (the agent Write-s it during the turn). A marker keyed by the
    plan's ``norm_hash`` makes this idempotent across poll re-renders AND a
    restart, and lets ``session_monitor.filter_live_prose_duplicates`` suppress
    the post-resolution JSONL copy (mint/validate parity via the SAME
    ``prose_norm_hash``). A miss (no path / file gone / send fail) is a silent
    no-op: the JSONL copy still delivers post-resolution (no loss). Ordered
    AFTER ``_maybe_post_live_prose`` and BEFORE the card by call-site placement,
    under the route lock."""
    from .response_builder import build_response_parts

    # Prefer the JSONL tool_input (replay/parity-perfect); else the live file.
    plan_text: str | None = None
    if isinstance(tool_input, dict):
        p = tool_input.get("plan")
        if isinstance(p, str) and p.strip():
            plan_text = p
    if plan_text is None:
        plan_text = await _read_epm_plan_file(extract_epm_plan_file_path(pane_text))
    if not plan_text or not plan_text.strip():
        return  # degrade: JSONL delivers post-resolution

    session_id = session_id_for_window(window_id)
    if not session_id:
        return
    norm_hash = md_capture.prose_norm_hash(plan_text)
    # Idempotency: once posted (this process OR a prior one — the marker is on
    # disk), never re-post; survives restart + the dedup consuming the marker.
    if md_capture.was_epm_plan_shown_live(session_id, norm_hash):
        return
    # Re-render guard: the card already exists → we are past first render.
    if _interactive_msgs.get((user_id, thread_id or 0)) is not None:
        return

    # Label the message so it reads as the plan, not stray assistant content.
    # The dedup ``norm_hash`` above is on the RAW ``plan_text`` (== the JSONL
    # ``input.plan``), NOT this display string, so the header can't break parity.
    display = f"📋 Plan\n\n{plan_text}"
    parts = build_response_parts(display, content_type="text", role="assistant")
    if not parts:
        return
    total = len(parts)
    sent_any = False
    for idx, chunk in enumerate(parts, start=1):
        try:
            sent, _outcome = await topic_send(
                bot,
                op="content",
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
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("EPM plan post raised (window=%s): %s", window_id, exc)
            return  # NO marker → JSONL still delivers; next render retries
        if sent is None:
            return  # send failed → NO marker (no silent loss, retry next render)
        sent_any = True
    if not sent_any:
        return
    # Record ONLY after all chunks landed — a partial/failed send leaves no
    # marker, so the JSONL copy is not suppressed and nothing is lost.
    md_capture.record_epm_plan_shown_live(
        session_id, norm_hash=norm_hash, shown_at=time.time()
    )
    logger.info(
        "EPM plan posted before card: window=%s session=%s len=%d hash=%s",
        window_id,
        session_id[:8],
        len(plan_text),
        norm_hash[:8],
    )


async def _maybe_post_live_prose(
    bot: Bot,
    *,
    user_id: int,
    thread_id: int | None,
    chat_id: int,
    window_id: str,
    ui_name: str,
) -> None:
    """Bug 2: deliver the assistant prose buffered behind a live AUQ /
    ExitPlanMode BEFORE the picker card.

    Claude Code co-flushes the whole turn (prose + the interactive ``tool_use``)
    to the session JSONL only at resolution, so without this the prose reaches
    Telegram only after the user already chose. The ``MessageDisplay`` hook
    captured it live (``md_capture``); here we read the fresh candidate, post it
    ahead of the card, and record a shown-live marker so the post-resolution
    JSONL copy is deduped (``session_monitor``).

    Idempotent: the shown-live marker (in the per-session capture file) makes a
    re-render / poll re-detect / post-``kickstart`` call skip re-posting.
    Fallback: no fresh capture (e.g. the hook isn't installed, or capture
    missed) → silent no-op; the JSONL copy delivers post-resolution exactly as
    before — no marker, no dedup, never a delayed picker. Called under the route
    lock so the prose is ordered strictly before the picker card.
    """
    session_id = session_id_for_window(window_id)
    if not session_id:
        # A6 observability: the next miss should be classifiable.
        logger.debug("Bug2 live-prose skip window=%s reason=no_session", window_id)
        return
    # If an interactive card already exists for this route we are PAST the first
    # render (a re-render / poll re-detect / UI change). Posting prose now would
    # land it BELOW the existing card and recreate the bug — so bail. This also
    # covers the late-finalize edge: prose that wasn't ready at first render is
    # NOT back-filled below the card; it falls back to post-resolution JSONL
    # delivery (panel PR-C+D P2). ``was_shown_live`` below is the second guard.
    if _interactive_msgs.get((user_id, thread_id or 0)) is not None:
        logger.debug("Bug2 live-prose skip window=%s reason=card_exists", window_id)
        return
    # PR-1: select the freshness TTL AND the emission anchor (+ its eps/lookback
    # tolerances) by modality. The emission anchor is a STABLE picker-emission
    # instant fed to ``select_fresh_prose``'s additive-OR leg so a prose the
    # render-time TTL aged out (the poller detected the picker tens of seconds
    # after the prose finalized — live: 20.7s) is still posted above the card:
    #   * AUQ → the PreToolUse side-file ``written_at`` (the tool_use invocation);
    #   * ExitPlanMode → the poller's first-detect stamp (EPM has no side file).
    # The poller stamp import is function-local: ``status_polling`` imports this
    # module at top, so the reverse edge must stay function-local.
    if ui_name == "ExitPlanMode":
        from .status_polling import peek_epm_surface_emitted_at

        ttl = md_capture.EPM_PROSE_TTL_S
        emitted_at = peek_epm_surface_emitted_at(user_id, thread_id, window_id)
        emit_eps = md_capture._EMIT_ANCHOR_EPS_EPM_S
        emit_lookback = md_capture._EMIT_ANCHOR_LOOKBACK_EPM_S
    else:
        ttl = md_capture.AUQ_PROSE_TTL_S
        emitted_at = auq_source.peek_side_file_written_at(session_id)
        emit_eps = md_capture._EMIT_ANCHOR_EPS_S
        emit_lookback = md_capture._EMIT_ANCHOR_LOOKBACK_S
    # Item 3 / P2-1: the turn-boundary anchor — the wall-clock instant the bot
    # delivered THIS route's current user turn into tmux (same clock as the prose
    # ``captured_at``). Resolved INSIDE this function (not threaded through
    # ``handle_interactive_ui``'s 22 callers) so the inbound:1061 + restart
    # first-render holes auto-close. ``None`` (e.g. after a restart that wiped the
    # in-memory stamp) DISABLES the turn-boundary filter — never a false-negative
    # on the live path; the emission-anchor OR leg above still applies if its
    # ``emitted_at`` is non-None (only ``emitted_at=None`` falls to TTL-only). The
    # filter (when present) drops a PRIOR turn's leftover prose (final_at <=
    # boundary) whose own turn produced no prose for this picker.
    from .message_queue import peek_route_user_turn_at

    nb = peek_route_user_turn_at(user_id, thread_id, window_id)
    deadline = time.monotonic() + _LIVE_PROSE_RETRY_BUDGET_S
    extended = False
    candidate: md_capture.ProseRecord | None = None
    while True:
        candidate = md_capture.select_fresh_prose(
            session_id,
            now=time.time(),
            ttl_seconds=ttl,
            not_before=nb,
            emitted_at=emitted_at,
            emit_anchor_eps_s=emit_eps,
            emit_anchor_lookback_s=emit_lookback,
        )
        if candidate is not None:
            break
        if time.monotonic() >= deadline:
            # Base catch-up budget exhausted with no finalized prose. The common
            # clean case finalizes prose BEFORE detection (the first read hits,
            # never reaching here). Extend the wait ONCE iff a prose message is
            # actively streaming — so a late-finalizing prose still posts BEFORE
            # the card; a prose-less picker bails here immediately (zero added
            # delay). Fail-safe: on the extended deadline we fall through to the
            # unchanged miss path (card created, JSONL delivers post-resolution).
            if extended or not md_capture.is_prose_streaming(
                session_id, now=time.time()
            ):
                break
            extended = True
            deadline = time.monotonic() + _LIVE_PROSE_STREAM_WAIT_BUDGET_S
            logger.debug(
                "Bug2 live-prose stream-wait extend window=%s ui=%s budget=%.1fs",
                window_id,
                ui_name,
                _LIVE_PROSE_STREAM_WAIT_BUDGET_S,
            )
        await asyncio.sleep(_LIVE_PROSE_RETRY_STEP_S)
    if candidate is None or not candidate.text.strip():
        # A6 observability: classify the miss (capture-absent vs TTL/anchor-reject
        # vs not_before-reject vs empty) so the next failure is diagnosable. Gated
        # on DEBUG so the classification re-read only runs when it would be logged.
        if logger.isEnabledFor(logging.DEBUG):
            if candidate is not None:
                reason = "empty_text"
                n = 1
            else:
                recs = md_capture.read_prose_records(session_id)
                n = len(recs)
                if not recs:
                    reason = "capture_absent"
                elif nb is not None and all(r.final_at <= nb for r in recs):
                    reason = "not_before_reject"
                else:
                    reason = "ttl_and_anchor_reject"
            logger.debug(
                "Bug2 live-prose miss window=%s reason=%s ui=%s n=%d ttl=%.1f "
                "emitted_at=%s not_before=%s",
                window_id,
                reason,
                ui_name,
                n,
                ttl,
                emitted_at,
                nb,
            )
        return
    # Already delivered live (re-render / poll re-detect / post-kickstart /
    # after the dedup consumed its marker) → don't double-post. Uses the
    # consumed-inclusive guard, NOT the unconsumed-marker set.
    if md_capture.was_shown_live(session_id, candidate.md_message_id):
        logger.debug(
            "Bug2 live-prose skip window=%s reason=already_shown_live md_msg=%s",
            window_id,
            candidate.md_message_id[:8],
        )
        return
    # ``topic_send`` does NOT split at Telegram's 4096-char limit (only the
    # normal content path does), so a long findings prose (>4096, common for
    # di-copilot) would fail with "Message is too long" and this whole post is
    # lost from the pre-card slot. Split into Telegram-safe chunks and send them
    # IN ORDER, all still BEFORE the picker card. The marker's ``norm_hash`` is
    # the FULL text's hash (UNCHANGED by splitting), so the dedup still suppresses
    # the JSONL copy correctly. A CONSERVATIVE cap (< 4096) absorbs the few chars
    # ``split_message`` can add when it auto-closes a fenced code block / wraps an
    # expandable quote at the boundary (it can otherwise return a chunk ~4097),
    # so no boundary chunk trips "Message is too long".
    from ..telegram_sender import split_message

    chunks = split_message(candidate.text, max_length=_LIVE_PROSE_CHUNK_MAX)
    for idx, chunk in enumerate(chunks, start=1):
        sent, _outcome = await topic_send(
            bot,
            op="content",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            window_id=window_id,
            text=chunk,
            plain=False,
            role="assistant",
            content_type="text",
            session_id=session_id,
            part_index=idx if len(chunks) > 1 else 0,
        )
        if sent is None:
            # A chunk failed — record NO marker so the JSONL copy still delivers
            # the FULL prose (split) post-resolution (no silent loss). Strictly
            # no worse than before: a >4096 prose was fully lost from the pre-card
            # slot anyway.
            return
    md_capture.record_shown_live(
        session_id,
        md_message_id=candidate.md_message_id,
        norm_hash=candidate.norm_hash,
        shown_at=time.time(),
    )
    logger.info(
        "Bug2 live-prose posted before picker: window=%s session=%s md_msg=%s len=%d",
        window_id,
        session_id[:8],
        candidate.md_message_id[:8],
        len(candidate.text),
    )


async def handle_interactive_ui(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
    *,
    tool_input: dict | None = None,
    from_poller: bool = False,
    tmux_mgr=None,
    session_mgr=None,
) -> bool:
    """Capture terminal and send interactive UI content to user.

    Handles AskUserQuestion, ExitPlanMode, RestoreCheckpoint, and — behind the
    ``CC_TELEGRAM_PERMISSION_PROMPTS`` flag — the Permission / Workflow approval
    gates (display-only in PR-1: a labels card + the manual nav keyboard, no
    option-pick buttons). Returns True if UI was detected and sent, False
    otherwise.

    ``tool_input`` is the raw JSONL ``tool_use.input`` dict when explicitly
    available from JSONL dispatch/replay. For a live pending AskUserQuestion,
    JSONL is not authoritative because Claude Code buffers the ``tool_use``
    line until the user answers; the tmux pane is the active source of truth.
    The pane is captured for structured AUQ parsing, verbatim text excerpt,
    and the keystroke fallback path.
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
    # Hoisted so the context-message gate site below can read them under the
    # route lock without an unbound-variable risk when content.name isn't
    # AskUserQuestion (the gate site short-circuits on name, but pyright
    # narrows on declarations, not control flow across blocks).
    form: AskUserQuestionForm | None = None
    render_source: auq_source.RenderAuqSource | None = None
    if content.name == "AskUserQuestion":
        # Unified resolver feeds both render and validate paths the same
        # form. Combines JSONL tool_input (full option list with
        # descriptions, plus the multi-question matrix) with pane state
        # (cursor / free-text / review-screen flags + current-tab
        # inference). For multi-question forms the resolver tracks
        # ``current_tab_inferred``; the FA5+ guard in
        # ``_build_pick_button_rows`` suppresses pick buttons when False so
        # we don't dispatch a digit against the wrong tab.
        # PR-3 PR-B: the RENDER-path resolver decides which source to render
        # from AND whether a tap can be TRUSTED to dispatch. side_file_ok
        # (side file consistent with the pane AND within the read-TTL) → trusted
        # side-file render; bail → the pane is a genuinely different live picker
        # → render the pane (trusted); rescue → busy / unparseable pane → render
        # the side file's full content DISPLAY-ONLY (no pick tokens). The
        # read-TTL-FREE read lets a busy >TTL pane still rescue, while
        # side_file_ok mirrors the TTL'd resolver ``validate_and_consume``
        # re-resolves → mint/validate parity, no dead-tap.
        render_source = auq_source.resolve_auq_source_for_render(
            window_id, pane_text, explicit=tool_input
        )
        form = render_source.form
        if form is None:
            # Belt-and-braces: the resolver returns a form for every structured
            # decision; this only fires on a pane carrying no AUQ at all.
            form = parse_ask_user_question(pane_text)
        # The trusted-mint source tags fed to ``_build_pick_button_rows`` /
        # ``pick_token.mint_row``; ``validate_and_consume`` re-resolves the same
        # (kind, fingerprint) via the strict resolver. Only consulted when
        # ``render_source.dispatch_trusted`` is True (gated below).
        resolved_source = auq_source.ResolvedAuqSource(
            kind=render_source.kind,
            payload=render_source.payload,
            source_fingerprint=render_source.source_fingerprint,
        )
        logger.info(
            "AUQ render resolve: window=%s from_poller=%s decision=%s reason=%s "
            "trusted=%s kind=%s",
            window_id,
            from_poller,
            render_source.decision,
            render_source.reason,
            render_source.dispatch_trusted,
            render_source.kind,
        )

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
        if form is not None:
            if form.select_mode == "multi":
                logger.debug(
                    "AUQ_RENDER window=%s from_poller=%s source_kind=%s source_fp=%s "
                    "sel_mode=%s opts_complete=%s current_tab_inferred=%s fp=%s cursor=%s selected=%s",
                    window_id,
                    from_poller,
                    resolved_source.kind,
                    resolved_source.source_fingerprint[:8],
                    form.select_mode,
                    form.options_complete,
                    form.current_tab_inferred,
                    form.fingerprint()[:8],
                    [o.number for o in form.options if o.cursor],
                    {o.number: o.selected for o in form.options},
                )
            # Selection-card completeness (DISPLAY-ONLY): on a single-question,
            # single-select PARTIAL-pane bail the resolver ``form`` is the pane
            # parse, which lost its top options to scroll ("Only options 2-3 are
            # visible"; option 1 gone). Swap a COMPLETE side-file form into the
            # BODY render so the selection card lists ALL options. This changes
            # ONLY the rendered body + the notice text. DISPATCH is byte-identical:
            # the ``if not render_source.dispatch_trusted`` / ``_build_pick_button_rows``
            # paths below still use the resolver ``form``, and on a partial bail
            # ``dispatch_trusted`` is False so NO pick buttons are minted anyway. The
            # dedup hash is untouched (``peek_render_identity`` / ``_ui_render_hash``
            # re-resolve the resolver pane form independently → no re-render churn).
            # Gated HARD (pessimistic-review-mandated):
            #   * single-QUESTION only — ``build_form_from_tool_input`` defaults to
            #     ``questions[0]`` (terminal_parser.py), so a Q2 bail would else
            #     render Q1's options;
            #   * single-SELECT only — multi-select side-file options carry
            #     ``selected=None`` → ``·``-everything, destroying the pane's real
            #     ☑/☐ checkbox state;
            #   * None-guard — ``_render_ask_user_question(None)`` accesses
            #     ``.is_review_screen`` → AttributeError.
            display_form = form
            if not render_source.dispatch_trusted and p14_suppress_picks:
                recovered = auq_source.recover_consistent_side_file_for_ctx(
                    window_id, pane_text
                )
                if (
                    recovered is not None
                    and len(recovered.payload.get("questions", [])) == 1
                ):
                    candidate = build_form_from_tool_input(recovered.payload)
                    if candidate is not None and candidate.select_mode == "single":
                        # Hermes P2: the recovered form is built purely from the
                        # side file, so every option is ``cursor=False`` — the
                        # swapped body would lose the live ``❯`` the pane body
                        # showed, making the manual ↑/↓/Tab nav this very notice
                        # points at more blind. Overlay the pane's cursor by
                        # option NUMBER (``build_form_from_tool_input`` numbers
                        # options 1..N in payload order, aligned with the pane
                        # slot numbers) so the highlighted option keeps its
                        # ``❯``. Fail-safe: if the pane's own cursor option
                        # scrolled off (no ``o.cursor``), no overlay → all
                        # ``False``, identical to no overlay. DISPLAY-only — the
                        # overlaid form still only feeds _render_ask_user_question,
                        # never dispatch or the dedup hash.
                        pane_cursor_number = next(
                            (o.number for o in form.options if o.cursor), None
                        )
                        if pane_cursor_number is not None:
                            candidate = replace(
                                candidate,
                                options=tuple(
                                    replace(o, cursor=(o.number == pane_cursor_number))
                                    for o in candidate.options
                                ),
                            )
                        display_form = candidate
                        partial_options_notice = (
                            "Tap-to-select is off on a scrolled screen — "
                            "use ↑/↓/Tab below or send your answer."
                        )
            structured = _render_ask_user_question(display_form)
            if structured:
                text = structured
            if partial_options_notice:
                text = f"{text}\n\n{partial_options_notice}"
            if not render_source.dispatch_trusted:
                # PR-3 PR-B DISPLAY-ONLY (rescue OR a partial-pane bail): mint NO
                # pick tokens (the busy/unparseable/partial pane means a tapped
                # digit can't be verified against the live picker — mint/validate
                # parity would break → dead-tap). CRITICAL (hermes round-2): prune
                # any prior tokens for this route UNCONDITIONALLY here — BEFORE
                # the p14 branch — because an untrusted bail is ALSO
                # p14_suppress_picks (its pane starts at option >1), and leaving a
                # stale trusted side_file/pane token row would make
                # status_polling._remint_on_source_drift see minted!=live every
                # tick → the exact re-render loop this PR kills. (The trusted path
                # self-prunes prior rows via mint_row's stale-row hygiene; only
                # this no-mint path needs the explicit prune.)
                pick_token.prune_for_route(user_id, thread_id, window_id)
                if not p14_suppress_picks:
                    # p14 already appended its own "only options X-Y visible"
                    # notice; add the busy-screen notice only when it didn't.
                    text = (
                        f"{text}\n\n⚠️ The live screen is busy, so the option "
                        "buttons are disabled — use ↑/↓/Tab below or send your "
                        "answer as text."
                    )
            elif not p14_suppress_picks:
                built = _build_pick_button_rows(
                    user_id, thread_id, window_id, form, resolved_source
                )
                if built:
                    pick_rows = built
    elif content.name in _GATE_RENDER_NAMES:
        # PR-1 interactive approval gate (Permission / Workflow), DISPLAY-ONLY.
        # No pick buttons (pick_rows stays None) — the user answers via the
        # window-keyed manual ↑/↓/⏎/Esc nav keyboard below. The card body is
        # the extracted pane region + the honest un-verified-keystroke notice
        # (P2-1). PR-2 will add a verified one-tap "Yes" through a gate-aware
        # validator; until then there is NO semantic option-button dispatch.
        text = _gate_card_text(content)

    # Build message with navigation keyboard (structured rows on top when
    # available, keystroke nav row below for free-text / manual paths).
    keyboard = _build_interactive_keyboard(
        window_id, ui_name=content.name, pick_rows=pick_rows
    )

    chat_id = session_mgr.resolve_chat_id(user_id, thread_id)
    lock = _get_route_lock(user_id, thread_id)

    # AUQ context message — posted ONCE per (window_id, tool_use_id) per
    # ``_should_post_auq_context``. The picker card renders option labels
    # only, so this message is where the full per-option descriptions live.
    # Held under the same per-route lock
    # as the picker send/edit below so concurrent bot.py + status_polling
    # callers serialize on this route: the first claims the context-post
    # slot via ``claim_auq_context_post_in_memory`` (Wave 1 two-phase
    # gate), posts context, then sends the picker; the second skips
    # context (already posted) and edits the existing picker. Without
    # the lock, the picker could land before the context message in
    # the chat order (race flagged by hermes P1 on the 2026-05-22
    # design review). The lock is per-route so this does not stall
    # other routes.
    async with lock:
        if content.name == "AskUserQuestion":
            # Source-of-truth selection — v4 plan, tri-level:
            #   1. JSONL via _resolve_ask_tool_input + _last_auq_tool_use_id
            #      → ctx_source = dict; source_tag = "dict_via_jsonl"
            #   2. PreToolUse-hook side file via auq_source.resolve_record
            #      → ctx_source = dict; source_tag = "dict_via_hook";
            #        dedup_key = "pretool:<tool_use_id>" (or
            #        "pretool:<input_fingerprint>" when the hook payload
            #        carried no tool_use_id).
            #   3. Pane-derived form fallback (today's default for live
            #      AUQs whose tool_use line has not yet flushed to JSONL)
            #      → ctx_source = form; source_tag = "form".
            #
            # Codex/Hermes R2 P1 fix: the prior gate overwrote ctx_source
            # with `form` whenever _last_auq_tool_use_id was unset, so the
            # PreToolUse-hook dict would never have rendered. The new path
            # routes through dict_via_hook before falling back to form.
            ctx_input = _resolve_ask_tool_input(window_id, tool_input)
            cached_tool_use_id = _last_auq_tool_use_id.get(window_id)
            # render_source is bound whenever content.name == "AskUserQuestion"
            # (set in the pre-lock block above gated on the same condition);
            # pyright can't correlate the two blocks, so assert the invariant.
            assert render_source is not None

            # PR-3 PR-B: the ctx (📋 full-descriptions) source is driven off the
            # render DECISION, not a second resolve_record call. side_file_ok /
            # rescue → post the side file's full per-option descriptions (rescue
            # is the V1/V2 fix — the card was previously DROPPED because the
            # pane-consistency check rejected on the busy pane). bail → the pane
            # is a genuinely different live picker, so NEVER post the stale
            # side-file descriptions.
            ctx_source: dict | AskUserQuestionForm | None
            source_tag: str
            if ctx_input is not None and cached_tool_use_id:
                ctx_source = ctx_input
                dedup_key = cached_tool_use_id
                source_tag = "dict_via_jsonl"
            elif render_source.decision in ("side_file_ok", "rescue") and isinstance(
                render_source.payload, dict
            ):
                ctx_source = render_source.payload
                dedup_key = f"pretool:{render_source.source_fingerprint[:16]}"
                source_tag = f"dict_via_render_{render_source.decision}"
            elif (
                render_source.decision == "bail" and not render_source.dispatch_trusted
            ):
                # round-2 P1b: a PARTIAL-pane bail whose side file is consistent AND clears
                # the evidence floor -> post the full-options ctx card from the side-file
                # dict. ACCEPTED RESIDUAL (round-2 convergent P1, §3.3 / §11(c)): the
                # pretool:<fp> marker this mints is NOT carried across a tmux @old->@new
                # renumber (the :949-961 remap keys on the picker's tool_use_id, None for an
                # aged card; broadening the prune-loop remap was REJECTED because it breaks
                # test_hydrate_mismatch_marker_not_remapped and strands the marker after
                # /clear). Bounded to <=1 duplicate per uninterrupted hydrate/render cycle on
                # the rare restart-during-live-renumbered-partial-bail coincidence;
                # PRE-EXISTING for the side_file_ok/rescue branch above (interactive_ui.py
                # :3288).
                recovered = auq_source.recover_consistent_side_file_for_ctx(
                    window_id, pane_text
                )
                if recovered is not None:
                    ctx_source = recovered.payload
                    dedup_key = f"pretool:{recovered.source_fingerprint[:16]}"  # STABLE side-file fp
                    source_tag = "dict_via_render_bail_recover"
                else:
                    ctx_source = None
                    dedup_key = ""
                    source_tag = "bail_no_ctx"
            elif (
                render_source.decision == "bail"
            ):  # complete-picker bail (trusted) — unchanged
                ctx_source = None
                dedup_key = ""
                source_tag = "bail_no_ctx"
            elif form is not None:
                ctx_source = form
                dedup_key = f"form:{form.fingerprint()}"
                source_tag = "form"
            else:
                ctx_source = None
                dedup_key = ""
                source_tag = "none"
            logger.info(
                "AUQ context gate eval: window=%s from_poller=%s "
                "explicit_input=%s cached_input=%s tool_use_id=%s "
                "decision=%s ctx_source=%s dedup_key=%s should_post=%s "
                "already_posted=%s",
                window_id,
                from_poller,
                tool_input is not None,
                _last_completed_ask_tool_input.get(window_id) is not None,
                cached_tool_use_id,
                render_source.decision,
                source_tag,
                dedup_key or "<empty>",
                _should_post_auq_context(ctx_source),
                _auq_context_posted.get(window_id) is not None,
            )
            if ctx_source is not None and _should_post_auq_context(ctx_source):
                # Wave 1 (plan §5.1): two-phase context-post gate. Claim
                # is in-memory only until at least one chunk lands; the
                # send function pairs the token with exactly one
                # commit/rollback before returning so the persisted
                # dedup marker (``_auq_context_posted[window_id]``)
                # never sits on disk without a matching chunked record
                # on Telegram. NONE_SENT no longer requires a caller-
                # side pop — rollback inside the send function cleaned
                # the pending slot already.
                claim_token = claim_auq_context_post_in_memory(window_id, dedup_key)
                if claim_token is not None:
                    await _send_auq_context_message(
                        bot,
                        user_id=user_id,
                        thread_id=thread_id,
                        chat_id=chat_id,
                        window_id=window_id,
                        source=ctx_source,
                        claim_token=claim_token,
                    )

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

        # Bug 2: post any prose buffered behind this live picker BEFORE the
        # card, under the same route lock so the ordering holds. Placed AFTER
        # the staleness gate above (which drops a stale-session
        # ``_interactive_msgs`` entry) so the card-existence guard inside
        # ``_maybe_post_live_prose`` reflects only a VALID current-session card —
        # a stale entry about to be replaced must NOT suppress live prose (codex
        # PR-C+D re-review). Idempotent + best-effort; a no-op when there's no
        # fresh live capture. Covers both AskUserQuestion and ExitPlanMode.
        #
        # §6: SKIP live-prose for the Permission / Workflow gates. The dedup
        # (``session_monitor.filter_live_prose_duplicates``) is AUQ/EPM-only, so
        # a gate live-prose post would DOUBLE with the post-resolution JSONL
        # copy. Gate prose flows through the normal JSONL path (undeduped but
        # single).
        if content.name not in _GATE_RENDER_NAMES:
            await _maybe_post_live_prose(
                bot,
                user_id=user_id,
                thread_id=thread_id,
                chat_id=chat_id,
                window_id=window_id,
                ui_name=content.name,
            )

        # ExitPlanMode: post the plan BODY before the card so the user sees what
        # they're approving (the card itself carries no plan text). Ordered
        # AFTER the findings prose above, BEFORE the card below — same route
        # lock. Idempotent + dedup'd against the post-resolution JSONL copy.
        if content.name == "ExitPlanMode":
            await _maybe_post_epm_plan(
                bot,
                user_id=user_id,
                thread_id=thread_id,
                chat_id=chat_id,
                window_id=window_id,
                pane_text=pane_text,
                tool_input=tool_input,
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
            # Fix B (di-copilot picker churn): a TRANSIENT edit failure must
            # NOT orphan + recreate a still-live card. Under a long-open AUQ the
            # ~1Hz poller re-edit periodically times out (TimedOut → OTHER) or
            # hits a RetryAfter (RATE_LIMITED); the old "any non-OK edit → fresh
            # send" path turned each into a delete-old + send-new card (a new
            # message + notification per timeout — the duplicate-card churn).
            # The card is almost certainly still live, so keep it and let the
            # next poll re-edit it in place. Only MESSAGE_NOT_FOUND (provably
            # gone) and the topic-broken outcomes (TOPIC_NOT_FOUND / TOPIC_CLOSED
            # / FORBIDDEN — which must reach the send-failed DM escalation below)
            # fall through to a fresh send. Mirrors dashboard.py:314 (hermes
            # Wave C review P2-2: re-sending on a transient orphans the
            # still-live message).
            if edit_outcome in (
                TopicSendOutcome.OTHER,
                TopicSendOutcome.RATE_LIMITED,
            ):
                _interactive_mode[ikey] = window_id
                logger.debug(
                    "interactive edit transient outcome=%s window=%s — keeping "
                    "card, re-edit next tick (no recreate)",
                    edit_outcome.value,
                    window_id,
                )
                return True
            # Edit failed (MESSAGE_NOT_FOUND / topic-broken) — fall through to a
            # fresh send while keeping the old id so we can delete it after a new
            # one lands.

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


_TOMBSTONE_TEXT = (
    "🪦 AskUserQuestion resolved without a Telegram pick.\n"
    "Claude continued — this picker is no longer active."
)


async def clear_interactive_msg(
    user_id: int,
    bot: Bot | None = None,
    thread_id: int | None = None,
    *,
    session_mgr=None,
    tombstone: bool = False,
) -> None:
    """Clear the tracked interactive single-card surface for this route.

    State mutations under the route lock (snapshot + drop), Telegram
    deletes outside the lock.

    ``tombstone``: when ``True`` and a single-card msg_id is tracked,
    edit that message into a non-actionable tombstone (text replaced,
    reply_markup cleared) instead of deleting it. Used by
    ``status_polling`` when the pane-absent hysteresis fires: the user
    never picked an option (no Telegram callback consumed) but Claude
    Code moved past the AUQ on its own (e.g. bypassPermissions
    auto-resolution). Without the tombstone the user would wake up to
    a chat with no record of the question.
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
        # key in the pick-token store.
        cleared_window_id = _interactive_mode.pop(ikey, None)

        # P2.2: prune pick-tokens for this route. Without this, a deleted
        # interactive card leaves its tokens live until the 5-minute TTL,
        # which combined with stale-scrollback liveness checks (P1.3)
        # would let a stale callback validate against a closed picker.
        # Scope by (user_id, thread_id, window_id) so concurrent
        # interactive surfaces on other routes are untouched. The store +
        # cache rows live in ``pick_token`` now; ``prune_for_route`` drops
        # the cache rows AND their sibling tokens for this route's window.
        if cleared_window_id is not None:
            pick_token.prune_for_route(user_id, thread_id, cleared_window_id)

    logger.debug(
        "Clear interactive: user=%d thread=%s single=%s",
        user_id,
        thread_id,
        single_msg_id,
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
        if tombstone:
            # Edit-in-place to a non-actionable tombstone. ``plain=True``
            # so the emoji + body don't run through MarkdownV2.
            # ``reply_markup=None`` clears the picker keyboard. If the
            # edit fails (message deleted, etc.), do NOT fall back to
            # delete — the user already lost the card once; a missing
            # tombstone is fine, a phantom delete event is not.
            await topic_edit(
                bot,
                op="interactive",
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                window_id=cleared_window_id,
                message_id=single_msg_id,
                text=_TOMBSTONE_TEXT,
                plain=True,
                reply_markup=None,
            )
        else:
            await topic_delete(
                bot,
                op="interactive",
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                window_id=cleared_window_id,
                message_id=single_msg_id,
            )

    # Fix 3c: this fires at the end of EVERY interactive-card clear (tombstone
    # or delete, any reason) — kind-aware so it tears down only the
    # interactive_ui card and never doubles as a notification_decision ack.
    await attention.dismiss_if_kind(
        bot, user_id=user_id, thread_id=thread_id, kind="interactive_ui"
    )


def reset_for_tests() -> None:
    """Test-only: drop all per-test module-level state for interactive UI.

    Co-located with the state it resets (the R3 reset-seam contract): every
    map is resolved by direct module reference, never ``getattr(name)`` string
    indirection. Clears the picker/mode/meta/context maps and ``_route_locks``
    (created lazily by ``_get_route_lock`` and otherwise never cleaned up, so
    clearing between tests is a pure improvement — they are recreated on
    demand). The PreToolUse record cache moved to ``auq_source`` (R5); reset it
    via ``auq_source.reset_for_tests``. The pick-token store moved to
    ``pick_token`` (R4); reset it via ``pick_token.reset_for_tests``.

    ``_clear_callbacks`` is a process-lifetime registry (``status_polling``
    registers ``_on_interactive_clear`` at import); it is intentionally NOT
    reset — clearing it would silently disable clear-callback propagation for
    the rest of the suite since the registration only runs once at import.
    """
    _interactive_msgs.clear()
    _interactive_mode.clear()
    _interactive_msg_meta.clear()
    _last_completed_ask_tool_input.clear()
    _last_auq_tool_use_id.clear()
    _auq_context_posted.clear()
    _auq_context_post_pending.clear()
    _auq_context_msgs.clear()
    _route_locks.clear()
