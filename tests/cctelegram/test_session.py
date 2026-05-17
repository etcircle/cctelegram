"""Tests for SessionManager pure dict operations."""

import pytest

from cctelegram.session import SessionManager


@pytest.fixture
def mgr(monkeypatch) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    return SessionManager()


class TestThreadBindings:
    def test_bind_and_get(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        assert mgr.get_window_for_thread(100, 1) == "@1"

    def test_bind_unbind_get_returns_none(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        mgr.unbind_thread(100, 1)
        assert mgr.get_window_for_thread(100, 1) is None

    def test_unbind_nonexistent_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.unbind_thread(100, 999) is None

    def test_iter_thread_bindings(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        mgr.bind_thread(100, 2, "@2")
        mgr.bind_thread(200, 3, "@3")
        result = set(mgr.iter_thread_bindings())
        assert result == {(100, 1, "@1"), (100, 2, "@2"), (200, 3, "@3")}


class TestGroupChatId:
    """Tests for group chat_id routing (supergroup forum topic support).

    IMPORTANT: These tests protect against regression. The group_chat_ids
    mapping is required for Telegram supergroup forum topics — without it,
    all outbound messages fail with "Message thread not found". This was
    erroneously removed once (26cb81f) and restored in PR #23. Do NOT
    delete these tests or the underlying functionality.
    """

    def test_resolve_with_stored_group_id(self, mgr: SessionManager) -> None:
        """resolve_chat_id returns stored group chat_id for known thread."""
        mgr.set_group_chat_id(100, 1, -1001234567890)
        assert mgr.resolve_chat_id(100, 1) == -1001234567890

    def test_resolve_without_group_id_falls_back_to_user_id(
        self, mgr: SessionManager
    ) -> None:
        """resolve_chat_id falls back to user_id when no group_id stored."""
        assert mgr.resolve_chat_id(100, 1) == 100

    def test_resolve_none_thread_id_falls_back_to_user_id(
        self, mgr: SessionManager
    ) -> None:
        """resolve_chat_id returns user_id when thread_id is None (private chat)."""
        mgr.set_group_chat_id(100, 1, -1001234567890)
        assert mgr.resolve_chat_id(100) == 100

    def test_set_group_chat_id_overwrites(self, mgr: SessionManager) -> None:
        """set_group_chat_id updates the stored value on change."""
        mgr.set_group_chat_id(100, 1, -999)
        mgr.set_group_chat_id(100, 1, -888)
        assert mgr.resolve_chat_id(100, 1) == -888

    def test_multiple_threads_independent(self, mgr: SessionManager) -> None:
        """Different threads for the same user store independent group chat_ids."""
        mgr.set_group_chat_id(100, 1, -111)
        mgr.set_group_chat_id(100, 2, -222)
        assert mgr.resolve_chat_id(100, 1) == -111
        assert mgr.resolve_chat_id(100, 2) == -222

    def test_multiple_users_independent(self, mgr: SessionManager) -> None:
        """Different users store independent group chat_ids."""
        mgr.set_group_chat_id(100, 1, -111)
        mgr.set_group_chat_id(200, 1, -222)
        assert mgr.resolve_chat_id(100, 1) == -111
        assert mgr.resolve_chat_id(200, 1) == -222

    def test_set_group_chat_id_with_none_thread(self, mgr: SessionManager) -> None:
        """set_group_chat_id handles None thread_id (mapped to 0)."""
        mgr.set_group_chat_id(100, None, -999)
        # thread_id=None in resolve falls back to user_id (by design)
        assert mgr.resolve_chat_id(100, None) == 100
        # The stored key is "100:0", only accessible with explicit thread_id=0
        assert mgr.group_chat_ids.get("100:0") == -999


class TestWindowState:
    def test_get_creates_new(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@0")
        assert state.session_id == ""
        assert state.cwd == ""

    def test_get_returns_existing(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@1")
        state.session_id = "abc"
        assert mgr.get_window_state("@1").session_id == "abc"

    def test_clear_window_session(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@1")
        state.session_id = "abc"
        mgr.clear_window_session("@1")
        assert mgr.get_window_state("@1").session_id == ""


class TestResolveWindowForThread:
    def test_none_thread_id_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.resolve_window_for_thread(100, None) is None

    def test_unbound_thread_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.resolve_window_for_thread(100, 42) is None

    def test_bound_thread_returns_window(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 42, "@3")
        assert mgr.resolve_window_for_thread(100, 42) == "@3"


class TestDisplayNames:
    def test_get_display_name_fallback(self, mgr: SessionManager) -> None:
        """get_display_name returns window_id when no display name is set."""
        assert mgr.get_display_name("@99") == "@99"

    def test_set_and_get_display_name(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="myproject")
        assert mgr.get_display_name("@1") == "myproject"

    def test_set_display_name_update(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="old-name")
        mgr.window_display_names["@1"] = "new-name"
        assert mgr.get_display_name("@1") == "new-name"

    def test_bind_thread_sets_display_name(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="proj")
        assert mgr.get_display_name("@1") == "proj"

    def test_bind_thread_without_name_no_display(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        # No display name set, fallback to window_id
        assert mgr.get_display_name("@1") == "@1"


class TestFindUsersForSession:
    """find_users_for_session must answer from in-memory window_states without
    reading any JSONL files — that's the hot-path call from handle_new_message,
    and file I/O there is what blew up to multi-minute Telegram-delivery delays.
    """

    @pytest.mark.asyncio
    async def test_matches_window_with_same_session_id(
        self, mgr: SessionManager
    ) -> None:
        from cctelegram.session import WindowState

        mgr.bind_thread(100, 1, "@1")
        mgr.bind_thread(100, 2, "@2")
        mgr.window_states["@1"] = WindowState(session_id="sid-A", cwd="/a")
        mgr.window_states["@2"] = WindowState(session_id="sid-B", cwd="/b")

        result = await mgr.find_users_for_session("sid-A")
        assert result == [(100, "@1", 1)]

    @pytest.mark.asyncio
    async def test_skips_windows_without_session_id(self, mgr: SessionManager) -> None:
        """A bound window with no session_id (hook hasn't fired yet) must not
        spuriously match queries for the empty string."""
        from cctelegram.session import WindowState

        mgr.bind_thread(100, 1, "@1")
        mgr.window_states["@1"] = WindowState(session_id="", cwd="/a")

        # Querying with empty string must not match.
        assert await mgr.find_users_for_session("") == []

    @pytest.mark.asyncio
    async def test_multiple_users_same_session(self, mgr: SessionManager) -> None:
        from cctelegram.session import WindowState

        mgr.bind_thread(100, 1, "@1")
        mgr.bind_thread(200, 5, "@1")
        mgr.window_states["@1"] = WindowState(session_id="sid-shared", cwd="/a")

        result = sorted(await mgr.find_users_for_session("sid-shared"))
        assert result == [(100, "@1", 1), (200, "@1", 5)]

    @pytest.mark.asyncio
    async def test_does_not_open_jsonl_files(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        """Regression: must NOT call resolve_session_for_window (which reads
        the JSONL). The in-memory window_states is enough.
        """
        from cctelegram.session import WindowState

        mgr.bind_thread(100, 1, "@1")
        mgr.window_states["@1"] = WindowState(session_id="sid-A", cwd="/a")

        called = {"resolve": 0}

        async def fake_resolve(self, window_id):  # pragma: no cover
            called["resolve"] += 1
            return None

        monkeypatch.setattr(SessionManager, "resolve_session_for_window", fake_resolve)

        await mgr.find_users_for_session("sid-A")
        assert called["resolve"] == 0


class TestBotSentTextDedup:
    """Tracks bot-originated send_to_window text for user-message echo dedup."""

    def setup_method(self) -> None:
        from cctelegram.session import reset_bot_send_tracking

        reset_bot_send_tracking()

    def teardown_method(self) -> None:
        from cctelegram.session import reset_bot_send_tracking

        reset_bot_send_tracking()

    def test_track_then_consume_matches(self) -> None:
        from cctelegram.session import _track_bot_sent_text, consume_bot_sent_text

        _track_bot_sent_text("sid-1", "hello")
        assert consume_bot_sent_text("sid-1", "hello") is True

    def test_consume_is_one_shot(self) -> None:
        """A single recorded send must not suppress two identical user messages."""
        from cctelegram.session import _track_bot_sent_text, consume_bot_sent_text

        _track_bot_sent_text("sid-1", "hello")
        assert consume_bot_sent_text("sid-1", "hello") is True
        assert consume_bot_sent_text("sid-1", "hello") is False

    def test_track_twice_consume_twice(self) -> None:
        from cctelegram.session import _track_bot_sent_text, consume_bot_sent_text

        _track_bot_sent_text("sid-1", "hello")
        _track_bot_sent_text("sid-1", "hello")
        assert consume_bot_sent_text("sid-1", "hello") is True
        assert consume_bot_sent_text("sid-1", "hello") is True
        assert consume_bot_sent_text("sid-1", "hello") is False

    def test_normalization_strips_whitespace(self) -> None:
        from cctelegram.session import _track_bot_sent_text, consume_bot_sent_text

        _track_bot_sent_text("sid-1", "  hello  ")
        assert consume_bot_sent_text("sid-1", "hello\n") is True

    def test_session_isolation(self) -> None:
        from cctelegram.session import _track_bot_sent_text, consume_bot_sent_text

        _track_bot_sent_text("sid-1", "hello")
        assert consume_bot_sent_text("sid-2", "hello") is False
        assert consume_bot_sent_text("sid-1", "hello") is True

    def test_unknown_session_returns_false(self) -> None:
        from cctelegram.session import consume_bot_sent_text

        assert consume_bot_sent_text("never-tracked", "anything") is False

    def test_empty_text_no_op(self) -> None:
        from cctelegram.session import _track_bot_sent_text, consume_bot_sent_text

        _track_bot_sent_text("sid-1", "   ")  # only whitespace
        assert consume_bot_sent_text("sid-1", "") is False


class TestIsWindowId:
    def test_valid_ids(self, mgr: SessionManager) -> None:
        assert mgr._is_window_id("@0") is True
        assert mgr._is_window_id("@12") is True
        assert mgr._is_window_id("@999") is True

    def test_invalid_ids(self, mgr: SessionManager) -> None:
        assert mgr._is_window_id("myproject") is False
        assert mgr._is_window_id("@") is False
        assert mgr._is_window_id("") is False
        assert mgr._is_window_id("@abc") is False
