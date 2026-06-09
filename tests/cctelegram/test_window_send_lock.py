"""Wave 3a: per-window send-lock registry + FIFO ``send_to_window`` (finding 9).

The registry lives on ``TmuxManager`` (``window_send_lock``); the FIFO contract
is that two concurrent ``SessionManager.send_to_window`` calls on the SAME
window serialize their whole text→settle→Enter transactions (strict
textA, EnterA, textB, EnterB), while sends to DIFFERENT windows do not
serialize against each other.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cctelegram.session import session_manager
from cctelegram.tmux_manager import TmuxManager, tmux_manager as real_tmux

Event = tuple[str, str, str]  # (phase, window_id, text)


def _recording_send(
    events: list[Event],
    *,
    settle: float = 0.01,
    block_until: dict[str, asyncio.Event] | None = None,
):
    """Fake ``tmux_manager.send_keys`` that records the text→Enter transaction.

    Models the production literal+enter path (text, 500ms settle, Enter) with a
    small await between the two phases so concurrent unserialized callers
    provably interleave pre-fix.
    """

    async def fake_send_keys(
        window_id: str, text: str, enter: bool = True, literal: bool = True
    ) -> bool:
        events.append(("text", window_id, text))
        if block_until is not None and window_id in block_until:
            await block_until[window_id].wait()
        else:
            await asyncio.sleep(settle)
        events.append(("enter", window_id, text))
        return True

    return fake_send_keys


async def _fake_find(window_id: str):
    await asyncio.sleep(0)  # a real lookup always yields at least once
    return SimpleNamespace(window_id=window_id)


@pytest.fixture(autouse=True)
def _fresh_locks():
    real_tmux.reset_window_send_locks_for_tests()
    yield
    real_tmux.reset_window_send_locks_for_tests()


# ── finding-9 FIFO contract ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_sends_same_window_strict_fifo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent sends to ONE window serialize as whole transactions."""
    events: list[Event] = []
    monkeypatch.setattr(real_tmux, "find_window_by_id", _fake_find)
    monkeypatch.setattr(real_tmux, "send_keys", _recording_send(events))

    await asyncio.gather(
        session_manager.send_to_window("@7", "A"),
        session_manager.send_to_window("@7", "B"),
    )
    assert events == [
        ("text", "@7", "A"),
        ("enter", "@7", "A"),
        ("text", "@7", "B"),
        ("enter", "@7", "B"),
    ]


@pytest.mark.asyncio
async def test_sends_to_different_windows_do_not_serialize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow transaction on @1 must NOT delay an independent send to @2."""
    events: list[Event] = []
    release_w1 = asyncio.Event()
    monkeypatch.setattr(real_tmux, "find_window_by_id", _fake_find)
    monkeypatch.setattr(
        real_tmux,
        "send_keys",
        _recording_send(events, block_until={"@1": release_w1}),
    )

    slow = asyncio.create_task(session_manager.send_to_window("@1", "slow"))
    # Wait until @1's transaction is mid-flight (text sent, Enter pending).
    for _ in range(200):
        if ("text", "@1", "slow") in events:
            break
        await asyncio.sleep(0.001)
    assert ("text", "@1", "slow") in events

    ok, _ = await asyncio.wait_for(session_manager.send_to_window("@2", "fast"), 1.0)
    assert ok
    assert ("enter", "@2", "fast") in events
    assert ("enter", "@1", "slow") not in events  # @1 still mid-transaction

    release_w1.set()
    await slow
    assert events[-1] == ("enter", "@1", "slow")


# ── registry lifecycle ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_window_send_lock_identity_per_window() -> None:
    mgr = TmuxManager(session_name="lock-test")
    lock1 = mgr.window_send_lock("@1")
    assert mgr.window_send_lock("@1") is lock1
    assert mgr.window_send_lock("@2") is not lock1


@pytest.mark.asyncio
async def test_failed_kill_keeps_lock_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wave 3a Hermes P3: a FAILED kill can leave the window alive with an
    in-flight holder — dropping the entry would hand a later acquirer a
    fresh lock for the same live window (the split-lock class). Keep it."""
    mgr = TmuxManager(session_name="lock-test")
    lock1 = mgr.window_send_lock("@1")
    monkeypatch.setattr(mgr, "get_session", lambda: None)  # kill fails
    assert await mgr.kill_window("@1") is False
    assert mgr.window_send_lock("@1") is lock1


@pytest.mark.asyncio
async def test_successful_kill_drops_lock_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeWindow:
        def kill(self) -> None:
            pass

    class _FakeWindows:
        def get(self, window_id: str) -> _FakeWindow:
            return _FakeWindow()

    class _FakeSession:
        windows = _FakeWindows()

    mgr = TmuxManager(session_name="lock-test")
    lock1 = mgr.window_send_lock("@1")
    monkeypatch.setattr(mgr, "get_session", lambda: _FakeSession())
    assert await mgr.kill_window("@1") is True
    assert mgr.window_send_lock("@1") is not lock1


def test_window_send_lock_survives_event_loop_replacement() -> None:
    """A registry entry from a dead loop is recreated, never reused.

    asyncio.Lock binds to the loop it is first acquired under; tests run a
    fresh loop per test against the module singleton, so reuse across loops
    would raise "is bound to a different event loop".
    """
    mgr = TmuxManager(session_name="lock-test")

    async def grab() -> None:
        async with mgr.window_send_lock("@1"):
            pass

    asyncio.run(grab())
    asyncio.run(grab())  # must not raise
