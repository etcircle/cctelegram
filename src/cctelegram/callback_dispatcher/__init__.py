"""Dispatch Telegram callback queries through a parse/authorize/execute seam.

Core responsibilities:
  - Parse raw callback data into callback commands with protocol limits enforced.
  - Authorize the initial Telegram user/topic lease before execution.
  - Route execution through registry-owned family modules with revalidation guards.

Key components: parse(), authorize_initial(), execute(), DispatcherAdapters.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from telegram import Update
from telegram.ext import ContextTypes


# ``checked_callback_data`` now lives on the dependency-free leaf
# ``handlers/callback_data.py`` (it was previously defined here, which dragged
# ``interactive_ui`` into the interactive_ui ↔ callback_dispatcher ↔
# inbound_telegram import cycle). Re-exported here so the intra-package
# ``from . import checked_callback_data`` in ``screenshot.py`` keeps resolving.
from cctelegram.handlers.callback_data import checked_callback_data  # noqa: F401

from cctelegram.handlers.directory_browser import (  # noqa: E402
    STATE_KEY,
    clear_browse_state,
)
from cctelegram.callback_dispatcher.registry import lookup  # noqa: E402
from cctelegram.handlers.inbound_telegram import (  # noqa: E402
    _clear_pending_route_payload,
    _get_thread_id,
    _is_ignored_stale_thread_id,
    _pending_owner_matches,
    _pending_thread_id,
)

STALE_CALLBACK_TEXT = "This button is stale for this topic — refresh the picker."
WRONG_USER_PICK_TEXT = "This control isn't yours."

logger = logging.getLogger(__name__)


# safe_answer is owned by cctelegram.handlers.message_sender (alongside
# safe_edit / safe_send / safe_reply). We re-export it here so existing
# `from . import safe_answer` imports in family modules keep working without
# creating a callback_dispatcher → handlers/inbound_telegram → ... → dispatcher
# cycle when handlers/inbound_telegram needs the helper too.
from cctelegram.handlers.message_sender import safe_answer  # noqa: F401, E402


@dataclass(frozen=True)
class RawCallbackCommand:
    """Parsed callback command preserving the raw callback payload."""

    data: str


CallbackCommand = RawCallbackCommand


@dataclass(frozen=True)
class InvalidCallback:
    """Parse failure returned for malformed external callback data."""

    reason: str


@dataclass(frozen=True)
class UpdateContext:
    """Initial callback context captured before command execution starts."""

    update: Update
    context: ContextTypes.DEFAULT_TYPE
    user: Any
    query: Any
    user_id: int
    thread_id: int | None


@dataclass(frozen=True)
class CallbackLease:
    """Callback lease that revalidates topic/window ownership at use time."""

    query: Any
    session_manager: Any
    user_id: int
    thread_id: int | None

    async def reject_stale_window(self, window_id: str) -> bool:
        """Return True after answering when a window callback is stale."""
        if await revalidate_before_tmux_send(
            self.query, self.session_manager, self.user_id, self.thread_id, window_id
        ):
            return False
        return True


@dataclass(frozen=True)
class AuthorizedCommand:
    """A lease proving the callback was initially allowed for this user/topic."""

    command: CallbackCommand
    ctx: UpdateContext


@dataclass(frozen=True)
class Rejected:
    """Authorization rejection with the already-answered callback reason."""

    reason: str


@dataclass(frozen=True)
class CallbackResult:
    """Result marker for a dispatched callback execution."""

    handled: bool = True


@dataclass(frozen=True)
class DispatcherAdapters:
    """Runtime dependencies injected into callback execution."""

    session_manager: Any
    tmux_manager: Any
    bot: Any
    route_runtime: Any
    config: Any
    terminal_parser: Any


def parse(data: bytes) -> CallbackCommand | InvalidCallback:
    """Parse callback bytes and reject payloads beyond Telegram's 64-byte cap."""
    if len(data) > 64:
        return InvalidCallback("callback_data exceeds Telegram 64-byte limit")
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError:
        return InvalidCallback("callback_data is not valid utf-8")
    if not decoded:
        return InvalidCallback("callback_data is empty")
    return RawCallbackCommand(decoded)


async def dispatch_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    adapters: DispatcherAdapters,
    *,
    is_user_allowed_func: Any,
) -> CallbackResult:
    """Run the full parse → authorize → execute flow for a Telegram callback."""
    query = update.callback_query
    if not query or not query.data:
        return CallbackResult(False)
    command = parse(query.data.encode("utf-8"))
    if isinstance(command, InvalidCallback):
        await safe_answer(query, "Invalid data", show_alert=True)
        return CallbackResult(False)
    user = update.effective_user
    if not user or not is_user_allowed_func(user.id):
        await safe_answer(query, "Not authorized")
        return CallbackResult(False)
    thread_id = _get_thread_id(update)
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        adapters.session_manager.set_group_chat_id(user.id, thread_id, chat.id)
    authorized = authorize_initial(
        command,
        UpdateContext(update, context, user, query, user.id, thread_id),
    )
    if isinstance(authorized, Rejected):
        return CallbackResult(False)
    return await execute(authorized, adapters)


def authorize_initial(
    command: CallbackCommand | InvalidCallback, ctx: UpdateContext | Any
) -> AuthorizedCommand | Rejected:
    """Authorize the initial callback lease; command-specific guards revalidate later."""
    if isinstance(command, InvalidCallback):
        return Rejected(command.reason)
    if not getattr(ctx, "query", None) or not getattr(ctx, "user", None):
        return Rejected("missing callback context")
    return AuthorizedCommand(command, ctx)


Executor = Callable[[AuthorizedCommand, DispatcherAdapters], Awaitable[None]]


def _resolve_executor(data: str) -> Executor | None:
    """Resolve the registry-owned family executor for callback data."""
    entry = lookup(data)
    if entry is None:
        return None
    module = importlib.import_module(entry.executor_function_path)
    executor = getattr(module, entry.executor_name)
    return executor


async def execute(
    authorized: AuthorizedCommand | Rejected, adapters: DispatcherAdapters
) -> CallbackResult:
    """Execute an authorized callback lease through the registry-owned family."""
    if isinstance(authorized, Rejected):
        return CallbackResult(False)
    if authorized.command.data == "noop":
        await safe_answer(authorized.ctx.query)
        return CallbackResult(True)
    executor = _resolve_executor(authorized.command.data)
    if executor is None:
        return CallbackResult(False)
    await executor(authorized, adapters)
    return CallbackResult(True)


def window_lease(
    authorized: AuthorizedCommand, adapters: DispatcherAdapters
) -> CallbackLease:
    """Create the current callback lease for family executors."""
    return CallbackLease(
        authorized.ctx.query,
        adapters.session_manager,
        authorized.ctx.user.id,
        authorized.ctx.thread_id,
    )


async def _answer_stale_pending_thread_mismatch(
    query: Any,
    user_data: dict | None,
    callback_thread_id: int | None,
    answer_text: str,
    *,
    clear_picker_state: bool = False,
) -> None:
    """Answer a pending-thread mismatch without deleting newer replacement media."""
    if not _is_ignored_stale_thread_id(user_data, callback_thread_id):
        if clear_picker_state:
            clear_browse_state(user_data)
        if user_data is not None:
            _clear_pending_route_payload(user_data, delete_files=True)
    await safe_answer(query, answer_text, show_alert=True)


_PICKER_STALE_TOPIC_MISMATCH = "topic_mismatch"


def _validate_pending_picker_callback(
    user_data: dict | None,
    callback_thread_id: int | None,
    expected_states: tuple[str, ...],
) -> tuple[bool, int | None, str | None]:
    """Validate that a picker callback still owns the pending topic route."""
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


async def _answer_invalid_pending_picker_callback(query: Any, answer_text: str) -> None:
    """Answer a stale picker callback without mutating pending picker state."""
    await safe_answer(query, answer_text, show_alert=True)


async def revalidate_before_mutation(
    query: Any,
    context: Any,
    pending_thread_id: int | None,
    answer_text: str,
) -> bool:
    """Revalidate pending-topic ownership before mutating picker state."""
    if _pending_owner_matches(context.user_data, pending_thread_id):
        return True
    await _answer_invalid_pending_picker_callback(query, answer_text)
    return False


async def revalidate_before_tmux_send(
    query: Any,
    session_manager: Any,
    user_id: int,
    thread_id: int | None,
    window_id: str,
) -> bool:
    """Revalidate topic/window ownership before looking up or sending to tmux."""
    if _callback_window_is_current(session_manager, user_id, thread_id, window_id):
        return True
    await safe_answer(query, STALE_CALLBACK_TEXT, show_alert=True)
    return False


def owner_matches(entry: Any, user_id: int) -> bool:
    """Return True when an interactive pick token belongs to the clicking user."""
    return entry.user_id == user_id


def _callback_window_is_current(
    session_manager: Any, user_id: int, thread_id: int | None, window_id: str
) -> bool:
    """Return True when a callback's encoded window still owns the topic."""
    return session_manager.resolve_window_for_thread(user_id, thread_id) == window_id
