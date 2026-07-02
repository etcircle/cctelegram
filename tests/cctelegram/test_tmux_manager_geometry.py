"""Tests for Wave B machine-surface window geometry (160x50 default).

One resize mechanism, two callsites:
  - ``TmuxManager._cmd_resize_window`` — raw ``resize-window -x <w> -y <h>``
    with the ``_cmd_send_literal`` stderr-check precedent (libtmux swallows
    tmux stderr; non-empty stderr == failure, returns bool, never raises).
  - Creation seam: ``create_window`` resizes AFTER ``new_window`` +
    ``allow-rename off`` but BEFORE the ``claude`` launch send_keys, so
    Claude Code starts at final geometry. A failed resize logs WARNING and
    the window still launches (geometry is an optimization, never a blocker).
  - Startup reconcile: ``bot._reconcile_window_geometry`` resizes every
    listed window once in ``post_init``; per-window failures are non-fatal.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from cctelegram.tmux_manager import TmuxManager, TmuxWindow


class FakePane:
    """Records ``send_keys`` launch calls into a shared event log."""

    def __init__(self, events: list[tuple[Any, ...]]) -> None:
        self._events = events

    def send_keys(self, text: str, enter: bool = True, literal: bool = True) -> None:
        self._events.append(("pane_send_keys", text, enter, literal))


class FakeWindow:
    """Fake libtmux Window recording raw ``cmd`` calls into a shared log."""

    def __init__(
        self,
        events: list[tuple[Any, ...]],
        window_id: str = "@7",
        resize_stderr: list[str] | None = None,
    ) -> None:
        self._events = events
        self.window_id = window_id
        self.active_pane = FakePane(events)
        self._resize_stderr = resize_stderr or []

    def cmd(self, *args: Any) -> SimpleNamespace:
        self._events.append(("window_cmd", *args))
        if args and args[0] == "resize-window":
            return SimpleNamespace(stderr=list(self._resize_stderr), stdout=[])
        return SimpleNamespace(stderr=[], stdout=[])

    def set_window_option(self, name: str, value: str) -> None:
        self._events.append(("set_window_option", name, value))


def _manager_for_create(
    window: FakeWindow,
) -> TmuxManager:
    """Manager whose session creation path returns the given fake window."""
    manager = TmuxManager(session_name="test-session")
    session = SimpleNamespace(
        new_window=lambda **kw: window,
        windows=SimpleNamespace(get=lambda **kw: window),
    )
    manager.get_or_create_session = lambda: session  # type: ignore[method-assign]
    manager.get_session = lambda: session  # type: ignore[method-assign]
    manager.find_window_by_name = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return manager


# ── _cmd_resize_window (the single mechanism) ────────────────────────────


class TestCmdResizeWindow:
    def test_success_empty_stderr(self):
        events: list[tuple[Any, ...]] = []
        window = FakeWindow(events)
        assert TmuxManager._cmd_resize_window(window, 160, 50) is True
        assert events == [("window_cmd", "resize-window", "-x", "160", "-y", "50")]

    def test_nonempty_stderr_returns_false(self, caplog):
        events: list[tuple[Any, ...]] = []
        window = FakeWindow(events, resize_stderr=["size too big"])
        with caplog.at_level("WARNING", logger="cctelegram.tmux_manager"):
            assert TmuxManager._cmd_resize_window(window, 9999, 50) is False
        assert any("resize-window" in r.getMessage() for r in caplog.records)

    def test_cmd_exception_returns_false_never_raises(self, caplog):
        class RaisingWindow(FakeWindow):
            def cmd(self, *args: Any) -> SimpleNamespace:
                raise RuntimeError("tmux gone")

        window = RaisingWindow([])
        with caplog.at_level("WARNING", logger="cctelegram.tmux_manager"):
            assert TmuxManager._cmd_resize_window(window, 160, 50) is False


# ── creation seam ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_window_resizes_before_launch(tmp_path, monkeypatch):
    """The new window is resized to config geometry BEFORE the claude launch."""
    from cctelegram.tmux_manager import config as tmux_config

    monkeypatch.setattr(tmux_config, "window_width", 160)
    monkeypatch.setattr(tmux_config, "window_height", 50)

    events: list[tuple[Any, ...]] = []
    window = FakeWindow(events)
    manager = _manager_for_create(window)

    with patch(
        "cctelegram.md_capture.ensure_capture_settings",
        side_effect=RuntimeError("not under test"),
    ):
        ok, _msg, _name, wid = await manager.create_window(str(tmp_path))

    assert ok is True
    assert wid == "@7"
    resize_calls = [e for e in events if e[:2] == ("window_cmd", "resize-window")]
    assert resize_calls == [("window_cmd", "resize-window", "-x", "160", "-y", "50")]
    launch_calls = [e for e in events if e[0] == "pane_send_keys"]
    assert len(launch_calls) == 1  # claude launched exactly once
    # Ordering: resize strictly BEFORE the launch send_keys.
    assert events.index(resize_calls[0]) < events.index(launch_calls[0])


@pytest.mark.asyncio
async def test_create_window_resize_failure_still_launches(
    tmp_path, monkeypatch, caplog
):
    """A failed resize (non-empty stderr) logs WARNING; the window launches."""
    from cctelegram.tmux_manager import config as tmux_config

    monkeypatch.setattr(tmux_config, "window_width", 160)
    monkeypatch.setattr(tmux_config, "window_height", 50)

    events: list[tuple[Any, ...]] = []
    window = FakeWindow(events, resize_stderr=["command resize-window: bad size"])
    manager = _manager_for_create(window)

    with (
        patch(
            "cctelegram.md_capture.ensure_capture_settings",
            side_effect=RuntimeError("not under test"),
        ),
        caplog.at_level("WARNING", logger="cctelegram.tmux_manager"),
    ):
        ok, _msg, _name, _wid = await manager.create_window(str(tmp_path))

    assert ok is True
    launch_calls = [e for e in events if e[0] == "pane_send_keys"]
    assert len(launch_calls) == 1  # launch happened despite the failed resize
    assert any("resize-window" in r.getMessage() for r in caplog.records)


# ── async resize_window (real libtmux Window resolved in-thread) ─────────


class TestResizeWindowAsync:
    @pytest.mark.asyncio
    async def test_resizes_real_window_by_id(self):
        events: list[tuple[Any, ...]] = []
        window = FakeWindow(events, window_id="@3")
        manager = _manager_for_create(window)

        ok = await manager.resize_window("@3", 160, 50)

        assert ok is True
        assert events == [("window_cmd", "resize-window", "-x", "160", "-y", "50")]

    @pytest.mark.asyncio
    async def test_missing_window_returns_false(self):
        manager = TmuxManager(session_name="test-session")
        session = SimpleNamespace(windows=SimpleNamespace(get=lambda **kw: None))
        manager.get_session = lambda: session  # type: ignore[method-assign]

        assert await manager.resize_window("@404", 160, 50) is False

    @pytest.mark.asyncio
    async def test_missing_session_returns_false(self):
        manager = TmuxManager(session_name="test-session")
        manager.get_session = lambda: None  # type: ignore[method-assign]

        assert await manager.resize_window("@1", 160, 50) is False

    @pytest.mark.asyncio
    async def test_window_lookup_exception_returns_false(self):
        manager = TmuxManager(session_name="test-session")

        def _boom(**kw: Any) -> None:
            raise RuntimeError("server reconnect")

        session = SimpleNamespace(windows=SimpleNamespace(get=_boom))
        manager.get_session = lambda: session  # type: ignore[method-assign]

        assert await manager.resize_window("@1", 160, 50) is False


# ── startup reconcile (bot.post_init one-time pass) ──────────────────────


class TestStartupReconcile:
    @pytest.mark.asyncio
    async def test_startup_reconcile_resizes_listed_windows(self, monkeypatch):
        from cctelegram import bot as bot_module

        monkeypatch.setattr(bot_module.config, "window_width", 160)
        monkeypatch.setattr(bot_module.config, "window_height", 50)
        listed = [
            TmuxWindow(window_id="@1", window_name="proj-a", cwd="/tmp/a"),
            TmuxWindow(window_id="@2", window_name="proj-b", cwd="/tmp/b"),
        ]
        list_mock = AsyncMock(return_value=listed)
        resize_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(bot_module.tmux_manager, "list_windows", list_mock)
        monkeypatch.setattr(bot_module.tmux_manager, "resize_window", resize_mock)

        await bot_module._reconcile_window_geometry()

        assert resize_mock.await_count == 2
        resize_mock.assert_any_await("@1", 160, 50)
        resize_mock.assert_any_await("@2", 160, 50)

    @pytest.mark.asyncio
    async def test_startup_reconcile_window_error_nonfatal(self, monkeypatch, caplog):
        """One window raising does not stop the pass or break startup."""
        from cctelegram import bot as bot_module

        monkeypatch.setattr(bot_module.config, "window_width", 160)
        monkeypatch.setattr(bot_module.config, "window_height", 50)
        listed = [
            TmuxWindow(window_id="@1", window_name="proj-a", cwd="/tmp/a"),
            TmuxWindow(window_id="@2", window_name="proj-b", cwd="/tmp/b"),
        ]
        list_mock = AsyncMock(return_value=listed)
        resize_mock = AsyncMock(side_effect=[RuntimeError("boom"), True])
        monkeypatch.setattr(bot_module.tmux_manager, "list_windows", list_mock)
        monkeypatch.setattr(bot_module.tmux_manager, "resize_window", resize_mock)

        with caplog.at_level("WARNING", logger="cctelegram.bot"):
            await bot_module._reconcile_window_geometry()  # must not raise

        assert resize_mock.await_count == 2  # @2 still resized after @1 raised
        resize_mock.assert_any_await("@2", 160, 50)
        assert any("geometry" in r.getMessage().lower() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_startup_reconcile_list_failure_nonfatal(self, monkeypatch, caplog):
        """list_windows raising is swallowed by the outer guard."""
        from cctelegram import bot as bot_module

        list_mock = AsyncMock(side_effect=RuntimeError("tmux down"))
        monkeypatch.setattr(bot_module.tmux_manager, "list_windows", list_mock)

        with caplog.at_level("WARNING", logger="cctelegram.bot"):
            await bot_module._reconcile_window_geometry()  # must not raise

        assert any("geometry" in r.getMessage().lower() for r in caplog.records)
