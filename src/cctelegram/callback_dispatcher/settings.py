"""Build and execute the per-user output-verbosity settings panel (/settings).

Core responsibilities:
  - Render the /settings panel text + inline keyboard from the user's
    RESOLVED OutputPrefs (stored override > env default > preset).
  - Own CB_SETTINGS execution: owner check (a second allowed user tapping
    someone else's panel mutates NOTHING), token validation against
    output_prefs, persistence via SessionManager, in-place re-render.
  - Settings are user-scoped, not window-scoped: the panel works in any
    topic (bound or unbound) and in DM; no window lease involved.

Key components:
  - settings_command()  (the /settings CommandHandler target)
  - build_settings_keyboard()
  - render_settings_text()
  - execute_settings_callback()
"""

from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from cctelegram.config import config
from cctelegram.handlers import output_prefs
from cctelegram.handlers.callback_data import CB_SETTINGS, checked_callback_data
from cctelegram.handlers.message_sender import safe_edit, safe_send
from cctelegram.handlers.output_prefs import OutputPrefs
from cctelegram.session import session_manager

from . import safe_answer

PRESET_LABELS: dict[str, str] = {
    "verbose": "Verbose",
    "standard": "Standard",
    "compact": "Compact",
    "quiet": "Quiet",
}
LINE_LABELS: dict[str, str] = {"64": "Short", "160": "Medium", "400": "Full"}

WRONG_OWNER_TEXT = "This control isn't yours."


def _btn(label: str, field: str, value: str, owner_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        label,
        callback_data=checked_callback_data(f"{CB_SETTINGS}{field}:{value}:{owner_id}"),
    )


def build_settings_keyboard(prefs: OutputPrefs, owner_id: int) -> InlineKeyboardMarkup:
    """Inline keyboard: preset rows + the tappable knob rows (✅ = current)."""

    def preset_btn(name: str) -> InlineKeyboardButton:
        mark = "✅ " if prefs.verbosity == name else ""
        return _btn(f"{mark}{PRESET_LABELS[name]}", "preset", name, owner_id)

    def line_btn(token: str) -> InlineKeyboardButton:
        mark = "✅ " if str(prefs.digest_line_chars) == token else ""
        return _btn(f"{mark}{LINE_LABELS[token]} ({token})", "lines", token, owner_id)

    def done_btn(token: str, label: str) -> InlineKeyboardButton:
        mark = "✅ " if prefs.digest_on_done == token else ""
        return _btn(f"{mark}{label}", "done", token, owner_id)

    def subcards_btn(token: str, label: str) -> InlineKeyboardButton:
        mark = "✅ " if prefs.subagent_cards == token else ""
        return _btn(f"{mark}{label}", "subcards", token, owner_id)

    echo_flip = "off" if prefs.user_echo else "on"
    footer_flip = "off" if prefs.context_footer else "on"
    return InlineKeyboardMarkup(
        [
            [preset_btn("verbose"), preset_btn("standard")],
            [preset_btn("compact"), preset_btn("quiet")],
            [line_btn("64"), line_btn("160"), line_btn("400")],
            [
                done_btn("keep", "Done: keep"),
                done_btn("summary", "collapse"),
                done_btn("delete", "delete"),
            ],
            [
                subcards_btn("keep", "Subagents: keep"),
                subcards_btn("summary", "collapse"),
                subcards_btn("off", "off"),
            ],
            [
                _btn(
                    f"👤 Echo: {'on' if prefs.user_echo else 'off'}",
                    "echo",
                    echo_flip,
                    owner_id,
                ),
                _btn(
                    f"📊 Footer: {'on' if prefs.context_footer else 'off'}",
                    "footer",
                    footer_flip,
                    owner_id,
                ),
            ],
        ]
    )


def render_settings_text(prefs: OutputPrefs) -> str:
    """Panel body — what the current resolution actually does."""
    lines = [
        "⚙️ *Output settings*",
        f"Preset: *{PRESET_LABELS.get(prefs.verbosity, prefs.verbosity)}*",
        f"Tool line length: {prefs.digest_line_chars} chars · "
        f"live lines: {prefs.digest_live_lines}",
        f"Done card: {prefs.digest_on_done} · sub-agent cards: {prefs.subagent_cards}",
        f"👤 Echo: {'on' if prefs.user_echo else 'off'} · "
        f"📊 Footer: {'on' if prefs.context_footer else 'off'}",
        "",
        "Applies to messages this bot sends *to you*, in every topic.",
    ]
    return "\n".join(lines)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/settings`` — post the output-verbosity panel for the invoking user.

    User-scoped: works in bound topics, unbound topics, and DM. Must be
    registered BEFORE the catch-all command forwarder in bot.py or Telegram
    users would type "/settings" straight into the tmux pane.
    """
    user = update.effective_user
    if not user or not config.is_user_allowed(user.id):
        return
    msg = update.message
    if msg is None:
        return
    prefs = output_prefs.resolve(user.id)
    thread_id = (
        msg.message_thread_id if getattr(msg, "is_topic_message", False) else None
    )
    await safe_send(
        context.bot,
        msg.chat_id,
        render_settings_text(prefs),
        message_thread_id=thread_id,
        reply_markup=build_settings_keyboard(prefs, user.id),
    )


async def execute_settings_callback(authorized: Any, adapters: Any) -> None:
    """Handle a ``stg:<field>:<value>:<owner_id>`` tap."""
    user = authorized.ctx.user
    query = authorized.ctx.query
    data = authorized.command.data

    rest = data[len(CB_SETTINGS) :]
    parts = rest.split(":")
    if len(parts) != 3:
        await safe_answer(query, "Invalid data")
        return
    field, value, owner_str = parts
    try:
        owner_id = int(owner_str)
    except ValueError:
        await safe_answer(query, "Invalid data")
        return

    # Owner check: settings panels are personal. The dispatcher already
    # gated on the global allowed-users list; this rejects a DIFFERENT
    # allowed user mutating someone else's preferences.
    if user.id != owner_id:
        await safe_answer(query, WRONG_OWNER_TEXT, show_alert=True)
        return

    sm = adapters.session_manager if adapters is not None else session_manager
    if field == "preset":
        if value not in output_prefs.PRESETS:
            await safe_answer(query, "Invalid preset")
            return
        # Preset tap = clean slate: drop stale per-knob overrides so the
        # panel shows exactly the preset's behavior.
        sm.replace_user_settings(user.id, {"verbosity": value})
    elif field in output_prefs.KNOB_CHOICES:
        choices = output_prefs.KNOB_CHOICES[field]
        if value not in choices:
            await safe_answer(query, "Invalid value")
            return
        sm.set_user_setting(user.id, field, choices[value])
    else:
        await safe_answer(query, "Invalid data")
        return

    prefs = output_prefs.resolve(user.id)
    await safe_edit(
        query,
        render_settings_text(prefs),
        reply_markup=build_settings_keyboard(prefs, user.id),
    )
    await safe_answer(query, "Saved ✓")
