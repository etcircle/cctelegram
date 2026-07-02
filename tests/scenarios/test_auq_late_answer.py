"""Scenario: AskUserQuestion ~60s AFK auto-resolve → late-answer card → tap.

Wave A (plan §A3–§A5): on Claude Code ≥2.1.198 an unanswered AUQ self-resolves
at ~60s with the "No response after 60s …" tool_result. Pre-Wave-A that
tool_result tore the picker card down exactly like a genuine answer, leaving
the bridged owner a topic with no card and no way to answer. Post-Wave-A:

  - the AFK tool_result CONVERTS the picker card in place to the honest
    "⏰ Claude proceeded after ~60s (no response)." card with fresh ``aql:``
    buttons (single-question single-select) instead of deleting it;
  - a tap delivers the choice as a NORMAL user text message through the
    effort.py route-ordering delivery subsequence (never a picker keystroke);
  - a GENUINE (answered) tool_result keeps today's teardown byte-identical.

Black-box per the scenario floor: Update → real handler stack
(``bot_module.handle_new_message`` / ``bot_module.callback_handler``) → fake
tmux / fake bot. Registry seeding for the tap-only tests goes through the
PUBLIC ``late_answer.mint_card`` API (mirroring the conversion seam's mint).
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from cctelegram import bot as bot_module
from cctelegram.handlers import interactive_ui, late_answer
from cctelegram.handlers.callback_data import CB_ASK_LATE
from cctelegram.session_monitor import NewMessage
from cctelegram.transcript_parser import TranscriptParser
from cctelegram.utils import app_dir
from tests.conftest import ScenarioHarness, make_update_callback

pytestmark = pytest.mark.scenario

_SESSION_ID = "44444444-4444-4444-8444-444444444444"
_THREAD_ID = 42
_TOOL_USE_ID = "toolu_01KJNBac1vb4Z48zSfc7mcT8"

_AFK_CONTENT = (
    "No response after 60s — the user may be away from keyboard. Proceed "
    "using your best judgment based on the context so far; you can re-ask "
    "this question later if it's still relevant."
)

_QUESTION = "What should we work on in this session?"
_LABELS = {1: "Start a new coding task", 2: "Review or debug existing code"}

_TOOL_INPUT: dict[str, Any] = {
    "questions": [
        {
            "question": _QUESTION,
            "header": "Session focus",
            "multiSelect": False,
            "options": [
                {"label": _LABELS[1], "description": "Kick off a fresh feature."},
                {"label": _LABELS[2], "description": "Dig into existing code."},
            ],
        }
    ]
}

# The AFK resolve's entry-level toolUseResult (A7 gate capture shape).
_AFK_META: dict[str, Any] = {
    "questions": _TOOL_INPUT["questions"],
    "answers": {},
    "annotations": {},
    "afkTimeoutMs": 60000,
}


def _afk_text() -> str:
    """msg.text exactly as the monitor emits the AUQ tool_result."""
    return TranscriptParser._format_tool_result_text(
        _AFK_CONTENT, "AskUserQuestion", None
    )


def _bind(scenario: ScenarioHarness, *, session_id: str = _SESSION_ID) -> str:
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(
        thread_id=_THREAD_ID,
        window_id=wid,
        display_name="repo",
        cwd="/repo",
        session_id=session_id,
    )
    return wid


def _seed_live_auq_surface(
    scenario: ScenarioHarness, wid: str, *, msg_id: int = 777
) -> None:
    """A published live AUQ picker, exactly as handle_interactive_ui leaves it."""
    interactive_ui.set_interactive_mode(scenario.user_id, wid, _THREAD_ID)
    interactive_ui._interactive_msgs[(scenario.user_id, _THREAD_ID)] = msg_id
    interactive_ui.remember_ask_tool_input(wid, _TOOL_INPUT, _TOOL_USE_ID)


def _mint_card(scenario: ScenarioHarness, wid: str, *, msg_id: int = 777) -> str:
    """Seed a converted late-answer card via the PUBLIC mint API."""
    return late_answer.mint_card(
        owner_id=scenario.user_id,
        thread_id=_THREAD_ID,
        window_id=wid,
        msg_id=msg_id,
        question=_QUESTION,
        labels=_LABELS,
    )


def _aql(wid: str, opt: int, token: str) -> str:
    return f"{CB_ASK_LATE}{wid}:{opt}:{token}"


async def _afk_tool_result(scenario: ScenarioHarness) -> None:
    await bot_module.handle_new_message(
        NewMessage(
            session_id=_SESSION_ID,
            text=_afk_text(),
            content_type="tool_result",
            tool_use_id=_TOOL_USE_ID,
            role="assistant",
            tool_name="AskUserQuestion",
            tool_result_meta=_AFK_META,
        ),
        scenario.bot,
    )


async def _tap(
    scenario: ScenarioHarness, data: str, *, user_id: int | None = None
) -> Any:
    update = make_update_callback(
        data,
        thread_id=_THREAD_ID,
        user_id=user_id if user_id is not None else scenario.user_id,
        chat_id=scenario.chat_id,
    )
    await bot_module.callback_handler(update, scenario.context)
    return update


def _edit_calls(update: Any) -> list[Any]:
    return update.callback_query.edit_message_text.await_args_list


def _norm(text: str) -> str:
    """Undo safe_edit's MarkdownV2 conversion artifacts (escapes + trailing
    newline) so assertions compare semantic content at the Telegram seam."""
    return text.replace("\\", "").strip()


def _answer_texts(update: Any) -> list[str]:
    return [
        (call.args[0] if call.args else call.kwargs.get("text", ""))
        for call in update.callback_query.answer.await_args_list
        if call.args or call.kwargs.get("text")
    ]


def _sent_texts(scenario: ScenarioHarness, wid: str) -> list[str]:
    return [keys for w, keys, _e, _l in scenario.tmux.sent_keys if w == wid]


# ── aql: tap — delivery ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_aql_tap_delivers_late_answer(scenario: ScenarioHarness) -> None:
    """A tap flushes the route, stamps the user turn PRE-send, delivers the
    exact single-line template into tmux, marks inbound sent, and edits the
    card to ✅ with the keyboard removed."""
    from cctelegram.handlers import message_queue

    wid = _bind(scenario)
    token = _mint_card(scenario, wid)

    before = time.time()
    update = await _tap(scenario, _aql(wid, 2, token))

    sent = _sent_texts(scenario, wid)
    assert sent == [
        f'Re your earlier question "{_QUESTION}" (it auto-resolved after 60s '
        f'while I was away): my answer is "{_LABELS[2]}". '
        "Please course-correct based on this."
    ]
    assert "\n" not in sent[0]
    # PRE-SEND user-turn stamp landed (same clock as time.time()).
    stamp = message_queue.peek_route_user_turn_at(scenario.user_id, _THREAD_ID, wid)
    assert stamp is not None and stamp >= before
    # Card lifecycle: ⏳ sending edit (keyboard removed) then ✅ final edit.
    edits = _edit_calls(update)
    assert len(edits) == 2
    sending_text = edits[0].args[0]
    assert "⏳ Sending:" in sending_text and _LABELS[2] in sending_text
    assert edits[0].kwargs.get("reply_markup") is None
    final_text = edits[1].args[0]
    assert _norm(final_text) == f"✅ Late answer sent: {_LABELS[2]}"
    assert edits[1].kwargs.get("reply_markup") is None
    # Single-use consumed.
    row = late_answer.lookup(token)
    assert row is not None and row.state == "consumed"


@pytest.mark.asyncio
async def test_aql_second_tap_already_sent(scenario: ScenarioHarness) -> None:
    wid = _bind(scenario)
    token = _mint_card(scenario, wid)

    await _tap(scenario, _aql(wid, 1, token))
    assert len(_sent_texts(scenario, wid)) == 1

    update2 = await _tap(scenario, _aql(wid, 1, token))
    assert "Late answer already sent." in _answer_texts(update2)
    assert len(_sent_texts(scenario, wid)) == 1, "no second delivery"


@pytest.mark.asyncio
async def test_aql_wrong_user_rejected(scenario: ScenarioHarness) -> None:
    from cctelegram.callback_dispatcher import WRONG_USER_PICK_TEXT

    wid = _bind(scenario)
    token = _mint_card(scenario, wid)

    update = await _tap(scenario, _aql(wid, 1, token), user_id=scenario.user_id + 1)

    assert WRONG_USER_PICK_TEXT in _answer_texts(update)
    assert _sent_texts(scenario, wid) == []
    row = late_answer.lookup(token)
    assert row is not None and row.state == "live", "wrong user never consumes"


@pytest.mark.asyncio
async def test_aql_stale_window_rejected(scenario: ScenarioHarness) -> None:
    """The topic re-bound to a different window → the lease rejects; the
    registry row stays live and nothing is sent to EITHER window."""
    from cctelegram.callback_dispatcher import STALE_CALLBACK_TEXT

    wid = _bind(scenario)
    token = _mint_card(scenario, wid)
    # Rebind the topic to a new window (the stale-callback shape).
    new_wid = scenario.add_window(window_name="repo2", cwd="/repo2")
    scenario.session_manager.thread_bindings[scenario.user_id][_THREAD_ID] = new_wid

    update = await _tap(scenario, _aql(wid, 1, token))

    assert STALE_CALLBACK_TEXT in _answer_texts(update)
    assert _sent_texts(scenario, wid) == []
    assert _sent_texts(scenario, new_wid) == []


@pytest.mark.asyncio
async def test_aql_restart_lookup_none_graceful_expired(
    scenario: ScenarioHarness,
) -> None:
    """Post-restart (registry wiped): the tap answers the graceful expired
    modal and best-effort clears the keyboard, preserving the message's own
    text (the only text source the registry can't reconstruct)."""
    wid = _bind(scenario)
    token = _mint_card(scenario, wid)
    late_answer.reset_for_tests()  # the restart

    update = make_update_callback(
        _aql(wid, 1, token),
        thread_id=_THREAD_ID,
        user_id=scenario.user_id,
        chat_id=scenario.chat_id,
    )
    update.callback_query.message.text = "⏰ old late-answer card body"
    await bot_module.callback_handler(update, scenario.context)

    answers = _answer_texts(update)
    assert any("expired" in a for a in answers)
    edits = _edit_calls(update)
    assert len(edits) == 1
    assert _norm(edits[0].args[0]) == "⏰ old late-answer card body"
    assert edits[0].kwargs.get("reply_markup") is None
    assert _sent_texts(scenario, wid) == []


@pytest.mark.asyncio
async def test_aql_sending_state_removes_keyboard_then_failure_restores_it(
    scenario: ScenarioHarness,
) -> None:
    """[R1 both P2] the in-flight edit removes the keyboard; a send FAILURE
    re-attaches the ORIGINAL aql keyboard (rebuilt from the registry row) and
    resets the single-use gate to live."""
    wid = _bind(scenario)
    token = _mint_card(scenario, wid)
    scenario.tmux.send_keys_response = False  # tmux send failure

    update = await _tap(scenario, _aql(wid, 1, token))

    edits = _edit_calls(update)
    assert len(edits) == 2
    # Sending state: keyboard removed.
    assert "⏳ Sending:" in edits[0].args[0]
    assert edits[0].kwargs.get("reply_markup") is None
    # Failure: original keyboard re-attached, retry hint shown.
    failure_text = edits[1].args[0]
    assert "❌" in failure_text and "tap again to retry" in failure_text
    markup = edits[1].kwargs.get("reply_markup")
    assert markup is not None
    all_buttons = [b for row in markup.inline_keyboard for b in row]
    assert [b.text for b in all_buttons] == [_LABELS[1], _LABELS[2]]
    assert all_buttons[0].callback_data == _aql(wid, 1, token)
    # Single-use reset — retry tap re-arms.
    row = late_answer.lookup(token)
    assert row is not None and row.state == "live"


@pytest.mark.asyncio
async def test_aql_send_failure_resets_single_use_and_keeps_keyboard(
    scenario: ScenarioHarness,
) -> None:
    """After a failed send, a SECOND tap retries (begin_send re-armed) and a
    now-working tmux delivers."""
    wid = _bind(scenario)
    token = _mint_card(scenario, wid)
    scenario.tmux.send_keys_response = False

    await _tap(scenario, _aql(wid, 1, token))
    assert late_answer.lookup(token).state == "live"  # type: ignore[union-attr]

    scenario.tmux.send_keys_response = None  # tmux recovers
    await _tap(scenario, _aql(wid, 1, token))
    sent = _sent_texts(scenario, wid)
    assert any("my answer is" in s for s in sent)
    assert late_answer.lookup(token).state == "consumed"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_aql_blocked_when_new_surface_live(scenario: ScenarioHarness) -> None:
    """Freshness guard: a NEWER live interactive surface owns the topic — the
    late tap answers the modal and delivers nothing."""
    wid = _bind(scenario)
    token = _mint_card(scenario, wid)
    # A newer prompt went live on this route after the card converted.
    interactive_ui.set_interactive_mode(scenario.user_id, wid, _THREAD_ID)
    interactive_ui._interactive_msgs[(scenario.user_id, _THREAD_ID)] = 888

    update = await _tap(scenario, _aql(wid, 1, token))

    assert any("newer prompt is live" in a for a in _answer_texts(update))
    assert _sent_texts(scenario, wid) == []
    assert late_answer.lookup(token).state == "live"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_aql_blocked_when_side_file_live(scenario: ScenarioHarness) -> None:
    """Freshness guard: a live PreToolUse side file (hook fires BEFORE the new
    picker renders) blocks the late tap even before any surface exists."""
    wid = _bind(scenario)
    token = _mint_card(scenario, wid)
    pending = app_dir() / "auq_pending"
    pending.mkdir(mode=0o700, parents=True, exist_ok=True)
    (pending / f"{_SESSION_ID}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": _SESSION_ID,
                "tool_use_id": "toolu_next",
                "written_at": time.time(),
                "tool_input": _TOOL_INPUT,
            }
        )
    )
    try:
        update = await _tap(scenario, _aql(wid, 1, token))
    finally:
        (pending / f"{_SESSION_ID}.json").unlink(missing_ok=True)

    assert any("newer prompt is live" in a for a in _answer_texts(update))
    assert _sent_texts(scenario, wid) == []


@pytest.mark.asyncio
async def test_aql_invalidated_on_new_auq_rotation(scenario: ScenarioHarness) -> None:
    """Lifecycle (b): a NEW AUQ tool_use_id rotating in for the same window
    invalidates the late-answer card (backstop) → tap answers expired."""
    wid = _bind(scenario)
    token = _mint_card(scenario, wid)
    interactive_ui.remember_ask_tool_input(wid, _TOOL_INPUT, "toolu_old")
    interactive_ui.remember_ask_tool_input(wid, _TOOL_INPUT, "toolu_new")  # rotation

    assert late_answer.lookup(token) is None
    update = await _tap(scenario, _aql(wid, 1, token))
    assert any("expired" in a for a in _answer_texts(update))
    assert _sent_texts(scenario, wid) == []


@pytest.mark.asyncio
async def test_aql_invalidated_on_topic_close(scenario: ScenarioHarness) -> None:
    """Lifecycle (c): closing the topic clears the card (clear_topic_state)."""
    from tests.conftest import make_update_topic_closed

    wid = _bind(scenario)
    token = _mint_card(scenario, wid)

    update = make_update_topic_closed(thread_id=_THREAD_ID, user_id=scenario.user_id)
    await bot_module.topic_closed_handler(update, scenario.context)

    assert late_answer.lookup(token) is None


@pytest.mark.asyncio
async def test_aql_malformed_callback_invalid_data(scenario: ScenarioHarness) -> None:
    wid = _bind(scenario)
    _mint_card(scenario, wid)

    update = await _tap(scenario, f"{CB_ASK_LATE}{wid}:notanint")
    assert "Invalid data" in _answer_texts(update)
    assert _sent_texts(scenario, wid) == []
