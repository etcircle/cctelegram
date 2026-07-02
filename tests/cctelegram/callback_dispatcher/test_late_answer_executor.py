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
