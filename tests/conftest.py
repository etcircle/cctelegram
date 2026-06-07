"""Shared fixtures for the whole CC Telegram test suite + Wave A scenario harness.

The import-time environment bootstrap for ``cctelegram.config`` lives in the
repository-root ``conftest.py`` so it runs before any test collection.

This file hosts the **scenario harness** used by ``tests/scenarios/*`` —
black-box tests that drive the bot from the public Telegram seam through the
real handler stack to ``tmux_manager`` / ``session_manager``, with no
monkeypatch of handler internals in *test bodies*.

Reset-seam note: handler modules expose a co-located ``reset_for_tests()``
seam next to the state it resets — ``message_queue.reset_for_tests()`` and
``interactive_ui.reset_for_tests()`` join the existing
``route_runtime`` / ``auq_ledger`` / ``attention`` seams. ``_reset_all_handler_state``
calls those seams directly. ``inbound_aggregator`` and ``status_polling``
still have small fixture-side clears below (their module state is a couple of
caches); keeping any residual reset code in this file — not in test bodies —
preserves the kill-criterion signal: scenarios fail the bar only when the
*tests themselves* must reach into handler internals.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cctelegram import bot as bot_module
from cctelegram.handlers import inbound_telegram as inbound_module
from cctelegram import route_runtime, transcript_event_adapter
from cctelegram.session import session_manager as _real_sm
from cctelegram.tmux_manager import TmuxWindow, tmux_manager as _real_tmux
from cctelegram.utils import app_dir
from cctelegram.handlers import (
    attention,
    auq_ledger,
    auq_source,
    inbound_aggregator,
    interactive_ui,
    message_queue,
    pick_intent,
    pick_token,
    status_polling,
)


# ──────────────────────────────────────────────────────────────────────────
# Fake tmux substrate
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class _PaneWindow:
    """In-memory representation of one fake tmux window."""

    window_id: str
    window_name: str
    cwd: str = "/tmp/test"
    pane_text: str = ""
    pane_text_ansi: str = ""
    pane_current_command: str = "claude"


@dataclass
class FakeTmux:
    """Stand-in for ``tmux_manager`` used by scenario tests.

    Fixture binds these methods onto the real ``tmux_manager`` singleton so
    every consumer (``bot.py``, ``session_monitor``, ``handlers/*``) sees the
    fake regardless of import order.
    """

    windows: dict[str, _PaneWindow] = field(default_factory=dict)
    sent_keys: list[tuple[str, str, bool, bool]] = field(default_factory=list)
    kill_calls: list[str] = field(default_factory=list)
    rename_calls: list[tuple[str, str]] = field(default_factory=list)
    create_calls: list[dict[str, Any]] = field(default_factory=list)
    create_response: tuple[bool, str] | None = None  # override for failure injection
    send_keys_response: bool | None = None  # override for failure injection
    _next_id: int = 0

    # ── seeding helpers ────────────────────────────────────────────────
    def add_window(
        self,
        *,
        window_id: str | None = None,
        window_name: str,
        cwd: str = "/tmp/test",
        pane_text: str = "",
        pane_text_ansi: str = "",
    ) -> str:
        if window_id is None:
            window_id = f"@{self._next_id}"
            self._next_id += 1
        elif window_id.startswith("@"):
            try:
                self._next_id = max(self._next_id, int(window_id[1:]) + 1)
            except ValueError:
                pass
        self.windows[window_id] = _PaneWindow(
            window_id=window_id,
            window_name=window_name,
            cwd=cwd,
            pane_text=pane_text,
            pane_text_ansi=pane_text_ansi or pane_text,
        )
        return window_id

    def set_pane(self, window_id: str, text: str, *, ansi: str | None = None) -> None:
        w = self.windows.get(window_id)
        if w:
            w.pane_text = text
            w.pane_text_ansi = ansi if ansi is not None else text

    def _to_tmux_window(self, w: _PaneWindow) -> TmuxWindow:
        return TmuxWindow(
            window_id=w.window_id,
            window_name=w.window_name,
            cwd=w.cwd,
            pane_current_command=w.pane_current_command,
        )

    # ── tmux_manager interface (async) ─────────────────────────────────
    async def list_windows(self) -> list[TmuxWindow]:
        return [self._to_tmux_window(w) for w in self.windows.values()]

    async def find_window_by_id(self, window_id: str) -> TmuxWindow | None:
        w = self.windows.get(window_id)
        return self._to_tmux_window(w) if w else None

    async def find_window_by_name(self, window_name: str) -> TmuxWindow | None:
        for w in self.windows.values():
            if w.window_name == window_name:
                return self._to_tmux_window(w)
        return None

    async def kill_window(self, window_id: str) -> bool:
        self.kill_calls.append(window_id)
        return self.windows.pop(window_id, None) is not None

    async def rename_window(self, window_id: str, new_name: str) -> bool:
        self.rename_calls.append((window_id, new_name))
        w = self.windows.get(window_id)
        if w:
            w.window_name = new_name
            return True
        return False

    async def send_keys(
        self,
        window_id: str,
        keys: str,
        enter: bool = True,
        literal: bool = True,
    ) -> bool:
        self.sent_keys.append((window_id, keys, enter, literal))
        if self.send_keys_response is not None:
            return self.send_keys_response
        return window_id in self.windows

    async def capture_pane(
        self,
        window_id: str,
        with_ansi: bool = False,
        scrollback_lines: int = 0,
    ) -> str:
        del scrollback_lines  # fake pane is whatever was set; no extra history
        w = self.windows.get(window_id)
        if not w:
            return ""
        return w.pane_text_ansi if with_ansi else w.pane_text

    async def create_window(
        self,
        cwd: str,
        window_name: str | None = None,
        **kwargs: Any,
    ) -> tuple[bool, str, str, str]:
        self.create_calls.append({"cwd": cwd, "window_name": window_name, **kwargs})
        if self.create_response is not None:
            ok, msg = self.create_response
            if not ok:
                return False, msg, "", ""
        name = window_name or Path(cwd).name or "window"
        wid = self.add_window(window_name=name, cwd=cwd)
        return True, f"Created window '{name}' at {cwd}", name, wid

    async def get_or_create_session(self) -> Any:
        return MagicMock(name="fake-tmux-session")

    async def session_exists(self) -> bool:
        return True

    # Sometimes called by older paths
    async def get_session(self) -> Any:
        return MagicMock(name="fake-tmux-session")


# ──────────────────────────────────────────────────────────────────────────
# v2.1.168 AUQ keystroke-aware fake picker
# ──────────────────────────────────────────────────────────────────────────

_OPT_LINE = re.compile(r"^(\s*)(?:❯ |  )(\d+)\. (.*)$")
_RESOLVED_PANE = "user@host repo % \n"


def render_cursor(pane: str, cursor_number: int) -> str:
    """Return ``pane`` with the ``❯`` cursor relocated onto option ``cursor_number``.

    Models a .168 cursor move: every numbered option line (real options AND the
    ``Type something`` / ``Chat about this`` affordance rows) is re-prefixed with
    ``❯ `` for the target number and ``  `` otherwise. Putting the cursor on an
    affordance number reproduces the affordance-cursor parse (no real option is
    marked ``cursor`` — the wrap-hazard case).
    """
    out: list[str] = []
    for line in pane.split("\n"):
        m = _OPT_LINE.match(line)
        if m:
            indent, num, rest = m.group(1), m.group(2), m.group(3)
            prefix = "❯ " if int(num) == cursor_number else "  "
            out.append(f"{indent}{prefix}{num}. {rest}")
        else:
            out.append(line)
    return "\n".join(out)


@dataclass
class _Screen:
    """One picker screen the :class:`Fake168Picker` can show."""

    pane: str  # the fixture pane text (cursor relocated dynamically)
    n_real: int  # count of REAL (non-affordance) options the screen offers
    n_nav: int  # total navigable numbered rows (real + affordances) for wrap


class Fake168Picker:
    """Keystroke-aware fake of the Claude Code v2.1.168 single-select picker.

    Models the captured .168 keystroke semantics so RED tests can prove the bot's
    dispatch is correct WITHOUT the version-fragile bare digit:

      - ``Up``/``Down`` move the cursor by one navigable row, **wrapping** at the
        edges (``Up`` from option 1 wraps to the last affordance row — NOT clamped).
      - ``Enter`` selects the cursor's REAL option and ADVANCES to the next screen
        (the final screen advancing resolves the tool → a non-picker pane).
      - a bare digit (``literal=True``): in ``variant="A"`` it select+advances (the
        inline picker — used by the over-advance guard); in ``variant="B"`` it only
        moves the cursor (the notes-side-panel variant that broke the bare digit).

    ``capture_pane`` is STATEFUL: it renders the CURRENT screen with the cursor on
    the current position, so a dispatch that navigates then re-captures sees the
    moved cursor, and a post-Enter capture sees the advanced screen.
    """

    def __init__(
        self, window_id: str, screens: list[_Screen], *, variant: str = "A"
    ) -> None:
        self.window_id = window_id
        self.screens = screens
        self.variant = variant
        self.idx = 0
        self.cursor = 1
        self.sent: list[tuple[str, str, bool, bool]] = []

    # ── introspection ──────────────────────────────────────────────────
    def current_pane(self) -> str:
        if self.idx >= len(self.screens):
            return _RESOLVED_PANE
        return render_cursor(self.screens[self.idx].pane, self.cursor)

    @property
    def resolved(self) -> bool:
        return self.idx >= len(self.screens)

    def _advance(self) -> None:
        self.idx += 1
        self.cursor = 1

    # ── tmux_manager interface ─────────────────────────────────────────
    async def capture_pane(
        self, window_id: str, with_ansi: bool = False, scrollback_lines: int = 0
    ) -> str:
        del with_ansi, scrollback_lines
        if window_id != self.window_id:
            return ""
        return self.current_pane()

    async def find_window_by_id(self, window_id: str) -> Any:
        if window_id != self.window_id:
            return None
        return SimpleNamespace(window_id=self.window_id, window_name="repo")

    async def send_keys(
        self, window_id: str, keys: str, enter: bool = True, literal: bool = True
    ) -> bool:
        self.sent.append((window_id, keys, enter, literal))
        if window_id != self.window_id or self.resolved:
            return True
        scr = self.screens[self.idx]
        if keys == "Down":
            self.cursor = self.cursor + 1 if self.cursor < scr.n_nav else 1
        elif keys == "Up":
            self.cursor = self.cursor - 1 if self.cursor > 1 else scr.n_nav
        elif keys == "Enter":
            if 1 <= self.cursor <= scr.n_real:
                self._advance()
        elif literal and keys.isdigit():
            d = int(keys)
            if self.variant == "B":
                if 1 <= d <= scr.n_nav:
                    self.cursor = d  # navigate only — the .168 notes-panel break
            elif 1 <= d <= scr.n_real:  # variant A: select + advance
                self.cursor = d
                self._advance()
        return True


# ──────────────────────────────────────────────────────────────────────────
# Fake Telegram Bot
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class _SentMessage:
    """Record of one outbound Telegram call."""

    method: str  # "send_message", "edit_message_text", ...
    kwargs: dict[str, Any]
    message_id: int


class FakeBot:
    """Records all outbound Telegram calls; returns Message-shaped objects.

    Behaves like an ``AsyncMock`` for the Bot methods bot.py uses, but with a
    monotonic ``message_id`` counter and a structured ``sent`` log so scenario
    tests can assert against the conversation transcript.
    """

    def __init__(self, *, bot_id: int = 555_000_001) -> None:
        self.id = bot_id
        self.sent: list[_SentMessage] = []
        self._next_msg_id = 1000

    # ── primary I/O ────────────────────────────────────────────────────
    async def send_message(self, *, chat_id: int, **kwargs: Any) -> Any:
        return self._record("send_message", {"chat_id": chat_id, **kwargs})

    async def edit_message_text(
        self, *, chat_id: int, message_id: int, **kwargs: Any
    ) -> Any:
        return self._record(
            "edit_message_text",
            {"chat_id": chat_id, "message_id": message_id, **kwargs},
            message_id=message_id,
        )

    async def edit_message_caption(
        self, *, chat_id: int, message_id: int, **kwargs: Any
    ) -> Any:
        return self._record(
            "edit_message_caption",
            {"chat_id": chat_id, "message_id": message_id, **kwargs},
            message_id=message_id,
        )

    async def edit_message_reply_markup(
        self, *, chat_id: int, message_id: int, **kwargs: Any
    ) -> Any:
        return self._record(
            "edit_message_reply_markup",
            {"chat_id": chat_id, "message_id": message_id, **kwargs},
            message_id=message_id,
        )

    async def delete_message(self, *, chat_id: int, message_id: int) -> bool:
        self._record("delete_message", {"chat_id": chat_id, "message_id": message_id})
        return True

    async def send_chat_action(
        self, *, chat_id: int, action: str, **kwargs: Any
    ) -> bool:
        self._record(
            "send_chat_action", {"chat_id": chat_id, "action": action, **kwargs}
        )
        return True

    async def send_photo(self, *, chat_id: int, **kwargs: Any) -> Any:
        return self._record("send_photo", {"chat_id": chat_id, **kwargs})

    async def send_document(self, *, chat_id: int, **kwargs: Any) -> Any:
        return self._record("send_document", {"chat_id": chat_id, **kwargs})

    async def send_voice(self, *, chat_id: int, **kwargs: Any) -> Any:
        return self._record("send_voice", {"chat_id": chat_id, **kwargs})

    async def answer_callback_query(self, *args: Any, **kwargs: Any) -> bool:
        if args and "callback_query_id" not in kwargs:
            kwargs["callback_query_id"] = args[0]
        self._record("answer_callback_query", kwargs)
        return True

    async def get_file(self, file_id: str) -> Any:
        f = MagicMock()
        f.file_id = file_id
        f.file_path = f"voice/{file_id}.oga"

        async def _download(out_path: Any) -> Any:
            Path(out_path).write_bytes(b"\x00")
            return out_path

        f.download_to_drive = AsyncMock(side_effect=_download)
        return f

    async def get_me(self) -> Any:
        return SimpleNamespace(id=self.id, username="cc_telegram_bot", is_bot=True)

    async def set_my_commands(self, *args: Any, **kwargs: Any) -> bool:
        return True

    async def delete_my_commands(self, *args: Any, **kwargs: Any) -> bool:
        return True

    async def get_my_commands(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    # ── helpers ────────────────────────────────────────────────────────
    def _record(
        self,
        method: str,
        kwargs: dict[str, Any],
        *,
        message_id: int | None = None,
    ) -> Any:
        if message_id is None:
            mid = self._next_msg_id
            self._next_msg_id += 1
        else:
            mid = message_id
        self.sent.append(_SentMessage(method=method, kwargs=kwargs, message_id=mid))
        # Return a Message-like object for handlers that capture the result.
        return SimpleNamespace(
            message_id=mid,
            chat_id=kwargs.get("chat_id"),
            text=kwargs.get("text"),
            caption=kwargs.get("caption"),
            reply_markup=kwargs.get("reply_markup"),
        )

    # Convenience filters for assertions.
    def texts(self) -> list[str]:
        return [
            s.kwargs.get("text") or s.kwargs.get("caption") or ""
            for s in self.sent
            if s.method in ("send_message", "edit_message_text", "edit_message_caption")
        ]

    def methods(self) -> list[str]:
        return [s.method for s in self.sent]


# ──────────────────────────────────────────────────────────────────────────
# Update / CallbackQuery factories — public Telegram seam
# ──────────────────────────────────────────────────────────────────────────


_DEFAULT_USER_ID = 12345
_DEFAULT_CHAT_ID = -1001234567890


def _make_chat(chat_id: int = _DEFAULT_CHAT_ID, chat_type: str = "supergroup") -> Any:
    chat = MagicMock(name="Chat")
    chat.id = chat_id
    chat.type = chat_type
    chat.is_forum = True
    chat.send_action = AsyncMock(return_value=True)
    chat.send_message = AsyncMock()
    return chat


def _make_user(user_id: int = _DEFAULT_USER_ID, *, is_bot: bool = False) -> Any:
    user = MagicMock(name="User")
    user.id = user_id
    user.is_bot = is_bot
    user.first_name = "Test"
    user.username = "tester"
    return user


def _make_message(
    *,
    text: str | None = None,
    caption: str | None = None,
    thread_id: int | None = None,
    chat_id: int = _DEFAULT_CHAT_ID,
    user_id: int = _DEFAULT_USER_ID,
    message_id: int = 100,
    photo: Any = None,
    voice: Any = None,
    document: Any = None,
    media_group_id: str | None = None,
    forum_topic_edited: Any = None,
    forum_topic_closed: Any = None,
    forum_topic_created: Any = None,
    reply_to_message: Any = None,
) -> Any:
    msg = MagicMock(name="Message")
    msg.message_id = message_id
    msg.text = text
    msg.caption = caption
    msg.message_thread_id = thread_id
    msg.is_topic_message = thread_id is not None
    msg.chat = _make_chat(chat_id=chat_id)
    msg.chat_id = chat_id
    msg.from_user = _make_user(user_id=user_id)
    msg.photo = photo or []
    msg.voice = voice
    msg.document = document
    msg.media_group_id = media_group_id
    msg.forum_topic_edited = forum_topic_edited
    msg.forum_topic_closed = forum_topic_closed
    msg.forum_topic_created = forum_topic_created
    msg.reply_to_message = reply_to_message
    # Async I/O on the Message object — make these awaitable so safe_reply /
    # safe_edit work against the real handler stack.
    msg.reply_text = AsyncMock(
        return_value=SimpleNamespace(
            message_id=message_id + 1,
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=None,
        )
    )
    msg.reply_html = AsyncMock(return_value=msg.reply_text.return_value)
    msg.reply_photo = AsyncMock(return_value=msg.reply_text.return_value)
    msg.reply_voice = AsyncMock(return_value=msg.reply_text.return_value)
    msg.reply_document = AsyncMock(return_value=msg.reply_text.return_value)
    msg.edit_text = AsyncMock(return_value=msg.reply_text.return_value)
    msg.edit_caption = AsyncMock(return_value=msg.reply_text.return_value)
    msg.delete = AsyncMock(return_value=True)
    return msg


def make_update_text(
    text: str,
    *,
    thread_id: int | None = None,
    user_id: int = _DEFAULT_USER_ID,
    chat_id: int = _DEFAULT_CHAT_ID,
    message_id: int = 100,
) -> Any:
    msg = _make_message(
        text=text,
        thread_id=thread_id,
        user_id=user_id,
        chat_id=chat_id,
        message_id=message_id,
    )
    update = MagicMock(name="Update")
    update.message = msg
    update.callback_query = None
    update.effective_user = _make_user(user_id=user_id)
    update.effective_chat = msg.chat
    update.effective_message = msg
    return update


def make_update_topic_closed(
    *,
    thread_id: int,
    user_id: int = _DEFAULT_USER_ID,
    chat_id: int = _DEFAULT_CHAT_ID,
) -> Any:
    msg = _make_message(
        thread_id=thread_id,
        user_id=user_id,
        chat_id=chat_id,
        forum_topic_closed=MagicMock(name="ForumTopicClosed"),
    )
    update = MagicMock(name="Update")
    update.message = msg
    update.callback_query = None
    update.effective_user = _make_user(user_id=user_id)
    update.effective_chat = msg.chat
    update.effective_message = msg
    return update


def make_update_topic_renamed(
    new_name: str,
    *,
    thread_id: int,
    user_id: int = _DEFAULT_USER_ID,
    chat_id: int = _DEFAULT_CHAT_ID,
) -> Any:
    edited = MagicMock(name="ForumTopicEdited")
    edited.name = new_name
    msg = _make_message(
        thread_id=thread_id,
        user_id=user_id,
        chat_id=chat_id,
        forum_topic_edited=edited,
    )
    update = MagicMock(name="Update")
    update.message = msg
    update.callback_query = None
    update.effective_user = _make_user(user_id=user_id)
    update.effective_chat = msg.chat
    update.effective_message = msg
    return update


def make_update_callback(
    data: str,
    *,
    thread_id: int | None = None,
    message_id: int = 200,
    user_id: int = _DEFAULT_USER_ID,
    chat_id: int = _DEFAULT_CHAT_ID,
) -> Any:
    query = MagicMock(name="CallbackQuery")
    query.id = "cbq-1"
    query.data = data
    query.from_user = _make_user(user_id=user_id)
    query.answer = AsyncMock(return_value=True)
    query.edit_message_text = AsyncMock()
    query.edit_message_caption = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.delete_message = AsyncMock()
    query.message = _make_message(
        thread_id=thread_id,
        user_id=user_id,
        chat_id=chat_id,
        message_id=message_id,
    )
    update = MagicMock(name="Update")
    update.message = None
    update.callback_query = query
    update.effective_user = _make_user(user_id=user_id)
    update.effective_chat = query.message.chat
    update.effective_message = query.message
    return update


def make_update_command(
    command: str,
    *,
    args: str = "",
    thread_id: int | None = None,
    user_id: int = _DEFAULT_USER_ID,
    chat_id: int = _DEFAULT_CHAT_ID,
    message_id: int = 100,
) -> Any:
    text = f"/{command}" + (f" {args}" if args else "")
    msg = _make_message(
        text=text,
        thread_id=thread_id,
        user_id=user_id,
        chat_id=chat_id,
        message_id=message_id,
    )
    msg.entities = [
        SimpleNamespace(type="bot_command", offset=0, length=len(f"/{command}"))
    ]
    update = MagicMock(name="Update")
    update.message = msg
    update.callback_query = None
    update.effective_user = _make_user(user_id=user_id)
    update.effective_chat = msg.chat
    update.effective_message = msg
    return update


def make_context(
    *,
    bot: Any,
    user_data: dict[str, Any] | None = None,
    user_id: int = _DEFAULT_USER_ID,
) -> Any:
    """Build a python-telegram-bot CallbackContext stand-in."""
    ctx = MagicMock(name="CallbackContext")
    ctx.bot = bot
    ctx.user_data = user_data if user_data is not None else {}
    ctx.chat_data = {}
    ctx.bot_data = {}
    ctx.application = MagicMock(name="Application")
    ctx.application.bot = bot
    ctx.application.user_data = {user_id: ctx.user_data}
    ctx.args = []
    return ctx


# ──────────────────────────────────────────────────────────────────────────
# State reset — clears module-level singletons between scenario tests.
# ──────────────────────────────────────────────────────────────────────────


def _reset_session_manager() -> None:
    """Empty the session_manager singleton's persisted dicts.

    SessionManager is a public dataclass; clearing its fields uses its public
    surface, not internals.
    """
    _real_sm.window_states.clear()
    _real_sm.user_window_offsets.clear()
    _real_sm.thread_bindings.clear()
    _real_sm.window_display_names.clear()
    _real_sm.group_chat_ids.clear()


def _reset_aggregator() -> None:
    agg = inbound_aggregator
    for name in ("_bundles", "_locks"):
        attr = getattr(agg, name, None)
        if isinstance(attr, dict):
            attr.clear()


def _reset_status_polling() -> None:
    sp = status_polling
    for name in (
        "_last_pane_capture",
        "_last_published_ui_hash",
        "_absent_streak",
        "_prev_run_state",
    ):
        attr = getattr(sp, name, None)
        if isinstance(attr, dict):
            attr.clear()


def _reset_all_handler_state() -> None:
    ledger_path = app_dir() / auq_ledger.LEDGER_FILENAME
    try:
        ledger_path.unlink()
    except FileNotFoundError:
        pass
    # D2: unlink the durable pick-intent store by its REAL module constant (the
    # shared CC_TELEGRAM_DIR would otherwise leak rows across tests — and a stale
    # neighbor's intent could make a restart-recovery assertion pass for the wrong
    # reason). Keyed by the current constant, never a literal (test-reset-noop).
    try:
        (app_dir() / pick_intent.STORE_FILENAME).unlink()
    except FileNotFoundError:
        pass
    pending_dir = app_dir() / "auq_pending"
    if pending_dir.is_dir():
        for path in pending_dir.glob("*.json"):
            path.unlink(missing_ok=True)
    auq_ledger.reset_for_tests()
    route_runtime.reset_for_tests()
    transcript_event_adapter.reset_for_tests()
    attention.reset_for_tests()
    message_queue.reset_for_tests()
    interactive_ui.reset_for_tests()
    pick_token.reset_for_tests()
    pick_intent.reset_for_tests()
    auq_source.reset_for_tests()
    # Re-inject the production JSONL-cache getter (bot.post_init wires this
    # once at startup, but post_init doesn't run under test). Without it the
    # ``jsonl_cache`` resolver branch would no-op and the render path would
    # silently lose the in-process ``_last_completed_ask_tool_input`` source —
    # a behavior divergence from production. Tests that need the no-op default
    # (getter-reset isolation) call ``auq_source.reset_for_tests()`` themselves.
    auq_source.set_jsonl_cache_getter(
        lambda wid: interactive_ui._last_completed_ask_tool_input.get(wid)
    )
    _reset_aggregator()
    _reset_status_polling()
    _reset_session_manager()


# ──────────────────────────────────────────────────────────────────────────
# Pytest fixtures
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_tmux(monkeypatch: pytest.MonkeyPatch) -> FakeTmux:
    """Replace ``tmux_manager`` singleton methods with a fresh in-memory fake.

    Patches the bound methods on the real singleton so every module that
    already cached ``from .tmux_manager import tmux_manager`` sees the fake.
    """
    fake = FakeTmux()
    for name in (
        "list_windows",
        "find_window_by_id",
        "find_window_by_name",
        "kill_window",
        "rename_window",
        "send_keys",
        "capture_pane",
        "create_window",
        "get_or_create_session",
        "get_session",
        "session_exists",
    ):
        if hasattr(fake, name):
            monkeypatch.setattr(_real_tmux, name, getattr(fake, name), raising=False)
    return fake


@pytest.fixture
def fake_bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def fresh_handler_state() -> Any:
    """Wipe all handler module state before AND after the test.

    Scenario tests use this to start from a clean module surface without
    monkeypatching internals in the test body.
    """
    _reset_all_handler_state()
    yield
    _reset_all_handler_state()


@dataclass
class ScenarioHarness:
    """Driver object wiring together fake tmux, fake bot, and a fresh state.

    Scenario tests typically:

      1. ``h.add_window(...)`` to seed tmux.
      2. ``h.bind_thread(thread_id, window_id)`` to set up an existing topic.
      3. Build an Update via ``make_update_*`` helpers.
      4. Call the real bot handler (``bot_module.text_handler`` etc.) with the
         Update and ``h.context``.
      5. Assert on ``h.bot.sent`` / ``h.tmux.sent_keys`` / state.
    """

    tmux: FakeTmux
    bot: FakeBot
    session_manager: Any
    user_data: dict[str, Any]
    context: Any
    user_id: int = _DEFAULT_USER_ID
    chat_id: int = _DEFAULT_CHAT_ID

    def add_window(
        self,
        *,
        window_id: str | None = None,
        window_name: str,
        cwd: str = "/tmp/test",
        pane_text: str = "",
        pane_text_ansi: str = "",
    ) -> str:
        return self.tmux.add_window(
            window_id=window_id,
            window_name=window_name,
            cwd=cwd,
            pane_text=pane_text,
            pane_text_ansi=pane_text_ansi,
        )

    def bind_thread(
        self,
        thread_id: int,
        window_id: str,
        *,
        display_name: str | None = None,
        cwd: str = "/tmp/test",
        session_id: str = "",
    ) -> None:
        self.session_manager.thread_bindings.setdefault(self.user_id, {})[thread_id] = (
            window_id
        )
        if display_name is not None:
            self.session_manager.window_display_names[window_id] = display_name
        from cctelegram.session import WindowState

        self.session_manager.window_states[window_id] = WindowState(
            session_id=session_id,
            cwd=cwd,
            window_name=display_name
            or self.tmux.windows.get(window_id, _PaneWindow(window_id, "")).window_name,
        )
        self.session_manager.group_chat_ids[f"{self.user_id}:{thread_id}"] = (
            self.chat_id
        )


@pytest.fixture
def scenario(
    fake_tmux: FakeTmux,
    fake_bot: FakeBot,
    fresh_handler_state: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> ScenarioHarness:
    """The Wave A scenario harness.

    Composes ``fake_tmux`` + ``fake_bot`` + a freshly-cleared session_manager
    + handler module state, and provides Update construction helpers.

    Also bypasses ``is_user_allowed`` so the default test user passes the
    allowlist gate without env-var configuration, and stubs
    ``resolve_session_for_window`` so JSONL-file-existence checks don't
    nuke ``window_states[*].session_id`` mid-test (the real path opens an
    on-disk transcript file we don't write in scenarios).
    """
    # `is_user_allowed` is canonically defined in
    # ``cctelegram.handlers.inbound_telegram`` and re-exported from ``bot``.
    # Patch both modules so allowlist bypass takes effect regardless of which
    # module's namespace the caller resolves through.
    monkeypatch.setattr(bot_module, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(inbound_module, "is_user_allowed", lambda _uid: True)

    from cctelegram.session import ClaudeSession

    async def _resolve_session_stub(window_id: str) -> ClaudeSession | None:
        state = _real_sm.window_states.get(window_id)
        if not state or not state.session_id:
            return None
        return ClaudeSession(
            session_id=state.session_id,
            summary="scenario-harness",
            message_count=0,
            file_path="",
        )

    monkeypatch.setattr(_real_sm, "resolve_session_for_window", _resolve_session_stub)

    user_data: dict[str, Any] = {}
    context = make_context(bot=fake_bot, user_data=user_data)
    return ScenarioHarness(
        tmux=fake_tmux,
        bot=fake_bot,
        session_manager=_real_sm,
        user_data=user_data,
        context=context,
    )


# ──────────────────────────────────────────────────────────────────────────
# Re-exports so scenario tests can import factories from this conftest.
# ──────────────────────────────────────────────────────────────────────────


__all__ = [
    "FakeBot",
    "FakeTmux",
    "ScenarioHarness",
    "make_context",
    "make_update_callback",
    "make_update_command",
    "make_update_text",
    "make_update_topic_closed",
    "make_update_topic_renamed",
]
