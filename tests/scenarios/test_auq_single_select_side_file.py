"""Scenario coverage for single-select AskUserQuestion side-file picks.

Exercises the public Telegram callback seam for the live AUQ regression where
render minted ``aqp:`` tokens from the PreToolUse side file but validation fell
back to pane-only parsing, making long/compressed pickers permanently bounce.

Keystroke model: the v2.1.168 ``aqp:`` dispatch arrow-navigates the live cursor
to the tapped option then presses ``Enter`` (no bare digit). The compressed panes
here show the cursor already on the tapped option (option 2), so the dispatch
sends ONLY ``Enter`` (delta=0). The dispatch tests drive a cursor-aware advancing
fake so the post-Enter confirm sees the single-question tool resolve.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cctelegram import bot as bot_module
from cctelegram import terminal_parser
from cctelegram.callback_dispatcher import DispatcherAdapters, dispatch_callback
from cctelegram.handlers import auq_source, interactive_ui, pick_token, status_polling
from cctelegram.handlers.callback_data import CB_ASK_PICK
from cctelegram.session_monitor import NewMessage
from cctelegram.tmux_manager import tmux_manager as _real_tmux
from cctelegram.utils import app_dir
from tests.conftest import ScenarioHarness, make_update_callback, render_cursor

pytestmark = pytest.mark.scenario

_SESSION_ID = "22222222-2222-4222-8222-222222222222"

# A resolved (non-picker) pane: no AUQ marker phrases → the v2.1.168 confirm step
# reads a single-question pick as positively RESOLVED.
_RESOLVED_PANE = "user@host repo % \n"


class _AdvancingPicker:
    """Cursor-aware advancing fake for the v2.1.168 ``aqp:`` single-select dispatch.

    Overrides ``send_keys`` + ``capture_pane`` on the scenario's tmux: ``Down``/
    ``Up`` move the cursor over ``n_nav`` rows (wrapping); ``Enter`` from a real
    option (1..``n_real``) resolves the single-question tool (picker disappears →
    ``_RESOLVED_PANE``). ``capture_pane`` is STATEFUL (renders ``pane`` with the
    cursor on its live row until the resolving Enter), so the dispatch's post-nav
    verify + post-Enter confirm observe a consistent live form.

    ``initial_cursor`` seeds the cursor where the live pane already shows ``❯``.
    """

    def __init__(
        self,
        scenario: ScenarioHarness,
        wid: str,
        pane: str,
        *,
        n_real: int,
        n_nav: int,
        initial_cursor: int,
    ) -> None:
        self._fake = scenario.tmux
        self._wid = wid
        self._pane = pane
        self._n_real = n_real
        self._n_nav = n_nav
        self.cursor = initial_cursor
        self.resolved = False

    async def send_keys(
        self, window_id: str, keys: str, enter: bool = True, literal: bool = True
    ) -> bool:
        self._fake.sent_keys.append((window_id, keys, enter, literal))
        if window_id != self._wid or self.resolved:
            return window_id in self._fake.windows
        if keys == "Down":
            self.cursor = self.cursor + 1 if self.cursor < self._n_nav else 1
        elif keys == "Up":
            self.cursor = self.cursor - 1 if self.cursor > 1 else self._n_nav
        elif keys == "Enter":
            if 1 <= self.cursor <= self._n_real:
                self.resolved = True
        return window_id in self._fake.windows

    async def capture_pane(
        self, window_id: str, with_ansi: bool = False, scrollback_lines: int = 0
    ) -> str:
        del with_ansi, scrollback_lines
        if window_id != self._wid:
            return ""
        if self.resolved:
            return _RESOLVED_PANE
        return render_cursor(self._pane, self.cursor)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> _AdvancingPicker:
        for target in (_real_tmux, self._fake):
            monkeypatch.setattr(target, "send_keys", self.send_keys, raising=False)
            monkeypatch.setattr(
                target, "capture_pane", self.capture_pane, raising=False
            )
        return self


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
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
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

    # v2.1.168: the dispatch navigates the cursor to option 2 then presses Enter.
    # The compressed pane already shows the cursor on option 2 (delta=0), so it
    # sends ONLY "Enter" (no bare digit). The resolving Enter clears the picker.
    _AdvancingPicker(scenario, wid, pane, n_real=3, n_nav=3, initial_cursor=2).install(
        monkeypatch
    )
    scenario.tmux.sent_keys.clear()
    await _tap(scenario, picks[1])

    assert scenario.tmux.sent_keys[-1:] == [
        (wid, "Enter", False, False),
    ]
    assert not any(lit and k.isdigit() for _w, k, _e, lit in scenario.tmux.sent_keys)
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
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
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

    # v2.1.168 arrows+Enter dispatch — cursor on option 2 (delta=0) → only "Enter".
    _AdvancingPicker(scenario, wid, pane, n_real=3, n_nav=3, initial_cursor=2).install(
        monkeypatch
    )
    scenario.tmux.sent_keys.clear()
    await _tap(scenario, picks[1])
    assert scenario.tmux.sent_keys[-1:] == [
        (wid, "Enter", False, False),
    ]
    assert not any(lit and k.isdigit() for _w, k, _e, lit in scenario.tmux.sent_keys)


@pytest.mark.asyncio
async def test_single_select_compressed_pane_title_absent_still_dispatches(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
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

    # v2.1.168 arrows+Enter dispatch — cursor on option 2 (delta=0) → only "Enter".
    _AdvancingPicker(scenario, wid, pane, n_real=3, n_nav=3, initial_cursor=2).install(
        monkeypatch
    )
    scenario.tmux.sent_keys.clear()
    await _tap(scenario, picks[1])
    assert scenario.tmux.sent_keys[-1:] == [
        (wid, "Enter", False, False),
    ]
    assert not any(lit and k.isdigit() for _w, k, _e, lit in scenario.tmux.sent_keys)


# ── Card-liveness regression: a live AUQ card must NOT auto-expire while the
#    question is still pending on the Claude side (2026-05-31 @4/msg48427). ────

_TASKLIST_OVERLAY_PANE = (
    # The exact incident shape: a Claude task-list overlay occupies the visible
    # pane tail, so NO picker/Submit anchors are present even though the AUQ is
    # live. Pre-fix this tombstoned the card after 3 absent polls.
    "  ◻ Wave 3 R6: pick_verdict() verdict-parity › blocked by #2\n"
    "  ◻ Wave 3 R7: extract auq_context_dedup.py  › blocked by #3\n"
    "  ◻ Wave 3 R8: EditableCard Route Outbox      › blocked by #4\n"
)


async def _poll(scenario: ScenarioHarness, wid: str, n: int) -> None:
    for _ in range(n):
        await status_polling.update_status_message(
            scenario.bot, user_id=scenario.user_id, window_id=wid, thread_id=42
        )


@pytest.mark.asyncio
async def test_tasklist_overlay_does_not_tombstone_live_auq(
    scenario: ScenarioHarness,
) -> None:
    """Core regression. With the PreToolUse side file present (question live),
    a pane obscured by the Claude task-list overlay must NOT tear down the
    card — not even past the absent-streak threshold.
    """
    wid = _bind(scenario, _compressed_pane())
    _write_side_file(_single_select_input())
    await _render(scenario, wid)
    assert interactive_ui.has_interactive_surface(scenario.user_id, 42)

    scenario.tmux.set_pane(wid, _TASKLIST_OVERLAY_PANE)
    await _poll(scenario, wid, status_polling.ABSENT_STREAK_THRESHOLD + 2)

    assert interactive_ui.has_interactive_surface(scenario.user_id, 42), (
        "live AUQ card must survive an obscured pane while the side file lives"
    )
    assert (app_dir() / "auq_pending" / f"{_SESSION_ID}.json").exists()


@pytest.mark.asyncio
async def test_overlay_does_not_tombstone_even_past_read_ttl(
    scenario: ScenarioHarness,
) -> None:
    """Presence-not-TTL at the public seam. A genuinely-live AUQ unanswered
    well past the 5-min read-TTL must still keep its card — the read-TTL bounds
    stale-render risk, NOT card liveness. A regression to a TTL-gated clear
    would tombstone here.
    """
    wid = _bind(scenario, _compressed_pane())
    _write_side_file(_single_select_input())
    await _render(scenario, wid)

    path = app_dir() / "auq_pending" / f"{_SESSION_ID}.json"
    rec = json.loads(path.read_text())
    rec["written_at"] = time.time() - (auq_source._PRETOOL_TTL_SECONDS + 120)
    path.write_text(json.dumps(rec))

    scenario.tmux.set_pane(wid, _TASKLIST_OVERLAY_PANE)
    await _poll(scenario, wid, status_polling.ABSENT_STREAK_THRESHOLD + 2)

    assert interactive_ui.has_interactive_surface(scenario.user_id, 42)


@pytest.mark.asyncio
async def test_overlay_card_clears_once_tool_result_resolves(
    scenario: ScenarioHarness,
) -> None:
    """The other half of the contract: the card persists through the obscured
    pane while live, then clears once the question truly resolves on the Claude
    side. The real AskUserQuestion tool_result flows through the bot seam, which
    unlinks the side file (forget_ask_tool_input → auq_source.forget_for_window)
    and clears the surface.
    """
    wid = _bind(scenario, _compressed_pane())
    _write_side_file(_single_select_input())
    await _render(scenario, wid)

    scenario.tmux.set_pane(wid, _TASKLIST_OVERLAY_PANE)
    await _poll(scenario, wid, status_polling.ABSENT_STREAK_THRESHOLD + 2)
    assert interactive_ui.has_interactive_surface(scenario.user_id, 42)

    await bot_module.handle_new_message(
        NewMessage(
            session_id=_SESSION_ID,
            text="**AskUserQuestion**(Choose the AUQ picker hotfix lane.) Answered.",
            content_type="tool_result",
            tool_use_id="tool-use-single-select",
            tool_name="AskUserQuestion",
            role="assistant",
        ),
        scenario.bot,
    )

    assert not (app_dir() / "auq_pending" / f"{_SESSION_ID}.json").exists(), (
        "tool_result must unlink the side file — the question resolved"
    )
    assert not interactive_ui.has_interactive_surface(scenario.user_id, 42), (
        "with the question resolved, the card must clear"
    )
