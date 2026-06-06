"""Scenario (D2 restart-recovery): the first AUQ pick tap after a bot restart is
recovered and dispatched.

D3-β keeps a live card's *in-memory* pick tokens alive while the poller observes
it, so an idle picker no longer dies. But a bot **restart** wipes the in-memory
``pick_token`` store; the already-published Telegram card keeps its old keyboard
with now-dead token strings baked into the callback_data, so the first tap
historically hit ``peek_none`` and degraded to the honest D3-α modal — for the
card's whole remaining lifetime.

D2 persists the per-token mint intent (``pick_intent.jsonl``, written at the
fresh aqp: render) so the callback handler RECOVERS and re-dispatches that first
token-less tap — row-scoped (single-select is single-use across siblings), with
the full owner+lease auth pair, read-TTL-free source parity, and the action
ledger as the durable single-use authority.

The restart is simulated by ``pick_token.reset_for_tests()`` (wipes the in-memory
token store / cache / reservations) while the durable ``pick_intent`` file + the
PreToolUse side file survive on disk — exactly a process restart's footprint.

Plan: temp/2026-06-06-auq-d2-restart-recovery-plan-v3.md (codex+hermes dual-PASS).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cctelegram import terminal_parser
from cctelegram.callback_dispatcher import (
    WRONG_USER_PICK_TEXT,
    DispatcherAdapters,
    dispatch_callback,
)
from cctelegram.handlers import auq_ledger, interactive_ui, pick_intent, pick_token
from cctelegram.handlers.callback_data import CB_ASK_PICK
from cctelegram.utils import app_dir
from tests.conftest import ScenarioHarness, make_update_callback

pytestmark = pytest.mark.scenario

_SESSION_ID = "33333333-3333-4333-8333-333333333333"


def _single_select_input() -> dict[str, Any]:
    return {
        "questions": [
            {
                "question": "Choose the post-restart recovery lane.",
                "header": "Recovery lane",
                "multiSelect": False,
                "options": [
                    {"label": "A) First", "description": "First option rationale."},
                    {"label": "B) Second", "description": "Second option rationale."},
                    {"label": "C) Third", "description": "Third option rationale."},
                ],
            }
        ]
    }


def _other_input() -> dict[str, Any]:
    return {
        "questions": [
            {
                "question": "A completely different question after the restart.",
                "header": "Different",
                "multiSelect": False,
                "options": [
                    {"label": "X) one", "description": "x rationale."},
                    {"label": "Y) two", "description": "y rationale."},
                ],
            }
        ]
    }


def _compressed_pane() -> str:
    return """← ☐ Recovery lane  ✔ Submit →
Choose the post-restart recovery lane.

❯ 2. B) Second
Enter to select · ↑/↓ to navigate · Esc to cancel
"""


def _other_pane() -> str:
    return """← ☐ Different  ✔ Submit →
A completely different question after the restart.

❯ 1. X) one
Enter to select · ↑/↓ to navigate · Esc to cancel
"""


def _bind(scenario: ScenarioHarness, pane: str) -> str:
    wid = scenario.add_window(window_name="repo", cwd="/repo", pane_text=pane)
    scenario.bind_thread(
        42, wid, display_name="repo", cwd="/repo", session_id=_SESSION_ID
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
                "tool_use_id": "tool-use-d2",
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


async def _tap(scenario: ScenarioHarness, callback_data: str, *, user_id: int) -> Any:
    update = make_update_callback(
        callback_data, thread_id=42, user_id=user_id, chat_id=scenario.chat_id
    )
    await dispatch_callback(
        update,
        scenario.context,
        _adapters(scenario),
        is_user_allowed_func=lambda _uid: True,
    )
    return update


def _pick_callbacks(scenario: ScenarioHarness) -> list[str]:
    for sent in reversed(scenario.bot.sent):
        markup = sent.kwargs.get("reply_markup")
        if markup is not None:
            return [b.callback_data for row in markup.inline_keyboard for b in row]
    raise AssertionError("no reply markup recorded")


def _token(callback_data: str) -> str:
    assert callback_data.startswith(CB_ASK_PICK)
    return callback_data.removeprefix(CB_ASK_PICK).split(":")[-1]


def _digits_sent(scenario: ScenarioHarness, wid: str) -> list[str]:
    return [
        keys
        for (w, keys, _, _) in scenario.tmux.sent_keys
        if w == wid and keys.isdigit()
    ]


def _answer(update: Any) -> str:
    return str(update.callback_query.answer.await_args.args[0])


async def _render_and_pick(scenario: ScenarioHarness) -> tuple[str, list[str]]:
    pane = _compressed_pane()
    wid = _bind(scenario, pane)
    _write_side_file(_single_select_input())
    await _render(scenario, wid)
    picks = [cb for cb in _pick_callbacks(scenario) if cb.startswith(CB_ASK_PICK)]
    assert len(picks) == 3
    return wid, picks


@pytest.mark.asyncio
async def test_recover_after_restart_dispatches_exactly_once(
    scenario: ScenarioHarness,
) -> None:
    """Core RED gate (flipped GREEN): after a restart wipes the in-memory tokens,
    the first tap on the still-open card dispatches the carried option exactly
    once and writes the ledger ``accepted → dispatched`` lifecycle."""
    wid, picks = await _render_and_pick(scenario)
    token = _token(picks[1])  # option 2 (cursor row)
    assert pick_token.peek(token) is not None

    pick_token.reset_for_tests()  # simulate restart
    assert pick_token.peek(token) is None

    await _tap(scenario, picks[1], user_id=scenario.user_id)

    assert scenario.tmux.sent_keys[-2:] == [
        (wid, "2", False, True),
        (wid, "Enter", False, False),
    ]
    # The recovered dispatch wrote the ledger lifecycle at the reconstructed key.
    _route_hash, fp8, _opt = picks[1].removeprefix(CB_ASK_PICK).split(":")[:3]
    ledger_key = auq_ledger.make_ledger_key(
        auq_ledger.make_route_hash(scenario.user_id, 42, wid), fp8, 2
    )
    entry = auq_ledger.lookup(ledger_key)
    assert entry is not None and entry.state == "dispatched"
    # The durable row was tombed on consume (row-scoped single-use).
    assert pick_intent.lookup_intent(token) is None


@pytest.mark.asyncio
async def test_durable_intent_written_at_fresh_render(
    scenario: ScenarioHarness,
) -> None:
    """PR-2B: a fresh aqp: render persists a per-token mint intent so recovery
    has the original intent to read after a restart."""
    _wid, picks = await _render_and_pick(scenario)
    assert (app_dir() / "pick_intent.jsonl").exists()
    intent = pick_intent.lookup_intent(_token(picks[1]))
    assert intent is not None
    assert intent.option_number == 2
    assert intent.source_kind == "side_file"
    assert intent.session_id == _SESSION_ID


@pytest.mark.asyncio
async def test_wrong_user_after_restart_is_owner_gated(
    scenario: ScenarioHarness,
) -> None:
    """(a) Recovery adds the owner-auth the peek_none branch lacks: a wrong-user
    post-restart tap answers WRONG_USER_PICK_TEXT and never dispatches."""
    wid, picks = await _render_and_pick(scenario)
    pick_token.reset_for_tests()

    update = await _tap(scenario, picks[1], user_id=scenario.user_id + 1)

    assert _digits_sent(scenario, wid) == []
    assert _answer(update) == WRONG_USER_PICK_TEXT


@pytest.mark.asyncio
async def test_second_tap_after_recovery_is_already_received(
    scenario: ScenarioHarness,
) -> None:
    """(c) Exactly-once: a second tap on the recovered option hits the action
    ledger's ``dispatched`` row at the top gate — no second digit."""
    wid, picks = await _render_and_pick(scenario)
    pick_token.reset_for_tests()
    await _tap(scenario, picks[1], user_id=scenario.user_id)
    assert _digits_sent(scenario, wid) == ["2"]

    update = await _tap(scenario, picks[1], user_id=scenario.user_id)
    assert _digits_sent(scenario, wid) == ["2"]  # still only one digit total
    assert "already received" in _answer(update).lower()


@pytest.mark.asyncio
async def test_sibling_single_use_after_restart(
    scenario: ScenarioHarness,
) -> None:
    """(n) Row-scoped single-use: after recovering option 2, a tap on the sibling
    option 3 must DECLINE (the row is tombed + the sibling-ledger guard) — a
    single-select row dispatches exactly one option."""
    wid, picks = await _render_and_pick(scenario)
    pick_token.reset_for_tests()
    await _tap(scenario, picks[2], user_id=scenario.user_id)  # recover option 3
    assert _digits_sent(scenario, wid) == ["3"]

    await _tap(scenario, picks[1], user_id=scenario.user_id)  # sibling option 2
    assert _digits_sent(scenario, wid) == ["3"]  # no sibling dispatch


@pytest.mark.asyncio
async def test_form_changed_after_restart_declines(
    scenario: ScenarioHarness,
) -> None:
    """(d) A genuinely different question after the restart → the live form
    fingerprint no longer matches the stored intent → DECLINE, no dispatch."""
    wid, picks = await _render_and_pick(scenario)
    pick_token.reset_for_tests()
    # Claude moved on: a different question now occupies the window + side file.
    _write_side_file(_other_input())
    scenario.tmux.set_pane(wid, _other_pane())

    await _tap(scenario, picks[1], user_id=scenario.user_id)
    assert _digits_sent(scenario, wid) == []


@pytest.mark.asyncio
async def test_idle_not_restarted_takes_normal_path_not_recovery(
    scenario: ScenarioHarness,
) -> None:
    """(h) D3-β overlap: with the in-memory token still live (no restart), the tap
    takes the normal validate_and_consume path — NOT recovery. Proven by the
    durable row still being present (recovery would have tombed it via
    consume_row; the normal path never touches pick_intent)."""
    wid, picks = await _render_and_pick(scenario)
    token = _token(picks[1])

    await _tap(scenario, picks[1], user_id=scenario.user_id)  # no reset

    assert scenario.tmux.sent_keys[-2:] == [
        (wid, "2", False, True),
        (wid, "Enter", False, False),
    ]
    # The normal path does NOT call pick_intent.consume_row → the durable row
    # survives (recovery was not entered).
    assert pick_intent.lookup_intent(token) is not None


@pytest.mark.asyncio
async def test_in_process_consume_then_redelivery_no_double_dispatch(
    scenario: ScenarioHarness,
) -> None:
    """(k) A redelivered callback after a NORMAL in-process consume must NOT
    recover-dispatch a second time (the ledger gate + the tombstoned cache row
    decline)."""
    wid, picks = await _render_and_pick(scenario)
    await _tap(scenario, picks[1], user_id=scenario.user_id)  # normal dispatch
    assert _digits_sent(scenario, wid) == ["2"]

    # Redelivery of the same callback (token still wiped only in-memory by the
    # consume; the ledger row persists) → "already received", no second digit.
    update = await _tap(scenario, picks[1], user_id=scenario.user_id)
    assert _digits_sent(scenario, wid) == ["2"]
    assert "already received" in _answer(update).lower()


# ── Review-Submit recovery survives a cursor move across the restart (RED) ──

_REVIEW_FIXTURES = Path(__file__).parents[1] / "cctelegram" / "fixtures"


def _review_fixture(name: str) -> str:
    return (_REVIEW_FIXTURES / name).read_text()


def _review_multi_input() -> dict[str, Any]:
    return {
        "questions": [
            {
                "question": "Which ones do you pick?",
                "header": "Pick",
                "multiSelect": True,
                "options": [
                    {"label": "A) Alpha"},
                    {"label": "B) Bravo"},
                    {"label": "C) Charlie"},
                    {"label": "D) Delta"},
                ],
            }
        ]
    }


@pytest.mark.asyncio
async def test_review_submit_recovery_after_restart_with_cursor_moved_dispatches(
    scenario: ScenarioHarness,
) -> None:
    """RED pre-fix / GREEN post-fix — D2 review-Submit recovery survives a nav.

    Render the multi-select REVIEW screen with the cursor on Submit (the fresh
    aqp: render persists the per-token mint intent). Simulate a restart
    (``reset_for_tests`` wipes the in-memory tokens but keeps ``pick_intent.jsonl``),
    then move the live pane to cursor-on-Cancel (the user pressed ↓ across the
    restart) and tap the still-displayed Submit button.

    RED today: ``recover_and_consume`` declines ``stale_form`` — the stored
    cursor-on-Submit ``full_fingerprint`` differs from the live cursor-on-Cancel
    parse (and the inlined recovery Submit guard also requires the cursor on
    option 1). Either way NO digit is dispatched. Post-fix the review fingerprint
    is cursor-blind AND the recovery guard is cursor-blind, so the recovery
    dispatches ``"1"`` + Enter.
    """
    wid = _bind(scenario, _review_fixture("auq_multiselect_review_cursor_submit.txt"))
    _write_side_file(_review_multi_input())
    await _render(scenario, wid)
    picks = [cb for cb in _pick_callbacks(scenario) if cb.startswith(CB_ASK_PICK)]
    # Option 1 (Submit answers) is the review-Submit row.
    submit_cb = next(cb for cb in picks if cb.split(":")[3] == "1")

    pick_token.reset_for_tests()  # simulate restart (durable pick_intent survives)
    assert pick_token.peek(_token(submit_cb)) is None

    # User pressed ↓ across the restart → live pane now has the cursor on Cancel.
    scenario.tmux.set_pane(
        wid, _review_fixture("auq_multiselect_review_cursor_cancel.txt")
    )

    await _tap(scenario, submit_cb, user_id=scenario.user_id)
    assert _digits_sent(scenario, wid) == ["1"]
