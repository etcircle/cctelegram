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
from cctelegram.handlers.message_sender import safe_edit

from . import window_lease

EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
EFFORT_LABELS: dict[str, str] = {
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra High",
    "max": "Max",
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
            await query.answer("Invalid data")
            return
        if level not in EFFORT_LEVELS:
            await query.answer("Invalid level")
            return
        if await reject_stale_window_callback(window_id):
            return
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await query.answer("Window no longer exists", show_alert=True)
            return
        label = EFFORT_LABELS[level]
        # Disable markup before dispatch — guards against rapid double-tap
        # under PTB concurrent_updates. The same edit also stands in for a
        # "sending" toast.
        await safe_edit(query, f"⏳ Setting effort to {label}…", reply_markup=None)
        await query.answer()
        # Mirror forward_command_handler's send sequence so /effort follows
        # the same per-route ordering as a regular slash command.
        route = (user.id, cb_thread_id or 0, window_id)
        await aggregator_flush_route(route)
        success, send_msg = await session_manager.send_to_window(
            window_id, f"/effort {level}"
        )
        if success:
            if adapters.config.busy_indicator_v2:
                await adapters.busy_indicator.mark_inbound_sent(route)
            if adapters.config.route_runtime_v2:
                await route_runtime.mark_inbound_sent(route)
            await safe_edit(query, f"✓ Effort set to {label}", reply_markup=None)
        else:
            await safe_edit(query, f"❌ {send_msg}", reply_markup=None)
