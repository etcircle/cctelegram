"""Scenario: per-user output verbosity settings (/settings panel + gates).

Plan v4 PR-1 (temp/2026-06-11-telegram-output-compactness-plan-v4.md): a
per-user ``OutputPrefs`` resolution (stored user override > env default >
preset) consulted at the Telegram emission points. These scenarios pin the
public seams:

  - /settings is a bot-owned command (never forwarded to tmux), renders an
    inline panel with ``stg:`` callbacks, and works in an UNBOUND topic —
    settings are user-scoped, not window-scoped.
  - A preset tap persists through SessionManager (state.json
    ``user_settings``) and re-renders the panel; a tap by a DIFFERENT
    allowed user on someone else's panel mutates nothing.
  - The 👤 user-echo gate is per-recipient: ``standard`` (echo off)
    suppresses a terminal-typed user entry for that user, ``verbose`` keeps
    it, and an external ``<task-notification>`` envelope is exempt (system
    event card, never gated).
  - The activity digest renders with the recipient's line budget
    (``standard`` = 160-char lines vs ``verbose`` = 400).

PR-1 is behavior-neutral at the ``verbose`` default — the existing scenario
floor pins that; these tests pin the non-default presets' PR-1 surface.
"""

from __future__ import annotations

import asyncio

import pytest
from telegram.ext import CommandHandler, MessageHandler

from cctelegram import bot as bot_module
from cctelegram.callback_dispatcher.settings import settings_command
from cctelegram.handlers import message_queue
from cctelegram.handlers.callback_data import CB_SETTINGS
from cctelegram.session_monitor import NewMessage
from tests.conftest import (
    ScenarioHarness,
    make_update_callback,
    make_update_command,
)

pytestmark = pytest.mark.scenario

_OWNER_ID = 12345
_OTHER_ID = 99999
_THREAD_ID = 42


async def _drain_route(route: tuple[int, int, str]) -> None:
    queue = message_queue.get_content_queue(route)
    if queue is not None:
        await queue.join()
    await asyncio.sleep(0)


def test_settings_handler_registered_before_command_forwarder() -> None:
    """/settings must be bot-owned — registered before the catch-all command
    forwarder, or Telegram users would type it straight into the tmux pane."""
    app = bot_module.create_bot()
    handlers = app.handlers[0]
    settings_idx = next(
        (
            i
            for i, h in enumerate(handlers)
            if isinstance(h, CommandHandler) and "settings" in h.commands
        ),
        None,
    )
    assert settings_idx is not None, "/settings CommandHandler is not registered"
    fwd_idx = next(
        i
        for i, h in enumerate(handlers)
        if isinstance(h, MessageHandler)
        and h.callback is bot_module.forward_command_handler
    )
    assert settings_idx < fwd_idx


@pytest.mark.asyncio
async def test_settings_command_renders_panel_in_unbound_topic(
    scenario: ScenarioHarness,
) -> None:
    """Settings are user-scoped: the panel renders even with no bound window,
    nothing reaches tmux, and every button is a namespaced ``stg:`` callback."""
    update = make_update_command("settings", thread_id=_THREAD_ID)
    await settings_command(update, scenario.context)

    assert scenario.tmux.sent_keys == []
    sends = [s for s in scenario.bot.sent if s.method == "send_message"]
    assert len(sends) == 1
    markup = sends[0].kwargs.get("reply_markup")
    assert markup is not None
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert buttons, "settings panel must carry an inline keyboard"
    assert all(b.callback_data.startswith(CB_SETTINGS) for b in buttons)
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons)
    # Preset row present, current preset marked.
    labels = [b.text for b in buttons]
    assert any("Standard" in label for label in labels)
    assert any("✅" in label for label in labels)


@pytest.mark.asyncio
async def test_settings_preset_tap_persists_and_rerenders(
    scenario: ScenarioHarness,
) -> None:
    update = make_update_callback(
        f"{CB_SETTINGS}preset:compact:{_OWNER_ID}",
        thread_id=_THREAD_ID,
        user_id=_OWNER_ID,
    )
    await bot_module.callback_handler(update, scenario.context)

    stored = scenario.session_manager.get_user_settings(_OWNER_ID)
    assert stored.get("verbosity") == "compact"
    update.callback_query.answer.assert_awaited()
    update.callback_query.edit_message_text.assert_awaited()


@pytest.mark.asyncio
async def test_settings_tap_by_other_user_mutates_nothing(
    scenario: ScenarioHarness,
) -> None:
    """A second ALLOWED user tapping someone else's panel must not mutate the
    owner's settings (hermes r1 P2-11 / plan §6 owner check)."""
    update = make_update_callback(
        f"{CB_SETTINGS}preset:quiet:{_OWNER_ID}",
        thread_id=_THREAD_ID,
        user_id=_OTHER_ID,
    )
    await bot_module.callback_handler(update, scenario.context)

    assert scenario.session_manager.get_user_settings(_OWNER_ID) == {}
    assert scenario.session_manager.get_user_settings(_OTHER_ID) == {}
    update.callback_query.answer.assert_awaited()
    answer_args = update.callback_query.answer.await_args
    assert "yours" in (answer_args.args[0] if answer_args.args else "")
    update.callback_query.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_echo_gated_per_recipient(scenario: ScenarioHarness) -> None:
    """``standard`` (echo off) suppresses a terminal-typed user entry; flipping
    back to ``verbose`` delivers the 👤 echo again. Gate is per-recipient in
    the bot fan-out — the monitor no longer drops user entries globally."""
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(
        thread_id=_THREAD_ID,
        window_id=wid,
        display_name="repo",
        cwd="/repo",
        session_id="sess-1",
    )
    route = (scenario.user_id, _THREAD_ID, wid)
    scenario.session_manager.set_user_setting(scenario.user_id, "verbosity", "standard")

    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="typed directly into the terminal",
            content_type="text",
            role="user",
        ),
        scenario.bot,
    )
    await _drain_route(route)
    assert scenario.bot.sent == [], "standard preset must suppress the 👤 echo"

    scenario.session_manager.set_user_setting(scenario.user_id, "verbosity", "verbose")
    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="typed directly into the terminal",
            content_type="text",
            role="user",
        ),
        scenario.bot,
    )
    await _drain_route(route)
    sends = [s for s in scenario.bot.sent if s.method == "send_message"]
    assert len(sends) == 1
    assert "👤" in sends[0].kwargs["text"]


@pytest.mark.asyncio
async def test_task_notification_envelope_exempt_from_echo_gate(
    scenario: ScenarioHarness,
) -> None:
    """External <task-notification> envelopes are system events — their card
    is delivered even when the recipient's echo pref is off."""
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(
        thread_id=_THREAD_ID,
        window_id=wid,
        display_name="repo",
        cwd="/repo",
        session_id="sess-1",
    )
    route = (scenario.user_id, _THREAD_ID, wid)
    scenario.session_manager.set_user_setting(scenario.user_id, "verbosity", "standard")

    envelope = (
        "<task-notification><task-id>b12345</task-id>"
        "<summary>Background command completed</summary></task-notification>"
    )
    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text=envelope,
            content_type="text",
            role="user",
        ),
        scenario.bot,
    )
    await _drain_route(route)
    sends = [s for s in scenario.bot.sent if s.method == "send_message"]
    assert len(sends) == 1
    assert "b12345" in sends[0].kwargs["text"]
    assert "👤" not in sends[0].kwargs["text"]


@pytest.mark.asyncio
async def test_quiet_preset_suppresses_dispatch_but_keeps_agent_report(
    scenario: ScenarioHarness,
) -> None:
    """Quiet: no 🤖 dispatch bubble, no digest card off the Agent counter
    path (hermes r3 P1-1) — but the 🤖✅ report STILL arrives (codex r2
    P1-1: the _agent_tool_ids stash happens even with the bubble off)."""
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(
        thread_id=_THREAD_ID,
        window_id=wid,
        display_name="repo",
        cwd="/repo",
        session_id="sess-1",
    )
    route = (scenario.user_id, _THREAD_ID, wid)
    scenario.session_manager.set_user_setting(scenario.user_id, "verbosity", "quiet")

    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="**Agent**(explore the repo)",
            content_type="tool_use",
            tool_use_id="agent-t1",
            tool_name="Agent",
            tool_input={
                "subagent_type": "Explore",
                "description": "explore the repo",
                "prompt": "Find the rendering seams.",
            },
            role="assistant",
        ),
        scenario.bot,
    )
    await _drain_route(route)
    assert scenario.bot.sent == [], "quiet must suppress the dispatch bubble"

    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="The rendering seams are in message_queue.py.",
            content_type="tool_result",
            tool_use_id="agent-t1",
            tool_name="Agent",
            role="assistant",
        ),
        scenario.bot,
    )
    await _drain_route(route)
    sends = [s for s in scenario.bot.sent if s.method == "send_message"]
    assert len(sends) == 1, "the 🤖✅ report must survive quiet"
    assert "🤖✅" in sends[0].kwargs["text"]
    assert "rendering seams" in sends[0].kwargs["text"]
    assert not any(
        "Activity:" in s.kwargs.get("text", "") for s in scenario.bot.sent
    ), "no digest card may be created off the Agent counter path under quiet"


@pytest.mark.asyncio
async def test_legacy_env_tool_calls_false_drops_agent_surfaces(
    scenario: ScenarioHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The faithful legacy mapping: an EXPLICIT CC_TELEGRAM_SHOW_TOOL_CALLS=
    false drops ALL tool surfaces including the 🤖 dispatch/report — exactly
    like the old global gate (plan v4 §4; contrast with quiet above)."""
    from cctelegram.config import config as live_config

    monkeypatch.setattr(live_config, "env_show_tool_calls_set", True)
    monkeypatch.setattr(live_config, "show_tool_calls", False)

    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(
        thread_id=_THREAD_ID,
        window_id=wid,
        display_name="repo",
        cwd="/repo",
        session_id="sess-1",
    )
    route = (scenario.user_id, _THREAD_ID, wid)

    for msg in (
        NewMessage(
            session_id="sess-1",
            text="**Agent**(explore the repo)",
            content_type="tool_use",
            tool_use_id="agent-t1",
            tool_name="Agent",
            tool_input={"subagent_type": "Explore", "description": "x"},
            role="assistant",
        ),
        NewMessage(
            session_id="sess-1",
            text="report text",
            content_type="tool_result",
            tool_use_id="agent-t1",
            tool_name="Agent",
            role="assistant",
        ),
    ):
        await bot_module.handle_new_message(msg, scenario.bot)
        await _drain_route(route)

    assert scenario.bot.sent == []

    # Hermes PR-1 review P1: the env is a DEFAULT, not a ceiling — once the
    # user picks a preset via /settings, tool surfaces come back.
    scenario.session_manager.set_user_setting(scenario.user_id, "verbosity", "verbose")
    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="**Agent**(explore again)",
            content_type="tool_use",
            tool_use_id="agent-t2",
            tool_name="Agent",
            tool_input={"subagent_type": "Explore", "description": "again"},
            role="assistant",
        ),
        scenario.bot,
    )
    await _drain_route(route)
    sends = [s for s in scenario.bot.sent if s.method == "send_message"]
    assert any("🤖 Subagent dispatched" in s.kwargs.get("text", "") for s in sends)


@pytest.mark.asyncio
async def test_digest_line_budget_follows_recipient_preset(
    scenario: ScenarioHarness,
) -> None:
    """``standard`` caps digest lines at 160 chars where ``verbose`` keeps 400.
    Same tool turn, different recipients' budgets (multi-user fan-out keys
    digest state per (user, thread) so prefs apply per recipient)."""
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(
        thread_id=_THREAD_ID,
        window_id=wid,
        display_name="repo",
        cwd="/repo",
        session_id="sess-1",
    )
    route = (scenario.user_id, _THREAD_ID, wid)
    scenario.session_manager.set_user_setting(scenario.user_id, "verbosity", "standard")

    long_cmd = "x" * 380  # under verbose's 400, over standard's 160
    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text=f"**Bash**({long_cmd})",
            content_type="tool_use",
            tool_use_id="t1",
            tool_name="Bash",
            role="assistant",
        ),
        scenario.bot,
    )
    await _drain_route(route)
    # Flush the LIVE card (PR-2's standard preset collapses the body on
    # finalize, so the line budget is observable only while running).
    await message_queue._flush_activity_digest_now(
        scenario.bot, scenario.user_id, _THREAD_ID
    )

    digest_sends = [
        s
        for s in scenario.bot.sent
        if s.method == "send_message" and "Activity:" in s.kwargs.get("text", "")
    ]
    assert digest_sends, "the live digest must have been painted"
    digest_text = digest_sends[0].kwargs["text"]
    line = next(ln for ln in digest_text.split("\n") if "Bash" in ln)
    # 160-char budget: the rendered line is truncated with … well below the
    # verbose 400 budget (line carries "• ⚙️ " chrome on top of the raw cap).
    assert len(line) <= 170
    assert "…" in line
