"""Telegram bot handlers — the main UI layer of CC Telegram.

Registers all command/callback/message handlers and manages the bot lifecycle.
Each Telegram topic maps 1:1 to a tmux window (Claude session).

Core responsibilities:
  - Command handlers: /start, /history, /screenshot, /esc, /kill, /unbind,
    plus forwarding unknown /commands to Claude Code via tmux.
  - Callback query handler: directory browser, history pagination,
    interactive UI navigation, screenshot refresh.
  - Topic-based routing: each named topic binds to one tmux window.
    Unbound topics trigger the directory browser to create a new session.
  - Photo handling: photos sent by user are downloaded and forwarded
    to Claude Code as file paths (photo_handler).
  - Voice handling: voice messages are transcribed via OpenAI API and
    forwarded as text (voice_handler).
  - Automatic cleanup: closing a topic kills the associated window
    (topic_closed_handler). Unsupported content (stickers, etc.)
    is rejected with a warning (unsupported_content_handler).
  - Bot lifecycle management: post_init, post_shutdown, create_bot.

Handler modules (in handlers/):
  - callback_data: Callback data constants
  - message_queue: Per-user message queue management
  - message_sender: Safe message sending helpers
  - history: Message history pagination
  - directory_browser: Directory browser UI
  - interactive_ui: Interactive UI handling
  - status_polling: Terminal status polling
  - response_builder: Response message building

Key functions: create_bot(), handle_new_message().
"""

import asyncio
import io
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from telegram import (
    Bot,
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeChatAdministrators,
    BotCommandScopeChatMember,
    BotCommandScopeDefault,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    Message,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import config
from .handlers.callback_data import (
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
    CB_DIR_CANCEL,
    CB_DIR_CONFIRM,
    CB_DIR_PAGE,
    CB_DIR_SELECT,
    CB_DIR_BIND_EXISTING,
    CB_DIR_UP,
    CB_HISTORY_NEXT,
    CB_HISTORY_PREV,
    CB_SESSION_CANCEL,
    CB_SESSION_NEW,
    CB_SESSION_SELECT,
    CB_KEYS_PREFIX,
    CB_SCREENSHOT_REFRESH,
    CB_WIN_BIND,
    CB_WIN_CANCEL,
    CB_WIN_NEW,
)
from .handlers.directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    BROWSE_UNBOUND_COUNT_KEY,
    SESSIONS_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_SELECTING_SESSION,
    STATE_SELECTING_WINDOW,
    UNBOUND_WINDOWS_KEY,
    build_directory_browser,
    build_session_picker,
    build_window_picker,
    clear_browse_state,
    clear_session_picker_state,
    clear_window_picker_state,
)
from .handlers import attention, busy_indicator
from .handlers.cleanup import clear_topic_state
from .handlers.history import send_history
from .handlers.inbound_aggregator import (
    AggregatorReplayAttachment,
    aggregator_clear_route,
    aggregator_flush_route,
    aggregator_offer_document,
    aggregator_offer_photo,
    aggregator_offer_text,
    aggregator_offer_voice,
    aggregator_replay_payload,
)
from .handlers.interactive_ui import (
    INTERACTIVE_TOOL_NAMES,
    NAV_ESC_CLEAR,
    assert_nav_dispatchable,
    clear_interactive_mode,
    clear_interactive_msg,
    consume_pick_token,
    peek_pick_token,
    forget_ask_tool_input,
    get_interactive_window,
    handle_interactive_ui,
    has_interactive_surface,
    remember_ask_tool_input,
    set_interactive_mode,
)
from .handlers.message_queue import (
    clear_status_msg_info,
    enqueue_content_message,
    enqueue_status_update,
    get_content_queue,
    probe_topic_liveness,
    set_route_last_user_message,
    shutdown_workers,
)
from . import message_refs
from .handlers import reply_context as reply_context_mod
from .handlers.reply_context import extract_reply_context, render_for_claude
from .handlers.message_sender import (
    NO_LINK_PREVIEW,
    safe_edit,
    safe_reply,
    send_with_fallback,
)
from .markdown_v2 import convert_markdown
from .handlers.response_builder import build_response_parts
from .handlers.status_polling import status_poll_loop, typing_action_loop
from .screenshot import text_to_image
from .session import session_manager
from .session_monitor import NewMessage, SessionMonitor, TranscriptEvent
from .terminal_parser import extract_bash_output, is_interactive_ui
from .tmux_manager import tmux_manager
from .transcribe import close_client as close_transcribe_client
from .transcribe import transcribe_voice
from .utils import app_dir

logger = logging.getLogger(__name__)

# Session monitor instance
session_monitor: SessionMonitor | None = None

# Status polling task
_status_poll_task: asyncio.Task | None = None

# Typing-action refresher (V2 indicator only). Decoupled from status_poll_loop
# so its cadence stays under Telegram's 5s typing TTL regardless of how many
# bindings exist or how slow tmux capture_pane is.
_typing_action_task: asyncio.Task | None = None

# §2.5.3 Stage 5.c: daily GC pass for the message_refs SQLite table.
_message_refs_gc_task: asyncio.Task | None = None
_MESSAGE_REFS_GC_INTERVAL_SECONDS = 24 * 60 * 60


async def _message_refs_gc_loop(bot: Bot) -> None:
    """Once-per-day prune of rows older than the retention window.

    Long-sleep + cancel-aware shape mirrors ``status_poll_loop``. A SQLite
    blip is logged inside ``message_refs.prune_older_than``; this loop
    tolerates exceptions and keeps running so a single failure does not
    leave the table growing unboundedly.
    """
    while True:
        try:
            deleted = await message_refs.prune_older_than(
                config.message_refs_retention_days
            )
            if deleted:
                logger.info("message_refs GC dropped %d row(s)", deleted)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("message_refs GC iteration failed: %s", e)
        # Topic-liveness probe: catch silently-deleted topics whose sessions
        # are dormant (reactive cleanup in _emergency_dm covers the active
        # case). Telegram does not emit forum_topic_deleted, so this once-a-
        # day probe is the only fallback for the dormant case.
        try:
            await probe_topic_liveness(bot)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("topic liveness probe iteration failed: %s", e)
        try:
            await asyncio.sleep(_MESSAGE_REFS_GC_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise


# Claude Code commands shown in bot menu (forwarded via tmux).
# Only commands whose output actually lands in the JSONL transcript belong
# here. /memory and /help open TUI-interactive panels inside Claude Code
# that never reach the transcript, so they're useless over Telegram.
CC_COMMANDS: dict[str, str] = {
    "clear": "↗ Clear conversation history",
    "compact": "↗ Compact conversation context",
    "cost": "↗ Show token/cost usage",
    "model": "↗ Switch AI model",
}


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


async def _answer_stale_pending_thread_mismatch(
    query: CallbackQuery,
    user_data: dict | None,
    callback_thread_id: int | None,
    answer_text: str,
    *,
    clear_picker_state: bool = False,
) -> None:
    """Answer a pending-thread mismatch without deleting newer replacement media.

    Old callbacks from a pending topic that was explicitly replaced by a newer
    pending topic are only acknowledged as stale. Other mismatches retain the
    prior safety behavior: reject and clear/delete the active stale payload.
    """
    if not _is_ignored_stale_thread_id(user_data, callback_thread_id):
        if clear_picker_state:
            clear_browse_state(user_data)
        if user_data is not None:
            _clear_pending_route_payload(user_data, delete_files=True)
    await query.answer(answer_text, show_alert=True)


_PICKER_STALE_TOPIC_MISMATCH = "topic_mismatch"


def _validate_pending_picker_callback(
    user_data: dict | None,
    callback_thread_id: int | None,
    expected_states: tuple[str, ...],
) -> tuple[bool, int | None, str | None]:
    """Validate a picker callback still owns an active pending topic route.

    Directory/session/window picker buttons are only actionable while their
    pending route payload is active. Missing user_data, missing/wrong state, a
    missing ``_pending_thread_id``, or a different callback topic are all stale.
    """
    if user_data is None:
        return False, None, "missing_user_data"

    current_state = user_data.get(STATE_KEY)
    if current_state not in expected_states:
        return False, None, "wrong_state"

    pending_tid = _pending_thread_id(user_data)
    if pending_tid is None:
        return False, None, "missing_pending_owner"

    if callback_thread_id != pending_tid:
        return False, pending_tid, _PICKER_STALE_TOPIC_MISMATCH

    return True, pending_tid, None


async def _answer_invalid_pending_picker_callback(
    query: CallbackQuery,
    answer_text: str,
) -> None:
    """Answer a stale picker callback without mutating pending picker state."""
    await query.answer(answer_text, show_alert=True)


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


def _callback_window_is_current(
    user_id: int, thread_id: int | None, window_id: str
) -> bool:
    """Return True when a callback's encoded window still owns the topic."""
    return session_manager.resolve_window_for_thread(user_id, thread_id) == window_id


async def _list_unbound_windows() -> list[tuple[str, str, str]]:
    """Return tmux windows not currently bound to any topic, as (id, name, cwd)."""
    all_windows = await tmux_manager.list_windows()
    bound_ids = {bid for _, _, bid in session_manager.iter_thread_bindings()}
    return [
        (w.window_id, w.window_name, w.cwd)
        for w in all_windows
        if w.window_id not in bound_ids
    ]


# --- Command handlers ---


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    clear_browse_state(context.user_data)

    if update.message:
        await safe_reply(
            update.message,
            "🤖 *Claude Code Monitor*\n\n"
            "Each topic is a session. Create a new topic to start.",
        )


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show message history for the active session or bound thread."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    await send_history(update.message, wid)


async def screenshot_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Capture the current tmux pane and send it as an image."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return

    text = await tmux_manager.capture_pane(w.window_id, with_ansi=True)
    if not text:
        await safe_reply(update.message, "❌ Failed to capture pane content.")
        return

    png_bytes = await text_to_image(text, with_ansi=True)
    keyboard = _build_screenshot_keyboard(wid)
    await update.message.reply_document(
        document=io.BytesIO(png_bytes),
        filename="screenshot.png",
        reply_markup=keyboard,
    )


async def unbind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unbind this topic from its Claude session without killing the window."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, "❌ This command only works in a topic.")
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    display = session_manager.get_display_name(wid)
    session_manager.unbind_thread(user.id, thread_id)
    await clear_topic_state(
        user.id,
        thread_id,
        context.bot,
        context.user_data,
        drop_pending=False,
    )

    await safe_reply(
        update.message,
        f"✅ Topic unbound from window '{display}'.\n"
        "The Claude session is still running in tmux.\n"
        "Send a message to bind to a new session.",
    )


async def kill_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Kill this topic's tmux window and clear bot state. Topic stays open in Telegram."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, "❌ This command only works in a topic.")
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    display = session_manager.get_display_name(wid)
    w = await tmux_manager.find_window_by_id(wid)
    if w:
        await tmux_manager.kill_window(w.window_id)
        logger.info(
            "/kill: killed window %s (user=%d, thread=%d)",
            display,
            user.id,
            thread_id,
        )
    else:
        logger.info(
            "/kill: window %s already gone (user=%d, thread=%d)",
            display,
            user.id,
            thread_id,
        )
    session_manager.unbind_thread(user.id, thread_id)
    await clear_topic_state(
        user.id,
        thread_id,
        context.bot,
        context.user_data,
        drop_pending=True,
    )

    await safe_reply(
        update.message,
        f"✅ Killed session '{display}'.\n"
        "Topic remains open — send a message to bind to a new session.",
    )


async def esc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send Escape key to interrupt Claude."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return

    # Send Escape control character (no enter)
    await tmux_manager.send_keys(w.window_id, "\x1b", enter=False)
    await safe_reply(update.message, "⎋ Sent Escape")


async def usage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch Claude Code usage stats from TUI and send to Telegram."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, "No session bound to this topic.")
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        await safe_reply(update.message, f"Window '{wid}' no longer exists.")
        return

    # Send /usage command to Claude Code TUI
    await tmux_manager.send_keys(w.window_id, "/usage")
    # Wait for the modal to render
    await asyncio.sleep(2.0)
    # Capture the pane content
    pane_text = await tmux_manager.capture_pane(w.window_id)
    # Dismiss the modal
    await tmux_manager.send_keys(w.window_id, "Escape", enter=False, literal=False)

    if not pane_text:
        await safe_reply(update.message, "Failed to capture usage info.")
        return

    # Try to parse structured usage info
    from .terminal_parser import parse_usage_output

    usage = parse_usage_output(pane_text)
    if usage and usage.parsed_lines:
        text = "\n".join(usage.parsed_lines)
        await safe_reply(update.message, f"```\n{text}\n```")
    else:
        # Fallback: send raw pane capture trimmed
        trimmed = pane_text.strip()
        if len(trimmed) > 3000:
            trimmed = trimmed[:3000] + "\n... (truncated)"
        await safe_reply(update.message, f"```\n{trimmed}\n```")


# --- Screenshot keyboard with quick control keys ---

# key_id → (tmux_key, enter, literal)
_KEYS_SEND_MAP: dict[str, tuple[str, bool, bool]] = {
    "up": ("Up", False, False),
    "dn": ("Down", False, False),
    "lt": ("Left", False, False),
    "rt": ("Right", False, False),
    "esc": ("Escape", False, False),
    "ent": ("Enter", False, False),
    "spc": ("Space", False, False),
    "tab": ("Tab", False, False),
    "cc": ("C-c", False, False),
}

# key_id → display label (shown in callback answer toast)
_KEY_LABELS: dict[str, str] = {
    "up": "↑",
    "dn": "↓",
    "lt": "←",
    "rt": "→",
    "esc": "⎋ Esc",
    "ent": "⏎ Enter",
    "spc": "␣ Space",
    "tab": "⇥ Tab",
    "cc": "^C",
}


def _build_screenshot_keyboard(window_id: str) -> InlineKeyboardMarkup:
    """Build inline keyboard for screenshot: control keys + refresh."""

    def btn(label: str, key_id: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            label,
            callback_data=f"{CB_KEYS_PREFIX}{key_id}:{window_id}"[:64],
        )

    return InlineKeyboardMarkup(
        [
            [btn("␣ Space", "spc"), btn("↑", "up"), btn("⇥ Tab", "tab")],
            [btn("←", "lt"), btn("↓", "dn"), btn("→", "rt")],
            [btn("⎋ Esc", "esc"), btn("^C", "cc"), btn("⏎ Enter", "ent")],
            [
                InlineKeyboardButton(
                    "🔄 Refresh",
                    callback_data=f"{CB_SCREENSHOT_REFRESH}{window_id}"[:64],
                )
            ],
        ]
    )


async def topic_closed_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle topic closure — kill the associated tmux window and clean up state."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        return

    _clear_pending_route_payload_for_thread(
        context.user_data,
        thread_id,
        delete_files=True,
    )

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if wid:
        display = session_manager.get_display_name(wid)
        w = await tmux_manager.find_window_by_id(wid)
        if w:
            await tmux_manager.kill_window(w.window_id)
            logger.info(
                "Topic closed: killed window %s (user=%d, thread=%d)",
                display,
                user.id,
                thread_id,
            )
        else:
            logger.info(
                "Topic closed: window %s already gone (user=%d, thread=%d)",
                display,
                user.id,
                thread_id,
            )
        session_manager.unbind_thread(user.id, thread_id)
        # Clean up all memory state for this topic
        await clear_topic_state(user.id, thread_id, context.bot, context.user_data)
    else:
        logger.debug(
            "Topic closed: no binding (user=%d, thread=%d)", user.id, thread_id
        )


async def topic_edited_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle topic rename — sync new name to tmux window and internal state."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return

    msg = update.message
    if not msg or not msg.forum_topic_edited:
        return

    new_name = msg.forum_topic_edited.name
    if new_name is None:
        # Icon-only change, no rename needed
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if not wid:
        logger.debug(
            "Topic edited: no binding (user=%d, thread=%d)", user.id, thread_id
        )
        return

    old_name = session_manager.get_display_name(wid)
    if old_name == new_name:
        # Idempotent: most likely Telegram echoing our own rename back.
        return
    await tmux_manager.rename_window(wid, new_name)
    session_manager.update_display_name(wid, new_name)
    logger.info(
        "Topic renamed: '%s' -> '%s' (window=%s, user=%d, thread=%d)",
        old_name,
        new_name,
        wid,
        user.id,
        thread_id,
    )


async def forward_command_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Forward any non-bot command as a slash command to the active Claude Code session."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)

    # Capture group chat_id for supergroup forum topic routing.
    # Required: Telegram Bot API needs group chat_id (not user_id) to send
    # messages with message_thread_id. Do NOT remove — see session.py docs.
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    cmd_text = update.message.text or ""
    # The full text is already a slash command like "/clear" or "/compact foo"
    cc_slash = cmd_text.split("@")[0]  # strip bot mention
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return

    display = session_manager.get_display_name(wid)
    logger.info(
        "Forwarding command %s to window %s (user=%d)", cc_slash, display, user.id
    )
    await update.message.chat.send_action(ChatAction.TYPING)

    # §2.8: drain any pending aggregator bundle BEFORE the slash command so
    # "user types text+photo, then a slash command" preserves arrival order.
    # Without this, the text+photo bundle would still be debouncing while
    # the slash command lands first in the tmux pane.
    route = (user.id, thread_id or 0, wid)
    await aggregator_flush_route(route)

    success, message = await session_manager.send_to_window(wid, cc_slash)
    if success:
        await safe_reply(update.message, f"⚡ [{display}] Sent: {cc_slash}")
        # Mark route busy so the V2 typing loop has something to refresh
        # while Claude processes the slash command (most slash commands
        # never produce a transcript event — /model opens a pane UI, /clear
        # resets state — so the JSONL signal alone cannot light the
        # indicator). status_polling will downgrade to WAITING_ON_USER if a
        # pane interactive UI is detected later.
        if config.busy_indicator_v2:
            await busy_indicator.mark_inbound_sent(route)
        # If /clear command was sent, clear the session association
        # so we can detect the new session after first message
        if cc_slash.strip().lower() == "/clear":
            logger.info("Clearing session for window %s after /clear", display)
            session_manager.clear_window_session(wid)

        # Interactive commands (e.g. /model) render a terminal-based UI
        # with no JSONL tool_use entry.  The status poller already detects
        # interactive UIs every 1s (status_polling.py), so no
        # proactive detection needed here — the poller handles it.
    else:
        await safe_reply(update.message, f"❌ {message}")


async def unsupported_content_handler(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Reply to non-text messages (stickers, video, etc.)."""
    if not update.message:
        return
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    logger.debug("Unsupported content from user %d", user.id)
    await safe_reply(
        update.message,
        "⚠ Only text, photo, voice, and document messages are supported. Stickers and video cannot be forwarded to Claude Code.",
    )


# --- Image directory for incoming photos ---
_IMAGES_DIR = app_dir() / "images"
_IMAGES_DIR.mkdir(parents=True, exist_ok=True)

# --- File directory for incoming documents ---
_FILES_DIR = app_dir() / "files"
_FILES_DIR.mkdir(parents=True, exist_ok=True)


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
        unbound_count = len(await _list_unbound_windows())
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

        unbound_count = len(await _list_unbound_windows())
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

    # Must be in a named topic
    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return

    if wid is None:
        # Unbound topic — always show the directory browser. If unbound
        # tmux windows exist, the browser includes a "🖥 Bind existing
        # window" opt-in row that pivots to the window picker. We never
        # auto-default to an existing window's cwd, since that locks the
        # user into a directory they didn't choose.
        unbound_count = len(await _list_unbound_windows())
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
        await handle_interactive_ui(context.bot, user.id, wid, thread_id)
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
    # a fresh notification.
    await attention.dismiss(context.bot, user_id=user.id, thread_id=thread_id)

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
        await handle_interactive_ui(context.bot, user.id, wid, thread_id)


# --- Window creation helper ---


async def _cleanup_unbound_created_window(
    window_id: str,
    window_name: str,
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
        killed = await tmux_manager.kill_window(window_id)
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
    await query.answer("Stale picker", show_alert=show_alert)


async def _create_and_bind_window(
    query: object,
    context: ContextTypes.DEFAULT_TYPE,
    user: object,
    selected_path: str,
    pending_thread_id: int | None,
    resume_session_id: str | None = None,
) -> None:
    """Create a tmux window, bind it to a topic, and forward pending text.

    Shared by CB_DIR_CONFIRM (no sessions), CB_SESSION_NEW, and CB_SESSION_SELECT.
    """
    from telegram import CallbackQuery, User

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
        await query.answer("Stale picker", show_alert=True)
        return

    success, message, created_wname, created_wid = await tmux_manager.create_window(
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
                created_wid=created_wid,
                created_wname=created_wname,
                resume_session_id=resume_session_id,
            )
            return

        # Wait for Claude Code's SessionStart hook to register in session_map.
        # Resume sessions take longer to start (loading session state), so use
        # a longer timeout to avoid silently dropping messages.
        hook_timeout = 15.0 if resume_session_id else 5.0
        hook_ok = await session_manager.wait_for_session_map_entry(
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
                created_wid, created_wname
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
            await query.answer(
                "Hook timeout" if cleanup_ok else "Hook timeout; cleanup failed",
                show_alert=not cleanup_ok,
            )
            return

        # --resume creates a new session_id in the hook, but messages continue
        # writing to the resumed session's JSONL file. Override window_state to
        # track the original session_id so the monitor can route messages back.
        if resume_session_id:
            ws = session_manager.get_window_state(created_wid)
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
                session_manager._save_state()
            elif ws.session_id != resume_session_id:
                logger.info(
                    "Resume override: window %s session_id %s -> %s",
                    created_wid,
                    ws.session_id,
                    resume_session_id,
                )
                ws.session_id = resume_session_id
                session_manager._save_state()

        if pending_thread_id is not None:
            # Pre-register the new session in the monitor so the first
            # user/assistant exchange isn't dropped by the default
            # end-of-file offset initialization in
            # ``SessionMonitor.check_for_updates``.
            ws = session_manager.get_window_state(created_wid)
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
                await query.answer("Hook timeout")
                return

            if session_monitor is not None:
                file_path = session_manager._build_session_file_path(
                    track_sid, track_cwd
                )
                if file_path is not None:
                    # Resume: skip pre-existing transcript history. New
                    # sessions: read from the start so the seed message and
                    # first reply are picked up.
                    if resume_session_id and file_path.exists():
                        offset = file_path.stat().st_size
                    else:
                        offset = 0
                    session_monitor.register_session(
                        track_sid, file_path, offset=offset
                    )

            if not _pending_owner_matches(context.user_data, pending_thread_id):
                await _abort_created_window_after_pending_owner_change(
                    query,
                    user_data=context.user_data,
                    user_id=user.id,
                    pending_thread_id=pending_thread_id,
                    created_wid=created_wid,
                    created_wname=created_wname,
                    resume_session_id=resume_session_id,
                )
                return

            # Thread bind flow: bind thread to newly created window
            session_manager.bind_thread(
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
                await query.answer(f"{status}; first message failed", show_alert=True)
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
    await query.answer("Created" if success else "Failed")


# --- Callback query handler ---


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        await query.answer("Not authorized")
        return

    data = query.data

    # Capture group chat_id for supergroup forum topic routing.
    # Required: Telegram Bot API needs group chat_id (not user_id) to send
    # messages with message_thread_id. Do NOT remove — see session.py docs.
    cb_thread_id = _get_thread_id(update)
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        session_manager.set_group_chat_id(user.id, cb_thread_id, chat.id)

    async def reject_stale_window_callback(window_id: str) -> bool:
        """Answer and short-circuit if controls no longer match this topic."""
        if _callback_window_is_current(user.id, cb_thread_id, window_id):
            return False
        await query.answer("Stale controls (topic mismatch)", show_alert=True)
        return True

    async def reject_invalid_pending_picker(
        expected_states: tuple[str, ...],
        answer_text: str,
    ) -> tuple[bool, int | None]:
        """Answer and short-circuit if a picker callback lost pending ownership."""
        ok, pending_tid, _reason = _validate_pending_picker_callback(
            context.user_data,
            cb_thread_id,
            expected_states,
        )
        if ok:
            return False, pending_tid
        await _answer_invalid_pending_picker_callback(
            query,
            answer_text,
        )
        return True, pending_tid

    # History: older/newer pagination
    # Format: hp:<page>:<window_id>:<start>:<end> or hn:<page>:<window_id>:<start>:<end>
    if data.startswith(CB_HISTORY_PREV) or data.startswith(CB_HISTORY_NEXT):
        prefix_len = len(CB_HISTORY_PREV)  # same length for both
        rest = data[prefix_len:]
        try:
            parts = rest.split(":")
            if len(parts) < 4:
                # Old format without byte range: page:window_id
                offset_str, window_id = rest.split(":", 1)
                start_byte, end_byte = 0, 0
            else:
                # New format: page:window_id:start:end (window_id may contain colons)
                offset_str = parts[0]
                start_byte = int(parts[-2])
                end_byte = int(parts[-1])
                window_id = ":".join(parts[1:-2])
            offset = int(offset_str)
        except (ValueError, IndexError):
            await query.answer("Invalid data")
            return

        if not _callback_window_is_current(user.id, cb_thread_id, window_id):
            await query.answer("Stale history (topic mismatch)", show_alert=True)
            return

        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await send_history(
                query,
                window_id,
                offset=offset,
                edit=True,
                start_byte=start_byte,
                end_byte=end_byte,
                # Don't pass user_id for pagination - offset update only on initial view
                # This prevents offset from going backwards if new messages arrive while paging
            )
        else:
            await safe_edit(query, "Window no longer exists.")
        await query.answer("Page updated")

    # Directory browser handlers
    elif data.startswith(CB_DIR_SELECT):
        stale, pending_tid = await reject_invalid_pending_picker(
            (STATE_BROWSING_DIRECTORY,), "Stale browser (topic mismatch)"
        )
        if stale:
            return
        # callback_data contains index, not dir name (to avoid 64-byte limit)
        try:
            idx = int(data[len(CB_DIR_SELECT) :])
        except ValueError:
            await query.answer("Invalid data")
            return

        # Look up dir name from cached subdirs
        cached_dirs: list[str] = (
            context.user_data.get(BROWSE_DIRS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_dirs):
            await query.answer(
                "Directory list changed, please refresh", show_alert=True
            )
            return
        subdir_name = cached_dirs[idx]

        default_path = str(Path.cwd())
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        new_path = (Path(current_path) / subdir_name).resolve()

        if not new_path.exists() or not new_path.is_dir():
            await query.answer("Directory not found", show_alert=True)
            return

        new_path_str = str(new_path)
        logger.info(
            "CB_DIR_SELECT: idx=%d name=%s current=%s -> new=%s (user=%d, thread=%s)",
            idx,
            subdir_name,
            current_path,
            new_path_str,
            user.id,
            pending_tid,
        )
        if context.user_data is not None:
            context.user_data[BROWSE_PATH_KEY] = new_path_str
            context.user_data[BROWSE_PAGE_KEY] = 0

        unbound_count = (
            context.user_data.get(BROWSE_UNBOUND_COUNT_KEY, 0)
            if context.user_data
            else 0
        )
        msg_text, keyboard, subdirs = build_directory_browser(
            new_path_str, unbound_count=unbound_count
        )
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data == CB_DIR_UP:
        stale, _pending_tid = await reject_invalid_pending_picker(
            (STATE_BROWSING_DIRECTORY,), "Stale browser (topic mismatch)"
        )
        if stale:
            return
        default_path = str(Path.cwd())
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        current = Path(current_path).resolve()
        parent = current.parent
        # No restriction - allow navigating anywhere

        parent_path = str(parent)
        if context.user_data is not None:
            context.user_data[BROWSE_PATH_KEY] = parent_path
            context.user_data[BROWSE_PAGE_KEY] = 0

        unbound_count = (
            context.user_data.get(BROWSE_UNBOUND_COUNT_KEY, 0)
            if context.user_data
            else 0
        )
        msg_text, keyboard, subdirs = build_directory_browser(
            parent_path, unbound_count=unbound_count
        )
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data.startswith(CB_DIR_PAGE):
        stale, _pending_tid = await reject_invalid_pending_picker(
            (STATE_BROWSING_DIRECTORY,), "Stale browser (topic mismatch)"
        )
        if stale:
            return
        try:
            pg = int(data[len(CB_DIR_PAGE) :])
        except ValueError:
            await query.answer("Invalid data")
            return
        default_path = str(Path.cwd())
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        if context.user_data is not None:
            context.user_data[BROWSE_PAGE_KEY] = pg

        unbound_count = (
            context.user_data.get(BROWSE_UNBOUND_COUNT_KEY, 0)
            if context.user_data
            else 0
        )
        msg_text, keyboard, subdirs = build_directory_browser(
            current_path, pg, unbound_count=unbound_count
        )
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data == CB_DIR_CONFIRM:
        stale, pending_thread_id = await reject_invalid_pending_picker(
            (STATE_BROWSING_DIRECTORY,),
            "Stale browser (topic mismatch)",
        )
        if stale:
            return
        default_path = str(Path.cwd())
        selected_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )

        clear_browse_state(context.user_data)

        # Check for existing sessions in this directory
        sessions = await session_manager.list_sessions_for_directory(selected_path)
        if not _pending_owner_matches(context.user_data, pending_thread_id):
            await _answer_invalid_pending_picker_callback(
                query,
                "Stale browser (topic mismatch)",
            )
            return
        if sessions:
            # Show session picker — store state for later
            if context.user_data is not None:
                context.user_data[STATE_KEY] = STATE_SELECTING_SESSION
                context.user_data[SESSIONS_KEY] = sessions
                context.user_data["_selected_path"] = selected_path
            text, keyboard = build_session_picker(sessions)
            await safe_edit(query, text, reply_markup=keyboard)
            await query.answer()
            return

        # No existing sessions — create new window directly
        await _create_and_bind_window(
            query, context, user, selected_path, pending_thread_id
        )

    elif data == CB_DIR_CANCEL:
        stale, _pending_tid = await reject_invalid_pending_picker(
            (STATE_BROWSING_DIRECTORY,), "Stale browser (topic mismatch)"
        )
        if stale:
            return
        clear_browse_state(context.user_data)
        if context.user_data is not None:
            _clear_pending_route_payload(context.user_data, delete_files=True)
        await safe_edit(query, "Cancelled")
        await query.answer("Cancelled")

    # Session picker: resume existing session
    elif data.startswith(CB_SESSION_SELECT):
        stale, pending_tid = await reject_invalid_pending_picker(
            (STATE_SELECTING_SESSION,), "Stale picker (topic mismatch)"
        )
        if stale:
            return
        try:
            idx = int(data[len(CB_SESSION_SELECT) :])
        except ValueError:
            await query.answer("Invalid data")
            return

        cached_sessions = (
            context.user_data.get(SESSIONS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_sessions):
            await query.answer("Session not found")
            return

        session = cached_sessions[idx]
        selected_path = (
            context.user_data.get("_selected_path", str(Path.cwd()))
            if context.user_data
            else str(Path.cwd())
        )
        clear_session_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_selected_path", None)

        await _create_and_bind_window(
            query,
            context,
            user,
            selected_path,
            pending_tid,
            resume_session_id=session.session_id,
        )

    elif data == CB_SESSION_NEW:
        stale, pending_tid = await reject_invalid_pending_picker(
            (STATE_SELECTING_SESSION,), "Stale picker (topic mismatch)"
        )
        if stale:
            return
        selected_path = (
            context.user_data.get("_selected_path", str(Path.cwd()))
            if context.user_data
            else str(Path.cwd())
        )
        clear_session_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_selected_path", None)

        await _create_and_bind_window(query, context, user, selected_path, pending_tid)

    elif data == CB_SESSION_CANCEL:
        stale, _pending_tid = await reject_invalid_pending_picker(
            (STATE_SELECTING_SESSION,), "Stale picker (topic mismatch)"
        )
        if stale:
            return
        clear_session_picker_state(context.user_data)
        if context.user_data is not None:
            _clear_pending_route_payload(context.user_data, delete_files=True)
        await safe_edit(query, "Cancelled")
        await query.answer("Cancelled")

    # Window picker: bind existing window
    elif data.startswith(CB_WIN_BIND):
        stale, _pending_tid = await reject_invalid_pending_picker(
            (STATE_SELECTING_WINDOW,), "Stale picker (topic mismatch)"
        )
        if stale:
            return
        try:
            idx = int(data[len(CB_WIN_BIND) :])
        except ValueError:
            await query.answer("Invalid data")
            return

        cached_windows: list[str] = (
            context.user_data.get(UNBOUND_WINDOWS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_windows):
            await query.answer("Window list changed, please retry", show_alert=True)
            return
        selected_wid = cached_windows[idx]

        # Verify window still exists
        w = await tmux_manager.find_window_by_id(selected_wid)
        if not w:
            display = session_manager.get_display_name(selected_wid)
            await query.answer(f"Window '{display}' no longer exists", show_alert=True)
            return

        thread_id = _get_thread_id(update)
        if thread_id is None:
            await query.answer("Not in a topic", show_alert=True)
            return

        current_unbound_ids = {wid for wid, _, _ in await _list_unbound_windows()}
        if selected_wid not in current_unbound_ids:
            await query.answer(
                "Window is no longer unbound, please retry", show_alert=True
            )
            return

        ok, _pending_tid, _reason = _validate_pending_picker_callback(
            context.user_data,
            cb_thread_id,
            (STATE_SELECTING_WINDOW,),
        )
        if not ok:
            await _answer_invalid_pending_picker_callback(
                query,
                "Stale picker (topic mismatch)",
            )
            return

        display = w.window_name
        clear_window_picker_state(context.user_data)
        session_manager.bind_thread(
            user.id, thread_id, selected_wid, window_name=display
        )

        # Replay pending text and/or attachments through the synchronous
        # aggregator helper so §2.8.2 formatting is preserved without
        # offer-path background/intermediate flushes hiding failures.
        route = (user.id, thread_id, selected_wid)
        pending_delivered = await _flush_pending_route_payload(route, context.user_data)
        if pending_delivered is False:
            await safe_edit(
                query,
                f"✅ Bound to window `{display}`\n\n"
                "⚠️ First message failed to send. The pending payload was "
                "cleared; please resend it here.",
            )
            await query.answer("Bound; first message failed", show_alert=True)
            return

        first_turn_note = "\n\nFirst message sent." if pending_delivered is True else ""
        await safe_edit(
            query,
            f"✅ Bound to window `{display}`{first_turn_note}",
        )
        await query.answer("Bound")

    # Window picker: new session → transition to directory browser
    elif data == CB_WIN_NEW:
        stale, pending_tid = await reject_invalid_pending_picker(
            (STATE_SELECTING_WINDOW,), "Stale picker (topic mismatch)"
        )
        if stale:
            return
        # Preserve pending thread info, clear only picker state
        clear_window_picker_state(context.user_data)
        unbound_count = len(await _list_unbound_windows())
        start_path = str(config.browse_root)
        msg_text, keyboard, subdirs = build_directory_browser(
            start_path, unbound_count=unbound_count
        )
        logger.info(
            "CB_WIN_NEW: opening directory browser at %s (subdirs=%d, user=%d, thread=%s)",
            start_path,
            len(subdirs),
            user.id,
            pending_tid,
        )
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = start_path
            context.user_data[BROWSE_PAGE_KEY] = 0
            context.user_data[BROWSE_DIRS_KEY] = subdirs
            context.user_data[BROWSE_UNBOUND_COUNT_KEY] = unbound_count
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    # Directory browser: opt-in pivot to window picker
    elif data == CB_DIR_BIND_EXISTING:
        stale, _pending_tid = await reject_invalid_pending_picker(
            (STATE_BROWSING_DIRECTORY,), "Stale browser (topic mismatch)"
        )
        if stale:
            return
        unbound = await _list_unbound_windows()
        if not unbound:
            await query.answer("No unbound windows available", show_alert=True)
            return
        msg_text, keyboard, win_ids = build_window_picker(unbound)
        # Swap state from browse → picker. Keep pending thread/text/attachments
        # so the bind handler can flush them once a window is chosen.
        clear_browse_state(context.user_data)
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_SELECTING_WINDOW
            context.user_data[UNBOUND_WINDOWS_KEY] = win_ids
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    # Window picker: cancel
    elif data == CB_WIN_CANCEL:
        stale, _pending_tid = await reject_invalid_pending_picker(
            (STATE_SELECTING_WINDOW,), "Stale picker (topic mismatch)"
        )
        if stale:
            return
        clear_window_picker_state(context.user_data)
        if context.user_data is not None:
            _clear_pending_route_payload(context.user_data, delete_files=True)
        await safe_edit(query, "Cancelled")
        await query.answer("Cancelled")

    # Screenshot: Refresh
    elif data.startswith(CB_SCREENSHOT_REFRESH):
        window_id = data[len(CB_SCREENSHOT_REFRESH) :]
        if await reject_stale_window_callback(window_id):
            return
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await query.answer("Window no longer exists", show_alert=True)
            return

        text = await tmux_manager.capture_pane(w.window_id, with_ansi=True)
        if not text:
            await query.answer("Failed to capture pane", show_alert=True)
            return

        png_bytes = await text_to_image(text, with_ansi=True)
        keyboard = _build_screenshot_keyboard(window_id)
        try:
            await query.edit_message_media(
                media=InputMediaDocument(
                    media=io.BytesIO(png_bytes), filename="screenshot.png"
                ),
                reply_markup=keyboard,
            )
            await query.answer("Refreshed")
        except Exception as e:
            logger.error(f"Failed to refresh screenshot: {e}")
            await query.answer("Failed to refresh", show_alert=True)

    elif data == "noop":
        await query.answer()

    # Interactive UI: Up arrow
    elif data.startswith(CB_ASK_UP):
        window_id = data[len(CB_ASK_UP) :]
        thread_id = _get_thread_id(update)
        if await reject_stale_window_callback(window_id):
            return
        w = await assert_nav_dispatchable(query, user.id, thread_id, window_id)
        if w is None:
            return
        await tmux_manager.send_keys(w.window_id, "Up", enter=False, literal=False)
        await asyncio.sleep(0.5)
        await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await query.answer()

    # Interactive UI: Down arrow
    elif data.startswith(CB_ASK_DOWN):
        window_id = data[len(CB_ASK_DOWN) :]
        thread_id = _get_thread_id(update)
        if await reject_stale_window_callback(window_id):
            return
        w = await assert_nav_dispatchable(query, user.id, thread_id, window_id)
        if w is None:
            return
        await tmux_manager.send_keys(w.window_id, "Down", enter=False, literal=False)
        await asyncio.sleep(0.5)
        await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await query.answer()

    # Interactive UI: Left arrow
    elif data.startswith(CB_ASK_LEFT):
        window_id = data[len(CB_ASK_LEFT) :]
        thread_id = _get_thread_id(update)
        if await reject_stale_window_callback(window_id):
            return
        w = await assert_nav_dispatchable(query, user.id, thread_id, window_id)
        if w is None:
            return
        await tmux_manager.send_keys(w.window_id, "Left", enter=False, literal=False)
        await asyncio.sleep(0.5)
        await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await query.answer()

    # Interactive UI: Right arrow
    elif data.startswith(CB_ASK_RIGHT):
        window_id = data[len(CB_ASK_RIGHT) :]
        thread_id = _get_thread_id(update)
        if await reject_stale_window_callback(window_id):
            return
        w = await assert_nav_dispatchable(query, user.id, thread_id, window_id)
        if w is None:
            return
        await tmux_manager.send_keys(w.window_id, "Right", enter=False, literal=False)
        await asyncio.sleep(0.5)
        await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await query.answer()

    # Interactive UI: Escape
    elif data.startswith(CB_ASK_ESC):
        window_id = data[len(CB_ASK_ESC) :]
        thread_id = _get_thread_id(update)
        if await reject_stale_window_callback(window_id):
            return
        # F2: ESC carve-out. On a stale picker, still reap the Telegram card.
        w = await assert_nav_dispatchable(
            query, user.id, thread_id, window_id, is_esc=True
        )
        if w == NAV_ESC_CLEAR:
            await clear_interactive_msg(user.id, context.bot, thread_id)
            await query.answer("⎋ Esc")
            return
        if w is None:
            return
        await tmux_manager.send_keys(w.window_id, "Escape", enter=False, literal=False)
        await clear_interactive_msg(user.id, context.bot, thread_id)
        await query.answer("⎋ Esc")

    # Interactive UI: Enter
    elif data.startswith(CB_ASK_ENTER):
        window_id = data[len(CB_ASK_ENTER) :]
        thread_id = _get_thread_id(update)
        if await reject_stale_window_callback(window_id):
            return
        w = await assert_nav_dispatchable(query, user.id, thread_id, window_id)
        if w is None:
            return
        await tmux_manager.send_keys(w.window_id, "Enter", enter=False, literal=False)
        await asyncio.sleep(0.5)
        await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await query.answer("⏎ Enter")

    # Interactive UI: Space
    elif data.startswith(CB_ASK_SPACE):
        window_id = data[len(CB_ASK_SPACE) :]
        thread_id = _get_thread_id(update)
        if await reject_stale_window_callback(window_id):
            return
        w = await assert_nav_dispatchable(query, user.id, thread_id, window_id)
        if w is None:
            return
        await tmux_manager.send_keys(w.window_id, "Space", enter=False, literal=False)
        await asyncio.sleep(0.5)
        await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await query.answer("␣ Space")

    # Interactive UI: Tab
    elif data.startswith(CB_ASK_TAB):
        window_id = data[len(CB_ASK_TAB) :]
        thread_id = _get_thread_id(update)
        if await reject_stale_window_callback(window_id):
            return
        w = await assert_nav_dispatchable(query, user.id, thread_id, window_id)
        if w is None:
            return
        await tmux_manager.send_keys(w.window_id, "Tab", enter=False, literal=False)
        await asyncio.sleep(0.5)
        await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await query.answer("⇥ Tab")

    # Interactive UI: refresh display (F1: included in the nav-guard family)
    elif data.startswith(CB_ASK_REFRESH):
        window_id = data[len(CB_ASK_REFRESH) :]
        thread_id = _get_thread_id(update)
        if await reject_stale_window_callback(window_id):
            return
        w = await assert_nav_dispatchable(query, user.id, thread_id, window_id)
        if w is None:
            return
        await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await query.answer("🔄")

    # Interactive UI: structured option pick (PR 2b)
    elif data.startswith(CB_ASK_PICK):
        token = data[len(CB_ASK_PICK) :]
        # CB3: peek BEFORE consume. The old consume_pick_token-only flow
        # destroyed the token + its sibling cache row even on user-id
        # mismatch, letting a wrong user click another user's button and
        # burn the legitimate owner's tokens. Validate ownership first,
        # consume only after.
        entry = peek_pick_token(token)
        if entry is None:
            # Token never existed, was already used, or has aged past the
            # 5-minute TTL. Refresh the card so the user sees the live form
            # state and can click a fresh button.
            await query.answer("Card expired, refreshing.", show_alert=False)
            thread_id = _get_thread_id(update)
            window_id = get_interactive_window(user.id, thread_id) or ""
            if window_id:
                await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
            return
        thread_id = entry.thread_id
        window_id = entry.window_id
        # Wrong user clicking another user's card — refuse WITHOUT
        # consuming the token (CB3). The legitimate owner's click still
        # lands. Telegram answers the same way ("Not your card.") so no
        # information leaks about whether the token was valid.
        if entry.user_id != user.id:
            await query.answer("Not your card.", show_alert=False)
            return
        # Ownership confirmed — now consume atomically. From here on,
        # ``entry`` is the canonical reference; the token + its siblings
        # are gone, so any concurrent click on a stale button hits the
        # "Card expired" branch above.
        consume_pick_token(token)
        if await reject_stale_window_callback(window_id):
            await query.answer("Window gone, refreshing.")
            return
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await query.answer("Window not found", show_alert=True)
            return

        # Staleness check: re-capture the pane and re-resolve before dispatching
        # any key. If the form has shifted under us (user navigated, skill
        # advanced, Claude Code redrew, /clear fired), the minted fingerprint
        # won't match and we MUST NOT send a digit — picking "1" on a new
        # form could submit the wrong answer.
        #
        # PR 2: use ``resolve_ask_form`` with the same cached JSONL payload
        # the render path saw (via ``resolve_ask_tool_input``). Without
        # this, a multi-tab form rendered with the JSONL overlay would
        # mint fingerprints the pane-only re-parse here could never match,
        # bouncing every click to "Form changed, refreshing".
        from .handlers.interactive_ui import resolve_ask_tool_input
        from .terminal_parser import resolve_ask_form

        # Capture with the SAME scrollback as the render path
        # (handlers/interactive_ui.py uses scrollback_lines=500). A
        # smaller scrollback here produces a different pane slice from
        # what render saw → different ``current_tab_inferred`` /
        # ``current_question_title`` / options → fingerprint mismatch at
        # validate vs mint, causing taps on long pickers (where options
        # were only recoverable in the 500-line capture) to bounce with
        # "Form changed, refreshing".
        pane = await tmux_manager.capture_pane(w.window_id, scrollback_lines=500)
        cached_input = resolve_ask_tool_input(window_id)
        current_form = resolve_ask_form(cached_input, pane) if pane else None
        if current_form is None or current_form.fingerprint() != entry.fingerprint:
            logger.info(
                "Pick-token staleness reject: user=%d window=%s opt=%d "
                "minted_fp=%s current_fp=%s",
                user.id,
                window_id,
                entry.option_number,
                entry.fingerprint,
                current_form.fingerprint() if current_form else "none",
            )
            await query.answer("Form changed, refreshing.", show_alert=False)
            await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
            return

        # Submit-button guardrail: a click flagged ``is_review_submit`` only
        # fires when the live parse still says we're on the review screen
        # with the cursor on the submit row, AND the label matches. The
        # fingerprint check above already encodes is_review_screen + cursor
        # + option number + option label, so a mismatch would already have
        # bounced — Hermes review asked for an explicit label compare here
        # as belt-and-braces, so a future fingerprint-format change can't
        # accidentally let an off-screen Submit dispatch.
        if entry.is_review_submit:
            cursor_on_submit_one = (
                current_form.is_review_screen
                and current_form.options
                and current_form.options[0].cursor
                and current_form.options[0].number == 1
                and current_form.options[0].label == entry.option_label
            )
            if not cursor_on_submit_one:
                logger.info(
                    "Pick-token submit-guard reject: user=%d window=%s",
                    user.id,
                    window_id,
                )
                await query.answer("Review screen moved, refreshing.", show_alert=False)
                await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
                return

        # Dispatch: send the literal digit. Claude Code's AskUserQuestion
        # picker accepts ``1``-``9`` as shortcuts; the digit moves the
        # cursor to that option and Enter submits. We send digit + Enter
        # in two passes (no auto-Enter on the digit) so the picker has
        # time to register the selection before the Enter key arrives.
        # 500ms matches the gap tmux_manager uses internally for the
        # literal-text-then-Enter path — boring beats flaky.
        await tmux_manager.send_keys(
            w.window_id, str(entry.option_number), enter=False, literal=True
        )
        await asyncio.sleep(0.5)
        await tmux_manager.send_keys(w.window_id, "Enter", enter=False, literal=False)
        await query.answer(f"{entry.option_number}. {entry.option_label[:32]}")
        await asyncio.sleep(0.5)
        # PR 3: snapshot the JSONL cache digest BEFORE re-rendering. If a
        # concurrent ``tool_result`` clears the cache between this point
        # and ``handle_interactive_ui`` reacquiring the route lock, the
        # re-render sees the guard mismatch and aborts — no orphan card
        # posted after the prompt has already advanced.
        from .handlers.interactive_ui import _ask_tool_input_digest

        rerender_guard = _ask_tool_input_digest(resolve_ask_tool_input(window_id))
        await handle_interactive_ui(
            context.bot,
            user.id,
            window_id,
            thread_id,
            rerender_guard=rerender_guard,
        )

    # Screenshot quick keys: send key to tmux window
    elif data.startswith(CB_KEYS_PREFIX):
        rest = data[len(CB_KEYS_PREFIX) :]
        colon_idx = rest.find(":")
        if colon_idx < 0:
            await query.answer("Invalid data")
            return
        key_id = rest[:colon_idx]
        window_id = rest[colon_idx + 1 :]

        key_info = _KEYS_SEND_MAP.get(key_id)
        if not key_info:
            await query.answer("Unknown key")
            return

        tmux_key, enter, literal = key_info
        if await reject_stale_window_callback(window_id):
            return
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await query.answer("Window not found", show_alert=True)
            return

        await tmux_manager.send_keys(
            w.window_id, tmux_key, enter=enter, literal=literal
        )
        await query.answer(_KEY_LABELS.get(key_id, key_id))

        # Refresh screenshot after key press
        await asyncio.sleep(0.5)
        text = await tmux_manager.capture_pane(w.window_id, with_ansi=True)
        if text:
            png_bytes = await text_to_image(text, with_ansi=True)
            keyboard = _build_screenshot_keyboard(window_id)
            try:
                await query.edit_message_media(
                    media=InputMediaDocument(
                        media=io.BytesIO(png_bytes),
                        filename="screenshot.png",
                    ),
                    reply_markup=keyboard,
                )
            except Exception:
                pass  # Screenshot unchanged or message too old


# --- Streaming response / notifications ---


_TURN_END_STOP_REASONS = frozenset({"end_turn", "stop_sequence"})


async def _build_context_footer(
    user_id: int, thread_id: int | None, window_id: str
) -> str | None:
    """Read the latest usage for a window's session and render a footer.

    Returns ``None`` when no usage has been observed yet (e.g. brand-new
    session, post-/clear, JSONL not yet flushed). The footer is a snapshot
    of context size at the moment the assistant turn ended, so a user
    scrolling back through history sees how context evolved turn by turn.

    Routes through ``busy_indicator.update_context_usage`` so the 1M-cap
    latch is shared with the topic-title indicator — otherwise a session
    that previously crossed 200k but is now back to 80k would show
    ``80k / 1M`` in the title and ``80k / 200k`` in the footer.
    """
    if not window_id:
        return None

    session = await session_manager.resolve_session_for_window(window_id)
    if session is None or not session.file_path:
        return None

    from .handlers import busy_indicator
    from .handlers.topic_title import format_max, format_tokens
    from .transcript_parser import read_latest_usage

    latest = read_latest_usage(session.file_path)
    if latest is None:
        return None

    route = (user_id, thread_id or 0, window_id)
    busy_indicator.update_context_usage(route, latest.tokens, latest.model)
    usage = busy_indicator.context_usage(route)
    if usage is None:
        return None
    return f"_📊 {format_tokens(usage.tokens)} / {format_max(usage.max_tokens)}_"


async def handle_new_message(msg: NewMessage, bot: Bot) -> None:
    """Handle a new assistant message — enqueue for sequential processing.

    Messages are queued per-user to ensure status messages always appear last.
    Routes via thread_bindings to deliver to the correct topic.
    """
    logger.info(
        f"handle_new_message: session={msg.session_id}, text_len={len(msg.text)}"
    )

    # Find users whose thread-bound window matches this session
    active_users = await session_manager.find_users_for_session(msg.session_id)

    if not active_users:
        logger.info(f"No active users for session {msg.session_id}")
        return

    for user_id, wid, thread_id in active_users:
        # Handle interactive tools specially - capture terminal and send UI.
        # Sub-agent (sidechain) tool calls are routed to the per-sub-agent
        # digest regardless of tool name; their interactive prompts don't
        # surface to the parent topic — only the top-level Agent
        # tool_use/tool_result pair does.
        if (
            msg.subagent_key is None
            and msg.tool_name in INTERACTIVE_TOOL_NAMES
            and msg.content_type == "tool_use"
        ):
            # Mark interactive mode BEFORE sleeping so polling skips this window
            set_interactive_mode(user_id, wid, thread_id)
            # Cache the structured input so the status-poller safety-net path
            # (which has only pane text) can also render the full option list
            # via the JSONL payload when it dispatches handle_interactive_ui.
            if msg.tool_name == "AskUserQuestion":
                remember_ask_tool_input(wid, msg.tool_input)
            # Flush pending content for THIS route only — unrelated topics
            # must not delay the interactive prompt.
            queue = get_content_queue((user_id, thread_id or 0, wid))
            if queue:
                await queue.join()
            # Wait briefly for Claude Code to render the question UI
            await asyncio.sleep(0.3)
            handled = await handle_interactive_ui(
                bot, user_id, wid, thread_id, tool_input=msg.tool_input
            )
            if handled:
                # Update user's read offset
                session = await session_manager.resolve_session_for_window(wid)
                if session and session.file_path:
                    try:
                        file_size = Path(session.file_path).stat().st_size
                        session_manager.update_user_window_offset(
                            user_id, wid, file_size
                        )
                    except OSError:
                        pass
                continue  # Don't send the normal tool_use message
            else:
                # UI not rendered — clear the early-set mode
                clear_interactive_mode(user_id, thread_id)

        # Any non-interactive message means the interaction is complete — delete
        # all UI cards (single OR multi-tab). PR 3 added ``has_interactive_surface``
        # to cover both maps; ``get_interactive_msg_id`` alone missed multi-tab
        # sessions and left their cards orphaned in chat.
        if has_interactive_surface(user_id, thread_id):
            await clear_interactive_msg(user_id, bot, thread_id)
            forget_ask_tool_input(wid)

        # Skip tool call notifications when CC_TELEGRAM_SHOW_TOOL_CALLS=false
        if not config.show_tool_calls and msg.content_type in (
            "tool_use",
            "tool_result",
        ):
            continue

        # Per-turn context footer. Appended at send-time (no edits later)
        # so MarkdownV2 is rendered once and forgotten — assistant text
        # bubbles end up with a small "_ctx 113k/200k_" line on the last
        # block of each turn. Sub-agent (sidechain) blocks are skipped:
        # their context budget is independent and the footer would clutter
        # the per-sub-agent digest.
        text = msg.text
        if (
            config.context_in_message_footer
            and msg.role == "assistant"
            and msg.content_type == "text"
            and msg.stop_reason in _TURN_END_STOP_REASONS
            and msg.subagent_key is None
        ):
            footer = await _build_context_footer(user_id, thread_id, wid)
            if footer:
                text = f"{text}\n\n{footer}"

        parts = build_response_parts(
            text,
            msg.content_type,
            msg.role,
        )

        # Enqueue content message task
        # Note: tool_result editing is handled inside _process_content_task
        # to ensure sequential processing with tool_use message sending
        await enqueue_content_message(
            bot=bot,
            user_id=user_id,
            window_id=wid,
            parts=parts,
            tool_use_id=msg.tool_use_id,
            content_type=msg.content_type,
            text=msg.text,
            thread_id=thread_id,
            image_data=msg.image_data,
            tool_name=msg.tool_name,
            tool_input=msg.tool_input,
            transcript_uuid=msg.transcript_uuid,
            subagent_key=msg.subagent_key,
        )

        # Update user's read offset to current file position
        # This marks these messages as "read" for this user
        session = await session_manager.resolve_session_for_window(wid)
        if session and session.file_path:
            try:
                file_size = Path(session.file_path).stat().st_size
                session_manager.update_user_window_offset(user_id, wid, file_size)
            except OSError:
                pass


# --- App lifecycle ---


async def post_init(application: Application) -> None:
    global \
        session_monitor, \
        _status_poll_task, \
        _typing_action_task, \
        _message_refs_gc_task

    # §2.5.3 Stage 5.c: open the persistent provenance DB before anything
    # else can write to it. ``post_init`` runs before the message handlers
    # are exposed, so no race against ``topic_send``'s fire-and-forget
    # insert tasks.
    await message_refs.init_db(config.message_refs_db_path)

    # Telegram resolves the bot's command menu by scope priority. In a
    # forum / group chat the order is, roughly:
    #   chat → chat_administrators → all_chat_administrators
    #     → all_group_chats → default
    # Whichever scope has commands set wins; only "no commands at this
    # scope" falls through. A prior deploy that ever called
    # set_my_commands with ``all_chat_administrators`` (Stage 1 of this
    # bot did exactly that) leaves an orphan menu that shadows
    # ``all_group_chats`` forever for any admin user — including the bot
    # owner, who is always an admin of their own forum.
    # ``delete_my_commands`` without a scope only clears Default, so we
    # explicitly walk every scope we've ever published to and clear it,
    # then re-set the two scopes we actually want.
    for scope in (
        BotCommandScopeDefault(),
        BotCommandScopeAllGroupChats(),
        BotCommandScopeAllPrivateChats(),
        BotCommandScopeAllChatAdministrators(),
    ):
        await application.bot.delete_my_commands(scope=scope)

    # Per-chat scopes (``chat`` and ``chat_administrators``) outrank the
    # ``all_*`` family, so an old menu set on a specific chat shadows the
    # one we just installed for every admin in that chat. We never set
    # these scopes intentionally, but earlier deploys did, and Telegram
    # has no "delete every per-chat scope" API call. Walk the persisted
    # ``group_chat_ids`` map for the set of chats this bot has ever
    # touched and clear both per-chat scopes there. Idempotent: deleting
    # an already-empty scope is a no-op success on the Telegram side.
    # Group chat scopes — keys in ``group_chat_ids`` are
    # ``"user_id:thread_id"`` and values are chat_ids. We need the
    # distinct (user_id, chat_id) pairs for ``chat_member`` (the
    # highest-priority scope of all — set on a specific user in a
    # specific chat) and the distinct chat_ids for ``chat`` /
    # ``chat_administrators``.
    seen_chats: set[int] = set()
    seen_user_chat: set[tuple[int, int]] = set()
    for key, chat_id in session_manager.group_chat_ids.items():
        seen_chats.add(chat_id)
        try:
            user_id_str, _ = key.split(":", 1)
            seen_user_chat.add((int(user_id_str), chat_id))
        except (ValueError, AttributeError):
            continue

    chat_scopes: list[BotCommandScopeChat | BotCommandScopeChatAdministrators] = []
    for chat_id in seen_chats:
        chat_scopes.append(BotCommandScopeChat(chat_id=chat_id))
        chat_scopes.append(BotCommandScopeChatAdministrators(chat_id=chat_id))
    member_scopes = [
        BotCommandScopeChatMember(chat_id=chat_id, user_id=user_id)
        for (user_id, chat_id) in seen_user_chat
    ]

    for scope in (*chat_scopes, *member_scopes):
        try:
            await application.bot.delete_my_commands(scope=scope)
        except Exception as e:
            # Telegram returns 400 BadRequest if the bot isn't a member
            # of the chat anymore (group archived, kicked, etc.) or the
            # user has blocked the bot. Don't let one stale chat block
            # startup.
            logger.warning(
                "delete_my_commands failed for scope=%s: %s",
                scope.type,
                e,
            )

    bot_commands = [
        BotCommand("start", "Show welcome message"),
        BotCommand("history", "Message history for this topic"),
        BotCommand("screenshot", "Terminal screenshot with control keys"),
        BotCommand("esc", "Send Escape to interrupt Claude"),
        BotCommand("kill", "Kill session, leave topic open"),
        BotCommand("unbind", "Unbind topic from session (keeps window running)"),
        BotCommand("usage", "Show Claude Code usage remaining"),
    ]
    # Add Claude Code slash commands
    for cmd_name, desc in CC_COMMANDS.items():
        bot_commands.append(BotCommand(cmd_name, desc))

    await application.bot.set_my_commands(bot_commands)
    await application.bot.set_my_commands(
        bot_commands, scope=BotCommandScopeAllGroupChats()
    )

    # Re-resolve stale window IDs from persisted state against live tmux windows
    await session_manager.resolve_stale_ids()

    # Pre-fill global rate limiter bucket on restart.
    # AsyncLimiter starts at _level=0 (full burst capacity), but Telegram's
    # server-side counter persists across bot restarts.  Setting _level=max_rate
    # forces the bucket to start "full" so capacity drains in naturally (~1s).
    # AIORateLimiter has no per-private-chat limiter, so max_retries is the
    # primary protection (retry + pause all concurrent requests on 429).
    rate_limiter = application.bot.rate_limiter
    if rate_limiter and rate_limiter._base_limiter:
        rate_limiter._base_limiter._level = rate_limiter._base_limiter.max_rate
        logger.info("Pre-filled global rate limiter bucket")

    monitor = SessionMonitor()

    async def message_callback(msg: NewMessage) -> None:
        await handle_new_message(msg, application.bot)

    monitor.set_message_callback(message_callback)

    # Stage 3: event-driven RunState. Gated together with the digest /
    # status_polling RunState reads — flipping the flag wires both ends so
    # the indicator never updates state without affecting any UI surface.
    if config.busy_indicator_v2:

        async def event_callback(event: TranscriptEvent) -> None:
            active = await session_manager.find_users_for_session(event.session_id)
            if not active:
                return
            routes: list[busy_indicator.Route] = [
                (user_id, thread_id or 0, wid) for user_id, wid, thread_id in active
            ]
            await busy_indicator.on_transcript_event(event, routes)
            # End-of-turn-question "🔔 Awaiting your reply" card with Yes/No
            # quick-replies removed 2026-05-17 at user request: it fired on
            # any assistant turn that ended with ``?``, producing a Yes/No
            # presumption that was misleading for list-selection questions
            # ("Which of those would you change?"). Real AskUserQuestion
            # tool calls still surface as full interactive pickers via
            # ``handle_interactive_ui``; plain-text questions no longer get
            # a half-card with the wrong action shape.

        monitor.set_event_callback(event_callback)

        # Replay tool_use/tool_result pairs from each tracked parent JSONL so
        # tools that were open at the moment of bot shutdown (most painfully,
        # long-running sub-agent Task calls) are visible to the busy indicator
        # immediately. Without this, ``_open_tools`` is empty after restart
        # and routes stay IDLE_CLEARED until the parent emits a fresh event —
        # for an in-flight Task that means no typing indicator for the entire
        # sub-agent runtime, since sub-agents write to a separate JSONL.
        seeded_routes = 0
        for sid, tracked in monitor.state.tracked_sessions.items():
            pending = await asyncio.to_thread(
                busy_indicator.parse_pending_tools_from_jsonl, tracked.file_path
            )
            if not pending:
                continue
            active = await session_manager.find_users_for_session(sid)
            for user_id, wid, thread_id in active:
                route: busy_indicator.Route = (user_id, thread_id or 0, wid)
                busy_indicator.seed_open_tools(route, pending)
                seeded_routes += 1
        if seeded_routes:
            logger.info(
                "Replayed pending tool state for %d route(s) at startup",
                seeded_routes,
            )

    monitor.start()
    session_monitor = monitor
    logger.info("Session monitor started")

    # Start status polling task
    _status_poll_task = asyncio.create_task(status_poll_loop(application.bot))
    logger.info("Status polling task started")

    # Start typing-action refresher (V2 only; the function no-ops on V1)
    _typing_action_task = asyncio.create_task(typing_action_loop(application.bot))
    logger.info("Typing-action task started")

    # §2.5.3 Stage 5.c: daily GC. Drift in cadence is fine — the only goal
    # is keeping the table proportional to retention, not exact daily.
    _message_refs_gc_task = asyncio.create_task(_message_refs_gc_loop(application.bot))
    logger.info("message_refs GC task started")


async def post_shutdown(application: Application) -> None:
    global _status_poll_task, _typing_action_task, _message_refs_gc_task

    # Stop status polling
    if _status_poll_task:
        _status_poll_task.cancel()
        try:
            await _status_poll_task
        except asyncio.CancelledError:
            pass
        _status_poll_task = None
        logger.info("Status polling stopped")

    # Stop typing-action refresher
    if _typing_action_task:
        _typing_action_task.cancel()
        try:
            await _typing_action_task
        except asyncio.CancelledError:
            pass
        _typing_action_task = None
        logger.info("Typing-action task stopped")

    # Stop the message_refs GC loop before closing the DB so a tick in
    # flight can finish its prune cleanly.
    if _message_refs_gc_task:
        _message_refs_gc_task.cancel()
        try:
            await _message_refs_gc_task
        except asyncio.CancelledError:
            pass
        _message_refs_gc_task = None
        logger.info("message_refs GC task stopped")

    # Stop all queue workers
    await shutdown_workers()

    if session_monitor:
        session_monitor.stop()
        logger.info("Session monitor stopped")

    await close_transcribe_client()

    # Close the message_refs DB last so any final fire-and-forget insert
    # tasks scheduled by the queue workers above can drain.
    await message_refs.close()


def create_bot() -> Application:
    application = (
        Application.builder()
        .token(config.telegram_bot_token)
        .rate_limiter(AIORateLimiter(max_retries=5))
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("screenshot", screenshot_command))
    application.add_handler(CommandHandler("esc", esc_command))
    application.add_handler(CommandHandler("unbind", unbind_command))
    application.add_handler(CommandHandler("kill", kill_command))
    application.add_handler(CommandHandler("usage", usage_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    # Topic closed event — auto-kill associated window
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.FORUM_TOPIC_CLOSED,
            topic_closed_handler,
        )
    )
    # Topic edited event — sync renamed topic to tmux window
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.FORUM_TOPIC_EDITED,
            topic_edited_handler,
        )
    )
    # Forward any other /command to Claude Code
    application.add_handler(MessageHandler(filters.COMMAND, forward_command_handler))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )
    # Photos: download and forward file path to Claude Code
    application.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    # Voice: transcribe via OpenAI and forward text to Claude Code
    application.add_handler(MessageHandler(filters.VOICE, voice_handler))
    # Documents: download and forward file path to Claude Code (≤20 MB)
    application.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    # Catch-all: non-text content (stickers, video, etc.)
    application.add_handler(
        MessageHandler(
            ~filters.COMMAND & ~filters.TEXT & ~filters.StatusUpdate.ALL,
            unsupported_content_handler,
        )
    )

    return application
