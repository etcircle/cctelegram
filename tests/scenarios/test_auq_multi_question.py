"""Scenario coverage for multi-QUESTION AskUserQuestion picker delivery.

The bug: a single-select pick dispatched a bare digit AND a trailing ``Enter``.
On Claude Code v2.1.167 a bare digit already selects+advances (Q1 → Q2), so the
extra ``Enter`` auto-answered Q2 with its cursor-default and jumped to the
Submit review — Q2's live picker never reached the Telegram user.

This scenario drives the public callback seam (Update → real handler stack →
fake tmux / fake bot) with the real PII-scrubbed v2.1.167 captures, using a
keystroke-aware advancing fake tmux that encodes the verified TUI semantics:

    Q1     + "1"     -> Q2 pane              Q1     + "Enter" -> Q2 pane
    Q2     + "1"     -> Submit pane          Q2     + "Enter" -> Submit pane
    Submit + "1"     -> resolved/inert       Submit + "Enter" -> resolved/inert

so a stray ``Enter`` is what over-advances. Pre-fix: tapping Q1 sends ``1`` then
``Enter`` → lands on Submit → the card never shows Q2 (RED). Post-fix: only the
bare digit is sent → lands on Q2 (GREEN).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cctelegram import bot as bot_module, terminal_parser
from cctelegram.callback_dispatcher import DispatcherAdapters, dispatch_callback
from cctelegram.handlers import interactive_ui
from cctelegram.handlers.callback_data import CB_ASK_PICK
from cctelegram.session_monitor import NewMessage
from cctelegram.tmux_manager import tmux_manager as _real_tmux
from cctelegram.utils import app_dir
from tests.conftest import ScenarioHarness, make_update_callback

pytestmark = pytest.mark.scenario

_FIXTURES = Path(__file__).parents[1] / "cctelegram" / "fixtures"
_SESSION_ID = "33333333-3333-4333-8333-333333333333"

_Q1_FIXTURE = "auq_multiq_q1_pane.txt"
_Q2_FIXTURE = "auq_multiq_q2_after_pick_pane.txt"
_SUBMIT_FIXTURE = "auq_multiq_submit_pane.txt"

# A non-picker prompt — what the pane shows after the form resolves (Submit, or
# a single-question pick). A stray Enter landing here is a harmless no-op, which
# is why the old digit+Enter "accidentally worked" for single-question forms.
_RESOLVED_PANE = "user@host repo % \n"

# The inline 2-question tool_input (mirrors temp/auq-multiq-tool-input-reference.py).
_TOOL_INPUT: dict[str, Any] = {
    "questions": [
        {
            "question": (
                "Which implementation approach should we take for the new "
                "caching layer?"
            ),
            "header": "Approach",
            "multiSelect": False,
            "options": [
                {
                    "label": "Write-through cache with Redis backend",
                    "description": (
                        "Writes go to cache and datastore synchronously; strong "
                        "consistency, slightly higher write latency."
                    ),
                },
                {
                    "label": "Write-back cache with periodic flush",
                    "description": (
                        "Writes hit the cache first and flush to the datastore in "
                        "batches; faster writes, risk of data loss on crash."
                    ),
                },
                {
                    "label": "No cache, optimize queries instead",
                    "description": (
                        "Skip the caching layer entirely and improve query "
                        "performance directly; simpler, less infrastructure."
                    ),
                },
            ],
        },
        {
            "question": "How should we roll this out to production users?",
            "header": "Rollout",
            "multiSelect": False,
            "options": [
                {
                    "label": "Immediate full rollout to everyone",
                    "description": (
                        "Ship to 100% of users at once; fastest but highest blast "
                        "radius if something breaks."
                    ),
                },
                {
                    "label": "Gradual canary over one week",
                    "description": (
                        "Ramp traffic incrementally with monitoring; balances speed "
                        "and safety."
                    ),
                },
                {
                    "label": "Feature-flagged opt-in only",
                    "description": (
                        "Users explicitly enable it; safest, but slowest adoption "
                        "and limited real-world signal."
                    ),
                },
            ],
        },
    ]
}


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text()


# Verified-keystroke advance map: (current_pane_fixture, key) -> next_pane.
# Only the digit "1" and "Enter" are modeled (the test taps option 1 only).
_ADVANCE: dict[tuple[str, str], str] = {
    (_Q1_FIXTURE, "1"): _Q2_FIXTURE,
    (_Q1_FIXTURE, "Enter"): _Q2_FIXTURE,
    (_Q2_FIXTURE, "1"): _SUBMIT_FIXTURE,
    (_Q2_FIXTURE, "Enter"): _SUBMIT_FIXTURE,
    # Submit + 1/Enter resolves; the pane goes inert. Modeled as a sentinel.
    (_SUBMIT_FIXTURE, "1"): "__RESOLVED__",
    (_SUBMIT_FIXTURE, "Enter"): "__RESOLVED__",
}


class _AdvancingTmux:
    """Wraps the scenario's FakeTmux so send_keys advances the pane per the
    verified v2.1.167 keystroke semantics — processing each dispatched key
    SEQUENTIALLY against the CURRENT pane state (not a callback-start snapshot).
    """

    def __init__(self, scenario: ScenarioHarness, wid: str) -> None:
        self._fake = scenario.tmux
        self._wid = wid
        # The current fixture NAME the pane represents (None once resolved).
        self._state: str | None = _Q1_FIXTURE
        self.original_send_keys = self._fake.send_keys

    async def send_keys(
        self, window_id: str, keys: str, enter: bool = True, literal: bool = True
    ) -> bool:
        # Record the keystroke on the real fake (so sent_keys assertions hold).
        self._fake.sent_keys.append((window_id, keys, enter, literal))
        if window_id != self._wid:
            return window_id in self._fake.windows
        # Advance only the modeled keys; an unmodeled key is a no-op (records but
        # does not move the pane) — keeps the test minimal to option-1 taps.
        # The advance map keys on the literal key STRING only; the `aqp:`/`aqt:`
        # dispatch always uses enter=False (a separate keystroke for Enter), so
        # an implicit `enter=True` is not modeled — fine here because the picker
        # paths under test never set it (only the nav ⏎ button does, untested).
        nxt = _ADVANCE.get((self._state or "", keys))
        if nxt is not None:
            if nxt == "__RESOLVED__":
                self._state = None
                self._fake.set_pane(self._wid, _RESOLVED_PANE)
            else:
                self._state = nxt
                self._fake.set_pane(self._wid, _fixture(nxt))
        return self._wid in self._fake.windows


def _bind(scenario: ScenarioHarness) -> str:
    wid = scenario.add_window(
        window_name="repo", cwd="/repo", pane_text=_fixture(_Q1_FIXTURE)
    )
    scenario.bind_thread(
        42, wid, display_name="repo", cwd="/repo", session_id=_SESSION_ID
    )
    return wid


def _write_side_file() -> Path:
    pending = app_dir() / "auq_pending"
    pending.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = pending / f"{_SESSION_ID}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": _SESSION_ID,
                "tool_use_id": "tool-use-multiq",
                "written_at": time.time(),
                "tool_input": _TOOL_INPUT,
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


def _pick_callbacks(scenario: ScenarioHarness) -> list[str]:
    for sent in reversed(scenario.bot.sent):
        markup = sent.kwargs.get("reply_markup")
        if markup is not None:
            return [b.callback_data for row in markup.inline_keyboard for b in row]
    raise AssertionError("no reply markup recorded")


def _picks(scenario: ScenarioHarness) -> list[str]:
    return [cb for cb in _pick_callbacks(scenario) if cb.startswith(CB_ASK_PICK)]


def _texts(scenario: ScenarioHarness) -> str:
    return "\n---\n".join(scenario.bot.texts())


def _last_card_text(scenario: ScenarioHarness) -> str:
    for sent in reversed(scenario.bot.sent):
        if sent.kwargs.get("reply_markup") is not None:
            return str(sent.kwargs.get("text") or "")
    raise AssertionError("no picker card recorded")


def _install_advancing_tmux(
    scenario: ScenarioHarness, wid: str, monkeypatch: pytest.MonkeyPatch
) -> _AdvancingTmux:
    adv = _AdvancingTmux(scenario, wid)
    # Bind the advancing send_keys onto the real singleton AND the fake instance
    # so every consumer (handlers, dispatcher) sees the advancing behaviour.
    monkeypatch.setattr(_real_tmux, "send_keys", adv.send_keys, raising=False)
    monkeypatch.setattr(scenario.tmux, "send_keys", adv.send_keys, raising=False)
    return adv


@pytest.mark.asyncio
async def test_multi_question_q1_pick_advances_to_q2_then_submit(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full multi-question flow. RED on current code at step 2 (the bot's stray
    Enter lands on Submit so the card never shows Q2); GREEN after the Enter is
    deleted (the bare digit lands on Q2)."""
    wid = _bind(scenario)
    side_file = _write_side_file()
    _install_advancing_tmux(scenario, wid, monkeypatch)

    # 1. Render Q1 — three Approach pick buttons (affordances 4/5 excluded).
    await _render(scenario, wid)
    q1_picks = _picks(scenario)
    assert len(q1_picks) == 3
    assert "Which implementation approach" in _last_card_text(scenario)

    # 2. Tap Q1 option 1. The advancing fake processes each dispatched key.
    #    Post-fix the bot sends ONLY "1" → pane lands on Q2; the post-dispatch
    #    re-render must now show Q2's three Rollout options.
    scenario.bot.sent.clear()
    await _tap(scenario, q1_picks[0])
    q2_picks = _picks(scenario)
    assert len(q2_picks) == 3, (
        "after tapping Q1 the card must advance to Q2's picker — "
        "a trailing Enter over-advances past Q2 to the Submit screen"
    )
    q2_text = _last_card_text(scenario)
    assert "How should we roll this out" in q2_text
    assert "Immediate full rollout to everyone" in q2_text
    # The bare digit only — no Enter — was dispatched for the Q1 pick.
    assert scenario.tmux.sent_keys[-1] == (wid, "1", False, True)
    assert (wid, "Enter", False, False) not in scenario.tmux.sent_keys

    # 3. Tap Q2 option 1 → the card advances to the Submit review screen.
    scenario.bot.sent.clear()
    await _tap(scenario, q2_picks[0])
    review_picks = _picks(scenario)
    assert len(review_picks) == 2, "expected Submit + Cancel review buttons"
    review_text = _last_card_text(scenario)
    assert "Submit answers" in review_text
    assert "Cancel" in review_text

    # 4. Tap Submit → a bare digit "1" is dispatched (no Enter); then resolve.
    submit_cb = review_picks[0]
    await _tap(scenario, submit_cb)
    assert scenario.tmux.sent_keys[-1] == (wid, "1", False, True)
    assert (wid, "Enter", False, False) not in scenario.tmux.sent_keys

    # tool_result cleanup teardown (forget_ask_tool_input unlinks the side file).
    await bot_module.handle_new_message(
        NewMessage(
            session_id=_SESSION_ID,
            text="AskUserQuestion answered",
            content_type="tool_result",
            tool_use_id="tool-use-multiq",
            tool_name="AskUserQuestion",
            role="assistant",
        ),
        scenario.bot,
    )
    assert not side_file.exists()
