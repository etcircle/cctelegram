"""Wave 3b: reject-if-held keystroke classification + compound transactions.

Every direct pane-keystroke path is classified against the per-window send
lock (Wave 3a):

  - the 8 interactive control keys (Up/Down/Left/Right/Escape/Enter/Space/Tab)
    and the ``aqt:`` toggle digit REJECT when the lock is held ("⏳ Action in
    progress — try again in a second") and otherwise try-acquire + send the
    single key UNDER the lock;
  - ``/esc`` is reject-if-held (Hermes R2 P1-1 — NO bypass: a bypassed Escape
    that dismisses the picker between nav-verify and Enter would make
    ``_classify_advance`` read ``resolved=True`` as success and mint a FALSE
    ``dispatched``);
  - ``/usage`` and the screenshot quick-key hold the lock across their whole
    send→settle→capture(→dismiss) transaction, with all Telegram I/O strictly
    after release (Hermes P2-5; the lock is a leaf).

The R2 regression pin (test_r2_esc_mid_dispatch_*) forces the exact
interleaving Hermes R2 described and proves the lock closes the class.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cctelegram.callback_dispatcher import (
    DispatcherAdapters,
    authorize_initial,
    execute,
    parse,
)
from cctelegram.callback_dispatcher import bash as cbb
from cctelegram.callback_dispatcher import interactive as cbi
from cctelegram.handlers import auq_ledger, auq_source, pick_token
from cctelegram.handlers.callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_TOGGLE,
    CB_ASK_UP,
    CB_KEYS_PREFIX,
)
from cctelegram.terminal_parser import resolve_ask_form
from tests.conftest import Fake168Picker, _Screen

BUSY_TEXT = "⏳ Action in progress — try again in a second"
USAGE_BUSY_TEXT = "⏳ Window busy — try again in a second"
SEND_FAILED_TEXT = "❌ Failed to send — window may be gone"

_FX = Path(__file__).parents[1] / "fixtures"
_SINGLE_PANE = (_FX / "auq_single_select_with_affordances_pane.txt").read_text()
_MULTI_PANE = (_FX / "auq_multiselect_long_scrolled_toggled_S500.txt").read_text()
_OPT2_LABEL = "Descriptions + ordering only (defer the first-render backstop)"

_NAV_CASES = [
    (CB_ASK_UP, "Up"),
    (CB_ASK_DOWN, "Down"),
    (CB_ASK_LEFT, "Left"),
    (CB_ASK_RIGHT, "Right"),
    (CB_ASK_ESC, "Escape"),
    (CB_ASK_ENTER, "Enter"),
    (CB_ASK_SPACE, "Space"),
    (CB_ASK_TAB, "Tab"),
]


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path) -> Any:
    pick_token.reset_for_tests()
    auq_ledger.reset_for_tests(path=tmp_path / "ledger.jsonl", start_time=time.time())
    auq_source.reset_for_tests()
    yield
    pick_token.reset_for_tests()
    auq_ledger.reset_for_tests()
    auq_source.reset_for_tests()


class FakeQuery:
    def __init__(self, data: str, lock: asyncio.Lock | None = None) -> None:
        self.data = data
        self.message = SimpleNamespace(message_thread_id=10)
        self.answers: list[tuple[str | None, bool | None]] = []
        self._lock = lock
        self.locked_during_answer: list[bool] = []
        self.locked_during_edit: list[bool] = []
        self.media_edits: list[Any] = []

    async def answer(
        self, text: str | None = None, show_alert: bool | None = None
    ) -> None:
        if self._lock is not None:
            self.locked_during_answer.append(self._lock.locked())
        self.answers.append((text, show_alert))

    async def edit_message_media(self, media: Any = None, reply_markup: Any = None):
        if self._lock is not None:
            self.locked_during_edit.append(self._lock.locked())
        self.media_edits.append((media, reply_markup))


class FakeSessionManager:
    def __init__(self, current_window: str | None = "@1") -> None:
        self.current_window = current_window

    def resolve_window_for_thread(
        self, _user_id: int, _thread_id: int | None
    ) -> str | None:
        return self.current_window


class LockFakeTmux:
    """Fake tmux manager owning a real per-window lock registry."""

    def __init__(self, pane: str = "") -> None:
        self.locks: dict[str, asyncio.Lock] = {}
        self.sent: list[tuple[str, str, bool, bool]] = []
        self.pane = pane
        self.locked_during_send: list[bool] = []
        self.locked_during_capture: list[bool] = []
        self.events: list[str] = []

    def window_send_lock(self, window_id: str) -> asyncio.Lock:
        return self.locks.setdefault(window_id, asyncio.Lock())

    async def find_window_by_id(self, window_id: str) -> Any:
        return SimpleNamespace(window_id=window_id, window_name="repo")

    async def send_keys(
        self, window_id: str, keys: str, enter: bool = True, literal: bool = True
    ) -> bool:
        self.locked_during_send.append(self.window_send_lock(window_id).locked())
        self.sent.append((window_id, keys, enter, literal))
        self.events.append(f"send:{keys}")
        return True

    async def capture_pane(
        self, window_id: str, with_ansi: bool = False, scrollback_lines: int = 0
    ) -> str:
        del with_ansi, scrollback_lines
        self.locked_during_capture.append(self.window_send_lock(window_id).locked())
        self.events.append("capture")
        return self.pane


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


def _adapters(session_manager: Any, tmux: Any) -> DispatcherAdapters:
    return DispatcherAdapters(
        session_manager=session_manager,
        tmux_manager=tmux,
        bot=SimpleNamespace(),
        route_runtime=SimpleNamespace(
            snapshot=lambda _route: None,
            mark_inbound_sent=AsyncMock(),
        ),
        config=SimpleNamespace(browse_root="."),
        terminal_parser=SimpleNamespace(resolve_ask_form=resolve_ask_form),
    )


# ──────────────────────────────────────────────────────────────────────────
# (a) the 8 interactive control keys: reject-if-held / send-under-lock
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix,key", _NAV_CASES)
async def test_control_rejected_while_lock_held(
    prefix: str, key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmux = LockFakeTmux()
    monkeypatch.setattr(
        cbi,
        "assert_nav_dispatchable",
        AsyncMock(return_value=SimpleNamespace(window_id="@1")),
    )
    hui = AsyncMock(return_value=True)
    monkeypatch.setattr(cbi, "handle_interactive_ui", hui)
    cim = AsyncMock()
    monkeypatch.setattr(cbi, "clear_interactive_msg", cim)
    query = FakeQuery(f"{prefix}@1")
    authorized = authorize_initial(parse(query.data.encode()), _ctx(query))
    lock = tmux.window_send_lock("@1")
    await lock.acquire()
    try:
        await execute(authorized, _adapters(FakeSessionManager(), tmux))
    finally:
        lock.release()
    assert query.answers == [(BUSY_TEXT, False)]
    assert tmux.sent == [], f"{key} must NOT be sent while the lock is held"
    hui.assert_not_called()
    cim.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix,key", _NAV_CASES)
async def test_control_sends_under_lock_when_free(
    prefix: str, key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmux = LockFakeTmux()
    monkeypatch.setattr(
        cbi,
        "assert_nav_dispatchable",
        AsyncMock(return_value=SimpleNamespace(window_id="@1")),
    )
    monkeypatch.setattr(cbi, "handle_interactive_ui", AsyncMock(return_value=True))
    monkeypatch.setattr(cbi, "clear_interactive_msg", AsyncMock())
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    query = FakeQuery(f"{prefix}@1")
    authorized = authorize_initial(parse(query.data.encode()), _ctx(query))
    await execute(authorized, _adapters(FakeSessionManager(), tmux))
    assert [(k, e, lit) for _w, k, e, lit in tmux.sent] == [(key, False, False)]
    # Try-acquire (option b): the single key is sent UNDER the lock.
    assert tmux.locked_during_send == [True]
    assert query.answers and query.answers[0][0] != BUSY_TEXT
    assert not tmux.window_send_lock("@1").locked()


# ──────────────────────────────────────────────────────────────────────────
# (a2) the waiter gap (Hermes Wave-3b P2-1): ``release()`` frees the lock and
#      wakes the first waiter, but until the loop schedules that waiter the
#      lock reports ``locked() == False`` while a pending waiter sits in its
#      queue. A control arriving in that gap must be REJECTED, not queued
#      behind the waiter and fired late.
# ──────────────────────────────────────────────────────────────────────────


async def _park_waiter(
    lock: asyncio.Lock, body: Any
) -> tuple[asyncio.Task[None], asyncio.Event]:
    """Queue a waiter task on a held ``lock``; returns (task, ran-event)."""
    ran = asyncio.Event()

    async def _waiter() -> None:
        async with lock:
            ran.set()
            await body()

    task = asyncio.create_task(_waiter())
    await asyncio.sleep(0)  # park the waiter in lock._waiters
    assert not ran.is_set()
    return task, ran


@pytest.mark.asyncio
async def test_control_rejected_in_release_to_waiter_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A control callback arriving AFTER release but BEFORE the woken waiter
    resumes must get the busy answer with NO key sent — reject, never queue."""
    tmux = LockFakeTmux()
    monkeypatch.setattr(
        cbi,
        "assert_nav_dispatchable",
        AsyncMock(return_value=SimpleNamespace(window_id="@1")),
    )
    hui = AsyncMock(return_value=True)
    monkeypatch.setattr(cbi, "handle_interactive_ui", hui)
    monkeypatch.setattr(cbi, "clear_interactive_msg", AsyncMock())
    query = FakeQuery(f"{CB_ASK_ESC}@1")
    authorized = authorize_initial(parse(query.data.encode()), _ctx(query))

    lock = tmux.window_send_lock("@1")
    await lock.acquire()  # the in-flight transaction
    waiter_task, waiter_ran = await _park_waiter(
        lock, lambda: tmux.send_keys("@1", "waiter text", enter=True, literal=True)
    )
    # The release→waiter-wakeup gap: locked() is False, the waiter is still
    # queued and has NOT run. The control callback runs in this exact window
    # (no scheduling yield between release() and execute()).
    lock.release()
    assert not lock.locked()
    await execute(authorized, _adapters(FakeSessionManager(), tmux))

    assert query.answers == [(BUSY_TEXT, False)], (
        "control in the release→waiter gap must be rejected as busy"
    )
    assert all(k != "Escape" for _w, k, _e, _l in tmux.sent), (
        "the control key was QUEUED behind the pending waiter and fired late"
    )
    hui.assert_not_called()
    await asyncio.wait_for(waiter_task, 2.0)
    assert waiter_ran.is_set()


@pytest.mark.asyncio
async def test_esc_rejected_in_release_to_waiter_gap() -> None:
    """Same waiter-gap interleaving through the /esc command path."""
    lock = asyncio.Lock()
    tmux, _ = _make_bot_tmux(lock)
    safe_reply_mock = AsyncMock()
    await lock.acquire()
    waiter_task, _ = await _park_waiter(lock, AsyncMock())
    lock.release()
    assert not lock.locked()
    with _bot_patches(tmux, safe_reply_mock):
        from cctelegram.bot import esc_command

        await esc_command(_make_update(), MagicMock())
    safe_reply_mock.assert_awaited_once()
    assert safe_reply_mock.call_args.args[1] == BUSY_TEXT
    tmux.send_keys.assert_not_called()
    await asyncio.wait_for(waiter_task, 2.0)


@pytest.mark.asyncio
async def test_lock_busy_cpython_contract_free_with_waiter() -> None:
    """CPython contract guard: ``_lock_busy`` must see the free-with-pending-
    waiter state on the REAL asyncio.Lock. If a future CPython renames the
    private ``_waiters`` attribute, this breaks loudly in CI instead of the
    helper silently degrading to the bare ``locked()`` behavior."""
    lock = asyncio.Lock()
    assert cbi._lock_busy(lock) is False  # plain free
    await lock.acquire()
    assert cbi._lock_busy(lock) is True  # plain held

    waited = asyncio.Event()

    async def _waiter() -> None:
        async with lock:
            waited.set()

    task = asyncio.create_task(_waiter())
    await asyncio.sleep(0)  # park in lock._waiters
    lock.release()
    # The gap: locked() is False but a live (non-cancelled) waiter is queued.
    assert not lock.locked()
    assert cbi._lock_busy(lock) is True, (
        "free-with-pending-waiter must be busy — asyncio.Lock._waiters not "
        "visible (CPython internal renamed?)"
    )
    await asyncio.wait_for(task, 2.0)
    assert waited.is_set()
    assert cbi._lock_busy(lock) is False  # waiter done → genuinely free

    # A cancelled-only waiter queue is NOT busy (mirrors acquire()'s own
    # all-cancelled fast path).
    await lock.acquire()
    task2 = asyncio.create_task(lock.acquire())
    await asyncio.sleep(0)
    task2.cancel()
    await asyncio.sleep(0)
    lock.release()
    assert cbi._lock_busy(lock) is False


# ──────────────────────────────────────────────────────────────────────────
# (a) aqt: multi-select toggle digit
# ──────────────────────────────────────────────────────────────────────────


def _mint_multi_token(user_id: int = 1, window_id: str = "@1") -> tuple[str, str]:
    """Mint a toggle token matching the real multiselect fixture pane."""
    form = resolve_ask_form(None, _MULTI_PANE)
    assert form is not None and form.select_mode == "multi"
    src = auq_source.resolve_auq_source(window_id, None, _MULTI_PANE)
    entry = pick_token.PickTokenEntry(
        window_id=window_id,
        user_id=user_id,
        thread_id=10,
        fingerprint=form.fingerprint(),
        option_number=1,
        option_label=form.options[0].label,
        is_review_submit=False,
        expires_at=time.monotonic() + 300,
        source_kind=src.kind,
        source_fingerprint=src.source_fingerprint,
        row_generation=1,
    )
    token = pick_token.mint(entry)
    route_hash = auq_ledger.make_route_hash(user_id, 10, window_id)
    return token, f"{CB_ASK_TOGGLE}{route_hash}:{form.fingerprint()[:8]}:1:{token}"


@pytest.mark.asyncio
async def test_toggle_rejected_while_lock_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmux = LockFakeTmux(pane=_MULTI_PANE)
    hui = AsyncMock(return_value=True)
    monkeypatch.setattr(cbi, "handle_interactive_ui", hui)
    _token, data = _mint_multi_token()
    query = FakeQuery(data)
    authorized = authorize_initial(parse(query.data.encode()), _ctx(query))
    lock = tmux.window_send_lock("@1")
    await lock.acquire()
    try:
        await execute(authorized, _adapters(FakeSessionManager(), tmux))
    finally:
        lock.release()
    assert query.answers == [(BUSY_TEXT, False)]
    assert tmux.sent == [], "toggle digit must NOT be sent while the lock is held"
    hui.assert_not_called()


@pytest.mark.asyncio
async def test_toggle_dispatches_under_lock_when_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmux = LockFakeTmux(pane=_MULTI_PANE)
    monkeypatch.setattr(cbi, "handle_interactive_ui", AsyncMock(return_value=True))
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    _token, data = _mint_multi_token()
    query = FakeQuery(data)
    authorized = authorize_initial(parse(query.data.encode()), _ctx(query))
    await execute(authorized, _adapters(FakeSessionManager(), tmux))
    assert [(k, e, lit) for _w, k, e, lit in tmux.sent] == [("1", False, True)]
    assert tmux.locked_during_send == [True]
    assert query.answers == [("Toggled 1", False)]


# ──────────────────────────────────────────────────────────────────────────
# (b) + (e) /esc: reject-if-held; free lock sends under the lock
# ──────────────────────────────────────────────────────────────────────────


def _make_bot_tmux(
    lock: asyncio.Lock, *, send_results: bool | list[bool] = True
) -> tuple[MagicMock, list[bool]]:
    tmux = MagicMock()
    tmux.window_send_lock = MagicMock(return_value=lock)
    window = MagicMock()
    window.window_id = "@1"
    tmux.find_window_by_id = AsyncMock(return_value=window)
    locked_during_send: list[bool] = []
    results = send_results if isinstance(send_results, list) else None
    single = send_results if isinstance(send_results, bool) else True

    async def _send(*args: Any, **kwargs: Any) -> bool:
        locked_during_send.append(lock.locked())
        if results is not None:
            return results.pop(0)
        return single

    tmux.send_keys = AsyncMock(side_effect=_send)
    tmux.capture_pane = AsyncMock(return_value="raw usage pane")
    return tmux, locked_during_send


def _bot_patches(tmux: Any, safe_reply_mock: Any) -> Any:
    import contextlib

    @contextlib.contextmanager
    def _cm():
        with (
            patch("cctelegram.bot.is_user_allowed", return_value=True),
            patch("cctelegram.bot._get_thread_id", return_value=42),
            patch("cctelegram.bot.session_manager") as mock_sm,
            patch("cctelegram.bot.tmux_manager", tmux),
            patch("cctelegram.bot.safe_reply", safe_reply_mock),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_sm.resolve_window_for_thread.return_value = "@1"
            yield

    return _cm()


def _make_update() -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = 1
    update.message = MagicMock()
    update.message.message_thread_id = 42
    return update


@pytest.mark.asyncio
async def test_esc_rejected_while_lock_held() -> None:
    lock = asyncio.Lock()
    tmux, _ = _make_bot_tmux(lock)
    safe_reply_mock = AsyncMock()
    await lock.acquire()
    try:
        with _bot_patches(tmux, safe_reply_mock):
            from cctelegram.bot import esc_command

            await esc_command(_make_update(), MagicMock())
    finally:
        lock.release()
    safe_reply_mock.assert_awaited_once()
    assert safe_reply_mock.call_args.args[1] == BUSY_TEXT
    tmux.send_keys.assert_not_called()


@pytest.mark.asyncio
async def test_esc_free_lock_sends_escape_under_lock() -> None:
    lock = asyncio.Lock()
    tmux, locked_during_send = _make_bot_tmux(lock)
    locked_during_reply: list[bool] = []

    async def _reply(*args: Any, **kwargs: Any) -> None:
        locked_during_reply.append(lock.locked())

    safe_reply_mock = AsyncMock(side_effect=_reply)
    with _bot_patches(tmux, safe_reply_mock):
        from cctelegram.bot import esc_command

        await esc_command(_make_update(), MagicMock())
    assert tmux.send_keys.await_count == 1
    assert tmux.send_keys.call_args.args[1] == "\x1b"
    assert locked_during_send == [True]
    safe_reply_mock.assert_awaited_once()
    assert safe_reply_mock.call_args.args[1] == "⎋ Sent Escape"
    assert locked_during_reply == [False], "no Telegram I/O under the lock"


@pytest.mark.asyncio
async def test_esc_free_lock_send_failure_reply_preserved() -> None:
    lock = asyncio.Lock()
    tmux, _ = _make_bot_tmux(lock, send_results=False)
    safe_reply_mock = AsyncMock()
    with _bot_patches(tmux, safe_reply_mock):
        from cctelegram.bot import esc_command

        await esc_command(_make_update(), MagicMock())
    safe_reply_mock.assert_awaited_once()
    assert safe_reply_mock.call_args.args[1] == SEND_FAILED_TEXT
    assert not lock.locked()


# ──────────────────────────────────────────────────────────────────────────
# (d) /usage: reject-if-held; whole transaction under the lock
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_usage_rejected_while_lock_held() -> None:
    lock = asyncio.Lock()
    tmux, _ = _make_bot_tmux(lock)
    safe_reply_mock = AsyncMock()
    await lock.acquire()
    try:
        with _bot_patches(tmux, safe_reply_mock):
            from cctelegram.bot import usage_command

            await usage_command(_make_update(), MagicMock())
    finally:
        lock.release()
    safe_reply_mock.assert_awaited_once()
    assert safe_reply_mock.call_args.args[1] == USAGE_BUSY_TEXT
    # No /usage text may land in the pane, and no dependent capture runs.
    tmux.send_keys.assert_not_called()
    tmux.capture_pane.assert_not_called()


@pytest.mark.asyncio
async def test_usage_normal_transaction_under_lock_reply_after_release() -> None:
    lock = asyncio.Lock()
    tmux, locked_during_send = _make_bot_tmux(lock)
    locked_during_capture: list[bool] = []

    async def _capture(*args: Any, **kwargs: Any) -> str:
        locked_during_capture.append(lock.locked())
        return "raw usage pane"

    tmux.capture_pane = AsyncMock(side_effect=_capture)
    locked_during_reply: list[bool] = []

    async def _reply(*args: Any, **kwargs: Any) -> None:
        locked_during_reply.append(lock.locked())

    safe_reply_mock = AsyncMock(side_effect=_reply)
    with _bot_patches(tmux, safe_reply_mock):
        from cctelegram.bot import usage_command

        await usage_command(_make_update(), MagicMock())
    # send /usage + dismiss Escape + the capture all ran UNDER the lock …
    assert tmux.send_keys.await_count == 2
    assert locked_during_send == [True, True]
    assert locked_during_capture == [True]
    # … and the usage output was presented strictly AFTER release.
    safe_reply_mock.assert_awaited_once()
    assert "raw usage pane" in safe_reply_mock.call_args.args[1]
    assert locked_during_reply == [False]
    assert not lock.locked()


# ──────────────────────────────────────────────────────────────────────────
# (c) THE R2 REGRESSION PIN: /esc mid-dispatch is rejected; no false
#     ``dispatched`` for an Escape-dismissed picker.
# ──────────────────────────────────────────────────────────────────────────


class _R2Picker(Fake168Picker):
    """Fake168Picker gated at the dispatch's Enter send, Escape-dismissible.

    The dispatch task blocks just BEFORE the committing Enter (after the nav
    verify capture passed) — the exact window Hermes R2 identified. An Escape
    (or ``\\x1b``) landing on the live picker DISMISSES it (``resolved`` pane),
    which would make the dispatch's confirm capture read ``resolved=True`` and
    ``_classify_advance`` mint a FALSE ``dispatched`` on a single-question pick.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.lock = asyncio.Lock()
        self.at_enter = asyncio.Event()
        self.release_enter = asyncio.Event()
        self.escape_dismissed = False

    def window_send_lock(self, window_id: str) -> asyncio.Lock:
        return self.lock

    async def send_keys(
        self, window_id: str, keys: str, enter: bool = True, literal: bool = True
    ) -> bool:
        if keys in ("Escape", "\x1b"):
            self.sent.append((window_id, keys, enter, literal))
            if not self.resolved:
                self.escape_dismissed = True
                self.idx = len(self.screens)  # picker dismissed → resolved pane
            return True
        if keys == "Enter" and not self.at_enter.is_set():
            self.at_enter.set()
            await self.release_enter.wait()
        return await super().send_keys(window_id, keys, enter=enter, literal=literal)


@pytest.mark.asyncio
async def test_r2_esc_mid_dispatch_rejected_no_false_dispatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    picker = _R2Picker("@1", [_Screen(_SINGLE_PANE, 3, 5)])
    form = resolve_ask_form(None, _SINGLE_PANE)
    assert form is not None
    route_hash = auq_ledger.make_route_hash(1, 10, "@1")
    ledger_key = auq_ledger.make_ledger_key(route_hash, form.fingerprint()[:8], 2)
    auq_ledger.record(
        ledger_key,
        state="accepted",
        user_id=1,
        window_id="@1",
        full_fingerprint=form.fingerprint(),
        option_number=2,
        option_label=_OPT2_LABEL,
    )
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    monkeypatch.setattr(cbi, "handle_interactive_ui", AsyncMock(return_value=True))
    query = FakeQuery("aqp:test", picker.lock)
    dispatch = asyncio.create_task(
        cbi._dispatch_pick(
            query=query,
            context=SimpleNamespace(bot=SimpleNamespace()),
            user=SimpleNamespace(id=1),
            tmux_manager=picker,
            adapters=SimpleNamespace(session_manager=SimpleNamespace()),
            w=SimpleNamespace(window_id="@1"),
            window_id="@1",
            thread_id=10,
            fingerprint=form.fingerprint(),
            option_number=2,
            option_label=_OPT2_LABEL,
            is_review_submit=False,
            current_form=form,
            ledger_key=ledger_key,
        )
    )
    # The dispatch is now parked between nav-verify and Enter, lock held.
    await asyncio.wait_for(picker.at_enter.wait(), 2.0)
    assert picker.lock.locked()

    # /esc arrives mid-critical-section → MUST be rejected, NOT bypassed.
    esc_replies: list[str] = []

    async def _reply(_msg: Any, text: str, *args: Any, **kwargs: Any) -> None:
        esc_replies.append(text)

    esc_tmux = MagicMock()
    esc_tmux.window_send_lock = MagicMock(return_value=picker.lock)
    esc_window = MagicMock()
    esc_window.window_id = "@1"
    esc_tmux.find_window_by_id = AsyncMock(return_value=esc_window)
    esc_tmux.send_keys = AsyncMock(side_effect=picker.send_keys)
    with _bot_patches(esc_tmux, AsyncMock(side_effect=_reply)):
        from cctelegram.bot import esc_command

        await esc_command(_make_update(), MagicMock())

    assert esc_replies == [BUSY_TEXT]
    assert not picker.escape_dismissed, (
        "a bypassed Escape dismissed the picker mid-dispatch — the R2 false-"
        "dispatched interleaving"
    )
    assert ("@1", "\x1b", False, True) not in picker.sent
    assert ("@1", "Escape", False, False) not in picker.sent

    # DIRECT no-false-dispatched proof (Hermes P3-1): while the dispatch is
    # still parked pre-Enter — after the rejected /esc — the ledger row must
    # still be the caller's ``accepted`` claim. No terminal ``dispatched``
    # (or any other state) may have been minted in the blocked window.
    entry_mid = auq_ledger.lookup(ledger_key)
    assert entry_mid is not None and entry_mid.state == "accepted", (
        "a ledger state was minted while Enter was blocked / after the rejected Escape"
    )

    # The dispatch completes normally; the recorded ``dispatched`` is the
    # GENUINE Enter-confirmed advance, never an Escape-dismissal misread.
    picker.release_enter.set()
    await asyncio.wait_for(dispatch, 2.0)
    entry = auq_ledger.lookup(ledger_key)
    assert entry is not None and entry.state == "dispatched"
    assert picker.resolved and not picker.escape_dismissed
    assert not picker.lock.locked()


# ──────────────────────────────────────────────────────────────────────────
# (e) + (f) screenshot quick-key compound transaction
# ──────────────────────────────────────────────────────────────────────────


def _quickkey_authorized(query: FakeQuery) -> Any:
    return authorize_initial(parse(query.data.encode()), _ctx(query))


@pytest.mark.asyncio
async def test_quickkey_rejected_while_lock_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmux = LockFakeTmux(pane="pane text")
    monkeypatch.setattr(cbb, "text_to_image", AsyncMock(return_value=b"png"))
    query = FakeQuery(f"{CB_KEYS_PREFIX}up:@1")
    authorized = _quickkey_authorized(query)
    lock = tmux.window_send_lock("@1")
    await lock.acquire()
    try:
        await execute(authorized, _adapters(FakeSessionManager(), tmux))
    finally:
        lock.release()
    assert query.answers == [(BUSY_TEXT, False)]
    assert tmux.sent == []
    assert query.media_edits == []


@pytest.mark.asyncio
async def test_quickkey_transaction_under_lock_telegram_io_after_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmux = LockFakeTmux(pane="pane text")
    monkeypatch.setattr(cbb, "text_to_image", AsyncMock(return_value=b"png"))
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    query = FakeQuery(f"{CB_KEYS_PREFIX}up:@1", tmux.window_send_lock("@1"))
    authorized = _quickkey_authorized(query)
    await execute(authorized, _adapters(FakeSessionManager(), tmux))
    # key send + the dependent pane capture ran UNDER the lock …
    assert tmux.locked_during_send == [True]
    assert tmux.locked_during_capture == [True]
    # … all Telegram I/O (answer + media edit) strictly AFTER release.
    assert query.locked_during_answer and not any(query.locked_during_answer)
    assert query.locked_during_edit and not any(query.locked_during_edit)
    assert len(query.media_edits) == 1
    assert not tmux.window_send_lock("@1").locked()


@pytest.mark.asyncio
async def test_quickkey_transaction_atomic_vs_concurrent_same_window_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent text send cannot land between the quick-key send and its
    settle/capture; a DIFFERENT window's lock is unaffected."""

    class _GatedTmux(LockFakeTmux):
        def __init__(self, pane: str) -> None:
            super().__init__(pane)
            self.in_capture = asyncio.Event()
            self.release_capture = asyncio.Event()

        async def capture_pane(
            self, window_id: str, with_ansi: bool = False, scrollback_lines: int = 0
        ) -> str:
            self.in_capture.set()
            await self.release_capture.wait()
            return await super().capture_pane(
                window_id, with_ansi=with_ansi, scrollback_lines=scrollback_lines
            )

    tmux = _GatedTmux(pane="pane text")
    monkeypatch.setattr(cbb, "text_to_image", AsyncMock(return_value=b"png"))
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    query = FakeQuery(f"{CB_KEYS_PREFIX}up:@1")
    authorized = _quickkey_authorized(query)
    quickkey = asyncio.create_task(
        execute(authorized, _adapters(FakeSessionManager(), tmux))
    )
    await asyncio.wait_for(tmux.in_capture.wait(), 2.0)

    async def _competing_text_send() -> None:
        async with tmux.window_send_lock("@1"):
            await tmux.send_keys("@1", "competing text", enter=True, literal=True)

    competitor = asyncio.create_task(_competing_text_send())
    done, _ = await asyncio.wait({competitor}, timeout=0.05)
    assert not done, "same-window text send must queue behind the transaction"
    # A different window's lock is NOT serialized by this transaction.
    async with tmux.window_send_lock("@2"):
        pass
    tmux.release_capture.set()
    await asyncio.wait_for(quickkey, 2.0)
    await asyncio.wait_for(competitor, 2.0)
    assert tmux.events == ["send:Up", "capture", "send:competing text"]
