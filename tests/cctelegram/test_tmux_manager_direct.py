"""Tests for the direct `tmux list-panes -a -F` snapshot path in TmuxManager.

Covers the Wave 1 CPU-perf overhaul: replaces libtmux's per-window subprocess
fan-out with a single `tmux list-panes -a -F` snapshot. These tests stub
asyncio.create_subprocess_exec so they don't actually spawn tmux.

Cases:
  - Happy path: multi-line output, multiple sessions filtered to ours.
  - Active-pane filter: pane_active != "1" rows excluded.
  - Main window filter: rows matching tmux_main_window_name excluded.
  - Malformed line: too-few-fields rows skipped, valid rows still parsed.
  - Non-zero exit: libtmux fallback invoked.
  - Empty output: returns [].
  - capture_pane plain-text success and failure.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cctelegram.config import config
from cctelegram.tmux_manager import TmuxManager, TmuxWindow

SEP = "\x1f"


def _row(
    session_name: str,
    window_id: str,
    window_name: str,
    pane_active: str,
    cwd: str,
    pane_cmd: str,
) -> str:
    return SEP.join([session_name, window_id, window_name, pane_active, cwd, pane_cmd])


def _make_proc(
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
) -> MagicMock:
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    return proc


@pytest.fixture
def manager() -> TmuxManager:
    """Fresh TmuxManager bound to the configured session name."""
    return TmuxManager(session_name=config.tmux_session_name)


# ── _list_windows_direct ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_windows_direct_happy_path(manager: TmuxManager) -> None:
    """Multi-session output: only rows in our session are returned."""
    sess = config.tmux_session_name
    lines = [
        _row(sess, "@0", "alpha", "1", "/home/x/alpha", "claude"),
        # inactive pane in our session — must be excluded
        _row(sess, "@0", "alpha", "0", "/home/x/alpha", "bash"),
        _row(sess, "@5", "beta", "1", "/home/x/beta", "node"),
        # different session — must be excluded
        _row("other", "@9", "gamma", "1", "/home/x/gamma", "vim"),
    ]
    stdout = ("\n".join(lines) + "\n").encode("utf-8")

    with patch(
        "cctelegram.tmux_manager.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_make_proc(stdout=stdout)),
    ):
        windows = await manager._list_windows_direct()

    assert windows == [
        TmuxWindow(
            window_id="@0",
            window_name="alpha",
            cwd="/home/x/alpha",
            pane_current_command="claude",
        ),
        TmuxWindow(
            window_id="@5",
            window_name="beta",
            cwd="/home/x/beta",
            pane_current_command="node",
        ),
    ]


@pytest.mark.asyncio
async def test_list_windows_direct_filters_main_window(
    manager: TmuxManager,
) -> None:
    """The placeholder main window is excluded by name."""
    sess = config.tmux_session_name
    main = config.tmux_main_window_name
    lines = [
        _row(sess, "@0", main, "1", "/home", "bash"),
        _row(sess, "@1", "real", "1", "/home/r", "claude"),
    ]
    stdout = ("\n".join(lines) + "\n").encode("utf-8")

    with patch(
        "cctelegram.tmux_manager.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_make_proc(stdout=stdout)),
    ):
        windows = await manager._list_windows_direct()

    assert [w.window_name for w in windows] == ["real"]


@pytest.mark.asyncio
async def test_list_windows_direct_skips_malformed_lines(
    manager: TmuxManager,
) -> None:
    """A line with fewer than 6 fields is skipped; valid lines still parsed."""
    sess = config.tmux_session_name
    valid = _row(sess, "@2", "ok", "1", "/x", "claude")
    malformed = SEP.join(["only", "three", "fields"])
    stdout = ("\n".join([malformed, valid]) + "\n").encode("utf-8")

    with patch(
        "cctelegram.tmux_manager.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_make_proc(stdout=stdout)),
    ):
        windows = await manager._list_windows_direct()

    assert len(windows) == 1
    assert windows[0].window_id == "@2"


@pytest.mark.asyncio
async def test_list_windows_direct_falls_back_on_nonzero_exit(
    manager: TmuxManager,
) -> None:
    """Non-zero return code triggers the libtmux fallback."""
    sentinel = [
        TmuxWindow(
            window_id="@99",
            window_name="fallback",
            cwd="/fallback",
            pane_current_command="claude",
        )
    ]

    fallback = MagicMock(return_value=sentinel)

    with (
        patch(
            "cctelegram.tmux_manager.asyncio.create_subprocess_exec",
            new=AsyncMock(
                return_value=_make_proc(
                    stdout=b"",
                    stderr=b"tmux: server not running",
                    returncode=1,
                )
            ),
        ),
        patch.object(manager, "_list_windows_libtmux", fallback),
    ):
        windows = await manager._list_windows_direct()

    assert windows == sentinel
    fallback.assert_called_once()


@pytest.mark.asyncio
async def test_list_windows_direct_empty_output(manager: TmuxManager) -> None:
    """Empty stdout returns an empty list."""
    with patch(
        "cctelegram.tmux_manager.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_make_proc(stdout=b"")),
    ):
        windows = await manager._list_windows_direct()

    assert windows == []


@pytest.mark.asyncio
async def test_list_windows_direct_falls_back_on_subprocess_exception(
    manager: TmuxManager,
) -> None:
    """A subprocess-spawn exception (e.g. FileNotFoundError) routes to fallback."""
    sentinel = [
        TmuxWindow(
            window_id="@77",
            window_name="boom-fallback",
            cwd="/boom",
            pane_current_command="claude",
        )
    ]
    fallback = MagicMock(return_value=sentinel)

    with (
        patch(
            "cctelegram.tmux_manager.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=FileNotFoundError("tmux gone")),
        ),
        patch.object(manager, "_list_windows_libtmux", fallback),
    ):
        windows = await manager._list_windows_direct()

    assert windows == sentinel
    fallback.assert_called_once()


# ── capture_pane plain-text ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_capture_pane_plain_success(manager: TmuxManager) -> None:
    """Plain-text capture returns decoded stdout on success."""
    with patch(
        "cctelegram.tmux_manager.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_make_proc(stdout=b"hello\n")),
    ):
        out = await manager.capture_pane("@0", with_ansi=False)

    assert out == "hello\n"


@pytest.mark.asyncio
async def test_capture_pane_plain_failure(manager: TmuxManager) -> None:
    """Non-zero exit yields None."""
    with patch(
        "cctelegram.tmux_manager.asyncio.create_subprocess_exec",
        new=AsyncMock(
            return_value=_make_proc(
                stdout=b"",
                stderr=b"can't find pane",
                returncode=1,
            )
        ),
    ):
        out = await manager.capture_pane("@nonexistent", with_ansi=False)

    assert out is None
