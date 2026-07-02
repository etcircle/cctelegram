"""Tmux session/window management via libtmux.

Wraps libtmux to provide async-friendly operations on a single tmux session:
  - list_windows / find_window_by_name: discover Claude Code windows.
  - capture_pane: read terminal content (plain or with ANSI colors).
  - send_keys: forward user input or control keys to a window.
  - create_window / kill_window: lifecycle management.
  - resize_window / creation-time resize: machine-surface geometry
    (config.window_width x window_height, default 160x50) so tall AUQ
    pickers render fully for the parser.

All blocking libtmux calls are wrapped in asyncio.to_thread().

Performance:
  - shutil.which("tmux") is cached process-wide. libtmux's tmux_cmd
    constructor (libtmux/common.py) calls it on every command; py-spy showed
    PATH-walking accounted for ~25% of CPU under 1Hz × 8-binding polling.
  - list_windows() has a 1s TTL cache so the 8 concurrent gather() callers
    in status_poll_loop coalesce to a single tmux subprocess per cycle.

Concurrency (Wave 3a):
  - window_send_lock(window_id) is a per-window asyncio.Lock registry that
    serializes multi-keystroke transactions to one pane: the text→settle→Enter
    send in SessionManager.send_to_window, and the nav→verify→Enter→confirm
    critical section of the AUQ pick dispatch. Lifecycle: an entry is dropped
    on kill_window ONLY; a stale entry for an externally-vanished or
    topic-closed window is harmless (an asyncio.Lock with no holders) and
    bounded by tmux window-id reuse — the next claimant of a reused id simply
    inherits an idle lock. The lock is a LEAF: holders must never acquire
    route locks / route_runtime / message_queue internals while holding it,
    and (with the single exception of an already-in-flight callback answer)
    no Telegram I/O may run while it is held.

Key class: TmuxManager (singleton instantiated as `tmux_manager`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

# Cache the resolved tmux binary path before libtmux is used. Every
# libtmux command (libtmux/common.py:tmux_cmd.__init__) calls
# shutil.which("tmux"), which on a 1Hz × 8-binding poller burns enormous CPU
# walking $PATH. Patch shutil.which itself rather than libtmux.common.shutil
# — libtmux/server.py and libtmux/common.py each `import shutil`, so
# attribute-patching one module would miss the other. One module-level patch
# covers all of them.
_TMUX_BIN: str | None = shutil.which("tmux")
_orig_shutil_which = shutil.which


def _cached_shutil_which(
    cmd: str,
    mode: int = os.F_OK | os.X_OK,
    path: str | None = None,
) -> str | None:
    if cmd == "tmux" and _TMUX_BIN is not None:
        return _TMUX_BIN
    return _orig_shutil_which(cmd, mode=mode, path=path)


# NOTE: this is a process-wide patch of shutil.which, not scoped to libtmux.
# Other libraries that call shutil.which (for any binary other than "tmux")
# pass through to _orig_shutil_which unchanged. A stale _TMUX_BIN (e.g. tmux
# reinstalled to a new path mid-process) is only refreshed on bot restart.
shutil.which = _cached_shutil_which  # type: ignore[assignment]

import libtmux  # noqa: E402  (must follow the shutil patch)

from .config import SENSITIVE_ENV_VARS, config  # noqa: E402

logger = logging.getLogger(__name__)


def _compose_launch_command(
    base_command: str, md_settings_path: str, resume_session_id: str | None
) -> str:
    """Compose the ``claude`` launch command line sent to the pane.

    Appends ``--settings <path>`` (the bot-managed MessageDisplay live-prose
    capture settings — Bug 2) when a path is given, then ``--resume <id>`` when
    resuming. Both injected values are shell-quoted: the string is executed by
    the shell via tmux ``send_keys``, so an unquoted path with a space or shell
    metacharacter would split or be mangled. ``base_command`` is left verbatim
    (it is the trusted ``CLAUDE_COMMAND`` config, which may itself carry flags).
    """
    cmd = base_command
    if md_settings_path:
        cmd = f"{cmd} --settings {shlex.quote(md_settings_path)}"
    if resume_session_id:
        cmd = f"{cmd} --resume {shlex.quote(resume_session_id)}"
    return cmd


@dataclass
class TmuxWindow:
    """Information about a tmux window."""

    window_id: str
    window_name: str
    cwd: str  # Current working directory
    pane_current_command: str = ""  # Process running in active pane


class TmuxManager:
    """Manages tmux windows for Claude Code sessions."""

    # list_windows TTL. Status polling runs at 1Hz, so a 1s cache window
    # collapses the 8 concurrent gather() callers in status_poll_loop into a
    # single tmux subprocess per cycle. External tmux mutations (manual
    # kill-window from another pane, Claude process exiting) are picked up
    # within one TTL window; explicit mutations through this manager
    # invalidate immediately.
    _LIST_CACHE_TTL = 1.0

    def __init__(self, session_name: str | None = None):
        """Initialize tmux manager.

        Args:
            session_name: Name of the tmux session to use (default from config)
        """
        self.session_name = session_name or config.tmux_session_name
        self._server: libtmux.Server | None = None
        # list_windows cache, keyed by window_id for O(1) find_window_by_id.
        self._list_cache: dict[str, TmuxWindow] | None = None
        self._list_cache_at: float = 0.0
        # asyncio.Lock is created lazily inside _ensure_list_cache. The
        # global tmux_manager is constructed at module import (before any
        # event loop exists), and tests may run multiple asyncio.run()
        # invocations against the same instance — binding a lock to a
        # specific loop here would explode in those cases.
        self._list_lock: asyncio.Lock | None = None
        # Per-window send locks (see "Concurrency" in the module docstring).
        # Each entry records the event loop it was created under: asyncio.Lock
        # is loop-bound at first acquire, so under tests that run a fresh loop
        # per test against this module singleton a stale entry must be
        # recreated rather than reused (production has exactly one loop, so
        # the loop check never fires there).
        self._window_send_locks: dict[
            str, tuple[asyncio.Lock, asyncio.AbstractEventLoop]
        ] = {}

    @property
    def server(self) -> libtmux.Server:
        """Get or create tmux server connection."""
        if self._server is None:
            self._server = libtmux.Server()
        return self._server

    def get_session(self) -> libtmux.Session | None:
        """Get the tmux session if it exists."""
        try:
            return self.server.sessions.get(session_name=self.session_name)
        except Exception:
            return None

    def get_or_create_session(self) -> libtmux.Session:
        """Get existing session or create a new one."""
        session = self.get_session()
        if session:
            self._scrub_session_env(session)
            return session

        # Create new session with main window named specifically
        session = self.server.new_session(
            session_name=self.session_name,
            start_directory=str(Path.home()),
        )
        # Rename the default window to the main window name
        if session.windows:
            session.windows[0].rename_window(config.tmux_main_window_name)
        self._scrub_session_env(session)
        return session

    @staticmethod
    def _scrub_session_env(session: libtmux.Session) -> None:
        """Remove sensitive env vars from the tmux session environment.

        Prevents new windows (and their child processes like Claude Code)
        from inheriting secrets such as TELEGRAM_BOT_TOKEN.
        """
        for var in SENSITIVE_ENV_VARS:
            try:
                session.unset_environment(var)
            except Exception:
                pass  # var not set in session env — nothing to remove

    # Field separator for `tmux list-panes -F`. ASCII unit separator (\x1f) is
    # highly unlikely to appear in the captured fields (window names, paths,
    # command names), so split-by-separator is normally unambiguous. Some tmux
    # builds (e.g. tmux 3.4) emit this control byte as its literal octal escape
    # "\037" rather than the raw byte, so both forms are accepted; a line that
    # does not contain exactly five delimiters of a single form is treated as
    # ambiguous and handed to the libtmux fallback (see `_list_windows_direct`).
    _PANE_FIELD_SEP = "\x1f"
    # The literal octal-escape form some tmux builds emit for the separator.
    _ESCAPED_FIELD_SEP = "\\037"
    _PANE_FORMAT = _PANE_FIELD_SEP.join(
        [
            "#{session_name}",
            "#{window_id}",
            "#{window_name}",
            "#{pane_active}",
            "#{pane_current_path}",
            "#{pane_current_command}",
        ]
    )

    async def _list_windows_direct(self) -> list[TmuxWindow]:
        """List windows by running a single `tmux list-panes -a -F` subprocess.

        Replaces the libtmux-driven path which fans out one `tmux list-panes`
        subprocess per window. Falls back to the libtmux implementation
        (`_list_windows_libtmux`) on tmux failure so the bot keeps working
        if tmux misbehaves.
        """
        # If tmux wasn't resolvable at import, the libtmux fallback would
        # fail the same way every cycle. Skip the subprocess attempt to
        # avoid per-second warning spam, and route straight to the
        # fallback (which logs at debug and returns []).
        if _TMUX_BIN is None:
            return await asyncio.to_thread(self._list_windows_libtmux)
        try:
            proc = await asyncio.create_subprocess_exec(
                _TMUX_BIN,
                "list-panes",
                "-a",
                "-F",
                self._PANE_FORMAT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
        except Exception as e:
            logger.warning(
                "tmux list-panes subprocess failed (%s); falling back to libtmux",
                e,
            )
            return await asyncio.to_thread(self._list_windows_libtmux)

        if proc.returncode != 0:
            logger.warning(
                "tmux list-panes returned non-zero (%s): %s; falling back to libtmux",
                proc.returncode,
                stderr.decode("utf-8", errors="replace").strip(),
            )
            return await asyncio.to_thread(self._list_windows_libtmux)

        windows: list[TmuxWindow] = []
        for raw_line in stdout.decode("utf-8", errors="replace").splitlines():
            line = raw_line.rstrip("\r")
            if not line:
                continue
            # Accept either separator form (raw 0x1F, or the literal "\037" that
            # some tmux builds — e.g. tmux 3.4 — emit for that control byte), but
            # only when exactly five delimiters of a single form are present.
            # Anything else (wrong field count, a value that itself contains a
            # delimiter, or a mix of both forms) is malformed for our purposes
            # and skipped so the remaining valid lines are still parsed. Normal
            # lines keep the fast single-subprocess path across tmux versions.
            raw_count = line.count(self._PANE_FIELD_SEP)
            esc_count = line.count(self._ESCAPED_FIELD_SEP)
            if raw_count == 5:
                parts = line.split(self._PANE_FIELD_SEP)
            elif raw_count == 0 and esc_count == 5:
                parts = line.split(self._ESCAPED_FIELD_SEP)
            else:
                logger.debug("Skipping malformed pane line: %r", line)
                continue
            (
                session_name,
                window_id,
                window_name,
                pane_active,
                cwd,
                pane_cmd,
            ) = parts
            if session_name != self.session_name:
                continue
            if pane_active != "1":
                continue
            if window_name == config.tmux_main_window_name:
                continue
            if window_id == "":
                continue
            windows.append(
                TmuxWindow(
                    window_id=window_id,
                    window_name=window_name,
                    cwd=cwd,
                    pane_current_command=pane_cmd,
                )
            )
        return windows

    def _list_windows_libtmux(self) -> list[TmuxWindow]:
        """Fallback: enumerate windows via libtmux (one subprocess per window).

        Used only when the direct `tmux list-panes -a` path fails. Kept as a
        safety net so the bot remains functional if tmux output format
        changes or the binary misbehaves. Wrapped in a top-level try/except
        because libtmux can raise mid-iteration during a server reconnect —
        a fallback must never propagate.
        """
        windows: list[TmuxWindow] = []
        try:
            session = self.get_session()
            if not session:
                return windows
            for window in session.windows:
                name = window.window_name or ""
                # Skip the main window (placeholder window)
                if name == config.tmux_main_window_name:
                    continue
                try:
                    pane = window.active_pane
                    if pane:
                        cwd = pane.pane_current_path or ""
                        pane_cmd = pane.pane_current_command or ""
                    else:
                        cwd = ""
                        pane_cmd = ""
                    windows.append(
                        TmuxWindow(
                            window_id=window.window_id or "",
                            window_name=name,
                            cwd=cwd,
                            pane_current_command=pane_cmd,
                        )
                    )
                except Exception as e:
                    logger.debug(f"Error getting window info: {e}")
        except Exception as e:
            logger.warning("libtmux fallback failed: %s; returning empty list", e)
            return []
        return windows

    async def _ensure_list_cache(self) -> dict[str, TmuxWindow]:
        """Return the dict-shaped list_windows cache, refreshing if stale.

        Lock-protected slow path keeps 8 concurrent gather() callers from
        each spawning their own tmux subprocess. The fast-path read is
        unsynchronized — safe under a single asyncio loop where dict
        assignment is atomic.
        """
        # Lazy lock init. Two coroutines hitting a freshly-constructed manager
        # cannot both observe ``None`` and both construct: ``asyncio.Lock()``
        # is a synchronous constructor and the check + assignment have no
        # ``await`` between them, so they execute as one cooperative-scheduling
        # step. Do not insert an ``await`` between these two lines.
        if self._list_lock is None:
            self._list_lock = asyncio.Lock()
        now = time.monotonic()
        if (
            self._list_cache is not None
            and (now - self._list_cache_at) < self._LIST_CACHE_TTL
        ):
            return self._list_cache
        async with self._list_lock:
            now = time.monotonic()
            if (
                self._list_cache is not None
                and (now - self._list_cache_at) < self._LIST_CACHE_TTL
            ):
                return self._list_cache
            windows = await self._list_windows_direct()
            self._list_cache = {w.window_id: w for w in windows if w.window_id}
            self._list_cache_at = now
            return self._list_cache

    def _invalidate_list_cache(self) -> None:
        """Drop the list_windows cache after an explicit mutation.

        Always called from async-side code AFTER the libtmux operation has
        returned (i.e. after `await asyncio.to_thread(...)` resolves), so a
        concurrent `list_windows` cannot observe a half-applied state.
        """
        self._list_cache = None
        self._list_cache_at = 0.0

    async def list_windows(self) -> list[TmuxWindow]:
        """List all windows in the session with their working directories.

        Returns:
            List of TmuxWindow with window info and cwd. Served from a 1s
            TTL cache; mutations through this manager invalidate.
        """
        cache = await self._ensure_list_cache()
        return list(cache.values())

    async def find_window_by_name(self, window_name: str) -> TmuxWindow | None:
        """Find a window by its name."""
        cache = await self._ensure_list_cache()
        for window in cache.values():
            if window.window_name == window_name:
                return window
        logger.debug("Window not found by name: %s", window_name)
        return None

    async def find_window_by_id(self, window_id: str) -> TmuxWindow | None:
        """Find a window by its tmux window ID (e.g. '@0', '@12')."""
        cache = await self._ensure_list_cache()
        w = cache.get(window_id)
        if w is None:
            logger.debug("Window not found by id: %s", window_id)
        return w

    async def capture_pane(
        self,
        window_id: str,
        with_ansi: bool = False,
        scrollback_lines: int = 0,
    ) -> str | None:
        """Capture the visible text content of a window's active pane.

        Args:
            window_id: The window ID to capture
            with_ansi: If True, capture with ANSI color codes
            scrollback_lines: If > 0, include this many lines of history
                above the visible region. Useful for AskUserQuestion
                rendering where a long question pushes early options off
                the top of the visible pane; capturing 100+ lines of
                history brings them back. Default 0 = visible only,
                matching the legacy behavior callers like status-line
                parsing depend on.

        Returns:
            The captured text, or None on failure.
        """
        tmux_bin = _TMUX_BIN if _TMUX_BIN is not None else "tmux"
        args: list[str] = [tmux_bin, "capture-pane"]
        if with_ansi:
            args.append("-e")
        if scrollback_lines > 0:
            args.extend(["-S", f"-{scrollback_lines}"])
        args.extend(["-p", "-t", window_id])
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                return stdout.decode("utf-8", errors="replace")
            logger.error(
                f"Failed to capture pane {window_id}: "
                f"{stderr.decode('utf-8', errors='replace')}"
            )
            return None
        except Exception as e:
            logger.error(f"Unexpected error capturing pane {window_id}: {e}")
            return None

    @staticmethod
    def _cmd_send_literal(pane: libtmux.Pane, window_id: str, chars: str) -> bool:
        """Send literal text via raw ``send-keys -l -- <chars>`` and check stderr.

        libtmux's ``pane.send_keys(..., literal=True)`` omits the ``--``
        end-of-options separator and never checks stderr, so a payload
        starting with ``-`` (a bullet list, ``--continue``) makes tmux exit 1
        with "invalid flag" while the call silently succeeds. The raw command
        with ``--`` passes dash-leading payloads verbatim; non-empty stderr
        from the returned ``tmux_cmd`` is treated as failure.
        """
        result = pane.cmd("send-keys", "-l", "--", chars)
        if result.stderr:
            logger.error(
                f"tmux send-keys -l failed for window {window_id}: {result.stderr}"
            )
            return False
        return True

    @staticmethod
    def _cmd_resize_window(window: libtmux.Window, width: int, height: int) -> bool:
        """Resize a window via raw ``resize-window -x <w> -y <h>``, check stderr.

        Wave B machine-surface geometry. Follows the ``_cmd_send_literal``
        precedent: libtmux swallows tmux stderr, so a failed resize (e.g.
        size out of tmux's bounds) would silently "succeed" — non-empty
        stderr from the returned ``tmux_cmd`` is treated as failure. On a
        detached window the resize implicitly flips ``window-size`` to
        ``manual`` (rig-verified, tmux 3.6a). Returns bool; never raises —
        geometry is an optimization, never a blocker for the caller.
        """
        try:
            result = window.cmd("resize-window", "-x", str(width), "-y", str(height))
        except Exception as e:
            logger.warning(
                "tmux resize-window failed for window %s: %s",
                getattr(window, "window_id", "?"),
                e,
            )
            return False
        if result.stderr:
            logger.warning(
                "tmux resize-window failed for window %s: %s",
                getattr(window, "window_id", "?"),
                result.stderr,
            )
            return False
        return True

    async def resize_window(self, window_id: str, width: int, height: int) -> bool:
        """Resize a tmux window by its ID to ``width`` x ``height``.

        Resolves the REAL ``libtmux.Window`` INSIDE the worker thread —
        never the lightweight ``TmuxWindow`` dataclass that
        ``find_window_by_id`` returns (it has no ``.cmd``). Idempotent:
        resize-to-same-size is a tmux no-op. Returns False when the session
        or window is gone; never raises.
        """

        def _sync_resize() -> bool:
            session = self.get_session()
            if not session:
                logger.warning("resize_window: no tmux session found")
                return False
            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    logger.warning("resize_window: window %s not found", window_id)
                    return False
                return self._cmd_resize_window(window, width, height)
            except Exception as e:
                logger.warning("Failed to resize window %s: %s", window_id, e)
                return False

        return await asyncio.to_thread(_sync_resize)

    def window_send_lock(self, window_id: str) -> asyncio.Lock:
        """Return the per-window send lock for ``window_id``, creating on demand.

        Must be called from a running event loop. Serializes multi-keystroke
        pane transactions (see "Concurrency" in the module docstring for the
        lifecycle and the leaf rule). A registry entry created under a
        previous, now-replaced event loop (test-only situation) is recreated:
        the stale lock provably has no holders because its loop is gone.
        """
        running = asyncio.get_running_loop()
        entry = self._window_send_locks.get(window_id)
        if entry is not None:
            lock, loop = entry
            if loop is running:
                return lock
        lock = asyncio.Lock()
        self._window_send_locks[window_id] = (lock, running)
        return lock

    def reset_window_send_locks_for_tests(self) -> None:
        """Drop all per-window send locks (test isolation seam)."""
        self._window_send_locks.clear()

    async def send_keys(
        self, window_id: str, text: str, enter: bool = True, literal: bool = True
    ) -> bool:
        """Send keys to a specific window.

        Args:
            window_id: The window ID to send to
            text: Text to send
            enter: Whether to press enter after the text
            literal: If True, send text literally. If False, interpret special keys
                     like "Up", "Down", "Left", "Right", "Escape", "Enter".

        Returns:
            True if successful, False otherwise
        """
        if literal and enter:
            # Split into text + delay + Enter via libtmux.
            # Claude Code's TUI sometimes interprets a rapid-fire Enter
            # (arriving in the same input batch as the text) as a newline
            # rather than submit.  A 500ms gap lets the TUI process the
            # text before receiving Enter.
            def _send_literal(chars: str) -> bool:
                session = self.get_session()
                if not session:
                    logger.error("No tmux session found")
                    return False
                try:
                    window = session.windows.get(window_id=window_id)
                    if not window:
                        logger.error(f"Window {window_id} not found")
                        return False
                    pane = window.active_pane
                    if not pane:
                        logger.error(f"No active pane in window {window_id}")
                        return False
                    return self._cmd_send_literal(pane, window_id, chars)
                except Exception as e:
                    logger.error(f"Failed to send keys to window {window_id}: {e}")
                    return False

            def _send_enter() -> bool:
                session = self.get_session()
                if not session:
                    return False
                try:
                    window = session.windows.get(window_id=window_id)
                    if not window:
                        return False
                    pane = window.active_pane
                    if not pane:
                        return False
                    pane.send_keys("", enter=True, literal=False)
                    return True
                except Exception as e:
                    logger.error(f"Failed to send Enter to window {window_id}: {e}")
                    return False

            # Claude Code's ! command mode: send "!" first so the TUI
            # switches to bash mode, wait 1s, then send the rest.
            if text.startswith("!"):
                if not await asyncio.to_thread(_send_literal, "!"):
                    return False
                rest = text[1:]
                if rest:
                    await asyncio.sleep(1.0)
                    if not await asyncio.to_thread(_send_literal, rest):
                        return False
            else:
                if not await asyncio.to_thread(_send_literal, text):
                    return False
            await asyncio.sleep(0.5)
            return await asyncio.to_thread(_send_enter)

        # Other cases: special keys (literal=False) or no-enter
        def _sync_send_keys() -> bool:
            session = self.get_session()
            if not session:
                logger.error("No tmux session found")
                return False

            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    logger.error(f"Window {window_id} not found")
                    return False

                pane = window.active_pane
                if not pane:
                    logger.error(f"No active pane in window {window_id}")
                    return False

                if literal:
                    # Raw `send-keys -l --` path: dash-leading payloads pass
                    # verbatim, tmux errors surface as False (finding 1).
                    if not self._cmd_send_literal(pane, window_id, text):
                        return False
                    if enter:
                        pane.send_keys("", enter=True, literal=False)
                    return True
                pane.send_keys(text, enter=enter, literal=literal)
                return True

            except Exception as e:
                logger.error(f"Failed to send keys to window {window_id}: {e}")
                return False

        return await asyncio.to_thread(_sync_send_keys)

    async def rename_window(self, window_id: str, new_name: str) -> bool:
        """Rename a tmux window by its ID."""

        def _sync_rename() -> bool:
            session = self.get_session()
            if not session:
                return False
            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    return False
                window.rename_window(new_name)
                logger.info("Renamed window %s to '%s'", window_id, new_name)
                return True
            except Exception as e:
                logger.error(f"Failed to rename window {window_id}: {e}")
                return False

        result = await asyncio.to_thread(_sync_rename)
        self._invalidate_list_cache()
        return result

    async def kill_window(self, window_id: str) -> bool:
        """Kill a tmux window by its ID."""

        def _sync_kill() -> bool:
            session = self.get_session()
            if not session:
                return False
            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    return False
                window.kill()
                logger.info("Killed window %s", window_id)
                return True
            except Exception as e:
                logger.error(f"Failed to kill window {window_id}: {e}")
                return False

        result = await asyncio.to_thread(_sync_kill)
        self._invalidate_list_cache()
        # Drop the per-window send lock ONLY on a confirmed kill (Wave 3a
        # Hermes P3): a failed kill can leave the window ALIVE with an
        # in-flight holder, and popping here would hand a later acquirer a
        # FRESH lock for the same live window — the split-lock class this
        # registry exists to prevent. A window that vanished externally
        # leaves a stale no-holder entry, which is the documented harmless
        # bound (module docstring).
        if result:
            self._window_send_locks.pop(window_id, None)
        return result

    async def create_window(
        self,
        work_dir: str,
        window_name: str | None = None,
        start_claude: bool = True,
        resume_session_id: str | None = None,
    ) -> tuple[bool, str, str, str]:
        """Create a new tmux window and optionally start Claude Code.

        Args:
            work_dir: Working directory for the new window
            window_name: Optional window name (defaults to directory name)
            start_claude: Whether to start claude command
            resume_session_id: If set, append --resume <id> to claude command

        Returns:
            Tuple of (success, message, window_name, window_id)
        """
        # Validate directory first
        path = Path(work_dir).expanduser().resolve()
        if not path.exists():
            return False, f"Directory does not exist: {work_dir}", "", ""
        if not path.is_dir():
            return False, f"Not a directory: {work_dir}", "", ""

        # Create window name, adding suffix if name already exists
        final_window_name = window_name if window_name else path.name

        # Check for existing window name
        base_name = final_window_name
        counter = 2
        while await self.find_window_by_name(final_window_name):
            final_window_name = f"{base_name}-{counter}"
            counter += 1

        # Resolve the bot-managed MessageDisplay capture settings once, off the
        # tmux worker thread. Passing it via ``claude --settings`` scopes the
        # live-prose hook (Bug 2) to bot-launched sessions and merges with the
        # global SessionStart / PreToolUse hooks. A failed write degrades
        # gracefully — the window still launches, just without live-prose
        # capture (falls back to post-resolution JSONL delivery).
        from . import md_capture

        try:
            md_settings_path = md_capture.ensure_capture_settings()
            md_settings = str(md_settings_path) if md_settings_path.exists() else ""
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not prepare MessageDisplay capture settings: %s", e)
            md_settings = ""

        # Create window in thread
        def _create_and_start() -> tuple[bool, str, str, str]:
            session = self.get_or_create_session()
            try:
                # Create new window
                window = session.new_window(
                    window_name=final_window_name,
                    start_directory=str(path),
                )

                wid = window.window_id or ""

                # Prevent Claude Code from overriding window name
                window.set_window_option("allow-rename", "off")

                # Wave B machine-surface geometry: resize BEFORE the claude
                # launch so Claude Code starts at final geometry and never
                # repaints mid-startup. A False return is logged (inside the
                # helper) and the window still launches — geometry is an
                # optimization, never a launch blocker.
                self._cmd_resize_window(
                    window, config.window_width, config.window_height
                )

                # Start Claude Code if requested
                if start_claude:
                    pane = window.active_pane
                    if pane:
                        cmd = _compose_launch_command(
                            config.claude_command, md_settings, resume_session_id
                        )
                        pane.send_keys(cmd, enter=True)

                logger.info(
                    "Created window '%s' (id=%s) at %s",
                    final_window_name,
                    wid,
                    path,
                )
                return (
                    True,
                    f"Created window '{final_window_name}' at {path}",
                    final_window_name,
                    wid,
                )

            except Exception as e:
                logger.error(f"Failed to create window: {e}")
                return False, f"Failed to create window: {e}", "", ""

        result = await asyncio.to_thread(_create_and_start)
        # Invalidate AFTER to_thread returns so the brand-new window is
        # visible to the next list_windows call from the resume flow.
        self._invalidate_list_cache()
        return result


# Global instance with default session name
tmux_manager = TmuxManager()
