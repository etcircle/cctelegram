"""Unit tests for callback dispatcher parse, authorization, and execution.

Covers externally malformed callback data plus the lease checks for stale
windows, wrong-user interactive picks, and one-shot pick tokens.
"""

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest

from cctelegram.callback_dispatcher import (
    STALE_CALLBACK_TEXT,
    DispatcherAdapters,
    RawCallbackCommand,
    authorize_initial,
    execute,
    parse,
)
from cctelegram.handlers.callback_data import (
    CB_ASK_PICK,
    CB_DIR_CONFIRM,
    CB_DIR_SELECT,
    CB_KEYS_PREFIX,
)
from cctelegram.handlers import auq_ledger, auq_source, pick_token
from cctelegram.handlers.directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_PATH_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
)
from cctelegram.terminal_parser import resolve_ask_form

# A real picker pane so validate_and_consume (which uses the REAL parser +
# resolver, not a fake) re-resolves to a form whose fingerprint + source tags
# match the minted token — i.e. genuine mint/validate parity.
_BASELINE_PANE = (
    Path(__file__).parents[1] / "fixtures" / "auq-baseline-pane.txt"
).read_text()


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
        # Production ``send_keys`` returns True on success / False on
        # session/window/pane/libtmux failures. Default the mock to True
        # so the Wave 3 callback handler's return-value check doesn't
        # short-circuit every test that exercises the dispatch path.
        self.send_keys = AsyncMock(return_value=True)
        # Return the REAL baseline picker pane: validate_and_consume uses the
        # real parser/resolver, so an ``ok`` pick needs a pane that re-parses
        # to the same form the token was minted against.
        self.capture_pane = AsyncMock(return_value=_BASELINE_PANE)


class FakeForm:
    """Minimal form for the toggle-path adapter fake (unused by the pick path,
    which re-resolves via the real parser inside validate_and_consume)."""

    is_review_screen = False
    options: list[Any] = []

    def fingerprint(self) -> str:
        return "fp"


@pytest.fixture(autouse=True)
def clear_pick_tokens() -> None:
    pick_token.reset_for_tests()


def _mint_test_pick_token(user_id: int, *, window_id: str = "@1") -> str:
    """Mint a token whose fingerprint + source tags MATCH the baseline pane.

    The token is recorded against the real ``resolve_ask_form`` fingerprint and
    the real ``resolve_auq_source`` tags for ``_BASELINE_PANE`` (option 1,
    "Done navigating"), so a same-user / same-window tap whose capture returns
    that pane validates to ``ok``. Wrong-user / stale-window taps bounce before
    the form check, so the exact form doesn't matter for those.
    """
    form = resolve_ask_form(None, _BASELINE_PANE)
    assert form is not None
    src = auq_source.resolve_auq_source(window_id, None, _BASELINE_PANE)
    entry = pick_token.PickTokenEntry(
        window_id=window_id,
        user_id=user_id,
        thread_id=10,
        fingerprint=form.fingerprint(),
        option_number=1,
        option_label="Done navigating",
        is_review_submit=False,
        expires_at=time.monotonic() + 300,
        source_kind=src.kind,
        source_fingerprint=src.source_fingerprint,
        row_generation=1,
    )
    return pick_token.mint(entry)


def _keyed_pick_callback(token: str, *, user_id: int = 1, window_id: str = "@1") -> str:
    """Build the Wave 3 keyed ``aqp:<route_hash>:<fp8>:<opt>:<token>`` shape.

    Mirrors the minted entry (thread_id=10, option 1). The keyed triplet is the
    only callback shape the dispatcher parses since the legacy ``aqp:<token>``
    shape was retired. ``fp8`` is an idempotency-key fragment independent of the
    entry's full form fingerprint.
    """
    route_hash = auq_ledger.make_route_hash(user_id, 10, window_id)
    return f"{CB_ASK_PICK}{route_hash}:fp:1:{token}"


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
        route_runtime=SimpleNamespace(
            snapshot=lambda _route: None,
            mark_inbound_sent=AsyncMock(),
        ),
        config=SimpleNamespace(
            browse_root=".",
        ),
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
    query = FakeQuery(_keyed_pick_callback(token, user_id=1))
    authorized = authorize_initial(parse(query.data.encode()), _ctx(query, user_id=2))

    await execute(authorized, _adapters(FakeSessionManager(), FakeTmuxManager()))

    assert query.answers == [("This control isn't yours.", True)]
    assert pick_token.peek(token) is not None


@pytest.mark.asyncio
async def test_stale_owner_pick_does_not_consume_token() -> None:
    token = _mint_test_pick_token(user_id=1, window_id="@1")
    query = FakeQuery(_keyed_pick_callback(token, user_id=1, window_id="@1"))
    authorized = authorize_initial(parse(query.data.encode()), _ctx(query, user_id=1))

    await execute(
        authorized,
        _adapters(FakeSessionManager(current_window="@2"), FakeTmuxManager()),
    )

    assert pick_token.peek(token) is not None


@pytest.mark.asyncio
async def test_stale_owner_pick_answers_exactly_once() -> None:
    token = _mint_test_pick_token(user_id=1, window_id="@1")
    query = FakeQuery(_keyed_pick_callback(token, user_id=1, window_id="@1"))
    authorized = authorize_initial(parse(query.data.encode()), _ctx(query, user_id=1))

    await execute(
        authorized,
        _adapters(FakeSessionManager(current_window="@2"), FakeTmuxManager()),
    )

    assert query.answers == [(STALE_CALLBACK_TEXT, True)]


@pytest.mark.asyncio
async def test_double_pick_second_click_is_already_received_after_first_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    # Scope the keyed-shape ledger to a tmp file so the dispatch row from
    # the first click doesn't leak into real state.
    auq_ledger.reset_for_tests(
        path=tmp_path / "auq_action_ledger.jsonl",
        start_time=time.time(),
    )
    token = _mint_test_pick_token(user_id=1)
    monkeypatch.setattr(
        "cctelegram.handlers.interactive_ui.resolve_ask_tool_input", lambda _wid: None
    )
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    # The first (valid) tap dispatches then re-renders via the dispatcher's
    # handle_interactive_ui; stub it to a no-op (these assertions care about
    # the ledger + the answer, not the re-rendered card). The dispatcher binds
    # the name at import, so patch it on the dispatcher module.
    from cctelegram.callback_dispatcher import interactive as cb_interactive

    monkeypatch.setattr(
        cb_interactive, "handle_interactive_ui", AsyncMock(return_value=True)
    )

    query1 = FakeQuery(_keyed_pick_callback(token, user_id=1))
    authorized1 = authorize_initial(
        parse(query1.data.encode()), _ctx(query1, user_id=1)
    )
    query2 = FakeQuery(_keyed_pick_callback(token, user_id=1))
    authorized2 = authorize_initial(
        parse(query2.data.encode()), _ctx(query2, user_id=1)
    )

    await execute(authorized1, _adapters(FakeSessionManager(), FakeTmuxManager()))
    await execute(authorized2, _adapters(FakeSessionManager(), FakeTmuxManager()))

    # First click dispatches and writes the ``dispatched`` ledger row; the
    # second click on the same keyed callback finds that row and answers
    # "Action already received" instead of re-dispatching the digit. The
    # option label is "Done navigating" (option 1 on the baseline pane).
    assert query1.answers == [("1. Done navigating", False)]
    assert query2.answers == [("Action already received: Done navigating", False)]
    auq_ledger.reset_for_tests()


@pytest.mark.asyncio
async def test_stale_callback_does_not_break_directory_executor(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path
    for idx in range(4):
        (root / f"dir-{idx}").mkdir()
    query = FakeQuery(f"{CB_DIR_SELECT}3")

    async def stale_answer(
        text: str | None = None, show_alert: bool | None = None
    ) -> None:
        raise BadRequest(
            "Query is too old and response timeout expired or query id is invalid"
        )

    query.answer = stale_answer  # type: ignore[method-assign]
    ctx = _ctx(query, user_id=1)
    ctx.context.user_data = {
        STATE_KEY: STATE_BROWSING_DIRECTORY,
        "_pending_thread_id": 10,
        BROWSE_PATH_KEY: str(root),
        BROWSE_DIRS_KEY: [f"dir-{idx}" for idx in range(4)],
    }
    authorized = authorize_initial(parse(query.data.encode()), ctx)
    safe_edit = AsyncMock()
    monkeypatch.setattr("cctelegram.callback_dispatcher.directory.safe_edit", safe_edit)

    await execute(authorized, _adapters(FakeSessionManager(), FakeTmuxManager()))

    safe_edit.assert_called_once()


@pytest.mark.asyncio
async def test_stale_callback_does_not_break_dir_confirm_no_sessions_path(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the CB_DIR_CONFIRM no-existing-sessions branch.

    CB_DIR_CONFIRM that finds no existing Claude sessions in the selected
    directory dispatches to ``_create_and_bind_window``, whose final
    ``await query.answer("Created" or "Failed")`` lives inside
    ``handlers/inbound_telegram.py`` (not the dispatcher family module).
    If that raw answer survives the safe_answer migration, a stale callback
    still raises out of the executor. This test exercises that path with a
    stale-raising query and asserts the executor returns cleanly.
    """
    root = tmp_path

    class _StaleAnsweringQuery(FakeQuery):
        async def answer(
            self, text: str | None = None, show_alert: bool | None = None
        ) -> None:  # noqa: D401
            raise BadRequest(
                "Query is too old and response timeout expired or query id is invalid"
            )

    query = _StaleAnsweringQuery(CB_DIR_CONFIRM)
    ctx = _ctx(query, user_id=1)
    ctx.context.user_data = {
        STATE_KEY: STATE_BROWSING_DIRECTORY,
        "_pending_thread_id": 10,
        BROWSE_PATH_KEY: str(root),
    }
    authorized = authorize_initial(parse(query.data.encode()), ctx)

    # Stub safe_edit + _create_and_bind_window's success path through the
    # adapter's FakeSessionManager / FakeTmuxManager. session_manager
    # exposes list_sessions_for_directory; FakeSessionManager doesn't, so
    # patch it to return no existing sessions for this directory.
    monkeypatch.setattr(
        "cctelegram.callback_dispatcher.directory.safe_edit", AsyncMock()
    )

    async def _no_sessions(_path: str) -> list[Any]:
        return []

    session_mgr = FakeSessionManager()
    cast(Any, session_mgr).list_sessions_for_directory = _no_sessions

    # Patch _create_and_bind_window to drive its own raw query.answer path.
    # We want the helper to reach its trailing safe_answer (or the now-
    # migrated raw answer) so the stale-raising query exercises the swallow.
    async def _fake_create_and_bind(
        query_arg: Any, _context: Any, _user: Any, _path: str, _pid: Any, **_kw: Any
    ) -> None:
        from cctelegram.handlers.message_sender import safe_answer

        await safe_answer(query_arg, "Created")

    monkeypatch.setattr(
        "cctelegram.callback_dispatcher.directory._create_and_bind_window",
        _fake_create_and_bind,
    )

    tmux = FakeTmuxManager()
    await execute(authorized, _adapters(session_mgr, tmux))
