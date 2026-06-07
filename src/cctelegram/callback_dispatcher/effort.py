"""Build and execute Claude Code effort-level callbacks.

Core responsibilities:
  - Build /effort inline keyboards with callback-data window leases.
  - Own CB_EFFORT execution and stale-window rejection.
  - Send effort commands through the same route ordering as slash commands.

Key components:
  - EFFORT_LEVELS
  - EFFORT_LABELS
  - build_effort_keyboard()
  - execute_effort_callback()
"""

from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from cctelegram.handlers.callback_data import CB_EFFORT
from cctelegram.handlers.inbound_aggregator import aggregator_flush_route
from cctelegram.handlers.message_queue import set_route_user_turn_at
from cctelegram.handlers.message_sender import safe_edit

from . import safe_answer, window_lease

EFFORT_LEVELS: tuple[str, ...] = (
    "auto",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultracode",
)
EFFORT_LABELS: dict[str, str] = {
    "auto": "Auto",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra High",
    "max": "Max",
    "ultracode": "Ultracode",
}


def build_effort_keyboard(window_id: str) -> InlineKeyboardMarkup:
    """Build the inline keyboard for /effort level selection."""

    def btn(level: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            EFFORT_LABELS[level],
            callback_data=f"{CB_EFFORT}{level}:{window_id}",
        )

    return InlineKeyboardMarkup(
        [
            [btn("low"), btn("medium"), btn("high")],
            [btn("xhigh"), btn("max")],
            [btn("auto"), btn("ultracode")],
        ]
    )


async def execute_effort_callback(authorized: Any, adapters: Any) -> None:
    user = authorized.ctx.user
    query = authorized.ctx.query
    data = authorized.command.data
    cb_thread_id = authorized.ctx.thread_id
    lease = window_lease(authorized, adapters)
    session_manager = adapters.session_manager
    tmux_manager = adapters.tmux_manager
    route_runtime = adapters.route_runtime

    async def reject_stale_window_callback(window_id: str) -> bool:
        return await lease.reject_stale_window(window_id)

    # Effort level picker — set Claude Code reasoning effort for the session.
    # callback_data: eff:<level>:<window_id>.  window_id is embedded so a
    # stale button after topic rebind hits reject_stale_window_callback.
    if data.startswith(CB_EFFORT):
        rest = data[len(CB_EFFORT) :]
        try:
            level, window_id = rest.split(":", 1)
        except ValueError:
            await safe_answer(query, "Invalid data")
            return
        if level not in EFFORT_LEVELS:
            await safe_answer(query, "Invalid level")
            return
        if await reject_stale_window_callback(window_id):
            return
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await safe_answer(query, "Window no longer exists", show_alert=True)
            return
        label = EFFORT_LABELS[level]
        # Disable markup before dispatch — guards against rapid double-tap
        # under PTB concurrent_updates. The same edit also stands in for a
        # "sending" toast.
        await safe_edit(query, f"⏳ Setting effort to {label}…", reply_markup=None)
        await safe_answer(query)
        # Mirror forward_command_handler's send sequence so /effort follows
        # the same per-route ordering as a regular slash command.
        route = (user.id, cb_thread_id or 0, window_id)
        await aggregator_flush_route(route)
        # Item 3 / P2-1: stamp the user-turn delivery instant PRE-SEND, mirroring
        # forward_command_handler. /effort itself is a config toggle that rarely
        # streams prose + a picker, but stamping here keeps the turn boundary
        # uniform across both slash-command delivery seams (zero-cost).
        set_route_user_turn_at(user.id, cb_thread_id or 0, window_id)
        success, send_msg = await session_manager.send_to_window(
            window_id, f"/effort {level}"
        )
        if success:
            await route_runtime.mark_inbound_sent(route)
            await safe_edit(query, f"✓ Effort set to {label}", reply_markup=None)
        else:
            await safe_edit(query, f"❌ {send_msg}", reply_markup=None)
