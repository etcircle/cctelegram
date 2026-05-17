"""Unified cleanup API for topic state.

Provides centralized cleanup functions that coordinate state cleanup across
all modules, preventing memory leaks when topics are deleted.

Functions:
  - clear_topic_state: Clean up all memory state for a specific topic
"""

import logging
from pathlib import Path
from typing import Any

from telegram import Bot

from . import attention
from .inbound_aggregator import aggregator_clear_route
from .interactive_ui import clear_interactive_msg
from .message_queue import (
    clear_status_msg_info,
    clear_tool_msg_ids_for_topic,
    routes_for_topic,
    teardown_route,
)


logger = logging.getLogger(__name__)


def _delete_pending_attachment_files(attachments: list[Any]) -> None:
    """Best-effort deletion for pending-route attachment objects."""
    for attachment in attachments:
        path = getattr(attachment, "path", None)
        if path is None and isinstance(attachment, dict):
            path = attachment.get("path")
        if not isinstance(path, (str, Path)):
            continue
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as e:
            logger.debug("failed to delete pending attachment %s: %s", path, e)


async def clear_topic_state(
    user_id: int,
    thread_id: int,
    bot: Bot | None = None,
    user_data: dict[str, Any] | None = None,
    *,
    drop_pending: bool = True,
) -> None:
    """Clear all memory state associated with a topic.

    Callers must pass ``drop_pending`` per §2.1.2:
      - Topic close + window still alive → ``drop_pending=False`` (pending
        content may still be useful when topic reopens).
      - Topic delete + window killed → ``drop_pending=True``.
      - Stale-binding GC (window killed externally) → ``drop_pending=True``.
    """
    # Tear down any per-route queue first so its in-flight task can record
    # _tool_msg_ids before we sweep them below.
    for route in routes_for_topic(user_id, thread_id):
        # §2.8: drop any pending aggregator bundle for this route so a
        # debounced flush can't fire into a torn-down window after the
        # queue is gone.
        aggregator_clear_route(route)
        await teardown_route(route, drop_pending=drop_pending)

    # Clear status message tracking
    clear_status_msg_info(user_id, thread_id)

    # Clear tool message ID tracking
    clear_tool_msg_ids_for_topic(user_id, thread_id)

    # Clear interactive UI state (also deletes message from chat)
    await clear_interactive_msg(user_id, bot, thread_id)

    # Drop any live attention card state — fresh topic gets a fresh episode.
    attention.clear(user_id, thread_id)

    # Drop the status-polling idle counter so a re-bound topic starts fresh.
    # Lazy import: status_polling already imports from this module.
    from .status_polling import reset_idle_counter

    reset_idle_counter(user_id, thread_id)

    # Clear pending thread state from user_data
    if user_data is not None:
        if user_data.get("_pending_thread_id") == thread_id:
            attachments = list(user_data.get("_pending_thread_attachments") or [])
            if drop_pending:
                _delete_pending_attachment_files(attachments)
            user_data.pop("_pending_thread_id", None)
            user_data.pop("_pending_thread_text", None)
            user_data.pop("_pending_thread_attachments", None)
