"""Unit tests for callback dispatcher parse, authorization, and execution.

Covers externally malformed callback data plus the lease checks for stale
windows, wrong-user interactive picks, and one-shot pick tokens.
"""

import time
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from cctelegram.callback_dispatcher import (
    STALE_CALLBACK_TEXT,
    DispatcherAdapters,
    RawCallbackCommand,
    authorize_initial,
    execute,
    parse,
)
from cctelegram.handlers.callback_data import CB_ASK_PICK, CB_KEYS_PREFIX
from cctelegram.handlers import interactive_ui


class FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = SimpleNamespace(message_thread_id=10)
        self.answers: list[tuple[str | None, bool | None]] = []

    async def answer(
        self, text: str | None = None, show_alert: bool | None = None
    ) -> None:
        self.answers.append((text, show_alert))


class FakeSessionManager:
    def __init__(self, current_window: str | None = "@1") -> None:
        self.current_window = current_window

    def resolve_window_for_thread(
        self, _user_id: int, _thread_id: int | None
    ) -> str | None:
        return self.current_window


class FakeTmuxManager:
    def __init__(self) -> None:
        self.find_window_by_id = AsyncMock(return_value=SimpleNamespace(window_id="@1"))
        self.send_keys = AsyncMock()
        self.capture_pane = AsyncMock(return_value="pane")


class FakeForm:
    is_review_screen = False
    options: list[Any] = []

    def fingerprint(self) -> str:
        return "fp"


@pytest.fixture(autouse=True)
def clear_pick_tokens() -> None:
    interactive_ui.reset_pick_tokens_for_tests()


def _mint_test_pick_token(user_id: int, *, window_id: str = "@1") -> str:
    entry_cls = cast(Any, getattr(interactive_ui, "_PickTokenEntry"))
    mint = cast(Any, getattr(interactive_ui, "_mint_pick_token"))
    return cast(
        str,
        mint(
            entry_cls(
                window_id=window_id,
                user_id=user_id,
                thread_id=10,
                fingerprint="fp",
                option_number=1,
                option_label="Yes",
                is_review_submit=False,
                expires_at=time.monotonic() + 300,
            )
        ),
    )


def _ctx(query: FakeQuery, user_id: int = 1) -> SimpleNamespace:
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
        thread_id=10,
    )


def _adapters(
    session_manager: FakeSessionManager, tmux_manager: FakeTmuxManager
) -> DispatcherAdapters:
    return DispatcherAdapters(
        session_manager=session_manager,
        tmux_manager=tmux_manager,
        bot=SimpleNamespace(),
        route_runtime=SimpleNamespace(snapshot=lambda _route: None),
        config=SimpleNamespace(
            busy_indicator_v2=False,
            route_runtime_v2=False,
            browse_root=".",
        ),
        busy_indicator=SimpleNamespace(mark_inbound_sent=AsyncMock()),
        terminal_parser=SimpleNamespace(
            resolve_ask_form=lambda _cached_input, _pane: FakeForm()
        ),
    )


def test_parse_rejects_too_long_external_callback() -> None:
    result = parse(b"x" * 65)

    assert not isinstance(result, RawCallbackCommand)
    assert result.reason == "callback_data exceeds Telegram 64-byte limit"


@pytest.mark.asyncio
async def test_execute_rejects_stale_window_before_tmux_lookup() -> None:
    query = FakeQuery(f"{CB_KEYS_PREFIX}up:@1")
    authorized = authorize_initial(parse(query.data.encode()), _ctx(query))
    tmux = FakeTmuxManager()

    await execute(authorized, _adapters(FakeSessionManager(current_window="@2"), tmux))

    assert query.answers == [
        ("This button is stale for this topic — refresh the picker.", True)
    ]
    tmux.find_window_by_id.assert_not_called()
    tmux.send_keys.assert_not_called()


@pytest.mark.asyncio
async def test_wrong_user_pick_does_not_consume_token() -> None:
    token = _mint_test_pick_token(user_id=1)
    query = FakeQuery(f"{CB_ASK_PICK}{token}")
    authorized = authorize_initial(parse(query.data.encode()), _ctx(query, user_id=2))

    await execute(authorized, _adapters(FakeSessionManager(), FakeTmuxManager()))

    assert query.answers == [("This control isn't yours.", True)]
    assert interactive_ui.peek_pick_token(token) is not None


@pytest.mark.asyncio
async def test_stale_owner_pick_does_not_consume_token() -> None:
    token = _mint_test_pick_token(user_id=1, window_id="@1")
    query = FakeQuery(f"{CB_ASK_PICK}{token}")
    authorized = authorize_initial(parse(query.data.encode()), _ctx(query, user_id=1))

    await execute(
        authorized,
        _adapters(FakeSessionManager(current_window="@2"), FakeTmuxManager()),
    )

    assert interactive_ui.peek_pick_token(token) is not None


@pytest.mark.asyncio
async def test_stale_owner_pick_answers_exactly_once() -> None:
    token = _mint_test_pick_token(user_id=1, window_id="@1")
    query = FakeQuery(f"{CB_ASK_PICK}{token}")
    authorized = authorize_initial(parse(query.data.encode()), _ctx(query, user_id=1))

    await execute(
        authorized,
        _adapters(FakeSessionManager(current_window="@2"), FakeTmuxManager()),
    )

    assert query.answers == [(STALE_CALLBACK_TEXT, True)]


@pytest.mark.asyncio
async def test_double_pick_second_click_is_expired_after_first_consumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _mint_test_pick_token(user_id=1)
    monkeypatch.setattr(
        "cctelegram.handlers.interactive_ui.resolve_ask_tool_input", lambda _wid: None
    )
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    query1 = FakeQuery(f"{CB_ASK_PICK}{token}")
    authorized1 = authorize_initial(
        parse(query1.data.encode()), _ctx(query1, user_id=1)
    )
    query2 = FakeQuery(f"{CB_ASK_PICK}{token}")
    authorized2 = authorize_initial(
        parse(query2.data.encode()), _ctx(query2, user_id=1)
    )

    await execute(authorized1, _adapters(FakeSessionManager(), FakeTmuxManager()))
    await execute(authorized2, _adapters(FakeSessionManager(), FakeTmuxManager()))

    assert query1.answers == [("1. Yes", None)]
    assert query2.answers == [("Card expired, refreshing.", False)]
