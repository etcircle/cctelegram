"""aql: executor unit tests — the effort.py route-ordering subsequence.

The scenario file (tests/scenarios/test_auq_late_answer.py) covers the tap
flows black-box; these unit tests pin the two properties only visible with
injected adapters:

  - the user-turn stamp is in BOTH stores at the instant send_to_window
    fires (PRE-send — mirrors test_effort_callback_end_to_end_stamps_pre_send),
  - mark_inbound_sent is awaited on the route AFTER a successful send.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cctelegram import route_runtime
from cctelegram.callback_dispatcher import late_answer as late_answer_exec
from cctelegram.handlers import late_answer, message_queue
from cctelegram.handlers.callback_data import CB_ASK_LATE


@pytest.fixture(autouse=True)
def _reset_state():
    late_answer.reset_for_tests()
    message_queue.reset_for_tests()
    route_runtime.reset_for_tests()
    yield
    late_answer.reset_for_tests()
    message_queue.reset_for_tests()
    route_runtime.reset_for_tests()


class _FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = SimpleNamespace(message_thread_id=10, text=None)
        self.answer = AsyncMock()
        self.edit_message_text = AsyncMock()


def _authorized(query: _FakeQuery, user_id: int, thread_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        command=SimpleNamespace(data=query.data),
        ctx=SimpleNamespace(
            query=query,
            user=SimpleNamespace(id=user_id),
            user_id=user_id,
            thread_id=thread_id,
        ),
    )


@pytest.mark.asyncio
async def test_aql_executor_stamps_user_turn_pre_send():
    user_id, thread_id, window_id = 1, 10, "@5"
    token = late_answer.mint_card(
        owner_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        msg_id=99,
        question="Which lane?",
        labels={1: "Lane A"},
    )
    stamp_at_send: dict[str, float | None] = {}

    async def send_to_window(_wid: str, _text: str):
        # Capture the stamp AT the send instant — must already be there.
        stamp_at_send["mq"] = message_queue.peek_route_user_turn_at(
            user_id, thread_id, window_id
        )
        stamp_at_send["rr"] = route_runtime.snapshot(
            (user_id, thread_id, window_id)
        ).last_user_turn_at
        return True, "ok"

    mark_inbound_sent = AsyncMock()
    adapters = SimpleNamespace(
        session_manager=SimpleNamespace(
            resolve_window_for_thread=lambda _u, _t: window_id,
            send_to_window=send_to_window,
        ),
        tmux_manager=SimpleNamespace(
            find_window_by_id=AsyncMock(
                return_value=SimpleNamespace(window_id=window_id)
            )
        ),
        route_runtime=SimpleNamespace(mark_inbound_sent=mark_inbound_sent),
    )
    query = _FakeQuery(f"{CB_ASK_LATE}{window_id}:1:{token}")

    before = time.time()
    await late_answer_exec.execute_late_answer_callback(
        _authorized(query, user_id, thread_id), adapters
    )

    assert stamp_at_send["mq"] is not None, "stamp missing at send time (post-send?)"
    assert stamp_at_send["mq"] >= before
    assert stamp_at_send["rr"] == stamp_at_send["mq"]
    mark_inbound_sent.assert_awaited_once_with((user_id, thread_id, window_id))
    row = late_answer.lookup(token)
    assert row is not None and row.state == "consumed"


@pytest.mark.asyncio
async def test_aql_executor_send_failure_no_mark_inbound_sent():
    """The (bool, str) return of send_to_window MUST be honored
    (feedback_tmux_send_keys_returns_false): failure never marks inbound sent
    and resets the single-use gate."""
    user_id, thread_id, window_id = 1, 10, "@5"
    token = late_answer.mint_card(
        owner_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        msg_id=99,
        question="Which lane?",
        labels={1: "Lane A"},
    )

    async def send_to_window(_wid: str, _text: str):
        return False, "Failed to send keys"

    mark_inbound_sent = AsyncMock()
    adapters = SimpleNamespace(
        session_manager=SimpleNamespace(
            resolve_window_for_thread=lambda _u, _t: window_id,
            send_to_window=send_to_window,
        ),
        tmux_manager=SimpleNamespace(
            find_window_by_id=AsyncMock(
                return_value=SimpleNamespace(window_id=window_id)
            )
        ),
        route_runtime=SimpleNamespace(mark_inbound_sent=mark_inbound_sent),
    )
    query = _FakeQuery(f"{CB_ASK_LATE}{window_id}:1:{token}")

    await late_answer_exec.execute_late_answer_callback(
        _authorized(query, user_id, thread_id), adapters
    )

    mark_inbound_sent.assert_not_awaited()
    row = late_answer.lookup(token)
    assert row is not None and row.state == "live"
    # The failure edit re-attached a keyboard.
    final_edit = query.edit_message_text.await_args_list[-1]
    assert final_edit.kwargs.get("reply_markup") is not None


# ── Review fold round 1 ───────────────────────────────────────────────────


def _adapters_with(send_to_window, *, mark_inbound_sent=None) -> SimpleNamespace:
    return SimpleNamespace(
        session_manager=SimpleNamespace(
            resolve_window_for_thread=lambda _u, _t: "@5",
            send_to_window=send_to_window,
        ),
        tmux_manager=SimpleNamespace(
            find_window_by_id=AsyncMock(return_value=SimpleNamespace(window_id="@5"))
        ),
        route_runtime=SimpleNamespace(
            mark_inbound_sent=mark_inbound_sent or AsyncMock()
        ),
    )


def _mint(user_id: int = 1, thread_id: int = 10, window_id: str = "@5") -> str:
    return late_answer.mint_card(
        owner_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        msg_id=99,
        question="Which lane?",
        labels={1: "Lane A"},
    )


@pytest.mark.asyncio
async def test_aql_late_recheck_side_file_blocks_before_send(monkeypatch, tmp_path):
    """[Codex P1] a side file going live DURING aggregator_flush_route must
    block the delivery: NO send, NO user-turn stamp, row back to live, the
    original keyboard restored, the newer-prompt modal answered."""
    import json
    import time as _time

    from cctelegram.session import WindowState, session_manager
    from cctelegram.utils import app_dir

    user_id, thread_id, window_id = 1, 10, "@5"
    session_id = "66666666-6666-4666-8666-666666666666"
    session_manager.window_states[window_id] = WindowState(
        session_id=session_id, cwd="/repo", window_name="repo"
    )
    token = _mint()
    side_file = app_dir() / "auq_pending" / f"{session_id}.json"

    async def flush_writes_side_file(_route):
        # A new AUQ's PreToolUse hook fires while the flush awaits.
        side_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        side_file.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "session_id": session_id,
                    "tool_use_id": "toolu_next",
                    "written_at": _time.time(),
                    "tool_input": {
                        "questions": [
                            {
                                "question": "Newer Q?",
                                "multiSelect": False,
                                "options": [{"label": "X", "description": "x"}],
                            }
                        ]
                    },
                }
            )
        )
        return True

    monkeypatch.setattr(
        late_answer_exec, "aggregator_flush_route", flush_writes_side_file
    )
    send = AsyncMock(return_value=(True, "ok"))
    adapters = _adapters_with(send)
    query = _FakeQuery(f"{CB_ASK_LATE}{window_id}:1:{token}")

    try:
        await late_answer_exec.execute_late_answer_callback(
            _authorized(query, user_id, thread_id), adapters
        )
    finally:
        session_manager.window_states.pop(window_id, None)
        side_file.unlink(missing_ok=True)

    send.assert_not_awaited()
    assert (
        message_queue.peek_route_user_turn_at(user_id, thread_id, window_id) is None
    ), "the user-turn stamp must not land on a blocked delivery"
    row = late_answer.lookup(token)
    assert row is not None and row.state == "live"
    # The last edit restored the ORIGINAL card (keyboard re-attached).
    final_edit = query.edit_message_text.await_args_list[-1]
    assert final_edit.kwargs.get("reply_markup") is not None
    answer_texts = [c.args[0] for c in query.answer.await_args_list if c.args]
    assert any("newer prompt is live" in t for t in answer_texts)


@pytest.mark.asyncio
async def test_aql_late_recheck_new_surface_blocks_before_send(monkeypatch):
    """[Codex P1] mirror: a NEW interactive surface appearing during the
    flush blocks the delivery the same way."""
    from cctelegram.handlers import interactive_ui

    user_id, thread_id, window_id = 1, 10, "@5"
    token = _mint()

    async def flush_creates_surface(_route):
        interactive_ui._interactive_msgs[(user_id, thread_id)] = 999
        return True

    monkeypatch.setattr(
        late_answer_exec, "aggregator_flush_route", flush_creates_surface
    )
    send = AsyncMock(return_value=(True, "ok"))
    adapters = _adapters_with(send)
    query = _FakeQuery(f"{CB_ASK_LATE}{window_id}:1:{token}")

    try:
        await late_answer_exec.execute_late_answer_callback(
            _authorized(query, user_id, thread_id), adapters
        )
    finally:
        interactive_ui.reset_for_tests()

    send.assert_not_awaited()
    assert message_queue.peek_route_user_turn_at(user_id, thread_id, window_id) is None
    row = late_answer.lookup(token)
    assert row is not None and row.state == "live"
    final_edit = query.edit_message_text.await_args_list[-1]
    assert final_edit.kwargs.get("reply_markup") is not None


@pytest.mark.asyncio
async def test_aql_pre_send_raise_resets_row_to_live():
    """[Codex P2] a raise BEFORE the send attempt (RetryAfter from the
    sending-state edit) provably delivered nothing — the row resets to live
    and a second tap dispatches."""
    from telegram.error import RetryAfter

    user_id, thread_id, window_id = 1, 10, "@5"
    token = _mint()
    send = AsyncMock(return_value=(True, "ok"))
    adapters = _adapters_with(send)
    query = _FakeQuery(f"{CB_ASK_LATE}{window_id}:1:{token}")
    query.edit_message_text.side_effect = RetryAfter(1)

    with pytest.raises(RetryAfter):
        await late_answer_exec.execute_late_answer_callback(
            _authorized(query, user_id, thread_id), adapters
        )

    send.assert_not_awaited()
    row = late_answer.lookup(token)
    assert row is not None and row.state == "live", (
        "a pre-send raise must re-enable the card (nothing was sent)"
    )

    # A second tap (edits working again) dispatches normally.
    query2 = _FakeQuery(f"{CB_ASK_LATE}{window_id}:1:{token}")
    await late_answer_exec.execute_late_answer_callback(
        _authorized(query2, user_id, thread_id), adapters
    )
    send.assert_awaited_once()
    assert late_answer.lookup(token).state == "consumed"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_aql_send_raise_leaves_row_in_flight(caplog):
    """[Codex P2] a raise FROM send_to_window itself is AMBIGUOUS (the send
    may have landed) — the row stays in_flight (the honest brake; text-reply
    is the escape hatch), WARNING logged, and a second tap answers
    already-sent."""
    import logging

    user_id, thread_id, window_id = 1, 10, "@5"
    token = _mint()

    async def send_raises(_wid, _text):
        raise RuntimeError("tmux exploded mid-send")

    adapters = _adapters_with(send_raises)
    query = _FakeQuery(f"{CB_ASK_LATE}{window_id}:1:{token}")

    with caplog.at_level(
        logging.WARNING, logger="cctelegram.callback_dispatcher.late_answer"
    ):
        with pytest.raises(RuntimeError):
            await late_answer_exec.execute_late_answer_callback(
                _authorized(query, user_id, thread_id), adapters
            )

    row = late_answer.lookup(token)
    assert row is not None and row.state == "in_flight", (
        "an ambiguous send must NOT re-enable the card (double-send risk)"
    )
    assert any("aql" in r.getMessage() for r in caplog.records)

    query2 = _FakeQuery(f"{CB_ASK_LATE}{window_id}:1:{token}")
    await late_answer_exec.execute_late_answer_callback(
        _authorized(query2, user_id, thread_id), adapters
    )
    answer_texts = [c.args[0] for c in query2.answer.await_args_list if c.args]
    assert "Late answer already sent." in answer_texts


@pytest.mark.asyncio
async def test_aql_post_success_raise_keeps_row_consumed(caplog):
    """[Codex P2] a raise AFTER a successful send (mark_inbound_sent) must
    NOT reset the row — the answer WAS delivered; the row is already
    consumed (finish_send(True) commits synchronously right after the send
    returns success), WARNING logged."""
    import logging

    user_id, thread_id, window_id = 1, 10, "@5"
    token = _mint()
    send = AsyncMock(return_value=(True, "ok"))
    adapters = _adapters_with(
        send, mark_inbound_sent=AsyncMock(side_effect=RuntimeError("rr boom"))
    )
    query = _FakeQuery(f"{CB_ASK_LATE}{window_id}:1:{token}")

    with caplog.at_level(
        logging.WARNING, logger="cctelegram.callback_dispatcher.late_answer"
    ):
        with pytest.raises(RuntimeError):
            await late_answer_exec.execute_late_answer_callback(
                _authorized(query, user_id, thread_id), adapters
            )

    row = late_answer.lookup(token)
    assert row is not None and row.state == "consumed", (
        "a delivered answer must stay consumed — resetting would double-send"
    )
    assert any("aql" in r.getMessage() for r in caplog.records)
