"""RED-first contract: the v2.1.168 pick dispatch is ARROWS-to-target + Enter.

On Claude Code v2.1.168 a bare digit no longer reliably SELECTS (in the notes
side-panel picker it only navigates), so the bot must drive the live cursor to the
tapped option with ``Up``/``Down`` and then press ``Enter`` (the version-stable
commit), recording the ledger ``dispatched`` lock ONLY after the form provably
resolved/advanced. This pins the observable keystroke + ledger contract through
the public ``execute`` seam with the keystroke-aware ``Fake168Picker``
(orchestrator-authored; GREEN implements ``_navigate_and_commit`` to pass).

RED on current code: it sends a single bare digit ``(wid, "<n>", False, True)``.
GREEN: it sends ``("Down"…, False, False)…("Enter", False, False)`` and no digit.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cctelegram.callback_dispatcher import (
    DispatcherAdapters,
    authorize_initial,
    execute,
    parse,
)
from cctelegram.handlers import auq_ledger, auq_source, pick_token
from cctelegram.handlers.callback_data import CB_ASK_PICK
from cctelegram.terminal_parser import resolve_ask_form
from tests.conftest import Fake168Picker, _Screen

_FX = Path(__file__).parents[1] / "fixtures"
_OWNER_ID = 1
_THREAD_ID = 10
_WINDOW_ID = "@1"
_SINGLE_PANE = (_FX / "auq_single_select_with_affordances_pane.txt").read_text()


class FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = SimpleNamespace(message_thread_id=_THREAD_ID)
        self.answers: list[tuple[str | None, bool | None]] = []

    async def answer(
        self, text: str | None = None, show_alert: bool | None = None
    ) -> None:
        self.answers.append((text, show_alert))


class FakeSessionManager:
    def resolve_window_for_thread(
        self, _user_id: int, _thread_id: int | None
    ) -> str | None:
        return _WINDOW_ID


def _ctx(query: FakeQuery, user_id: int = _OWNER_ID) -> SimpleNamespace:
    return SimpleNamespace(
        update=SimpleNamespace(
            message=None,
            callback_query=query,
            effective_user=SimpleNamespace(id=user_id),
            effective_chat=None,
        ),
        context=SimpleNamespace(user_data={}, bot=SimpleNamespace()),
        user=SimpleNamespace(id=user_id),
        query=query,
        user_id=user_id,
        thread_id=_THREAD_ID,
    )


def _adapters(picker: Fake168Picker) -> DispatcherAdapters:
    return DispatcherAdapters(
        session_manager=FakeSessionManager(),
        tmux_manager=picker,
        bot=SimpleNamespace(),
        route_runtime=SimpleNamespace(
            snapshot=lambda _route: None, mark_inbound_sent=AsyncMock()
        ),
        config=SimpleNamespace(browse_root="."),
        terminal_parser=SimpleNamespace(
            resolve_ask_form=lambda _cached, _pane: resolve_ask_form(None, _pane)
        ),
    )


def _single_picker(variant: str = "A") -> Fake168Picker:
    # One single-question screen (3 real options, 5 navigable rows); Enter on a
    # real option resolves the tool → the picker disappears (RESOLVED pane).
    return Fake168Picker(_WINDOW_ID, [_Screen(_SINGLE_PANE, 3, 5)], variant=variant)


def _mint_callback(option_number: int, option_label: str) -> str:
    form = resolve_ask_form(None, _SINGLE_PANE)
    assert form is not None
    fingerprint = form.fingerprint()
    source = auq_source.resolve_auq_source(_WINDOW_ID, None, _SINGLE_PANE)
    token = pick_token.mint(
        pick_token.PickTokenEntry(
            window_id=_WINDOW_ID,
            user_id=_OWNER_ID,
            thread_id=_THREAD_ID,
            fingerprint=fingerprint,
            option_number=option_number,
            option_label=option_label,
            is_review_submit=False,
            expires_at=time.monotonic() + 300,
            source_kind=source.kind,
            source_fingerprint=source.source_fingerprint,
            row_generation=1,
        )
    )
    route_hash = auq_ledger.make_route_hash(_OWNER_ID, _THREAD_ID, _WINDOW_ID)
    return f"{CB_ASK_PICK}{route_hash}:{fingerprint[:8]}:{option_number}:{token}"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from cctelegram.callback_dispatcher import interactive as cb_interactive

    pick_token.reset_for_tests()
    auq_ledger.reset_for_tests(path=tmp_path / "ledger.jsonl", start_time=time.time())
    auq_source.reset_for_tests()
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    monkeypatch.setattr(
        cb_interactive, "handle_interactive_ui", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        "cctelegram.handlers.interactive_ui.resolve_ask_tool_input", lambda _wid: None
    )
    yield
    pick_token.reset_for_tests()
    auq_ledger.reset_for_tests()
    auq_source.reset_for_tests()


async def _run(callback_data: str, picker: Fake168Picker) -> FakeQuery:
    query = FakeQuery(callback_data)
    authorized = authorize_initial(parse(query.data.encode()), _ctx(query))
    await execute(authorized, _adapters(picker))
    return query


def _picker_keys(picker: Fake168Picker) -> list[tuple[str, bool, bool]]:
    return [(k, e, lit) for _w, k, e, lit in picker.sent]


# ── keystroke contract ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pick_far_option_navigates_then_enter() -> None:
    picker = _single_picker()
    await _run(_mint_callback(3, "Descriptions only (defer ordering too)"), picker)
    keys = _picker_keys(picker)
    # cursor starts on option 1 → Down, Down to reach option 3 → Enter to commit.
    assert keys == [
        ("Down", False, False),
        ("Down", False, False),
        ("Enter", False, False),
    ]
    # NO bare digit was ever sent.
    assert not any(lit and k.isdigit() for k, _e, lit in keys)


@pytest.mark.asyncio
async def test_pick_cursor_option_sends_only_enter() -> None:
    picker = _single_picker()
    await _run(
        _mint_callback(
            1, "Full fix: descriptions + ordering + first-render robustness"
        ),
        picker,
    )
    assert _picker_keys(picker) == [("Enter", False, False)]


@pytest.mark.asyncio
async def test_dispatched_recorded_only_after_confirmed_resolution() -> None:
    picker = _single_picker()
    await _run(
        _mint_callback(
            2, "Descriptions + ordering only (defer the first-render backstop)"
        ),
        picker,
    )
    # The single-question tool resolved (picker gone) → the dispatch is confirmed.
    assert picker.resolved
    route_hash = auq_ledger.make_route_hash(_OWNER_ID, _THREAD_ID, _WINDOW_ID)
    form = resolve_ask_form(None, _SINGLE_PANE)
    key = auq_ledger.make_ledger_key(route_hash, form.fingerprint()[:8], 2)
    entry = auq_ledger.lookup(key)
    assert entry is not None and entry.state == "dispatched"


@pytest.mark.asyncio
async def test_no_bare_digit_and_no_digit_enter_pair() -> None:
    picker = _single_picker()
    await _run(_mint_callback(3, "Descriptions only (defer ordering too)"), picker)
    keys = _picker_keys(picker)
    assert not any(
        lit for _k, _e, lit in keys
    )  # no literal=True (digit) keystroke at all


@pytest.mark.asyncio
async def test_commit_unconfirmed_when_form_does_not_advance() -> None:
    # A picker whose Enter does NOT advance (models a future variant where the
    # commit key didn't take). The bot must NOT record `dispatched`, must NOT lock
    # re-taps with "Action already received".
    class _StuckPicker(Fake168Picker):
        async def send_keys(self, window_id, keys, enter=True, literal=True):
            self.sent.append((window_id, keys, enter, literal))
            if keys in ("Down", "Up") and not self.resolved:
                scr = self.screens[self.idx]
                if keys == "Down":
                    self.cursor = self.cursor + 1 if self.cursor < scr.n_nav else 1
                else:
                    self.cursor = self.cursor - 1 if self.cursor > 1 else scr.n_nav
            return True  # Enter is a no-op: the form never advances

    picker = _StuckPicker(_WINDOW_ID, [_Screen(_SINGLE_PANE, 3, 5)])
    await _run(
        _mint_callback(
            1, "Full fix: descriptions + ordering + first-render robustness"
        ),
        picker,
    )
    route_hash = auq_ledger.make_route_hash(_OWNER_ID, _THREAD_ID, _WINDOW_ID)
    form = resolve_ask_form(None, _SINGLE_PANE)
    key = auq_ledger.make_ledger_key(route_hash, form.fingerprint()[:8], 1)
    entry = auq_ledger.lookup(key)
    assert entry is not None
    assert entry.state == "commit_unconfirmed"
    assert entry.state != "dispatched"


@pytest.mark.asyncio
async def test_cursor_landing_on_wrong_option_does_not_commit() -> None:
    # Wrong-option safety: a picker whose nav does NOT move the cursor (a future
    # parse/TUI drift) leaves the cursor off the target → the verify step must
    # FAIL and NO Enter is ever sent → `not_advanced`, no wrong-option commit.
    class _StuckCursorPicker(Fake168Picker):
        async def send_keys(self, window_id, keys, enter=True, literal=True):
            self.sent.append((window_id, keys, enter, literal))
            # Down/Up are swallowed (cursor never leaves option 1); Enter advances.
            if keys == "Enter" and not self.resolved and self.cursor == 1:
                self._advance()
            return True

    picker = _StuckCursorPicker(_WINDOW_ID, [_Screen(_SINGLE_PANE, 3, 5)])
    # Tap option 3: the bot sends Down,Down but the cursor stays on 1 → verify fails.
    await _run(_mint_callback(3, "Descriptions only (defer ordering too)"), picker)
    keys = _picker_keys(picker)
    assert ("Enter", False, False) not in keys, "must NOT commit when cursor off target"
    route_hash = auq_ledger.make_route_hash(_OWNER_ID, _THREAD_ID, _WINDOW_ID)
    form = resolve_ask_form(None, _SINGLE_PANE)
    key = auq_ledger.make_ledger_key(route_hash, form.fingerprint()[:8], 3)
    entry = auq_ledger.lookup(key)
    assert entry is not None and entry.state == "not_advanced"


@pytest.mark.asyncio
async def test_commit_unconfirmed_retap_is_refresh_only_never_second_enter() -> None:
    # After a `commit_unconfirmed` row, a re-tap of the SAME option must REFRESH
    # ONLY — never re-send Enter (no auto-redispatch of a possibly-committed key)
    # and never lock with "Action already received".
    class _StuckPicker(Fake168Picker):
        async def send_keys(self, window_id, keys, enter=True, literal=True):
            self.sent.append((window_id, keys, enter, literal))
            return True  # nothing ever advances

    picker = _StuckPicker(_WINDOW_ID, [_Screen(_SINGLE_PANE, 3, 5)])
    label = "Full fix: descriptions + ordering + first-render robustness"
    await _run(_mint_callback(1, label), picker)
    route_hash = auq_ledger.make_route_hash(_OWNER_ID, _THREAD_ID, _WINDOW_ID)
    form = resolve_ask_form(None, _SINGLE_PANE)
    key = auq_ledger.make_ledger_key(route_hash, form.fingerprint()[:8], 1)
    assert auq_ledger.lookup(key).state == "commit_unconfirmed"

    # Re-tap (a fresh card mints a fresh token at the same ledger key); the matrix
    # commit_unconfirmed branch intercepts BEFORE any dispatch.
    picker.sent.clear()
    q = await _run(_mint_callback(1, label), picker)
    assert ("Enter", False, False) not in _picker_keys(picker), "no 2nd Enter"
    assert not any(lit for _k, _e, lit in _picker_keys(picker)), "no keystrokes at all"
    # Not a hard "already received" lock — it refreshes.
    assert all("already received" not in (t or "").lower() for t, _a in q.answers)
