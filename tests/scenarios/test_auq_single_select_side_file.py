"""Scenario coverage for single-select AskUserQuestion side-file picks.

Exercises the public Telegram callback seam for the live AUQ regression where
render minted ``aqp:`` tokens from the PreToolUse side file but validation fell
back to pane-only parsing, making long/compressed pickers permanently bounce.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cctelegram import terminal_parser
from cctelegram.callback_dispatcher import DispatcherAdapters, dispatch_callback
from cctelegram.handlers import auq_source, interactive_ui, pick_token
from cctelegram.handlers.callback_data import CB_ASK_PICK
from cctelegram.utils import app_dir
from tests.conftest import ScenarioHarness, make_update_callback

pytestmark = pytest.mark.scenario

_SESSION_ID = "22222222-2222-4222-8222-222222222222"

_DESCRIPTION_1 = (
    "Use the safe rollout lane only after confirming the callback source parity "
    "fix and keeping the separate context message as the place for detailed "
    "rationale."
)
_DESCRIPTION_2 = (
    "Ship the live hotfix path: the side-file form owns the complete numbered "
    "options while the compressed tmux pane proves the validator still checks "
    "the current screen before sending keys."
)
_DESCRIPTION_3 = (
    "Defer the unrelated cleanup lane because this regression is live and scope "
    "must stay surgical."
)


def _single_select_input() -> dict[str, Any]:
    return {
        "questions": [
            {
                "question": "Choose the AUQ picker hotfix lane.",
                "header": "Hotfix lane",
                "multiSelect": False,
                "options": [
                    {"label": "A) Safe rollout", "description": _DESCRIPTION_1},
                    {"label": "B) Live picker fix", "description": _DESCRIPTION_2},
                    {"label": "C) Cleanup later", "description": _DESCRIPTION_3},
                ],
            }
        ]
    }


def _compressed_pane() -> str:
    return """← ☐ Hotfix lane  ✔ Submit →
Choose the AUQ picker hotfix lane.

❯ 2. B) Live picker fix
Enter to select · ↑/↓ to navigate · Esc to cancel
"""


def _compressed_pane_different_question_same_labels() -> str:
    return """← ☐ Approval path  ✔ Submit →
Choose the production approval path.

❯ 2. B) Live picker fix
Enter to select · ↑/↓ to navigate · Esc to cancel
"""


def _compressed_pane_title_absent_same_labels() -> str:
    return """← ☐ Hotfix lane  ✔ Submit →
❯ 2. B) Live picker fix
Enter to select · ↑/↓ to navigate · Esc to cancel
"""


def _bind(scenario: ScenarioHarness, pane: str) -> str:
    wid = scenario.add_window(window_name="repo", cwd="/repo", pane_text=pane)
    scenario.bind_thread(
        42,
        wid,
        display_name="repo",
        cwd="/repo",
        session_id=_SESSION_ID,
    )
    return wid


def _write_side_file(tool_input: dict[str, Any]) -> Path:
    pending = app_dir() / "auq_pending"
    pending.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = pending / f"{_SESSION_ID}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": _SESSION_ID,
                "tool_use_id": "tool-use-single-select",
                "written_at": time.time(),
                "tool_input": tool_input,
            }
        )
    )
    return path


def _adapters(scenario: ScenarioHarness) -> DispatcherAdapters:
    return DispatcherAdapters(
        session_manager=scenario.session_manager,
        tmux_manager=scenario.tmux,
        bot=scenario.bot,
        route_runtime=SimpleNamespace(),
        config=SimpleNamespace(),
        terminal_parser=terminal_parser,
    )


async def _render(scenario: ScenarioHarness, wid: str) -> None:
    assert await interactive_ui.handle_interactive_ui(
        scenario.bot,
        scenario.user_id,
        wid,
        42,
        tmux_mgr=scenario.tmux,
        session_mgr=scenario.session_manager,
    )


async def _tap(scenario: ScenarioHarness, callback_data: str) -> None:
    update = make_update_callback(
        callback_data,
        thread_id=42,
        user_id=scenario.user_id,
        chat_id=scenario.chat_id,
    )
    await dispatch_callback(
        update,
        scenario.context,
        _adapters(scenario),
        is_user_allowed_func=lambda _uid: True,
    )


def _picker_text(scenario: ScenarioHarness) -> str:
    for sent in reversed(scenario.bot.sent):
        if sent.kwargs.get("reply_markup") is not None:
            return str(sent.kwargs.get("text") or "")
    raise AssertionError("no picker card recorded")


def _context_text(scenario: ScenarioHarness) -> str:
    for sent in scenario.bot.sent:
        text = str(sent.kwargs.get("text") or "")
        if text.startswith("📋 AskUserQuestion — full details"):
            return text
    raise AssertionError("no AUQ context message recorded")


def _pick_callbacks(scenario: ScenarioHarness) -> list[str]:
    for sent in reversed(scenario.bot.sent):
        markup = sent.kwargs.get("reply_markup")
        if markup is not None:
            return [b.callback_data for row in markup.inline_keyboard for b in row]
    raise AssertionError("no reply markup recorded")


def _token(callback_data: str) -> str:
    assert callback_data.startswith(CB_ASK_PICK)
    return callback_data.removeprefix(CB_ASK_PICK).split(":")[-1]


@pytest.mark.asyncio
async def test_single_select_side_file_fingerprint_dispatch_and_compact_card(
    scenario: ScenarioHarness,
) -> None:
    pane = _compressed_pane()
    wid = _bind(scenario, pane)
    _write_side_file(_single_select_input())

    await _render(scenario, wid)

    picks = [cb for cb in _pick_callbacks(scenario) if cb.startswith(CB_ASK_PICK)]
    assert len(picks) == 3

    entry = pick_token.peek(_token(picks[1]))
    assert entry is not None
    resolved_input = auq_source.resolve_auq_source(wid, None, pane).payload
    current_form = terminal_parser.resolve_ask_form(resolved_input, pane)
    assert current_form is not None
    assert entry.fingerprint == current_form.fingerprint()

    picker = _picker_text(scenario)
    assert "Choose the AUQ picker hotfix lane." in picker
    assert "1. A) Safe rollout" in picker
    assert "2. B) Live picker fix" in picker
    assert "3. C) Cleanup later" in picker
    assert _DESCRIPTION_1 not in picker
    assert _DESCRIPTION_2 not in picker
    assert _DESCRIPTION_3 not in picker

    context = _context_text(scenario)
    assert "Use the safe rollout lane" in context
    assert "Ship the live hotfix path" in context
    assert "Defer the unrelated cleanup lane" in context

    await _tap(scenario, picks[1])

    assert scenario.tmux.sent_keys[-2:] == [
        (wid, "2", False, True),
        (wid, "Enter", False, False),
    ]
    assert "Form changed, refreshing." not in [
        str(sent.kwargs.get("text") or "") for sent in scenario.bot.sent
    ]


@pytest.mark.asyncio
async def test_single_select_compressed_pane_rejects_stale_same_labels_title_mismatch(
    scenario: ScenarioHarness,
) -> None:
    wid = _bind(scenario, _compressed_pane())
    _write_side_file(_single_select_input())

    await _render(scenario, wid)
    picks = [cb for cb in _pick_callbacks(scenario) if cb.startswith(CB_ASK_PICK)]
    assert len(picks) == 3

    stale_record = auq_source._read_pretool_side_file(_SESSION_ID)
    assert stale_record is not None
    different_pane = _compressed_pane_different_question_same_labels()
    different_form = terminal_parser.resolve_ask_form(None, different_pane)
    assert different_form is not None
    assert (
        different_form.current_question_title == "Choose the production approval path."
    )
    assert not different_form.options_contiguous_from_one()
    assert auq_source._record_consistent_with_pane(stale_record, different_form) == (
        False,
        "title_mismatch",
    )

    scenario.tmux.set_pane(wid, different_pane)
    await _tap(scenario, picks[1])

    assert scenario.tmux.sent_keys == []


@pytest.mark.asyncio
async def test_single_select_compressed_pane_matching_title_still_dispatches(
    scenario: ScenarioHarness,
) -> None:
    pane = _compressed_pane()
    wid = _bind(scenario, pane)
    _write_side_file(_single_select_input())

    await _render(scenario, wid)
    picks = [cb for cb in _pick_callbacks(scenario) if cb.startswith(CB_ASK_PICK)]
    assert len(picks) == 3

    record = auq_source._read_pretool_side_file(_SESSION_ID)
    assert record is not None
    pane_form = terminal_parser.resolve_ask_form(None, pane)
    assert pane_form is not None
    assert pane_form.current_question_title == "Choose the AUQ picker hotfix lane."
    assert not pane_form.options_contiguous_from_one()
    assert auq_source._record_consistent_with_pane(record, pane_form) == (
        True,
        "ok",
    )

    await _tap(scenario, picks[1])
    assert scenario.tmux.sent_keys[-2:] == [
        (wid, "2", False, True),
        (wid, "Enter", False, False),
    ]


@pytest.mark.asyncio
async def test_single_select_compressed_pane_title_absent_still_dispatches(
    scenario: ScenarioHarness,
) -> None:
    pane = _compressed_pane_title_absent_same_labels()
    wid = _bind(scenario, pane)
    _write_side_file(_single_select_input())

    await _render(scenario, wid)
    picks = [cb for cb in _pick_callbacks(scenario) if cb.startswith(CB_ASK_PICK)]
    assert len(picks) == 3

    record = auq_source._read_pretool_side_file(_SESSION_ID)
    assert record is not None
    pane_form = terminal_parser.resolve_ask_form(None, pane)
    assert pane_form is not None
    assert pane_form.current_question_title is None
    assert not pane_form.options_contiguous_from_one()
    assert auq_source._record_consistent_with_pane(record, pane_form) == (
        True,
        "ok",
    )

    await _tap(scenario, picks[1])
    assert scenario.tmux.sent_keys[-2:] == [
        (wid, "2", False, True),
        (wid, "Enter", False, False),
    ]
