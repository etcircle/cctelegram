"""Scenario coverage for multi-QUESTION AskUserQuestion picker delivery.

The bug (v2.1.167 era): a single-select pick dispatched a bare digit AND a
trailing ``Enter``, which over-advanced past Q2 to the Submit review so Q2's
live picker never reached the Telegram user.

v2.1.168 model: the ``aqp:`` pick no longer trusts the bare digit at all — it
arrow-navigates the live cursor to the tapped option and presses ``Enter`` (the
version-stable commit), recording ``dispatched`` only after a confirmed advance.

This scenario drives the public callback seam (Update → real handler stack →
fake tmux / fake bot) with the real PII-scrubbed captures, using a
keystroke-aware CURSOR-AWARE advancing fake tmux that encodes the verified .168
TUI semantics:

    Down/Up move the cursor (wrapping over the numbered rows);
    Enter from a real option selects it AND advances to the next screen:
        Q1     + Enter -> Q2 pane
        Q2     + Enter -> Submit pane
        Submit + Enter -> resolved/inert

``capture_pane`` is STATEFUL and renders the cursor, so the dispatch's post-nav
VERIFY + post-Enter CONFIRM see the real form. The key property under test still
holds: tapping Q1 must land on Q2's picker (never over-advance to Submit).
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
from tests.conftest import ScenarioHarness, make_update_callback, render_cursor

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


# Per-screen geometry + the Enter-advance chain (verified .168 semantics). Each
# screen names the fixture, its REAL option count, the navigable-row count (real +
# affordances, for wrap), and the NEXT fixture an Enter on a real option lands on
# ("__RESOLVED__" → the picker disappears).
_SCREENS: dict[str, tuple[int, int, str]] = {
    # fixture: (n_real, n_nav, next_on_enter)
    _Q1_FIXTURE: (3, 5, _Q2_FIXTURE),
    _Q2_FIXTURE: (3, 5, _SUBMIT_FIXTURE),
    _SUBMIT_FIXTURE: (2, 2, "__RESOLVED__"),
}


class _AdvancingTmux:
    """Cursor-aware advancing wrapper over the scenario's FakeTmux.

    Models the captured v2.1.168 picker: ``Down``/``Up`` move the cursor over the
    current screen's navigable rows (wrapping), and ``Enter`` from a REAL option
    selects it and advances to the next screen (the final screen → resolved). It
    overrides both ``send_keys`` (state machine) and ``capture_pane`` (renders the
    current fixture with the cursor on its live row) so the dispatch's post-nav
    VERIFY and post-Enter CONFIRM both observe a consistent live form.

    A bare digit is intentionally NOT modeled as a selection — on .168 it does not
    reliably select, which is exactly why the bot stopped trusting it; a stray
    digit here is a recorded no-op.
    """

    def __init__(self, scenario: ScenarioHarness, wid: str) -> None:
        self._fake = scenario.tmux
        self._wid = wid
        # The current fixture NAME the pane represents (None once resolved).
        self._state: str | None = _Q1_FIXTURE
        self.cursor = 1
        self.original_send_keys = self._fake.send_keys

    def _sync_pane(self) -> None:
        """Mirror the live pane (cursor rendered) onto the fake for any consumer
        that reads ``capture_pane`` indirectly via the underlying fake."""
        if self._state is None:
            self._fake.set_pane(self._wid, _RESOLVED_PANE)
        else:
            self._fake.set_pane(
                self._wid, render_cursor(_fixture(self._state), self.cursor)
            )

    async def send_keys(
        self, window_id: str, keys: str, enter: bool = True, literal: bool = True
    ) -> bool:
        # Record the keystroke on the real fake (so sent_keys assertions hold).
        self._fake.sent_keys.append((window_id, keys, enter, literal))
        if window_id != self._wid:
            return window_id in self._fake.windows
        if self._state is not None:
            n_real, n_nav, nxt = _SCREENS[self._state]
            if keys == "Down":
                self.cursor = self.cursor + 1 if self.cursor < n_nav else 1
            elif keys == "Up":
                self.cursor = self.cursor - 1 if self.cursor > 1 else n_nav
            elif keys == "Enter":
                if 1 <= self.cursor <= n_real:
                    if nxt == "__RESOLVED__":
                        self._state = None
                    else:
                        self._state = nxt
                        self.cursor = 1
            # A bare digit (literal=True) does NOT select on .168 — recorded no-op.
            self._sync_pane()
        return self._wid in self._fake.windows

    async def capture_pane(
        self, window_id: str, with_ansi: bool = False, scrollback_lines: int = 0
    ) -> str:
        del with_ansi, scrollback_lines
        if window_id != self._wid:
            return ""
        if self._state is None:
            return _RESOLVED_PANE
        return render_cursor(_fixture(self._state), self.cursor)


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
    adv._sync_pane()  # seed the pane with the cursor rendered on option 1
    # Bind the advancing send_keys + cursor-aware capture_pane onto the real
    # singleton AND the fake instance so every consumer (handlers, dispatcher)
    # sees the advancing behaviour.
    monkeypatch.setattr(_real_tmux, "send_keys", adv.send_keys, raising=False)
    monkeypatch.setattr(scenario.tmux, "send_keys", adv.send_keys, raising=False)
    monkeypatch.setattr(_real_tmux, "capture_pane", adv.capture_pane, raising=False)
    monkeypatch.setattr(scenario.tmux, "capture_pane", adv.capture_pane, raising=False)
    return adv


@pytest.mark.asyncio
async def test_multi_question_q1_pick_advances_to_q2_then_submit(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full multi-question flow under the v2.1.168 arrows+Enter dispatch. The
    key property: tapping Q1 option 1 must land on Q2's picker — never
    over-advance to the Submit screen. The cursor starts on option 1, so each tap
    of option 1 sends only ``Enter`` (delta=0, no nav steps) and NEVER a bare
    digit on the ``aqp:`` path."""
    wid = _bind(scenario)
    side_file = _write_side_file()
    _install_advancing_tmux(scenario, wid, monkeypatch)

    # 1. Render Q1 — three Approach pick buttons (affordances 4/5 excluded).
    await _render(scenario, wid)
    q1_picks = _picks(scenario)
    assert len(q1_picks) == 3
    assert "Which implementation approach" in _last_card_text(scenario)

    # 2. Tap Q1 option 1. The advancing fake processes each dispatched key.
    #    The cursor is on option 1 (delta=0) → the bot sends ONLY "Enter" →
    #    the pane lands on Q2; the post-dispatch re-render must now show Q2's
    #    three Rollout options (NEVER over-advancing to Submit).
    scenario.bot.sent.clear()
    scenario.tmux.sent_keys.clear()
    await _tap(scenario, q1_picks[0])
    q2_picks = _picks(scenario)
    assert len(q2_picks) == 3, (
        "after tapping Q1 the card must advance to Q2's picker — "
        "over-advancing past Q2 to the Submit screen is the bug"
    )
    q2_text = _last_card_text(scenario)
    assert "How should we roll this out" in q2_text
    assert "Immediate full rollout to everyone" in q2_text
    # The arrows+Enter path: option 1 with the cursor on 1 → only "Enter", and
    # NO bare digit on the aqp: pick path.
    assert scenario.tmux.sent_keys[-1] == (wid, "Enter", False, False)
    assert not any(lit and k.isdigit() for _w, k, _e, lit in scenario.tmux.sent_keys)

    # 3. Tap Q2 option 1 → the card advances to the Submit review screen.
    scenario.bot.sent.clear()
    scenario.tmux.sent_keys.clear()
    await _tap(scenario, q2_picks[0])
    review_picks = _picks(scenario)
    assert len(review_picks) == 2, "expected Submit + Cancel review buttons"
    review_text = _last_card_text(scenario)
    assert "Submit answers" in review_text
    assert "Cancel" in review_text
    assert scenario.tmux.sent_keys[-1] == (wid, "Enter", False, False)

    # 4. Tap Submit → arrows+Enter (cursor on Submit=opt 1 → only "Enter"); the
    #    form resolves. No bare digit on the aqp: Submit path.
    scenario.bot.sent.clear()
    scenario.tmux.sent_keys.clear()
    submit_cb = review_picks[0]
    await _tap(scenario, submit_cb)
    assert scenario.tmux.sent_keys[-1] == (wid, "Enter", False, False)
    assert not any(lit and k.isdigit() for _w, k, _e, lit in scenario.tmux.sent_keys)

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
