"""Focused dispatch assertion: an ``aqp:`` single-select pick sends a BARE DIGIT.

On Claude Code v2.1.167 a bare digit is the universal select+advance (and, on
the review screen, the submit) action; the trailing ``Enter`` the bot used to
send over-advanced multi-question forms past Q2. This module pins that the pick
dispatch sends EXACTLY ``[(wid, "<digit>", enter=False, literal=True)]`` — no
``Enter`` keystroke — across the three live shapes:

  (a) a multi-question, non-final pick (Q1 → advances to Q2);
  (b) the review-screen **Submit** button (digit ``1`` submits);
  (c) a single-question single-select pick (digit resolves the tool).

RED on current code (it appends ``(wid, "Enter", False, False)``); GREEN after
the trailing Enter is deleted.
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

_FIXTURES = Path(__file__).parents[1] / "fixtures"

_OWNER_ID = 1
_THREAD_ID = 10
_WINDOW_ID = "@1"


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


def _adapters(send_keys: AsyncMock, pane: str) -> DispatcherAdapters:
    tmux = SimpleNamespace(
        find_window_by_id=AsyncMock(return_value=SimpleNamespace(window_id=_WINDOW_ID)),
        send_keys=send_keys,
        capture_pane=AsyncMock(return_value=pane),
    )
    return DispatcherAdapters(
        session_manager=FakeSessionManager(),
        tmux_manager=tmux,
        bot=SimpleNamespace(),
        route_runtime=SimpleNamespace(
            snapshot=lambda _route: None,
            mark_inbound_sent=AsyncMock(),
        ),
        config=SimpleNamespace(browse_root="."),
        terminal_parser=SimpleNamespace(
            resolve_ask_form=lambda _cached, _pane: resolve_ask_form(None, _pane)
        ),
    )


def _build_keyed_callback(
    *,
    pane: str,
    option_number: int,
    option_label: str,
    is_submit: bool,
) -> str:
    form = resolve_ask_form(None, pane)
    assert form is not None
    fingerprint = form.fingerprint()
    source = auq_source.resolve_auq_source(_WINDOW_ID, None, pane)
    token = pick_token.mint(
        pick_token.PickTokenEntry(
            window_id=_WINDOW_ID,
            user_id=_OWNER_ID,
            thread_id=_THREAD_ID,
            fingerprint=fingerprint,
            option_number=option_number,
            option_label=option_label,
            is_review_submit=is_submit,
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
        "cctelegram.handlers.interactive_ui.resolve_ask_tool_input",
        lambda _wid: None,
    )
    yield
    pick_token.reset_for_tests()
    auq_ledger.reset_for_tests()
    auq_source.reset_for_tests()


_Q1_PANE = (_FIXTURES / "auq_multiq_q1_pane.txt").read_text()
_SUBMIT_PANE = (_FIXTURES / "auq_multiq_submit_pane.txt").read_text()
_SINGLE_PANE = """Pick one.

❯ 1. A) One
  2. B) Two
Enter to select · ↑/↓ to navigate · Esc to cancel
"""


@pytest.mark.parametrize(
    ("pane", "option_number", "option_label", "is_submit"),
    [
        pytest.param(
            _Q1_PANE,
            1,
            "Write-through cache with Redis backend",
            False,
            id="multi-question-non-final",
        ),
        pytest.param(
            _SUBMIT_PANE,
            1,
            "Submit answers",
            True,
            id="review-submit",
        ),
        pytest.param(
            _SUBMIT_PANE,
            2,
            "Cancel",
            False,
            id="review-cancel",
        ),
        pytest.param(
            _SINGLE_PANE,
            1,
            "A) One",
            False,
            id="single-question-single-select",
        ),
    ],
)
@pytest.mark.asyncio
async def test_aqp_pick_dispatches_bare_digit_no_enter(
    pane: str, option_number: int, option_label: str, is_submit: bool
) -> None:
    callback_data = _build_keyed_callback(
        pane=pane,
        option_number=option_number,
        option_label=option_label,
        is_submit=is_submit,
    )
    sent_keys: list[tuple[str, str, bool, bool]] = []

    async def _send_keys(
        window_id: str, keys: str, enter: bool = True, literal: bool = True
    ) -> bool:
        sent_keys.append((window_id, keys, enter, literal))
        return True

    send_keys = AsyncMock(side_effect=_send_keys)
    query = FakeQuery(callback_data)
    authorized = authorize_initial(parse(query.data.encode()), _ctx(query))
    await execute(authorized, _adapters(send_keys, pane))

    # Exactly one keystroke: the bare digit, enter=False. NO Enter keystroke.
    assert sent_keys == [(_WINDOW_ID, str(option_number), False, True)]
    assert send_keys.await_count == 1


@pytest.mark.asyncio
async def test_landed_digit_not_downgraded_by_post_digit_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-digit failure must NOT downgrade a landed digit to a retryable state.

    codex+hermes diff-review P1: the digit ``send_keys`` is the only thing in the
    ``try``; once it lands, the TUI has consumed the selection, so the ledger must
    stay terminal ``dispatched`` even if the subsequent re-render raises. A
    downgrade to ``failed_before_digit`` would project as retryable and re-open
    the duplicate-tap double-dispatch the ledger exists to prevent.
    """
    from cctelegram.callback_dispatcher import interactive as cb_interactive

    callback_data = _build_keyed_callback(
        pane=_Q1_PANE,
        option_number=1,
        option_label="Write-through cache with Redis backend",
        is_submit=False,
    )
    # Recover the ledger key the dispatch will write to.
    _cb, route_hash, fp8, _opt, _token = callback_data.split(":")
    ledger_key = auq_ledger.make_ledger_key(route_hash, fp8, 1)

    send_keys = AsyncMock(return_value=True)  # the digit LANDS
    # The post-digit re-render raises — must not undo the terminal `dispatched`.
    monkeypatch.setattr(
        cb_interactive,
        "handle_interactive_ui",
        AsyncMock(side_effect=RuntimeError("re-render boom")),
    )
    query = FakeQuery(callback_data)
    authorized = authorize_initial(parse(query.data.encode()), _ctx(query))

    with pytest.raises(RuntimeError, match="re-render boom"):
        await execute(authorized, _adapters(send_keys, _Q1_PANE))

    # The digit landed → terminal `dispatched` was recorded BEFORE the re-render
    # failure, and the failure did not downgrade it.
    entry = auq_ledger.lookup(ledger_key)
    assert entry is not None
    assert entry.state == "dispatched"
    # Exactly the bare digit was sent — no Enter, no re-send.
    assert send_keys.await_count == 1
