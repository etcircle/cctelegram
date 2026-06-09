"""Wave 3a: ``_dispatch_pick`` locked/unlocked split (finding 6, Hermes P1-3).

The pane-critical section (cursor find, nav sends, settles, verify capture,
Enter, confirm capture, ``_classify_advance``, and the TERMINAL ledger write)
runs entirely under the per-window send lock; the response section
(``safe_answer`` + ``_rerender_picker`` → Telegram I/O) runs strictly AFTER
release, in all three outcome paths (dispatched / not_advanced /
commit_unconfirmed).

The seam is proven with an instrumented per-window lock: the fake tmux
manager exposes ``window_send_lock`` and every pane call + the ledger write
records whether the lock was held at call time; the callback answer and the
re-render record that it was NOT.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cctelegram.callback_dispatcher import interactive as cbi
from cctelegram.handlers import auq_ledger, auq_source, pick_token
from cctelegram.terminal_parser import resolve_ask_form
from tests.conftest import Fake168Picker, _Screen

_FX = Path(__file__).parents[1] / "fixtures"
_SINGLE_PANE = (_FX / "auq_single_select_with_affordances_pane.txt").read_text()
_WINDOW_ID = "@1"
_THREAD_ID = 10
_OWNER_ID = 1
_OPT1_LABEL = "Full fix: descriptions + ordering + first-render robustness"
_OPT2_LABEL = "Descriptions + ordering only (defer the first-render backstop)"
_OPT3_LABEL = "Descriptions only (defer ordering too)"


class LockSeamPicker(Fake168Picker):
    """Fake168Picker that owns the per-window lock and records held-ness."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.lock = asyncio.Lock()
        self.locked_during_send: list[bool] = []
        self.locked_during_capture: list[bool] = []

    def window_send_lock(self, window_id: str) -> asyncio.Lock:
        assert window_id == self.window_id
        return self.lock

    async def send_keys(
        self, window_id: str, keys: str, enter: bool = True, literal: bool = True
    ) -> bool:
        self.locked_during_send.append(self.lock.locked())
        return await super().send_keys(window_id, keys, enter=enter, literal=literal)

    async def capture_pane(
        self, window_id: str, with_ansi: bool = False, scrollback_lines: int = 0
    ) -> str:
        self.locked_during_capture.append(self.lock.locked())
        return await super().capture_pane(
            window_id, with_ansi=with_ansi, scrollback_lines=scrollback_lines
        )


class _StuckCursorPicker(LockSeamPicker):
    """Down/Up swallowed → nav-verify FAILS → ``not_advanced`` (pre-commit bail)."""

    async def send_keys(
        self, window_id: str, keys: str, enter: bool = True, literal: bool = True
    ) -> bool:
        self.locked_during_send.append(self.lock.locked())
        self.sent.append((window_id, keys, enter, literal))
        return True  # cursor never moves; Enter (never reached) would no-op too


class _StuckEnterPicker(LockSeamPicker):
    """Enter is a no-op → the form never advances → ``commit_unconfirmed``."""

    async def send_keys(
        self, window_id: str, keys: str, enter: bool = True, literal: bool = True
    ) -> bool:
        self.locked_during_send.append(self.lock.locked())
        self.sent.append((window_id, keys, enter, literal))
        if keys in ("Down", "Up") and not self.resolved:
            scr = self.screens[self.idx]
            if keys == "Down":
                self.cursor = self.cursor + 1 if self.cursor < scr.n_nav else 1
            else:
                self.cursor = self.cursor - 1 if self.cursor > 1 else scr.n_nav
        return True


class LockSeamQuery:
    def __init__(self, lock: asyncio.Lock) -> None:
        self._lock = lock
        self.answers: list[tuple[str | None, bool | None]] = []
        self.locked_during_answer: list[bool] = []
        self.message = SimpleNamespace(message_thread_id=_THREAD_ID)

    async def answer(
        self, text: str | None = None, show_alert: bool | None = None
    ) -> None:
        self.locked_during_answer.append(self._lock.locked())
        self.answers.append((text, show_alert))


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    pick_token.reset_for_tests()
    auq_ledger.reset_for_tests(path=tmp_path / "ledger.jsonl", start_time=time.time())
    auq_source.reset_for_tests()
    yield
    pick_token.reset_for_tests()
    auq_ledger.reset_for_tests()
    auq_source.reset_for_tests()


def _ledger_key(option_number: int) -> str:
    form = resolve_ask_form(None, _SINGLE_PANE)
    assert form is not None
    route_hash = auq_ledger.make_route_hash(_OWNER_ID, _THREAD_ID, _WINDOW_ID)
    return auq_ledger.make_ledger_key(route_hash, form.fingerprint()[:8], option_number)


def _seed_accepted(option_number: int, option_label: str) -> str:
    """Write the ``accepted`` claim the live path records BEFORE _dispatch_pick."""
    form = resolve_ask_form(None, _SINGLE_PANE)
    assert form is not None
    key = _ledger_key(option_number)
    auq_ledger.record(
        key,
        state="accepted",
        user_id=_OWNER_ID,
        window_id=_WINDOW_ID,
        full_fingerprint=form.fingerprint(),
        option_number=option_number,
        option_label=option_label,
    )
    return key


async def _run_dispatch(
    picker: LockSeamPicker,
    option_number: int,
    option_label: str,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[LockSeamQuery, list[bool], list[bool]]:
    """Drive ``_dispatch_pick`` directly; return (query, rerender_lockstate,
    ledger_write_lockstate)."""
    ledger_key = _seed_accepted(option_number, option_label)
    monkeypatch.setattr("asyncio.sleep", AsyncMock())  # NAV/COMMIT settles
    locked_during_rerender: list[bool] = []

    async def fake_handle_interactive_ui(*args: Any, **kwargs: Any) -> bool:
        locked_during_rerender.append(picker.lock.locked())
        return True

    monkeypatch.setattr(cbi, "handle_interactive_ui", fake_handle_interactive_ui)

    locked_during_record: list[bool] = []
    real_record = auq_ledger.record

    def recording_record(*args: Any, **kwargs: Any) -> Any:
        locked_during_record.append(picker.lock.locked())
        return real_record(*args, **kwargs)

    monkeypatch.setattr(auq_ledger, "record", recording_record)

    form = resolve_ask_form(None, _SINGLE_PANE)
    assert form is not None
    query = LockSeamQuery(picker.lock)
    await cbi._dispatch_pick(
        query=query,
        context=SimpleNamespace(bot=SimpleNamespace()),
        user=SimpleNamespace(id=_OWNER_ID),
        tmux_manager=picker,
        adapters=SimpleNamespace(session_manager=SimpleNamespace()),
        w=SimpleNamespace(window_id=_WINDOW_ID),
        window_id=_WINDOW_ID,
        thread_id=_THREAD_ID,
        fingerprint=form.fingerprint(),
        option_number=option_number,
        option_label=option_label,
        is_review_submit=False,
        current_form=form,
        ledger_key=ledger_key,
    )
    return query, locked_during_rerender, locked_during_record


def _assert_seam(
    picker: LockSeamPicker,
    query: LockSeamQuery,
    rerender_states: list[bool],
    record_states: list[bool],
) -> None:
    # Pane-critical work + the terminal ledger write ran UNDER the lock …
    assert picker.locked_during_send and all(picker.locked_during_send)
    assert all(picker.locked_during_capture)
    assert record_states and all(record_states)
    # … while the Telegram response ran strictly AFTER release.
    assert query.locked_during_answer and not any(query.locked_during_answer)
    assert rerender_states and not any(rerender_states)
    assert not picker.lock.locked()


@pytest.mark.asyncio
async def test_success_path_lock_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    picker = LockSeamPicker(_WINDOW_ID, [_Screen(_SINGLE_PANE, 3, 5)])
    query, rerender_states, record_states = await _run_dispatch(
        picker, 2, _OPT2_LABEL, monkeypatch
    )
    entry = auq_ledger.lookup(_ledger_key(2))
    assert entry is not None and entry.state == "dispatched"
    assert query.answers == [(f"2. {_OPT2_LABEL[:32]}", False)]
    _assert_seam(picker, query, rerender_states, record_states)


@pytest.mark.asyncio
async def test_not_advanced_path_lock_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    picker = _StuckCursorPicker(_WINDOW_ID, [_Screen(_SINGLE_PANE, 3, 5)])
    query, rerender_states, record_states = await _run_dispatch(
        picker, 3, _OPT3_LABEL, monkeypatch
    )
    entry = auq_ledger.lookup(_ledger_key(3))
    assert entry is not None and entry.state == "not_advanced"
    assert ("Enter", False, False) not in [(k, e, lit) for _w, k, e, lit in picker.sent]
    assert query.answers == [("Action not registered; refreshing card.", False)]
    _assert_seam(picker, query, rerender_states, record_states)


@pytest.mark.asyncio
async def test_commit_unconfirmed_path_lock_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    picker = _StuckEnterPicker(_WINDOW_ID, [_Screen(_SINGLE_PANE, 3, 5)])
    query, rerender_states, record_states = await _run_dispatch(
        picker, 1, _OPT1_LABEL, monkeypatch
    )
    entry = auq_ledger.lookup(_ledger_key(1))
    assert entry is not None and entry.state == "commit_unconfirmed"
    assert query.answers == [("Action sent; refreshing card.", False)]
    _assert_seam(picker, query, rerender_states, record_states)


@pytest.mark.asyncio
async def test_lock_held_blocks_concurrent_acquirer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe acquiring the window lock mid-dispatch blocks until it ends."""

    class _GatedPicker(LockSeamPicker):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
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

    picker = _GatedPicker(_WINDOW_ID, [_Screen(_SINGLE_PANE, 3, 5)])
    ledger_key = _seed_accepted(2, _OPT2_LABEL)
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    monkeypatch.setattr(cbi, "handle_interactive_ui", AsyncMock(return_value=True))
    form = resolve_ask_form(None, _SINGLE_PANE)
    assert form is not None
    query = LockSeamQuery(picker.lock)
    dispatch = asyncio.create_task(
        cbi._dispatch_pick(
            query=query,
            context=SimpleNamespace(bot=SimpleNamespace()),
            user=SimpleNamespace(id=_OWNER_ID),
            tmux_manager=picker,
            adapters=SimpleNamespace(session_manager=SimpleNamespace()),
            w=SimpleNamespace(window_id=_WINDOW_ID),
            window_id=_WINDOW_ID,
            thread_id=_THREAD_ID,
            fingerprint=form.fingerprint(),
            option_number=2,
            option_label=_OPT2_LABEL,
            is_review_submit=False,
            current_form=form,
            ledger_key=ledger_key,
        )
    )
    await asyncio.wait_for(picker.in_capture.wait(), 2.0)
    assert picker.lock.locked(), "window lock must be held mid-dispatch"
    probe = asyncio.create_task(picker.lock.acquire())
    done, _pending = await asyncio.wait({probe}, timeout=0.05)
    assert not done, "probe must block while the dispatch holds the lock"
    picker.release_capture.set()
    await asyncio.wait_for(dispatch, 2.0)
    await asyncio.wait_for(probe, 2.0)  # released after the critical section
    picker.lock.release()
