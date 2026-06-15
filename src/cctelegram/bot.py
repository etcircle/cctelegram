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
  - Topic lifecycle: closing a topic kills the associated window
    (topic_closed_handler). Renames sync through topic_edited_handler.
    Unsupported content (stickers, etc.) is rejected with a warning
    (unsupported_content_handler).
  - Inbound text/photo/voice/document handlers live in
    ``handlers.inbound_telegram`` (re-imported below so the original
    ``bot.<name>`` attribute access still resolves to the same function
    objects for tests and a few module-level lookups).
  - Bot lifecycle management: post_init, post_shutdown, create_bot.

Key functions: create_bot(), handle_new_message().
"""

import asyncio
import io
import logging
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
    Update,
)
from telegram.constants import ChatAction
from telegram.error import BadRequest, Conflict, Forbidden, NetworkError
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
from .callback_dispatcher import DispatcherAdapters, dispatch_callback
from .callback_dispatcher.effort import build_effort_keyboard as _build_effort_keyboard
from .callback_dispatcher.interactive import _lock_busy
from .callback_dispatcher.settings import settings_command
from .callback_dispatcher.screenshot import (
    build_screenshot_keyboard as _build_screenshot_keyboard,
)
from .handlers.directory_browser import (
    clear_browse_state,
)
from .handlers import output_prefs
from .handlers.auq_ledger import release_window as auq_ledger_release_window
from .handlers.cleanup import clear_topic_state
from .handlers.dashboard import clear_dashboards_in_thread, dashboard_command
from .handlers.history import send_history
from .handlers.inbound_aggregator import (
    aggregator_flush_route,
)

# Re-export everything extracted to ``handlers.inbound_telegram`` so
# ``cctelegram.bot.<name>`` keeps resolving for tests and module-level
# lookups; ``noqa: F401`` covers re-exports not used by bot.py directly.
from .handlers.inbound_telegram import (  # noqa: F401
    PendingAttachment,
    _abort_created_window_after_pending_owner_change,
    _apply_reply_context,
    _bash_capture_tasks,
    _cancel_bash_capture,
    _capture_bash_output,
    _cleanup_unbound_created_window,
    _clear_pending_route_payload,
    _clear_pending_route_payload_for_thread,
    _clear_picker_state_for_current_state,
    _create_and_bind_window,
    _delete_pending_attachment_files,
    _FILES_DIR,
    _flush_pending_route_payload,
    _forget_ignored_stale_thread_id,
    _get_thread_id,
    _IGNORED_STALE_THREAD_IDS_KEY,
    _IMAGES_DIR,
    _is_ignored_stale_thread_id,
    _list_unbound_windows,
    _pending_owner_matches,
    _pending_thread_id,
    _remember_ignored_stale_thread_id,
    _sanitize_filename_part,
    aggregator_clear_route,
    aggregator_offer_document,
    aggregator_offer_photo,
    aggregator_offer_text,
    aggregator_offer_voice,
    AggregatorReplayAttachment,
    aggregator_replay_payload,
    document_handler,
    extract_reply_context,
    is_user_allowed,
    photo_handler,
    render_for_claude,
    reply_context_mod,
    text_handler,
    transcribe_voice,
    voice_handler,
)
from .handlers.interactive_ui import (
    clear_interactive_mode,
    clear_interactive_msg,
    forget_ask_tool_input,
    handle_interactive_ui,
    has_interactive_surface,
    maybe_upgrade_auq_context_message,
    remember_ask_tool_input,
    set_interactive_mode,
)
from .handlers.message_queue import (
    enqueue_content_message,
    get_content_queue,
    probe_topic_liveness,
    set_route_user_turn_at,
    shutdown_workers,
)
from . import message_refs
from .handlers.message_sender import (
    safe_edit,  # noqa: F401 - re-exported for callback dispatcher override tests
    safe_reply,
)
from .handlers.response_builder import build_response_parts, is_task_notification
from .handlers.status_polling import status_poll_loop, typing_action_loop
from . import route_runtime, terminal_parser, transcript_event_adapter
from .handlers import pane_signals
from .screenshot import text_to_image
from .session import session_manager
from .session_monitor import (
    NewMessage,
    ParentSidechainActivity,
    SessionMonitor,
    TranscriptEvent,
)
from .tmux_manager import tmux_manager
from .transcribe import close_client as close_transcribe_client

logger = logging.getLogger(__name__)

# Session monitor instance
session_monitor: SessionMonitor | None = None

# Status polling task
_status_poll_task: asyncio.Task | None = None

# Typing-action refresher (V2 indicator only). Decoupled from status_poll_loop
# so its cadence stays under Telegram's 5s typing TTL regardless of how many
# bindings exist or how slow tmux capture_pane is.
_typing_action_task: asyncio.Task | None = None


async def _telegram_error_handler(update: object, context: object) -> None:
    err = getattr(context, "error", None)
    msg = str(err) if err else ""
    # Case-fold the substring match so PTB capitalization variants
    # ("Query id is invalid" vs "query id is invalid") all route to INFO.
    folded = msg.casefold()
    if isinstance(err, BadRequest) and (
        "query is too old" in folded
        or "query id is invalid" in folded
        or "message is not modified" in folded
        or "message to edit not found" in folded
    ):
        logger.info("telegram_stale_callback: %s", msg)
        return
    if isinstance(err, Forbidden):
        # User blocked the bot, or bot kicked from the chat. Not actionable
        # from our side — log at INFO for forensic visibility, do not retry.
        logger.info("telegram_forbidden: %s", msg)
        return
    if isinstance(err, Conflict):
        # Another bot instance is polling the same token. This is a real ops
        # bug (the duplicate-restart.sh incident shape) — surface loudly so
        # we notice fast.
        logger.critical("telegram_conflict_multiple_pollers: %s", msg, exc_info=err)
        return
    if isinstance(err, NetworkError) and not isinstance(err, BadRequest):
        logger.warning("telegram_network_error: %s", msg)
        return
    # Unknown — log with traceback like today
    logger.error("telegram_unhandled_error: %s", err, exc_info=err)


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
    "effort": "↗ Set reasoning effort level",
}


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

    png_bytes = await text_to_image(text)
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

    # Wave 3b reject-if-held (Hermes R2 P1-1 — NO bypass): while a multi-
    # keystroke pane transaction (e.g. an AUQ pick dispatch) holds the
    # window's send lock, an Escape slipping in mid-transaction could dismiss
    # the picker between nav-verify and Enter and make ``_classify_advance``
    # read the disappearance as a confirmed advance — a FALSE ``dispatched``.
    # The locked section is bounded (~2s of settles + captures), so the brake
    # is briefly delayed, never lost. The ``_lock_busy`` check (held OR live
    # waiters — the release→waiter-wakeup gap counts as busy, Hermes Wave-3b
    # P2-1) is immediately followed by the acquire with no await between them
    # (atomic on the event loop — a genuine try-acquire); the single Escape is
    # sent UNDER the lock and all Telegram I/O happens after release (the lock
    # is a leaf).
    lock = tmux_manager.window_send_lock(w.window_id)
    if _lock_busy(lock):
        await safe_reply(
            update.message, "⏳ Action in progress — try again in a second"
        )
        return
    async with lock:
        # Send Escape control character (no enter)
        sent = await tmux_manager.send_keys(w.window_id, "\x1b", enter=False)
    if not sent:
        await safe_reply(update.message, "❌ Failed to send — window may be gone")
        return
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

    # Wave 3b compound transaction (Hermes P2-5): hold the window send lock
    # across the WHOLE send→settle→capture→dismiss sequence so no other writer
    # (a pick dispatch, user text, a control key) can land inside the /usage
    # modal window — and conversely /usage can't inject "/usage" + Escape into
    # someone else's in-flight transaction. Reject-if-held rather than queue:
    # blocking a user command behind a multi-second transaction would just
    # pile up surprise keystrokes. All Telegram replies happen strictly AFTER
    # release (the lock is a leaf — no Telegram I/O while held); the
    # ``_lock_busy`` check (held OR live waiters — the release→waiter-wakeup
    # gap counts as busy, Hermes Wave-3b P2-1) + acquire pair has no await
    # between them (atomic on the event loop — a genuine try-acquire).
    lock = tmux_manager.window_send_lock(w.window_id)
    if _lock_busy(lock):
        await safe_reply(update.message, "⏳ Window busy — try again in a second")
        return
    pane_text: str | None = None
    dismiss_ok = False
    async with lock:
        # Send /usage command to Claude Code TUI
        sent = await tmux_manager.send_keys(w.window_id, "/usage")
        if sent:
            # Wait for the modal to render
            await asyncio.sleep(2.0)
            # Capture the pane content
            pane_text = await tmux_manager.capture_pane(w.window_id)
            # Dismiss the modal
            dismiss_ok = await tmux_manager.send_keys(
                w.window_id, "Escape", enter=False, literal=False
            )
    if not sent or not dismiss_ok:
        # The window vanished mid-command — don't present the capture as
        # usage output with a modal possibly left stranded on the pane.
        await safe_reply(update.message, "❌ Failed to send — window may be gone")
        return

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


# --- Screenshot and effort callback keyboards ---

# Builders live in callback_dispatcher so the 64-byte callback-data assertion
# is colocated with callback parsing/execution.


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
        # Hermes Wave C review P2-4: a dedicated dashboard host topic has no
        # bound window, so the binding-centric clear_topic_state above never
        # runs for it — still drop any dashboard record hosted in this
        # (chat, thread). The closed-topic update carries the chat directly.
        chat = update.effective_chat
        clear_dashboards_in_thread(
            thread_id, chat_id=chat.id if chat is not None else None
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
    # The full text is already a slash command like "/clear" or "/compact foo".
    # Strip @botname from the command token only — preserving any trailing
    # args. e.g. "/effort@mybot max" -> "/effort max", not "/effort".
    parts_text = cmd_text.strip().split(None, 1)
    base = parts_text[0].split("@")[0] if parts_text else ""
    cc_slash = base + (" " + parts_text[1] if len(parts_text) > 1 else "")
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return

    # Intercept bare `/effort` (no args) and show an inline picker instead of
    # forwarding. Claude Code's TUI menu for /effort is invisible in Telegram,
    # so a Telegram-native picker is the only sane UX. `/effort low` etc.
    # still forwards via the normal path.
    if base == "/effort" and len(parts_text) == 1:
        await safe_reply(
            update.message,
            "Choose effort level:",
            reply_markup=_build_effort_keyboard(wid),
        )
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

    # Item 3 / P2-1: stamp the user-turn delivery instant PRE-SEND — a forwarded
    # slash command is a user turn that can make Claude produce prose + a picker,
    # so the live-prose freshness gate needs this turn boundary.
    set_route_user_turn_at(user.id, thread_id or 0, wid)
    success, message = await session_manager.send_to_window(wid, cc_slash)
    if success:
        await safe_reply(update.message, f"⚡ [{display}] Sent: {cc_slash}")
        # Mark route busy so the typing loop has something to refresh while
        # Claude processes the slash command (most slash commands never
        # produce a transcript event — /model opens a pane UI, /clear resets
        # state — so the JSONL signal alone cannot light the indicator).
        # ``status_polling.update_status_message`` calls
        # ``route_runtime.mark_interactive_pending`` on a pane-confirmed live
        # interactive surface (AUQ picker or ExitPlanMode plan-approval),
        # promoting RUNNING → WAITING_ON_USER while the interactive ``tool_use``
        # is buffered in JSONL. It is retracted by the transcript flush
        # (reclaim), the poller liveness reconciliation when no live surface
        # remains, or route teardown (``clear_route`` / ``mark_session_reset``).
        await route_runtime.mark_inbound_sent(route)
        # If /clear command was sent, clear the session association
        # so we can detect the new session after first message
        if cc_slash.strip().lower() == "/clear":
            logger.info("Clearing session for window %s after /clear", display)
            session_manager.clear_window_session(wid)
            # /clear rotates the session_id — drop any in-flight open_tools /
            # context_usage that belong to the dead session. route_runtime
            # exposes the intent directly so consumers see IDLE_CLEARED
            # immediately rather than waiting for the next poll cycle. The
            # context footer reads route_runtime.context_usage, so this reset
            # (which DROPS that cache) keeps the new session's footer from
            # rendering the dead session's 1M latch.
            await route_runtime.mark_session_reset(route)
            pane_signals.clear_route(route)  # GH #43: dead session's count

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


# --- Callback query handler ---


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route callback queries through the callback dispatcher package."""
    await dispatch_callback(
        update,
        context,
        DispatcherAdapters(
            session_manager=session_manager,
            tmux_manager=tmux_manager,
            bot=context.bot,
            route_runtime=route_runtime,
            config=config,
            terminal_parser=terminal_parser,
        ),
        is_user_allowed_func=is_user_allowed,
    )


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

    Reads context usage from ``route_runtime.snapshot(route).context_usage``
    (the single authority for both the write and the read). The 1M-cap latch
    lives in ``route_runtime.update_context_usage`` so a session that
    previously crossed 200k but is now back to 80k still shows ``80k / 1M``
    rather than dropping back to ``80k / 200k``.
    """
    if not window_id:
        return None

    session = await session_manager.resolve_session_for_window(window_id)
    if session is None or not session.file_path:
        return None

    from .handlers.topic_title import format_max, format_tokens
    from .transcript_parser import read_latest_usage

    latest = read_latest_usage(session.file_path)
    if latest is None:
        return None

    route = (user_id, thread_id or 0, window_id)
    route_runtime.update_context_usage(route, latest.tokens, latest.model)
    usage = route_runtime.snapshot(route).context_usage
    if usage is None:
        return None
    return f"_📊 {format_tokens(usage.tokens)} / {format_max(usage.max_tokens)}_"


async def apply_sidechain_activity(
    activity: dict[str, ParentSidechainActivity],
) -> None:
    """GH #44 fan-out (successor of Wave A's ``mark_subagent_activity_for_parents``):
    the monitor's per-tick background-agent signals → the keyed route_runtime
    marks.

    Called once per monitor tick (``pop_sidechain_activity``) AFTER the
    tick's parent lifecycle dispatch (§4.2 ordering). Resolves bound routes
    exactly like the parent event fan-out (``find_users_for_session``) and
    applies, per (route, agent_key) — never deduped at route level (codex r2
    P2-1: sibling agents in one tick must each get their marks):

      1. launch marks (``is_background`` provenance — before activity so a
         same-tick first batch records as background),
      2. activity marks (keyed keep-alive + projection input),
      3. done marks (sidechain end-of-turn and/or parent task-notification).

    Pull-only; no lifecycle ingestion, no send-layer authority.
    """
    for sid, rec in activity.items():
        active = await session_manager.find_users_for_session(sid)
        seen_routes: set[route_runtime.Route] = set()
        for user_id, wid, thread_id in active:
            route: route_runtime.Route = (user_id, thread_id or 0, wid)
            if route in seen_routes:
                continue
            seen_routes.add(route)
            for key in rec.launched:
                await route_runtime.mark_background_agent_launched(route, key)
            # ISSUE-6 / Fix 2c (DESIGN B): the Workflow bracket's mtime-advance
            # heartbeat — a SEPARATE channel from rec.ticks (run-state never
            # consumes a Workflow's sidechain entries). Placed after the launch
            # loop so a same-tick launch→heartbeat refreshes the just-registered
            # key, and before the completed loop so a same-tick close still
            # tombstones last.
            for key, mtime in rec.bracket_heartbeats.items():
                await route_runtime.mark_background_agent_activity(route, key, mtime)
            for key, tick in rec.ticks.items():
                await route_runtime.mark_background_agent_activity(
                    route, key, tick.max_event_ts
                )
            for key, tick in rec.ticks.items():
                if tick.saw_end_of_turn:
                    await route_runtime.mark_background_agent_done(route, key)
            for key in rec.completed:
                await route_runtime.mark_background_agent_done(route, key)


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
        prefs = output_prefs.resolve(user_id)

        # Per-recipient 👤 user-echo gate (plan v4 §4). Sits at the TOP of
        # the loop body, mirroring the monitor-level skip it replaced
        # (session_monitor previously dropped user entries globally on
        # CC_TELEGRAM_SHOW_USER_MESSAGES=false) — so a gated entry fires no
        # side effects, exactly like today's env-false behavior. External
        # <task-notification> envelopes are EXEMPT: they are system events
        # rendered as their own card, not an echo of the user's words.
        if (
            msg.role == "user"
            and not prefs.user_echo
            and not is_task_notification(msg.text)
        ):
            continue

        # Handle interactive tools specially - capture terminal and send UI.
        # Sub-agent (sidechain) tool calls are routed to the per-sub-agent
        # digest regardless of tool name; their interactive prompts don't
        # surface to the parent topic — only the top-level Agent
        # tool_use/tool_result pair does.
        if (
            msg.subagent_key is None
            and msg.tool_name in route_runtime.INTERACTIVE_TOOL_NAMES
            and msg.content_type == "tool_use"
        ):
            # Mark interactive mode BEFORE sleeping so polling skips this window
            set_interactive_mode(user_id, wid, thread_id)
            # Cache the structured input so the status-poller safety-net path
            # (which has only pane text) can also render the full option list
            # via the JSONL payload when it dispatches handle_interactive_ui.
            if msg.tool_name == "AskUserQuestion":
                remember_ask_tool_input(wid, msg.tool_input, msg.tool_use_id)
                # If a context message was previously posted from the
                # pane-derived form source (commit 603c6bc), this is
                # the moment the rich JSONL dict arrives — upgrade the
                # already-posted Telegram message(s) in place so the
                # user sees per-option descriptions.
                if isinstance(msg.tool_input, dict):
                    try:
                        await maybe_upgrade_auq_context_message(bot, wid)
                    except Exception as exc:  # pragma: no cover — defensive
                        logger.warning(
                            "maybe_upgrade_auq_context_message raised (window=%s): %s",
                            wid,
                            exc,
                        )
            # Flush pending content for THIS route only — unrelated topics
            # must not delay the interactive prompt.
            queue = get_content_queue((user_id, thread_id or 0, wid))
            if queue:
                await queue.join()
            # Wait briefly for Claude Code to render the question UI
            await asyncio.sleep(0.3)
            handled = await handle_interactive_ui(
                bot,
                user_id,
                wid,
                thread_id,
                tool_input=msg.tool_input,
                tmux_mgr=tmux_manager,
                session_mgr=session_manager,
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

        # AUQ ``tool_result`` always invalidates the cached ``tool_input`` AND
        # unlinks the PreToolUse side file — done BEFORE the awaited
        # ``clear_interactive_msg`` below so a raise in the card clear can't
        # leave the side file orphaned. An orphaned side file would make
        # status_polling's side-file gate (``side_file_live_for_session``)
        # preserve a DEAD card indefinitely until the next restart /
        # window-switch / topic-close — the uptime half of the dead-card class
        # (Codex round-2 P2, 2026-05-31).
        #
        # This is also why the invalidation is UNCONDITIONAL (not gated on
        # ``has_interactive_surface``): a card that status_polling's
        # absent-streak hysteresis cleared BEFORE the JSONL ``tool_result``
        # arrives would otherwise leave the cache pointing at the
        # just-completed AUQ, and the NEXT AUQ's render overlays the new pane
        # onto the completed question's options with pick-buttons suppressed
        # via ``current_tab_inferred=False`` (2026-05-21 09:30:21 incident on
        # @40 / msg 34563: D1+D2 multi-Q answered at 09:29:16, hysteresis
        # cleared the card, the ``tool_result`` arrived at 09:29:31 with no
        # surface so the cache stayed, and the D3 AUQ at 09:30:21 rendered as
        # stale-D1 verbatim text). ``forget_ask_tool_input`` is ``dict.pop`` +
        # an idempotent unlink — safe to call here and again in the clear
        # branch below.
        if (
            msg.role == "assistant"
            and msg.tool_name == "AskUserQuestion"
            and msg.content_type == "tool_result"
        ):
            forget_ask_tool_input(wid)
            # Wave 2 fix 3b (P1-1 placement): tombstone this window's
            # action-ledger rows ONLY here — the AUQ ``tool_result`` is the
            # positive resolution proof. The ledger key is content-derived
            # (no per-instance entropy), so a same-day byte-identical AUQ
            # reconstructs the same (route_hash, fp8, opt) triplet — without
            # the `released` tombstone a stale `dispatched` row would answer
            # "Action already received" forever. Deliberately NOT inside
            # ``forget_ask_tool_input`` (a generic teardown helper also fired
            # from `/clear` / session replacement / the generic surface clear
            # below — none of which prove resolution; releasing there would
            # remove the durable single-use brake on a dispatched-but-
            # UNRESOLVED instance). ExitPlanMode needs no release: ledger
            # rows are minted only by AUQ ``aqp:`` picks (pick buttons are
            # built only for ``content.name == "AskUserQuestion"``). The
            # crash window (bot down between the tool_result and this seam)
            # is covered by the startup reconciler's tool_result-proven
            # release in ``session_monitor``. WINDOW-scoped: a
            # double-`--resume` sibling window's unresolved card keeps its
            # rows.
            auq_ledger_release_window(wid)

        # Any non-interactive message means the interaction is complete —
        # delete the UI card. ``has_interactive_surface`` is the bool
        # predicate the cleanup gate is written against.
        if has_interactive_surface(user_id, thread_id):
            await clear_interactive_msg(
                user_id, bot, thread_id, session_mgr=session_manager
            )
            forget_ask_tool_input(wid)

        # Per-recipient legacy tool-call suppression — the faithful
        # CC_TELEGRAM_SHOW_TOOL_CALLS=false mapping (plan v4 §4): drops ALL
        # tool surfaces including Agent/Task, exactly like the old global
        # gate at this same position (the AUQ tool_result seam above stays
        # ahead of it). Presets never set tool_activity=False — quiet's
        # digest suppression lives in the digest path instead, so the 🤖✅
        # Agent report survives there (codex r2 P1-1).
        if not prefs.tool_activity and msg.content_type in (
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
            prefs.context_footer
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
            stop_reason=msg.stop_reason,
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

    # Wave A (Bug A — duplicate picker on restart) requires the
    # SessionManager.window_states[wid].session_id field to be populated
    # BEFORE hydrate_interactive_state runs its staleness checks.
    # SessionMonitor calls load_session_map() inside its polling loop
    # (session_monitor.py:1081), but that's too late — by then the first
    # poll has already fired and a stale _interactive_msgs lookup would
    # have missed the persisted entry. Call it explicitly here.
    await session_manager.load_session_map()

    # Hydrate interactive UI persisted state (Wave A, Bug A fix).
    # MUST run AFTER resolve_stale_ids() AND load_session_map() so
    # window-id remaps and session_id bindings are both visible to
    # hydrate's resolve_window_for_thread + session_id_for_window
    # lookups. MUST run BEFORE SessionMonitor() and the polling tasks
    # so the first poll cycle sees the restored _interactive_msgs map
    # and takes the edit-branch instead of fresh-send.
    from .handlers import auq_source
    from .handlers import interactive_ui as _interactive_ui
    from .handlers.interactive_ui import hydrate_interactive_state

    # Wire the auq_source JSONL-cache getter ONCE (R5). The neutral
    # auq_source leaf reads interactive_ui's in-process
    # ``_last_completed_ask_tool_input`` cache through this injected getter
    # so it never imports interactive_ui (no import cycle). The ``jsonl_cache``
    # resolver branch resolves to nothing until this is set.
    auq_source.set_jsonl_cache_getter(
        lambda wid: _interactive_ui._last_completed_ask_tool_input.get(wid)
    )

    hydrate_interactive_state(session_manager)

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
    # Wire bot reference for hydrate-time AUQ context-message upgrades
    # (Codex P2 round 3 #3, 2026-05-25). Without this, a form-source
    # context message persisted pre-restart wouldn't get its
    # descriptions edited in once the buffered AUQ is discovered in
    # JSONL — the normal bot.handle_new_message upgrade hook only fires
    # for NEW lines, and the buffered AUQ is already past the offset.
    monitor.set_bot(application.bot)

    # Event-driven RunState: drive route_runtime from the JSONL event stream.

    async def event_callback(event: TranscriptEvent) -> None:
        active = await session_manager.find_users_for_session(event.session_id)
        if not active:
            return
        routes: list[route_runtime.Route] = [
            (user_id, thread_id or 0, wid) for user_id, wid, thread_id in active
        ]
        # The adapter normalises ``TranscriptEvent`` →
        # ``TranscriptLifecycleEvent`` and fans out per route. Any per-route
        # failure is logged once-per-session and the remaining routes still
        # get the event.
        await transcript_event_adapter.dispatch_transcript_event(event, routes)
        # End-of-turn-question "🔔 Awaiting your reply" card with Yes/No
        # quick-replies removed 2026-05-17 at user request: it fired on
        # any assistant turn that ended with ``?``, producing a Yes/No
        # presumption that was misleading for list-selection questions
        # ("Which of those would you change?"). Real AskUserQuestion
        # tool calls still surface as full interactive pickers via
        # ``handle_interactive_ui``; plain-text questions no longer get
        # a half-card with the wrong action shape.

    monitor.set_event_callback(event_callback)

    # GH #44 (ex-Wave A): keyed sidechain/background-agent fan-out. The
    # monitor reports per-parent agent ticks + launch/completion signals each
    # tick; the fan-out applies the route_runtime marks so a long sub-agent
    # run survives transient confirmed-idle pane frames, a pane-false-cleared
    # route resurrects, and a run_in_background agent lifts its route to a
    # projected RUNNING (typing + 🟡 Busy) after the parent's end-of-turn.
    monitor.set_subagent_activity_callback(apply_sidechain_activity)

    # Replay tool_use/tool_result pairs from each tracked parent JSONL so
    # tools that were open at the moment of bot shutdown (most painfully,
    # long-running sub-agent Task calls) are visible to route_runtime
    # immediately. Without this, ``open_tools`` is empty after restart
    # and routes stay IDLE_CLEARED until the parent emits a fresh event —
    # for an in-flight Task that means no typing indicator for the entire
    # sub-agent runtime, since sub-agents write to a separate JSONL.
    seeded_routes = 0
    for sid, tracked in monitor.state.tracked_sessions.items():
        pending = await asyncio.to_thread(
            route_runtime.parse_pending_tools_from_jsonl, tracked.file_path
        )
        if not pending:
            continue
        active = await session_manager.find_users_for_session(sid)
        for user_id, wid, thread_id in active:
            route: route_runtime.Route = (user_id, thread_id or 0, wid)
            route_runtime.seed_open_tools(route, pending)
            seeded_routes += 1
    if seeded_routes:
        logger.info(
            "Replayed pending tool state for %d route(s) at startup",
            seeded_routes,
        )

    monitor.start()
    session_monitor = monitor
    logger.info("Session monitor started")

    # AUQ PreToolUse side-file maintenance:
    #   1. Garbage-collect stale files (>1h old) left over from crashes
    #      / kickstart-between-AUQs cases.
    #   2. Warn (with the actionable install command) if the PreToolUse
    #      hook entry is missing from ~/.claude/settings.json — the bot
    #      will still work in form-source-only mode, but the user will
    #      lose AUQ descriptions at pick time until they run
    #      `cc-telegram hook --install`.
    try:
        from .handlers.auq_source import gc_stale
        from .handlers.interactive_ui import warn_if_pre_tool_use_hook_missing

        # Liveness gate: Claude buffers the AskUserQuestion tool_use in JSONL
        # until the prompt resolves, so a live AUQ left open >1h has a
        # stale-mtime side file that is STILL its card's liveness authority.
        # Skip reaping it while the session is tracked (same predicate shape as
        # the md_capture.gc_stale callsite below). The side file stem IS the
        # <session_id> the monitor tracks; the predicate is INJECTED so
        # auq_source stays a leaf.
        gc_stale(is_live_session=lambda sid: monitor.state.get_session(sid) is not None)
        warn_if_pre_tool_use_hook_missing()
    except Exception as e:  # noqa: BLE001
        # Never let cleanup/maintenance crash bot startup.
        logger.warning("AUQ pretool startup maintenance raised: %s", e)

    # Notification side-file maintenance (Wave B busy-signal):
    #   1. Garbage-collect stale notify_pending/ files (>24h) left by
    #      crashes / notifications fired while the bot was down. Same
    #      injected-liveness conservative-skip shape as the AUQ GC above;
    #      a tracked session's file is left for the runtime TTL / teardown
    #      seams to reap.
    #   2. Warn (one-time, with the install command) if the Notification
    #      hook is missing — the existing hook-health seam extended (plan
    #      v3 B-misc): without it, Workflow/permission approval waits stay
    #      an eternal "🟡 Busy" instead of "🔔 Waiting on you".
    try:
        from .handlers import notify_source
        from .handlers.interactive_ui import warn_if_notification_hook_missing

        notify_source.gc_stale(
            is_live_session=lambda sid: monitor.state.get_session(sid) is not None
        )
        warn_if_notification_hook_missing()
    except Exception as e:  # noqa: BLE001
        logger.warning("Notification startup maintenance raised: %s", e)

    # MessageDisplay live-prose capture maintenance (Bug 2 — prose buffered
    # behind a live AUQ / ExitPlanMode):
    #   1. Write the bot-managed --settings file that registers the
    #      MessageDisplay hook (scoped to bot-launched sessions via
    #      `claude --settings`; merges with the global hooks).
    #   2. GC stale per-session capture files (>1h) left by crashes /
    #      kickstart-between-prompts.
    #   3. Self-check: warn if the settings file ended up WITHOUT the hook
    #      (e.g. an unwritable config dir). The bot still works — live prose
    #      silently falls back to post-resolution JSONL delivery.
    try:
        from . import md_capture

        md_capture.ensure_capture_settings()
        # Item 3 / P2-2: gate the startup reap on session liveness so a
        # long-open AUQ/EPM picker's capture file (which also carries its
        # shown_live/consumed dedup markers) is NOT reaped while the prompt is
        # still live — reaping it would drop the markers and double-post the
        # prose at resolution. The ndjson stem IS the ORIGINAL session id the
        # monitor tracks (under --resume the bot tracks the original id), so
        # tracked-session membership is the AUQ+EPM-covering liveness test. The
        # predicate is INJECTED (md_capture stays a leaf, never imports here).
        md_capture.gc_stale(
            is_live_session=lambda sid: monitor.state.get_session(sid) is not None
        )
        if not md_capture.capture_settings_has_message_display():
            logger.warning(
                "MessageDisplay capture settings missing the hook (%s); live "
                "prose before an AUQ/ExitPlanMode will fall back to "
                "post-resolution delivery. Check that %s is writable.",
                md_capture.capture_settings_path(),
                md_capture.app_dir(),
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("MessageDisplay capture startup maintenance raised: %s", e)

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

    application.add_error_handler(_telegram_error_handler)

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("screenshot", screenshot_command))
    application.add_handler(CommandHandler("esc", esc_command))
    application.add_handler(CommandHandler("unbind", unbind_command))
    application.add_handler(CommandHandler("kill", kill_command))
    application.add_handler(CommandHandler("usage", usage_command))
    # /dashboard MUST register before the catch-all command forwarder below —
    # it is a bot-owned command and must never be forwarded to Claude Code
    # (Wave C, pre-C/P2-5).
    application.add_handler(CommandHandler("dashboard", dashboard_command))
    # /settings is bot-owned for the same reason (plan v4 PR-1 / codex r1
    # P2-7): registered before the catch-all forwarder or it lands in tmux.
    application.add_handler(CommandHandler("settings", settings_command))
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
