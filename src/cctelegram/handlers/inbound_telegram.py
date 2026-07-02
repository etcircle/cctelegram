"""Telegram inbound message handlers (text, photo, voice, document, media-group).

Extracts the inbound-side of bot.py: every code path that runs when a user
sends content into a Telegram topic. Each handler resolves the topic → tmux
window → Claude session route, applies §2.5 reply-context rendering, and
hands off to the aggregator/queue layer.

Core responsibilities:
  - text_handler / photo_handler / voice_handler / document_handler: the
    four inbound MessageHandler entrypoints registered in
    ``bot.create_bot()``. Photo and document handlers also drive the
    media-group bundling path via ``aggregator_offer_{photo,document}``.
  - Pending-route-payload state machine (``_clear_pending_route_payload``,
    ``_flush_pending_route_payload``, ``_remember_ignored_stale_thread_id``,
    ``_pending_owner_matches``): stashes text + attachments while an
    unbound topic is in the directory/session/window picker, and flushes
    them onto the freshly-bound route once the picker commits.
  - Window-creation helpers (``_create_and_bind_window``,
    ``_abort_created_window_after_pending_owner_change``,
    ``_cleanup_unbound_created_window``): shared by text_handler's
    auto-create path and by the callback dispatch in bot.py for
    CB_DIR_CONFIRM / CB_SESSION_NEW / CB_SESSION_SELECT.
  - ``_apply_reply_context``: §2.5 quote-rendering used by every
    inbound handler so a reply via voice/photo/document carries the same
    quote block as a text reply.
  - ``_capture_bash_output`` / ``_cancel_bash_capture``: text_handler's
    background tmux-pane capture for ``!`` bash commands.

Key callers in bot.py: command + callback handlers re-import these names
from ``handlers.inbound_telegram`` so the original ``bot.<name>``
attribute access (used in tests and a couple of module-level lookups)
keeps resolving to the same function objects.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telegram import (
    Bot,
    CallbackQuery,
    Message,
    Update,
    User,
)
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from .. import route_runtime
from . import pane_signals
from ..config import config
from ..markdown_v2 import convert_markdown
from ..session import session_manager
from ..terminal_parser import extract_bash_output, is_interactive_ui
from ..tmux_manager import tmux_manager
from ..transcribe import transcribe_voice
from ..utils import app_dir
from . import attention
from . import reply_context as reply_context_mod
from .directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    BROWSE_UNBOUND_COUNT_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_SELECTING_SESSION,
    STATE_SELECTING_WINDOW,
    build_directory_browser,
    clear_browse_state,
    clear_session_picker_state,
    clear_window_picker_state,
)
from .inbound_aggregator import (
    AggregatorReplayAttachment,
    aggregator_clear_route,
    aggregator_offer_document,
    aggregator_offer_photo,
    aggregator_offer_text,
    aggregator_offer_voice,
    aggregator_replay_payload,
)
from .interactive_ui import (
    get_interactive_window,
    handle_interactive_ui,
)
from .message_queue import (
    clear_status_msg_info,
    enqueue_status_update,
    set_route_last_user_message,
)
from .message_sender import (
    NO_LINK_PREVIEW,
    safe_answer,
    safe_edit,
    safe_reply,
    send_with_fallback,
)
from .reply_context import extract_reply_context, render_for_claude

logger = logging.getLogger(__name__)


def is_user_allowed(user_id: int | None) -> bool:
    return user_id is not None and config.is_user_allowed(user_id)


@dataclass(frozen=True)
class PendingAttachment:
    path: str
    caption: str
    media_group_id: str | None


_IGNORED_STALE_THREAD_IDS_KEY = "_ignored_stale_thread_ids"


def _pending_thread_id(user_data: dict | None) -> int | None:
    """Return the active pending route thread id, if present."""
    if user_data is None:
        return None
    value = user_data.get("_pending_thread_id")
    return value if isinstance(value, int) else None


def _pending_owner_matches(user_data: dict | None, thread_id: int | None) -> bool:
    """Return True when ``thread_id`` still owns the active pending payload."""
    if thread_id is None:
        return False
    return _pending_thread_id(user_data) == thread_id


def _remember_ignored_stale_thread_id(
    user_data: dict | None, thread_id: int | None
) -> None:
    """Remember a replaced pending thread whose old picker callbacks are stale."""
    if user_data is None or thread_id is None:
        return
    ignored = set(user_data.get(_IGNORED_STALE_THREAD_IDS_KEY, []) or [])
    ignored.add(thread_id)
    user_data[_IGNORED_STALE_THREAD_IDS_KEY] = sorted(ignored)


def _forget_ignored_stale_thread_id(
    user_data: dict | None, thread_id: int | None
) -> None:
    """Stop treating ``thread_id`` as a replaced stale pending thread."""
    if user_data is None or thread_id is None:
        return
    ignored = set(user_data.get(_IGNORED_STALE_THREAD_IDS_KEY, []) or [])
    ignored.discard(thread_id)
    if ignored:
        user_data[_IGNORED_STALE_THREAD_IDS_KEY] = sorted(ignored)
    else:
        user_data.pop(_IGNORED_STALE_THREAD_IDS_KEY, None)


def _is_ignored_stale_thread_id(user_data: dict | None, thread_id: int | None) -> bool:
    """Return True for old replaced-topic callbacks that must not clear current state."""
    if user_data is None or thread_id is None:
        return False
    return thread_id in set(user_data.get(_IGNORED_STALE_THREAD_IDS_KEY, []) or [])


def _clear_pending_route_payload(
    user_data: dict | None,
    *,
    delete_files: bool,
    clear_ignored_stale_threads: bool = True,
) -> list[PendingAttachment]:
    """Clear pending unbound-topic payload state and optionally delete files.

    Pending text/photo/document data lives outside the aggregator while the
    user is choosing a directory/window/session. Cancel, stale-topic mismatch,
    and bind failure must clear the whole bundle, not just text, otherwise a
    later bind can forward media the user already cancelled.
    """
    if user_data is None:
        return []
    attachments: list[PendingAttachment] = list(
        user_data.pop("_pending_thread_attachments", []) or []
    )
    user_data.pop("_pending_thread_id", None)
    user_data.pop("_pending_thread_text", None)
    user_data.pop("_selected_path", None)
    if clear_ignored_stale_threads:
        user_data.pop(_IGNORED_STALE_THREAD_IDS_KEY, None)
    if delete_files:
        for attachment in attachments:
            try:
                Path(attachment.path).unlink(missing_ok=True)
            except OSError as e:
                logger.debug(
                    "failed to delete pending attachment %s: %s", attachment.path, e
                )
    return attachments


def _clear_pending_route_payload_for_thread(
    user_data: dict | None,
    thread_id: int,
    *,
    delete_files: bool,
    clear_ignored_stale_threads: bool = True,
) -> list[PendingAttachment]:
    """Clear pending payload only when ``thread_id`` owns it.

    Topic-close cleanup can race with a newer unbound-topic payload in another
    thread. Keep all file deletion behind the same payload cleanup helper used
    by cancel/replacement, but gate it by pending owner so closing an old topic
    cannot delete the active newer payload.
    """
    if _pending_thread_id(user_data) != thread_id:
        return []
    _clear_picker_state_for_current_state(user_data)
    return _clear_pending_route_payload(
        user_data,
        delete_files=delete_files,
        clear_ignored_stale_threads=clear_ignored_stale_threads,
    )


def _clear_picker_state_for_current_state(user_data: dict | None) -> None:
    """Clear the active picker/browser state based on ``STATE_KEY``."""
    if user_data is None:
        return
    current_state = user_data.get(STATE_KEY)
    if current_state == STATE_BROWSING_DIRECTORY:
        clear_browse_state(user_data)
    elif current_state == STATE_SELECTING_WINDOW:
        clear_window_picker_state(user_data)
    elif current_state == STATE_SELECTING_SESSION:
        clear_session_picker_state(user_data)


def _delete_pending_attachment_files(attachments: list[PendingAttachment]) -> None:
    """Delete downloaded files that belonged to a failed pending-route payload."""
    for attachment in attachments:
        try:
            Path(attachment.path).unlink(missing_ok=True)
        except OSError as e:
            logger.debug(
                "failed to delete pending attachment %s: %s", attachment.path, e
            )


async def _flush_pending_route_payload(
    route: tuple[int, int, str],
    user_data: dict | None,
) -> bool | None:
    """Synchronously replay the pending first-turn payload for a new binding.

    Returns ``True`` when a pending payload was delivered, ``False`` when it
    failed, and ``None`` when there was no pending payload. Pending picker state
    is cleared before sending to make callback double-clicks idempotent; on
    failure, route buffers are cleared and downloaded pending files are deleted
    so the user gets an explicit resend prompt instead of a hidden retry that
    could duplicate a manual resend.
    """
    if user_data is not None and not _pending_owner_matches(user_data, route[1]):
        logger.warning(
            "Refusing to flush pending payload for route %s because pending owner is %s",
            route,
            _pending_thread_id(user_data),
        )
        return None

    pending_text = user_data.get("_pending_thread_text") if user_data else None
    pending_attachments: list[PendingAttachment] = (
        list(user_data.get("_pending_thread_attachments") or []) if user_data else []
    )
    if user_data is not None:
        _clear_pending_route_payload(user_data, delete_files=False)

    if not pending_text and not pending_attachments:
        return None

    replay_attachments = [
        AggregatorReplayAttachment(
            path=Path(attachment.path),
            caption=attachment.caption,
            media_group_id=attachment.media_group_id,
        )
        for attachment in pending_attachments
    ]

    try:
        delivered = await aggregator_replay_payload(
            route,
            text=pending_text if isinstance(pending_text, str) else None,
            attachments=replay_attachments,
        )
    except Exception as e:
        logger.error("pending route payload replay raised for route %s: %s", route, e)
        delivered = False

    if not delivered:
        aggregator_clear_route(route)
        _delete_pending_attachment_files(pending_attachments)
    return delivered


def _get_thread_id(update: Update) -> int | None:
    """Extract thread_id from an update, returning None if not in a named topic."""
    msg = update.message or (
        update.callback_query.message if update.callback_query else None
    )
    if msg is None:
        return None
    tid = getattr(msg, "message_thread_id", None)
    if tid is None or tid == 1:
        return None
    return tid


async def _list_unbound_windows(
    tmux_mgr: Any,
    session_mgr: Any,
) -> list[tuple[str, str, str]]:
    """Return tmux windows not currently bound to any topic, as (id, name, cwd)."""
    all_windows = await tmux_mgr.list_windows()
    bound_ids = {bid for _, _, bid in session_mgr.iter_thread_bindings()}
    return [
        (w.window_id, w.window_name, w.cwd)
        for w in all_windows
        if w.window_id not in bound_ids
    ]


def _ensure_private_media_dir(path: Path) -> Path:
    """Create-and-repair an attachment dir at mode 0700 and return it.

    User uploads can carry sensitive content, so these dirs follow the same
    0700/0600 posture as every other sensitive store (auq_pending/,
    msg_display/). The chmod ALWAYS runs — ``mkdir(mode=...)`` is a no-op on
    an existing dir, so an upgraded install's loose 0755 dir must be
    repaired. OSError → log WARNING + continue (never silent, never fatal).
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError as e:
        logger.warning("could not ensure %s at mode 0700: %s", path, e)
    return path


def _restrict_download_perms(path: Path) -> None:
    """Chmod a downloaded attachment to 0600 (owner-only).

    Downloads land with umask defaults (0644); tighten after write. OSError →
    log WARNING + continue — never fail the download over a perms repair.
    """
    try:
        os.chmod(path, 0o600)
    except OSError as e:
        logger.warning("could not chmod %s to mode 0600: %s", path, e)


# --- Image directory for incoming photos ---
_IMAGES_DIR = _ensure_private_media_dir(app_dir() / "images")

# --- File directory for incoming documents ---
_FILES_DIR = _ensure_private_media_dir(app_dir() / "files")


async def _apply_reply_context(
    message: Message,
    user_id: int,
    thread_id: int | None,
    text: str,
) -> str:
    """Render the §2.5 quote block onto ``text`` for a reply-aware message.

    Returns ``text`` unchanged when the kill switch is off, when there is
    no quoted referent, or when the quote points at a stale (e.g. /clear-ed)
    session — the same stale-quote guard the text_handler had inline. Used
    by text/voice/photo/document handlers so a reply made via voice or
    photo+caption carries the same quote-injection block as a text reply.
    """
    if not config.reply_context_enabled:
        return text
    reply_ctx = extract_reply_context(message)
    if reply_ctx is None:
        return text
    reply_ctx = await reply_context_mod.resolve(reply_ctx, message.chat.id)
    current_sid = None
    bound_wid = session_manager.resolve_window_for_thread(user_id, thread_id)
    if bound_wid is not None:
        current_session = await session_manager.resolve_session_for_window(bound_wid)
        if current_session is not None:
            current_sid = current_session.session_id
    stale_quote = (
        reply_ctx.session_id is not None
        and current_sid is not None
        and reply_ctx.session_id != current_sid
    )
    if stale_quote:
        # P1.5: render the quote with a cross-session marker rather than
        # dropping silently. The §2.5.4 routing rule still applies — the
        # topic's current window binding remains the routing authority;
        # the marker only tells Claude the quoted body is from a prior
        # session so it doesn't treat it as part of this conversation's
        # transcript. Kill switch ``CC_TELEGRAM_REPLY_CROSS_SESSION=false``
        # restores the pre-P1.5 silent-drop behaviour.
        if not config.reply_context_cross_session_enabled:
            logger.info(
                "Dropping reply context (cross-session kill switch on): "
                "quoted session %s != current %s (window=%s, thread=%s)",
                reply_ctx.session_id,
                current_sid,
                bound_wid,
                thread_id,
            )
            return text
        logger.info(
            "Rendering cross-session reply context: quoted session %s != "
            "current %s (window=%s, thread=%s)",
            reply_ctx.session_id,
            current_sid,
            bound_wid,
            thread_id,
        )
        return render_for_claude(text, reply_ctx, cross_session=True)
    return render_for_claude(text, reply_ctx)


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photos sent by the user: download and forward path to Claude Code."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.photo:
        return

    chat = update.message.chat
    thread_id = _get_thread_id(update)
    if chat.type in ("group", "supergroup") and thread_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    # Must be in a named topic
    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)

    # Download the highest-resolution photo (we need a path either way:
    # bound topic feeds the aggregator, unbound topic stashes the path so
    # the directory-pick flush has the file ready).
    photo = update.message.photo[-1]
    tg_file = await photo.get_file()
    filename = f"{int(time.time())}_{photo.file_unique_id}.jpg"
    file_path = _IMAGES_DIR / filename
    await tg_file.download_to_drive(file_path)
    _restrict_download_perms(file_path)

    caption = update.message.caption or ""
    media_group_id = update.message.media_group_id

    if wid is None:
        if context.user_data is not None:
            pending_tid = _pending_thread_id(context.user_data)
            if pending_tid is not None and pending_tid != thread_id:
                _clear_picker_state_for_current_state(context.user_data)
                _clear_pending_route_payload(
                    context.user_data,
                    delete_files=True,
                    clear_ignored_stale_threads=False,
                )
                _remember_ignored_stale_thread_id(context.user_data, pending_tid)
        # §2.5: render reply-context before stashing an unbound-topic caption
        # so the later directory/window/session-picker flush preserves the
        # same quote block as the bound aggregator path below. Keep the same
        # media-group guard as the bound path: non-caption-bearing album items
        # must not each synthesize their own quote block.
        if caption or media_group_id is None:
            caption = await _apply_reply_context(
                update.message, user.id, thread_id, caption
            )
        # §2.8.3 photo-in-unbound-topic: stash the path so the directory
        # picker's flush in _create_and_bind_window can feed the aggregator
        # for the freshly-bound route. Multiple photos can pile up here
        # while the user navigates the directory browser.
        if context.user_data is not None:
            pending_attachments = context.user_data.setdefault(
                "_pending_thread_attachments", []
            )
            pending_attachments.append(
                PendingAttachment(str(file_path), caption, media_group_id)
            )
            context.user_data["_pending_thread_id"] = thread_id
            _forget_ignored_stale_thread_id(context.user_data, thread_id)

        # If the user is already mid-picker (text_handler opened the
        # directory browser, window picker, or session picker for THIS
        # topic), stashing the photo is enough — re-emitting the picker
        # here would stomp on the existing browse/picker state and lose
        # the user's progress. Mirrors the same-thread guards in
        # text_handler.
        if context.user_data is not None:
            current_state = context.user_data.get(STATE_KEY)
            pending_tid = context.user_data.get("_pending_thread_id")
            if pending_tid == thread_id and current_state in (
                STATE_BROWSING_DIRECTORY,
                STATE_SELECTING_WINDOW,
                STATE_SELECTING_SESSION,
            ):
                return

        # Always open the directory browser for unbound topics. If
        # unbound tmux windows exist, the browser surfaces an opt-in
        # "🖥 Bind existing window" button so the user can pivot to the
        # window picker — but the directory choice stays primary.
        unbound_count = len(await _list_unbound_windows(tmux_manager, session_manager))
        start_path = str(config.browse_root)
        msg_text, keyboard, subdirs = build_directory_browser(
            start_path, unbound_count=unbound_count
        )
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = start_path
            context.user_data[BROWSE_PAGE_KEY] = 0
            context.user_data[BROWSE_DIRS_KEY] = subdirs
            context.user_data[BROWSE_UNBOUND_COUNT_KEY] = unbound_count
        await safe_reply(update.message, msg_text, reply_markup=keyboard)
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        session_manager.unbind_thread(user.id, thread_id)
        # Tear down route_runtime state for the now-unbound route (run-state /
        # open_tools / context_usage / pane_interactive_pending) — unbind_thread
        # alone leaks it. ``or 0`` matches the SET-path key in status_polling.
        route_runtime.clear_route((user.id, thread_id or 0, wid))
        pane_signals.clear_route((user.id, thread_id or 0, wid))  # GH #43
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists. Binding removed.\n"
            "Send a message to start a new session.",
        )
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    clear_status_msg_info(user.id, thread_id)

    # §2.5.2: anchor the next assistant-text response to the LATEST inbound
    # photo's message_id (matches Telegram's "reply to most recent" UX).
    set_route_last_user_message(user.id, thread_id, wid, update.message.message_id)

    # §2.5: render reply-context onto the caption so a photo reply carries
    # the same quote-injection block as a text reply. Skip when this update
    # is a non-caption-bearing item of a media group — Telegram puts the
    # caption on item 1 only, and rendering with empty caption on items 2-N
    # would re-emit the quote block multiple times (the random nonce in
    # ``render_for_claude`` defeats the aggregator's exact-string dedup).
    if caption or media_group_id is None:
        caption = await _apply_reply_context(
            update.message, user.id, thread_id, caption
        )

    # §2.8: feed photo + caption + media_group_id into the aggregator. The
    # bundle's flush handler builds the §2.8.2 single-text + grouped-paths
    # shape so a media-group with one caption stops fragmenting across
    # N Claude turns.
    route = (user.id, thread_id, wid)
    await aggregator_offer_photo(route, file_path, caption, media_group_id)

    # Confirm to user
    await safe_reply(update.message, "📷 Image sent to Claude Code.")


async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice messages: transcribe via OpenAI and forward text to Claude Code."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.voice:
        return

    if not config.openai_api_key:
        await safe_reply(
            update.message,
            "⚠ Voice transcription requires an OpenAI API key.\n"
            "Set `OPENAI_API_KEY` in your `.env` file and restart the bot.",
        )
        return

    chat = update.message.chat
    thread_id = _get_thread_id(update)
    if chat.type in ("group", "supergroup") and thread_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if wid is None:
        await safe_reply(
            update.message,
            "❌ No session bound to this topic. Send a text message first to create one.",
        )
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        session_manager.unbind_thread(user.id, thread_id)
        # Tear down route_runtime state for the now-unbound route (run-state /
        # open_tools / context_usage / pane_interactive_pending) — unbind_thread
        # alone leaks it. ``or 0`` matches the SET-path key in status_polling.
        route_runtime.clear_route((user.id, thread_id or 0, wid))
        pane_signals.clear_route((user.id, thread_id or 0, wid))  # GH #43
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists. Binding removed.\n"
            "Send a message to start a new session.",
        )
        return

    # Download voice as in-memory bytes
    voice_file = await update.message.voice.get_file()
    ogg_data = bytes(await voice_file.download_as_bytearray())

    # Transcribe
    try:
        text = await transcribe_voice(ogg_data)
    except ValueError as e:
        await safe_reply(update.message, f"⚠ {e}")
        return
    except Exception as e:
        logger.error("Voice transcription failed: %s", e)
        await safe_reply(update.message, f"⚠ Transcription failed: {e}")
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    clear_status_msg_info(user.id, thread_id)

    # §2.5.2 + §2.8: voice messages take the same path as text — anchor the
    # outbound response to the user's voice-message Telegram id, then feed
    # the transcribed text into the aggregator so a voice-then-text or
    # voice-then-photo bundle still lands as one Claude turn.
    set_route_last_user_message(user.id, thread_id, wid, update.message.message_id)
    route = (user.id, thread_id, wid)
    if not text:
        # ``aggregator_offer_voice`` (and its underlying
        # ``aggregator_offer_text``) silently no-op on empty text. Surface
        # that in logs so an empty-transcription failure mode is visible
        # rather than looking like the bot dropped the voice message.
        logger.debug(
            "voice transcription empty for user=%d thread=%s",
            user.id,
            thread_id,
        )
    # Show the raw transcription to the user (echo bubble) before wrapping
    # the prompt with §2.5 reply context — the echo is for the human, the
    # rendered text is what Claude actually sees.
    echo = text
    rendered = await _apply_reply_context(update.message, user.id, thread_id, text)
    await aggregator_offer_voice(route, rendered)

    await safe_reply(update.message, f'🎤 "{echo}"')


def _sanitize_filename_part(part: str, max_len: int) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", part)
    return cleaned[:max_len]


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle documents sent by the user: download and forward path to Claude Code."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.document:
        return

    chat = update.message.chat
    thread_id = _get_thread_id(update)
    if chat.type in ("group", "supergroup") and thread_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return

    document = update.message.document
    file_size = document.file_size
    limit_mb = config.max_attachment_size_bytes / (1024 * 1024)
    if file_size is None:
        await safe_reply(
            update.message,
            f"⚠ File size unknown — refusing to download. Limit is {limit_mb:.0f} MB.",
        )
        return
    if file_size > config.max_attachment_size_bytes:
        size_mb = file_size / (1024 * 1024)
        await safe_reply(
            update.message,
            f"⚠ File too large ({size_mb:.1f} MB). Limit is {limit_mb:.0f} MB.",
        )
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)

    original = document.file_name or "file"
    stem, ext = os.path.splitext(original)
    safe_stem = _sanitize_filename_part(stem, 100) or "file"
    safe_ext = _sanitize_filename_part(ext, 16) if ext else ""
    filename = f"{int(time.time())}_{document.file_unique_id}_{safe_stem}{safe_ext}"
    file_path = _FILES_DIR / filename

    tg_file = await document.get_file()
    await tg_file.download_to_drive(file_path)
    _restrict_download_perms(file_path)

    caption = update.message.caption or ""
    media_group_id = update.message.media_group_id

    if wid is None:
        if context.user_data is not None:
            pending_tid = _pending_thread_id(context.user_data)
            if pending_tid is not None and pending_tid != thread_id:
                _clear_picker_state_for_current_state(context.user_data)
                _clear_pending_route_payload(
                    context.user_data,
                    delete_files=True,
                    clear_ignored_stale_threads=False,
                )
                _remember_ignored_stale_thread_id(context.user_data, pending_tid)
            # §2.5: render reply-context before stashing an unbound-topic
            # caption so the later picker flush preserves the same quote block
            # as the bound aggregator path below. Keep the same media-group
            # guard as the bound path to avoid duplicate quote blocks for
            # non-caption-bearing album items.
            if caption or media_group_id is None:
                caption = await _apply_reply_context(
                    update.message, user.id, thread_id, caption
                )
            pending_attachments = context.user_data.setdefault(
                "_pending_thread_attachments", []
            )
            pending_attachments.append(
                PendingAttachment(str(file_path), caption, media_group_id)
            )
            context.user_data["_pending_thread_id"] = thread_id
            _forget_ignored_stale_thread_id(context.user_data, thread_id)

        if context.user_data is not None:
            current_state = context.user_data.get(STATE_KEY)
            pending_tid = context.user_data.get("_pending_thread_id")
            if pending_tid == thread_id and current_state in (
                STATE_BROWSING_DIRECTORY,
                STATE_SELECTING_WINDOW,
                STATE_SELECTING_SESSION,
            ):
                return

        unbound_count = len(await _list_unbound_windows(tmux_manager, session_manager))
        start_path = str(config.browse_root)
        msg_text, keyboard, subdirs = build_directory_browser(
            start_path, unbound_count=unbound_count
        )
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = start_path
            context.user_data[BROWSE_PAGE_KEY] = 0
            context.user_data[BROWSE_DIRS_KEY] = subdirs
            context.user_data[BROWSE_UNBOUND_COUNT_KEY] = unbound_count
        await safe_reply(update.message, msg_text, reply_markup=keyboard)
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        session_manager.unbind_thread(user.id, thread_id)
        # Tear down route_runtime state for the now-unbound route (run-state /
        # open_tools / context_usage / pane_interactive_pending) — unbind_thread
        # alone leaks it. ``or 0`` matches the SET-path key in status_polling.
        route_runtime.clear_route((user.id, thread_id or 0, wid))
        pane_signals.clear_route((user.id, thread_id or 0, wid))  # GH #43
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists. Binding removed.\n"
            "Send a message to start a new session.",
        )
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    clear_status_msg_info(user.id, thread_id)

    set_route_last_user_message(user.id, thread_id, wid, update.message.message_id)

    # §2.5: see photo_handler for the media-group caption-skip rationale.
    if caption or media_group_id is None:
        caption = await _apply_reply_context(
            update.message, user.id, thread_id, caption
        )

    route = (user.id, thread_id, wid)
    await aggregator_offer_document(route, file_path, caption, media_group_id)

    await safe_reply(update.message, "📎 File sent to Claude Code.")


# Active bash capture tasks: (user_id, thread_id) → asyncio.Task
_bash_capture_tasks: dict[tuple[int, int], asyncio.Task[None]] = {}


def _cancel_bash_capture(user_id: int, thread_id: int) -> None:
    """Cancel any running bash capture for this topic."""
    key = (user_id, thread_id)
    task = _bash_capture_tasks.pop(key, None)
    if task and not task.done():
        task.cancel()


async def _capture_bash_output(
    bot: Bot,
    user_id: int,
    thread_id: int,
    window_id: str,
    command: str,
) -> None:
    """Background task: capture ``!`` bash command output from tmux pane.

    Sends the first captured output as a new message, then edits it
    in-place as more output appears.  Stops after 30 s or when cancelled
    (e.g. user sends a new message, which pushes content down).
    """
    try:
        # Wait for the command to start producing output
        await asyncio.sleep(2.0)

        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
        msg_id: int | None = None
        last_output: str = ""

        for _ in range(30):
            raw = await tmux_manager.capture_pane(window_id)
            if raw is None:
                return

            output = extract_bash_output(raw, command)
            if not output:
                await asyncio.sleep(1.0)
                continue

            # Skip edit if nothing changed
            if output == last_output:
                await asyncio.sleep(1.0)
                continue

            last_output = output

            # Truncate to fit Telegram's 4096-char limit
            if len(output) > 3800:
                output = "… " + output[-3800:]

            if msg_id is None:
                # First capture — send a new message
                sent = await send_with_fallback(
                    bot,
                    chat_id,
                    output,
                    message_thread_id=thread_id,
                )
                if sent:
                    msg_id = sent.message_id
            else:
                # Subsequent captures — edit in place
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=convert_markdown(output),
                        parse_mode="MarkdownV2",
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                except Exception:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=msg_id,
                            text=output,
                            link_preview_options=NO_LINK_PREVIEW,
                        )
                    except Exception:
                        pass

            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        return
    finally:
        _bash_capture_tasks.pop((user_id, thread_id), None)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.text:
        return

    thread_id = _get_thread_id(update)
    wid = (
        session_manager.get_window_for_thread(user.id, thread_id)
        if thread_id is not None
        else None
    )

    # Capture group chat_id for supergroup forum topic routing.
    # Required: Telegram Bot API needs group chat_id (not user_id) to send
    # messages with message_thread_id. Do NOT remove — see session.py docs.
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    # Must be in a named topic — rejected BEFORE the cross-thread stale-picker
    # guards below (matching photo_handler/document_handler ordering). PTB
    # user_data is per-user across chats, so a stray DM/General text would
    # otherwise evaluate ``pending_tid == None`` → False in those guards and
    # destroy another topic's in-progress picker flow (clearing its browse
    # state and deleting its pending attachment files) before dead-ending
    # here anyway (review finding 8). A DM/General message must touch NOTHING.
    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return

    text = update.message.text

    # §2.5.1: render any reply-context BEFORE the _pending_thread_text stash
    # paths below — otherwise a brand-new-topic flow (where the directory
    # browser holds the text while the user picks a directory) would lose
    # the quote when it eventually flushes via _create_and_bind_window.
    text = await _apply_reply_context(update.message, user.id, thread_id, text)

    # Ignore text in window picker mode (only for the same thread)
    if context.user_data and context.user_data.get(STATE_KEY) == STATE_SELECTING_WINDOW:
        pending_tid = context.user_data.get("_pending_thread_id")
        if pending_tid == thread_id:
            await safe_reply(
                update.message,
                "Please use the window picker above, or tap Cancel.",
            )
            return
        # Stale picker state from a different thread — clear it
        stale_thread_id = pending_tid if isinstance(pending_tid, int) else None
        clear_window_picker_state(context.user_data)
        _clear_pending_route_payload(
            context.user_data,
            delete_files=True,
            clear_ignored_stale_threads=False,
        )
        _remember_ignored_stale_thread_id(context.user_data, stale_thread_id)

    # Ignore text in directory browsing mode (only for the same thread)
    if (
        context.user_data
        and context.user_data.get(STATE_KEY) == STATE_BROWSING_DIRECTORY
    ):
        pending_tid = context.user_data.get("_pending_thread_id")
        if pending_tid == thread_id:
            await safe_reply(
                update.message,
                "Please use the directory browser above, or tap Cancel.",
            )
            return
        # Stale browsing state from a different thread — clear it
        stale_thread_id = pending_tid if isinstance(pending_tid, int) else None
        clear_browse_state(context.user_data)
        _clear_pending_route_payload(
            context.user_data,
            delete_files=True,
            clear_ignored_stale_threads=False,
        )
        _remember_ignored_stale_thread_id(context.user_data, stale_thread_id)

    # Ignore text in session picker mode (only for the same thread)
    if (
        context.user_data
        and context.user_data.get(STATE_KEY) == STATE_SELECTING_SESSION
    ):
        pending_tid = context.user_data.get("_pending_thread_id")
        if pending_tid == thread_id:
            await safe_reply(
                update.message,
                "Please use the session picker above, or tap Cancel.",
            )
            return
        # Stale picker state from a different thread — clear it
        stale_thread_id = pending_tid if isinstance(pending_tid, int) else None
        clear_session_picker_state(context.user_data)
        _clear_pending_route_payload(
            context.user_data,
            delete_files=True,
            clear_ignored_stale_threads=False,
        )
        _remember_ignored_stale_thread_id(context.user_data, stale_thread_id)

    if wid is None:
        # Unbound topic — always show the directory browser. If unbound
        # tmux windows exist, the browser includes a "🖥 Bind existing
        # window" opt-in row that pivots to the window picker. We never
        # auto-default to an existing window's cwd, since that locks the
        # user into a directory they didn't choose.
        unbound_count = len(await _list_unbound_windows(tmux_manager, session_manager))
        logger.info(
            "Unbound topic: showing directory browser (user=%d, thread=%d, unbound=%d)",
            user.id,
            thread_id,
            unbound_count,
        )
        start_path = str(config.browse_root)
        msg_text, keyboard, subdirs = build_directory_browser(
            start_path, unbound_count=unbound_count
        )
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = start_path
            context.user_data[BROWSE_PAGE_KEY] = 0
            context.user_data[BROWSE_DIRS_KEY] = subdirs
            context.user_data[BROWSE_UNBOUND_COUNT_KEY] = unbound_count
            context.user_data["_pending_thread_id"] = thread_id
            context.user_data["_pending_thread_text"] = text
            _forget_ignored_stale_thread_id(context.user_data, thread_id)
        await safe_reply(update.message, msg_text, reply_markup=keyboard)
        return

    # Bound topic — forward to bound window
    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        logger.info(
            "Stale binding: window %s gone, unbinding (user=%d, thread=%d)",
            display,
            user.id,
            thread_id,
        )
        session_manager.unbind_thread(user.id, thread_id)
        # Tear down route_runtime state for the now-unbound route (run-state /
        # open_tools / context_usage / pane_interactive_pending) — unbind_thread
        # alone leaks it. ``or 0`` matches the SET-path key in status_polling.
        route_runtime.clear_route((user.id, thread_id or 0, wid))
        pane_signals.clear_route((user.id, thread_id or 0, wid))  # GH #43
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists. Binding removed.\n"
            "Send a message to start a new session.",
        )
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    await enqueue_status_update(context.bot, user.id, wid, None, thread_id=thread_id)

    # Cancel any running bash capture — new message pushes pane content down
    _cancel_bash_capture(user.id, thread_id)

    # Check for pending interactive UI before sending text.
    # This catches UIs (permission prompts, etc.) that status polling might have missed.
    pane_text = await tmux_manager.capture_pane(w.window_id)
    if pane_text and is_interactive_ui(pane_text):
        # UI detected — show it to user, then send text (acts as Enter)
        logger.info(
            "Detected pending interactive UI before sending text (user=%d, thread=%s)",
            user.id,
            thread_id,
        )
        await handle_interactive_ui(
            context.bot,
            user.id,
            wid,
            thread_id,
            tmux_mgr=tmux_manager,
            session_mgr=session_manager,
        )
        # Small delay to let UI render in Telegram before text arrives
        await asyncio.sleep(0.3)

    # §2.5.2: stash the latest user message_id at the OFFER site (not at the
    # aggregator flush) so the reply_parameters anchor follows the user's
    # most recent visible Telegram message, not whatever the aggregator
    # happens to flush at.
    set_route_last_user_message(user.id, thread_id, wid, update.message.message_id)

    # §2.8: feed the aggregator instead of sending direct. The reply-context
    # render above still happened; its output flows through the aggregator
    # and lands in Claude as one coherent turn alongside any caption /
    # photo / fast-follow text within the debounce window.
    route = (user.id, thread_id, wid)
    await aggregator_offer_text(route, text)

    # User just replied → Claude is no longer waiting. Flip the topic-first
    # attention card back to idle so the next idle→waiting transition fires
    # a fresh notification. Fix 3c (judgment call): kind-aware so this user-text
    # seam acks only the interactive_ui card — the notification_decision card
    # dismisses via the route_runtime USER clear → reason=USER → the poller's
    # reason-driven reconcile, NOT this display seam. (Flagged for codex+hermes:
    # the dismiss-audit classed this as a genuine-resolution path that could ack
    # any card; the contract converts it to keep the decision card's dismissal
    # on the single reason-driven channel.)
    await attention.dismiss_if_kind(
        context.bot, user_id=user.id, thread_id=thread_id, kind="interactive_ui"
    )

    # Start background capture for ! bash command output
    if text.startswith("!") and len(text) > 1:
        bash_cmd = text[1:]  # strip leading "!"
        task = asyncio.create_task(
            _capture_bash_output(context.bot, user.id, thread_id, wid, bash_cmd)
        )
        _bash_capture_tasks[(user.id, thread_id)] = task

    # If in interactive mode, refresh the UI after sending text
    interactive_window = get_interactive_window(user.id, thread_id)
    if interactive_window and interactive_window == wid:
        await asyncio.sleep(0.2)
        await handle_interactive_ui(
            context.bot,
            user.id,
            wid,
            thread_id,
            tmux_mgr=tmux_manager,
            session_mgr=session_manager,
        )


# --- Window creation helper ---


async def _cleanup_unbound_created_window(
    window_id: str,
    window_name: str,
    tmux_mgr,
    *,
    reason: str = "SessionStart hook timeout",
) -> bool:
    """Best-effort kill of a newly-created window that should not be bound."""
    if not window_id:
        logger.error(
            "Cannot clean up unbound tmux window '%s' after %s because no "
            "window_id was returned",
            window_name,
            reason,
        )
        return False
    try:
        killed = await tmux_mgr.kill_window(window_id)
    except Exception as e:  # pragma: no cover - tmux_manager normally swallows errors
        logger.error(
            "Failed to clean up unbound tmux window %s (%s) after %s: %s",
            window_id,
            window_name,
            reason,
            e,
        )
        return False
    if killed:
        logger.warning(
            "Cleaned up unbound tmux window %s (%s) after %s",
            window_id,
            window_name,
            reason,
        )
        return True
    logger.error(
        "Could not clean up unbound tmux window %s (%s) after %s",
        window_id,
        window_name,
        reason,
    )
    return False


async def _abort_created_window_after_pending_owner_change(
    query: CallbackQuery,
    *,
    user_data: dict | None,
    user_id: int,
    pending_thread_id: int,
    tmux_mgr,
    created_wid: str,
    created_wname: str,
    resume_session_id: str | None,
) -> None:
    """Surface a stale picker after a window was created but before binding."""
    logger.warning(
        "Pending owner changed before binding created window %s "
        "(user=%d, callback_thread=%d, current_owner=%s)",
        created_wid,
        user_id,
        pending_thread_id,
        _pending_thread_id(user_data),
    )
    cleanup_note = ""
    show_alert = False
    if resume_session_id is None:
        cleanup_ok = await _cleanup_unbound_created_window(
            created_wid,
            created_wname,
            tmux_mgr,
            reason="pending owner change before bind",
        )
        cleanup_note = (
            " The newly-created tmux window was cleaned up."
            if cleanup_ok
            else (
                f" The newly-created tmux window '{created_wname}' "
                f"({created_wid or 'unknown id'}) could not be cleaned up "
                "automatically; please inspect tmux."
            )
        )
        show_alert = not cleanup_ok
    else:
        cleanup_note = " The resumed tmux window was left unbound."

    await safe_edit(
        query,
        "⚠️ This picker is stale because another topic now owns the pending "
        f"message.{cleanup_note}",
    )
    await safe_answer(query, "Stale picker", show_alert=show_alert)


async def _create_and_bind_window(
    query: object,
    context: ContextTypes.DEFAULT_TYPE,
    user: object,
    selected_path: str,
    pending_thread_id: int | None,
    *,
    tmux_mgr: Any,
    session_mgr: Any,
    resume_session_id: str | None = None,
) -> None:
    """Create a tmux window, bind it to a topic, and forward pending text.

    Shared by CB_DIR_CONFIRM (no sessions), CB_SESSION_NEW, and CB_SESSION_SELECT.
    """
    assert isinstance(query, CallbackQuery)
    assert isinstance(user, User)

    if pending_thread_id is not None and not _pending_owner_matches(
        context.user_data, pending_thread_id
    ):
        logger.warning(
            "Refusing to create window for stale picker "
            "(user=%d, callback_thread=%d, current_owner=%s)",
            user.id,
            pending_thread_id,
            _pending_thread_id(context.user_data),
        )
        await safe_answer(query, "Stale picker", show_alert=True)
        return

    success, message, created_wname, created_wid = await tmux_mgr.create_window(
        selected_path, resume_session_id=resume_session_id
    )
    if success:
        logger.info(
            "Window created: %s (id=%s) at %s (user=%d, thread=%s, resume=%s)",
            created_wname,
            created_wid,
            selected_path,
            user.id,
            pending_thread_id,
            resume_session_id,
        )
        if pending_thread_id is not None and not _pending_owner_matches(
            context.user_data, pending_thread_id
        ):
            await _abort_created_window_after_pending_owner_change(
                query,
                user_data=context.user_data,
                user_id=user.id,
                pending_thread_id=pending_thread_id,
                tmux_mgr=tmux_mgr,
                created_wid=created_wid,
                created_wname=created_wname,
                resume_session_id=resume_session_id,
            )
            return

        # Wait for Claude Code's SessionStart hook to register in session_map.
        # Resume sessions take longer to start (loading session state), so use
        # a longer timeout to avoid silently dropping messages.
        # Configurable via CC_TELEGRAM_HOOK_TIMEOUT (seconds). The stock 5s can
        # be too tight when Claude starts on a slow filesystem (e.g. WSL DrvFs
        # under /mnt/c) or loads several MCP servers and only reaches
        # SessionStart after ~15-20s; allow raising it without penalising fast
        # setups. Resume keeps a larger default.
        default_hook_timeout = 15.0 if resume_session_id else 5.0
        raw_hook_timeout = os.getenv("CC_TELEGRAM_HOOK_TIMEOUT")
        try:
            hook_timeout = (
                float(raw_hook_timeout) if raw_hook_timeout else default_hook_timeout
            )
            if not math.isfinite(hook_timeout) or hook_timeout <= 0:
                raise ValueError("must be a positive, finite number of seconds")
        except ValueError:
            logger.warning(
                "Invalid CC_TELEGRAM_HOOK_TIMEOUT=%r; using default %ss",
                raw_hook_timeout,
                default_hook_timeout,
            )
            hook_timeout = default_hook_timeout
        hook_ok = await session_mgr.wait_for_session_map_entry(
            created_wid, timeout=hook_timeout
        )

        if pending_thread_id is not None and not _pending_owner_matches(
            context.user_data, pending_thread_id
        ):
            await _abort_created_window_after_pending_owner_change(
                query,
                user_data=context.user_data,
                user_id=user.id,
                pending_thread_id=pending_thread_id,
                tmux_mgr=tmux_mgr,
                created_wid=created_wid,
                created_wname=created_wname,
                resume_session_id=resume_session_id,
            )
            return

        if not hook_ok and not resume_session_id:
            # A brand-new (non-resume) window that never registers in
            # session_map is unmonitored: binding or sending to it would lose
            # the first response. Since this helper just created the window and
            # has not bound it yet, it is safe to clean up by exact window_id.
            logger.warning(
                "Hook timed out for new window %s — cleaning up before binding "
                "(user=%d, thread=%s)",
                created_wid,
                user.id,
                pending_thread_id,
            )
            cleanup_ok = await _cleanup_unbound_created_window(
                created_wid, created_wname, tmux_mgr
            )
            cleanup_note = (
                "The unmonitored tmux window was cleaned up."
                if cleanup_ok
                else (
                    "The hook timeout remains the primary failure, but the "
                    f"unmonitored tmux window '{created_wname}' ({created_wid or 'unknown id'}) "
                    "could not be cleaned up automatically. Please inspect tmux."
                )
            )
            await safe_edit(
                query,
                f"❌ {message}\n\nClaude session didn't register in time. "
                f"{cleanup_note} Send your message again to retry.",
            )
            if context.user_data is not None and _pending_owner_matches(
                context.user_data, pending_thread_id
            ):
                _clear_pending_route_payload(context.user_data, delete_files=True)
            await safe_answer(
                query,
                "Hook timeout" if cleanup_ok else "Hook timeout; cleanup failed",
                show_alert=not cleanup_ok,
            )
            return

        # --resume creates a new session_id in the hook, but messages continue
        # writing to the resumed session's JSONL file. Override window_state to
        # track the original session_id so the monitor can route messages back.
        if resume_session_id:
            ws = session_mgr.get_window_state(created_wid)
            if not hook_ok:
                # Hook timed out — manually populate window_state so the
                # monitor can still route messages back to this topic.
                logger.warning(
                    "Hook timed out for resume window %s, "
                    "manually setting session_id=%s cwd=%s",
                    created_wid,
                    resume_session_id,
                    selected_path,
                )
                ws.session_id = resume_session_id
                ws.cwd = str(selected_path)
                ws.window_name = created_wname
                session_mgr._save_state()
            elif ws.session_id != resume_session_id:
                logger.info(
                    "Resume override: window %s session_id %s -> %s",
                    created_wid,
                    ws.session_id,
                    resume_session_id,
                )
                ws.session_id = resume_session_id
                session_mgr._save_state()

        if pending_thread_id is not None:
            # Pre-register the new session in the monitor so the first
            # user/assistant exchange isn't dropped by the default
            # end-of-file offset initialization in
            # ``SessionMonitor.check_for_updates``.
            ws = session_mgr.get_window_state(created_wid)
            track_sid = resume_session_id or ws.session_id
            track_cwd = ws.cwd or selected_path

            if not track_sid:
                # Non-resume + hook timeout: we don't know the session_id, so
                # any pending text we send produces a response the monitor
                # cannot route back. Surface the failure instead of silently
                # dropping the first reply.
                logger.warning(
                    "Hook timed out for new window %s — refusing to forward "
                    "pending text since session is unmonitored",
                    created_wid,
                )
                await safe_edit(
                    query,
                    f"❌ {message}\n\nClaude session didn't register in time. "
                    "Send your message again to retry.",
                )
                if context.user_data is not None and _pending_owner_matches(
                    context.user_data, pending_thread_id
                ):
                    _clear_pending_route_payload(context.user_data, delete_files=True)
                await safe_answer(query, "Hook timeout")
                return

            # session_monitor lives in bot.py (mutated in post_init); look it
            # up lazily so this extracted helper still sees the current
            # monitor instance after restart / re-init, and so the
            # ``cctelegram.bot`` ↔ ``cctelegram.handlers.inbound_telegram``
            # import edge stays one-directional (lazy import dodges the
            # circular dependency if anything imports inbound_telegram
            # before bot.py finishes loading).
            from cctelegram import bot as _bot_module

            if _bot_module.session_monitor is not None:
                file_path = session_mgr._build_session_file_path(track_sid, track_cwd)
                if file_path is not None:
                    # Resume: skip pre-existing transcript history. New
                    # sessions: read from the start so the seed message and
                    # first reply are picked up.
                    if resume_session_id and file_path.exists():
                        offset = file_path.stat().st_size
                    else:
                        offset = 0
                    _bot_module.session_monitor.register_session(
                        track_sid, file_path, offset=offset
                    )

            if not _pending_owner_matches(context.user_data, pending_thread_id):
                await _abort_created_window_after_pending_owner_change(
                    query,
                    user_data=context.user_data,
                    user_id=user.id,
                    pending_thread_id=pending_thread_id,
                    tmux_mgr=tmux_mgr,
                    created_wid=created_wid,
                    created_wname=created_wname,
                    resume_session_id=resume_session_id,
                )
                return

            # Thread bind flow: bind thread to newly created window
            session_mgr.bind_thread(
                user.id, pending_thread_id, created_wid, window_name=created_wname
            )

            status = "Resumed" if resume_session_id else "Created"

            # Replay pending text and/or attachments through the synchronous
            # aggregator helper so §2.8.2 formatting is preserved without
            # offer-path background/intermediate flushes hiding failures.
            route = (user.id, pending_thread_id, created_wid)
            pending_delivered = await _flush_pending_route_payload(
                route, context.user_data
            )
            if pending_delivered is False:
                await safe_edit(
                    query,
                    f"✅ {message}\n\n{status}, but the first message failed to send. "
                    "The pending payload was cleared; please resend it here.",
                )
                await safe_answer(
                    query, f"{status}; first message failed", show_alert=True
                )
                return

            first_turn_note = (
                " First message sent." if pending_delivered is True else ""
            )
            await safe_edit(
                query,
                f"✅ {message}\n\n{status}.{first_turn_note} Send messages here.",
            )
        else:
            # Should not happen in topic-only mode, but handle gracefully
            await safe_edit(query, f"✅ {message}")
    else:
        await safe_edit(query, f"❌ {message}")
        if (
            pending_thread_id is not None
            and context.user_data is not None
            and _pending_owner_matches(context.user_data, pending_thread_id)
        ):
            _clear_pending_route_payload(context.user_data, delete_files=True)
    await safe_answer(query, "Created" if success else "Failed")
