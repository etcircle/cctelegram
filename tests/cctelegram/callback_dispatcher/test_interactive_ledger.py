"""Integration tests for the Wave 3 ledger flow in the pick callback handler.

Covers:
  - Per-state behavior matrix for the persisted states + the unknown
    load-time projection (post-restart). The legacy ``digit_sent`` /
    ``failed_before_digit`` / ``failed_after_digit`` projections are still
    served by the matrix (on-disk compat) and asserted as such.
  - Wrong-user replay returns WRONG_USER_PICK_TEXT (not the option label).
  - Legitimate live-token collision falls through to the in-process path.
  - Malformed callback shape bounces with "Card expired".
  - Same-user window-id collision falls through to the token path.

v2.1.168 dispatch model: the live ``aqp:`` dispatch arrow-navigates the cursor to
the tapped option, presses ``Enter`` (the version-stable commit), re-parses the
pane, and records ``dispatched`` ONLY after a confirmed advance. So the
dispatch-path fakes here are CURSOR-AWARE + advance-aware (``FakeTmuxManager``
moves a cursor on ``Down``/``Up`` and, on ``Enter`` from a real option, resolves
the single-question tool → a non-picker pane), and a nav/commit ``send_keys``
returning False now records ``not_advanced`` (the pre-commit bail), not the legacy
``failed_before_digit``.

Uses the same FakeQuery / _ctx / _adapters scaffolding as
``test_dispatcher.py`` for consistency.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cctelegram.callback_dispatcher import (
    DispatcherAdapters,
    authorize_initial,
    execute,
    parse,
)
from cctelegram.handlers import auq_ledger, auq_source, interactive_ui, pick_token
from cctelegram.handlers.callback_data import CB_ASK_PICK
from cctelegram.terminal_parser import resolve_ask_form
from tests.conftest import render_cursor


_OWNER_ID = 1
_INTRUDER_ID = 2
_THREAD_ID = 10
_WINDOW_ID = "@1"

# Real picker pane so validate_and_consume (which re-parses via the REAL parser
# + resolver) re-resolves to the same form the token was minted against — a
# genuine mint/validate parity round-trip rather than a faked fingerprint.
_BASELINE_PANE = (
    Path(__file__).parents[1] / "fixtures" / "auq-baseline-pane.txt"
).read_text()
_BASELINE_FORM = resolve_ask_form(None, _BASELINE_PANE)
assert _BASELINE_FORM is not None
_FINGERPRINT = _BASELINE_FORM.fingerprint()
_BASELINE_SOURCE = auq_source.resolve_auq_source(_WINDOW_ID, None, _BASELINE_PANE)
_OPT = 1
_LABEL = "Done navigating"  # option 1 on the baseline pane


class FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = SimpleNamespace(message_thread_id=_THREAD_ID)
        self.answers: list[tuple[str | None, bool | None]] = []

    async def answer(
        self, text: str | None = None, show_alert: bool | None = None
    ) -> None:
        self.answers.append((text, show_alert))


class FakeSessionManager:
    def __init__(self, current_window: str | None = _WINDOW_ID) -> None:
        self.current_window = current_window

    def resolve_window_for_thread(
        self, _user_id: int, _thread_id: int | None
    ) -> str | None:
        return self.current_window


# A resolved (non-picker) pane: no AUQ marker phrases, so
# ``_pane_looks_like_picker`` is False → the v2.1.168 confirm step reads the
# single-question pick as positively RESOLVED (the tool's picker disappeared).
_RESOLVED_PANE = "user@host repo % \n"

# The baseline pane offers 3 real options (1-3) plus affordances 4 (Type
# something) and 5 (Chat about this) — 5 navigable rows.
_BASELINE_N_REAL = 3
_BASELINE_N_NAV = 5


class FakeTmuxManager:
    """Cursor-aware + advance-aware fake of the v2.1.168 single-question picker.

    Models the captured .168 keystroke semantics for the live ``aqp:`` dispatch
    path: ``Down``/``Up`` move the cursor (with wrap), ``Enter`` from a real
    option resolves the single-question tool (the picker disappears →
    ``_RESOLVED_PANE``). ``capture_pane`` is STATEFUL — before the commit it
    renders the baseline pane with the cursor on its current row (so the dispatch's
    post-nav VERIFY sees the moved cursor), and after the resolving Enter it returns
    the non-picker pane (so the confirm step records ``dispatched``).

    The cursor starts on option 1 (the baseline pane's ``❯`` row), matching the
    pane the token is minted from.
    """

    def __init__(self) -> None:
        self.find_window_by_id = AsyncMock(
            return_value=SimpleNamespace(window_id=_WINDOW_ID)
        )
        self.cursor = 1
        self.resolved = False
        self.send_keys = AsyncMock(side_effect=self._send_keys)
        self.capture_pane = AsyncMock(side_effect=self._capture_pane)

    async def _send_keys(
        self, window_id: str, keys: str, enter: bool = True, literal: bool = True
    ) -> bool:
        if window_id != _WINDOW_ID or self.resolved:
            return True
        if keys == "Down":
            self.cursor = self.cursor + 1 if self.cursor < _BASELINE_N_NAV else 1
        elif keys == "Up":
            self.cursor = self.cursor - 1 if self.cursor > 1 else _BASELINE_N_NAV
        elif keys == "Enter":
            if 1 <= self.cursor <= _BASELINE_N_REAL:
                self.resolved = True  # single-question tool resolves
        return True

    async def _capture_pane(
        self, window_id: str, scrollback_lines: int = 0, with_ansi: bool = False
    ) -> str:
        if window_id != _WINDOW_ID:
            return ""
        if self.resolved:
            return _RESOLVED_PANE
        return render_cursor(_BASELINE_PANE, self.cursor)


def _ctx(query: FakeQuery, user_id: int = _OWNER_ID) -> SimpleNamespace:
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
        thread_id=_THREAD_ID,
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
        # Only consumed by the toggle path; the pick path re-parses via the
        # real parser inside validate_and_consume.
        terminal_parser=SimpleNamespace(
            resolve_ask_form=lambda _cached_input, _pane: _BASELINE_FORM
        ),
    )


def _build_keyed_callback(
    user_id: int = _OWNER_ID,
    *,
    window_id: str = _WINDOW_ID,
    fingerprint: str = _FINGERPRINT,
    option_number: int = _OPT,
    label: str = _LABEL,
    is_submit: bool = False,
) -> tuple[str, str]:
    """Mint a pick token + build the Wave 3 keyed callback_data.

    Returns ``(callback_data, ledger_key)`` so tests can assert against
    both the rendered shape and the derived ledger key. The token records the
    real baseline-pane source tags so validate_and_consume's source-parity
    compare passes on the dispatch (``ok``) path.
    """
    token = pick_token.mint(
        pick_token.PickTokenEntry(
            window_id=window_id,
            user_id=user_id,
            thread_id=_THREAD_ID,
            fingerprint=fingerprint,
            option_number=option_number,
            option_label=label,
            is_review_submit=is_submit,
            expires_at=time.monotonic() + 300,
            source_kind=_BASELINE_SOURCE.kind,
            source_fingerprint=_BASELINE_SOURCE.source_fingerprint,
            row_generation=1,
        )
    )
    route_hash = auq_ledger.make_route_hash(user_id, _THREAD_ID, window_id)
    fp8 = fingerprint[:8]
    callback_data = f"{CB_ASK_PICK}{route_hash}:{fp8}:{option_number}:{token}"
    ledger_key = auq_ledger.make_ledger_key(route_hash, fp8, option_number)
    return callback_data, ledger_key


@pytest.fixture(autouse=True)
def setup_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Reset both the pick-token store and the ledger before/after each test.

    Also stubs the dispatcher's post-dispatch ``handle_interactive_ui``
    re-render to a no-op: validate_and_consume now re-parses the REAL baseline
    pane (so the dispatch path reaches the re-render), but these tests assert
    ledger state + the callback answer, not the re-rendered card. The
    dispatcher binds ``handle_interactive_ui`` by name at import, so patch it on
    the dispatcher module.
    """
    from cctelegram.callback_dispatcher import interactive as cb_interactive

    pick_token.reset_for_tests()
    auq_ledger.reset_for_tests(
        path=tmp_path / "ledger.jsonl",
        start_time=time.time(),
    )
    monkeypatch.setattr(
        cb_interactive, "handle_interactive_ui", AsyncMock(return_value=True)
    )
    yield
    pick_token.reset_for_tests()
    auq_ledger.reset_for_tests()


def _seed_ledger(
    ledger_key: str,
    state: auq_ledger.LedgerState,
    *,
    user_id: int = _OWNER_ID,
    window_id: str = _WINDOW_ID,
    accepted_at: float | None = None,
) -> None:
    """Seed an entry directly via record() then patch accepted_at if needed."""
    auq_ledger.record(
        ledger_key,
        state="accepted",
        user_id=user_id,
        window_id=window_id,
        full_fingerprint=_FINGERPRINT,
        option_number=_OPT,
        option_label=_LABEL,
    )
    if state != "accepted":
        auq_ledger.record(ledger_key, state=state)
    if accepted_at is not None:
        # Replace the in-memory row to backdate accepted_at — emulates a
        # row written by a previous process.
        old = auq_ledger.lookup(ledger_key)
        assert old is not None
        from dataclasses import replace

        auq_ledger._entries[ledger_key] = replace(old, accepted_at=accepted_at)


class TestStateMatrixSameProcess:
    @pytest.mark.asyncio
    async def test_dispatched_returns_already_received(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        callback_data, ledger_key = _build_keyed_callback()
        _seed_ledger(ledger_key, "dispatched")
        query = FakeQuery(callback_data)
        authorized = authorize_initial(parse(query.data.encode()), _ctx(query))
        await execute(authorized, _adapters(FakeSessionManager(), FakeTmuxManager()))
        assert query.answers == [(f"Action already received: {_LABEL}", False)]

    @pytest.mark.asyncio
    async def test_accepted_same_process_returns_in_progress(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        callback_data, ledger_key = _build_keyed_callback()
        _seed_ledger(ledger_key, "accepted")
        query = FakeQuery(callback_data)
        authorized = authorize_initial(parse(query.data.encode()), _ctx(query))
        await execute(authorized, _adapters(FakeSessionManager(), FakeTmuxManager()))
        assert query.answers == [("Action in progress", False)]

    @pytest.mark.asyncio
    async def test_digit_sent_same_process_returns_in_progress(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        callback_data, ledger_key = _build_keyed_callback()
        _seed_ledger(ledger_key, "digit_sent")
        query = FakeQuery(callback_data)
        authorized = authorize_initial(parse(query.data.encode()), _ctx(query))
        await execute(authorized, _adapters(FakeSessionManager(), FakeTmuxManager()))
        assert query.answers == [("Action in progress", False)]

    @pytest.mark.asyncio
    async def test_failed_before_digit_refreshes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            interactive_ui, "handle_interactive_ui", AsyncMock(return_value=True)
        )
        callback_data, ledger_key = _build_keyed_callback()
        _seed_ledger(ledger_key, "failed_before_digit")
        # Bind the route's interactive window so refresh resolves.
        interactive_ui.set_interactive_mode(_OWNER_ID, _WINDOW_ID, _THREAD_ID)
        query = FakeQuery(callback_data)
        authorized = authorize_initial(parse(query.data.encode()), _ctx(query))
        await execute(authorized, _adapters(FakeSessionManager(), FakeTmuxManager()))
        assert query.answers == [("Action failed previously; refreshing.", False)]

    @pytest.mark.asyncio
    async def test_failed_after_digit_refreshes_with_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            interactive_ui, "handle_interactive_ui", AsyncMock(return_value=True)
        )
        callback_data, ledger_key = _build_keyed_callback()
        _seed_ledger(ledger_key, "failed_after_digit")
        interactive_ui.set_interactive_mode(_OWNER_ID, _WINDOW_ID, _THREAD_ID)
        query = FakeQuery(callback_data)
        authorized = authorize_initial(parse(query.data.encode()), _ctx(query))
        await execute(authorized, _adapters(FakeSessionManager(), FakeTmuxManager()))
        assert query.answers == [
            (
                "Action sent but interrupted; refreshing — verify in tmux.",
                False,
            )
        ]


class TestStateMatrixPostRestart:
    """``accepted`` / ``digit_sent`` entries written by a prior process
    (accepted_at < process_start_time) project to the ``unknown`` status
    and trigger a "please re-tap" refresh.
    """

    @pytest.mark.asyncio
    async def test_accepted_pre_start_projects_to_unknown(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            interactive_ui, "handle_interactive_ui", AsyncMock(return_value=True)
        )
        # process_start_time is "now"; backdate accepted_at to "before".
        now = time.time()
        auq_ledger.reset_for_tests(path=tmp_path / "ledger.jsonl", start_time=now)
        callback_data, ledger_key = _build_keyed_callback()
        _seed_ledger(ledger_key, "accepted", accepted_at=now - 60.0)
        interactive_ui.set_interactive_mode(_OWNER_ID, _WINDOW_ID, _THREAD_ID)
        query = FakeQuery(callback_data)
        authorized = authorize_initial(parse(query.data.encode()), _ctx(query))
        await execute(authorized, _adapters(FakeSessionManager(), FakeTmuxManager()))
        assert query.answers == [("Action interrupted; please re-tap.", False)]

    @pytest.mark.asyncio
    async def test_digit_sent_pre_start_projects_to_unknown(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            interactive_ui, "handle_interactive_ui", AsyncMock(return_value=True)
        )
        now = time.time()
        auq_ledger.reset_for_tests(path=tmp_path / "ledger.jsonl", start_time=now)
        callback_data, ledger_key = _build_keyed_callback()
        _seed_ledger(ledger_key, "digit_sent", accepted_at=now - 60.0)
        interactive_ui.set_interactive_mode(_OWNER_ID, _WINDOW_ID, _THREAD_ID)
        query = FakeQuery(callback_data)
        authorized = authorize_initial(parse(query.data.encode()), _ctx(query))
        await execute(authorized, _adapters(FakeSessionManager(), FakeTmuxManager()))
        assert query.answers == [("Action interrupted; please re-tap.", False)]


class TestOwnerSecurity:
    @pytest.mark.asyncio
    async def test_wrong_user_replay_returns_wrong_user_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """v4 §7.2 P1: owner already dispatched; intruder taps the same
        callback_data with no live token of their own → must return
        WRONG_USER_PICK_TEXT (NOT the option label, NOT "already received").
        """
        callback_data, ledger_key = _build_keyed_callback(user_id=_OWNER_ID)
        _seed_ledger(ledger_key, "dispatched", user_id=_OWNER_ID)
        query = FakeQuery(callback_data)
        authorized = authorize_initial(
            parse(query.data.encode()), _ctx(query, user_id=_INTRUDER_ID)
        )
        await execute(authorized, _adapters(FakeSessionManager(), FakeTmuxManager()))
        assert query.answers == [("This control isn't yours.", True)]
        # Crucial: the option label must NOT leak via the "already received"
        # text we'd send to a same-user replay.
        for text, _ in query.answers:
            assert _LABEL not in (text or "")

    @pytest.mark.asyncio
    async def test_owner_mismatch_without_collision_returns_wrong_user(
        self,
    ) -> None:
        """Realistic wrong-user replay (no key collision): owner's ledger
        row is ``dispatched``; the intruder taps the same callback_data;
        intruder's live token (if any) hashes to a DIFFERENT ledger key
        because route_hash depends on user_id. ``is_collision`` evaluates
        to False → handler returns WRONG_USER_PICK_TEXT, no label leak.

        Documents the safe outcome when sha1 over
        ``user_id:thread_id:window_id`` does NOT collide — the everyday
        case.
        """
        route_hash = auq_ledger.make_route_hash(_OWNER_ID, _THREAD_ID, _WINDOW_ID)
        fp8 = _FINGERPRINT[:8]
        ledger_key = auq_ledger.make_ledger_key(route_hash, fp8, _OPT)
        _seed_ledger(ledger_key, "dispatched", user_id=_OWNER_ID)
        # Intruder mints their own live token; route_hash differs from
        # owner's because user_id is in the sha1 input.
        b_callback_data, _ = _build_keyed_callback(user_id=_INTRUDER_ID)
        b_token = b_callback_data.split(":")[-1]
        forced_callback_data = f"{CB_ASK_PICK}{route_hash}:{fp8}:{_OPT}:{b_token}"
        query = FakeQuery(forced_callback_data)
        authorized = authorize_initial(
            parse(query.data.encode()), _ctx(query, user_id=_INTRUDER_ID)
        )
        await execute(authorized, _adapters(FakeSessionManager(), FakeTmuxManager()))
        assert query.answers == [("This control isn't yours.", True)]

    @pytest.mark.asyncio
    async def test_legitimate_collision_falls_through_to_token_dispatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Forced true collision: monkeypatch ``pick_token.stable_key`` so the
        clicker's live token reconstructs the owner's ledger key. The
        handler's ``is_collision=True`` branch should clear the ledger
        gate and fall through to the in-process token path; the clicker's
        tap dispatches normally; the owner's ledger row stays intact.

        Codex Wave 3 P2: the prior "collision" test exercised the
        rejection path (because real sha1 collisions are astronomically
        unlikely). This test forces the True branch via monkeypatch so
        the code under ``interactive.py``'s ``existing = None`` clear is
        actually executed.
        """
        monkeypatch.setattr("asyncio.sleep", AsyncMock())
        monkeypatch.setattr(
            "cctelegram.handlers.interactive_ui.resolve_ask_tool_input",
            lambda _wid: None,
        )

        # Seed owner's ledger row in ``dispatched``.
        route_hash = auq_ledger.make_route_hash(_OWNER_ID, _THREAD_ID, _WINDOW_ID)
        fp8 = _FINGERPRINT[:8]
        ledger_key = auq_ledger.make_ledger_key(route_hash, fp8, _OPT)
        _seed_ledger(ledger_key, "dispatched", user_id=_OWNER_ID)

        # Intruder mints their own live token (different real route_hash).
        b_callback_data, _ = _build_keyed_callback(user_id=_INTRUDER_ID)
        b_token = b_callback_data.split(":")[-1]
        # Intruder's callback data carries the OWNER'S key (collision shape).
        forced_callback_data = f"{CB_ASK_PICK}{route_hash}:{fp8}:{_OPT}:{b_token}"

        # Force ``pick_token.stable_key(live)`` to return the owner's ledger
        # key so the collision-defense predicate is True. In production this
        # would require an actual sha1 collision; here we just make the
        # path executable.
        monkeypatch.setattr(pick_token, "stable_key", lambda _entry: ledger_key)

        query = FakeQuery(forced_callback_data)
        authorized = authorize_initial(
            parse(query.data.encode()), _ctx(query, user_id=_INTRUDER_ID)
        )
        await execute(authorized, _adapters(FakeSessionManager(), FakeTmuxManager()))

        # Intruder's tap dispatches normally — answer carries the option
        # label.
        assert query.answers == [(f"{_OPT}. {_LABEL}", False)]
        # Plan v4 §7.2 contract: the owner's ledger row at this key MUST
        # stay put — the collision branch drops `ledger_key` so the
        # intruder's accepted/digit_sent/dispatched writes go to nothing.
        # On the owner's retry, "Action already received" still works.
        owner_row_after = auq_ledger.lookup(ledger_key)
        assert owner_row_after is not None
        assert owner_row_after.user_id == _OWNER_ID
        assert owner_row_after.state == "dispatched"


class TestMalformedCallbacks:
    @pytest.mark.asyncio
    async def test_malformed_three_part_callback_refreshes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            interactive_ui, "handle_interactive_ui", AsyncMock(return_value=True)
        )
        interactive_ui.set_interactive_mode(_OWNER_ID, _WINDOW_ID, _THREAD_ID)
        query = FakeQuery(f"{CB_ASK_PICK}foo:bar:baz")  # 3 parts, neither shape
        authorized = authorize_initial(parse(query.data.encode()), _ctx(query))
        await execute(authorized, _adapters(FakeSessionManager(), FakeTmuxManager()))
        assert query.answers == [("Card expired, refreshing.", False)]

    @pytest.mark.asyncio
    async def test_keyed_callback_with_non_int_opt_refreshes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hermes Wave 3 P2: a 4-part keyed callback whose ``opt`` slot
        isn't a parseable integer must bounce "Card expired, refreshing"
        — not crash on ``int()`` ValueError.
        """
        monkeypatch.setattr(
            interactive_ui, "handle_interactive_ui", AsyncMock(return_value=True)
        )
        interactive_ui.set_interactive_mode(_OWNER_ID, _WINDOW_ID, _THREAD_ID)
        query = FakeQuery(f"{CB_ASK_PICK}deadbeef:abcdef12:notint:deadbeefcafe")
        authorized = authorize_initial(parse(query.data.encode()), _ctx(query))
        await execute(authorized, _adapters(FakeSessionManager(), FakeTmuxManager()))
        assert query.answers == [("Card expired, refreshing.", False)]


class TestSameUserWindowCollision:
    @pytest.mark.asyncio
    async def test_window_mismatch_falls_through_to_token_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ledger row's window_id differs from the route's current bound
        window — treat as collision, drop the ledger gate, fall through
        to the in-process token path. No WRONG_USER_PICK_TEXT, no
        "already received".
        """
        monkeypatch.setattr("asyncio.sleep", AsyncMock())
        monkeypatch.setattr(
            "cctelegram.handlers.interactive_ui.resolve_ask_tool_input",
            lambda _wid: None,
        )
        callback_data, ledger_key = _build_keyed_callback()
        # Seed ledger with a different window_id on the same route_hash.
        _seed_ledger(ledger_key, "dispatched", window_id="@99")
        # Bind the route to the original window so get_interactive_window
        # returns _WINDOW_ID, which differs from the ledger row's @99.
        interactive_ui.set_interactive_mode(_OWNER_ID, _WINDOW_ID, _THREAD_ID)
        query = FakeQuery(callback_data)
        authorized = authorize_initial(parse(query.data.encode()), _ctx(query))
        await execute(authorized, _adapters(FakeSessionManager(), FakeTmuxManager()))
        # Falls through to dispatch: answer carries the option label.
        assert query.answers == [(f"{_OPT}. {_LABEL}", False)]


class TestAcceptedToDispatchedHappyPath:
    @pytest.mark.asyncio
    async def test_first_keyed_tap_writes_full_state_machine(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Owner taps a fresh keyed button. v2.1.168 navigate+Enter model:
        the ledger walks accepted → dispatched, where ``dispatched`` is recorded
        only after the post-Enter confirm proves the expected advance.
        """
        monkeypatch.setattr("asyncio.sleep", AsyncMock())
        monkeypatch.setattr(
            "cctelegram.handlers.interactive_ui.resolve_ask_tool_input",
            lambda _wid: None,
        )
        callback_data, ledger_key = _build_keyed_callback()
        query = FakeQuery(callback_data)
        authorized = authorize_initial(parse(query.data.encode()), _ctx(query))
        await execute(authorized, _adapters(FakeSessionManager(), FakeTmuxManager()))
        assert query.answers == [(f"{_OPT}. {_LABEL}", False)]
        entry = auq_ledger.lookup(ledger_key)
        assert entry is not None
        assert entry.state == "dispatched"
        assert entry.user_id == _OWNER_ID
        assert entry.option_label == _LABEL
        # digit_sent is no longer written by the dispatch path (legacy state).
        assert entry.digit_sent_at is None
        assert entry.dispatched_at is not None


class TestSendKeysFailureRecordsNotAdvanced:
    """Codex Wave 3 P1: tmux send_keys returns False on missing session /
    window / pane / libtmux exception. Handler MUST check the return and NOT
    record ``dispatched``. A silent False return used to convert a tmux failure
    into a permanent "already received" — duplicate tap then locked the user out
    of the action even though tmux never received the keystroke.

    v2.1.168 model: a nav (``Down``/``Up``) OR the commit ``Enter`` returning
    False is a PRE-COMMIT bail (``Enter`` provably never committed) → the ledger
    records ``not_advanced`` (the retryable state), NOT the legacy
    ``failed_before_digit``. The callback answers "Action not registered;
    refreshing card." and the row is fall-through retryable, never the terminal
    "already received" lock.
    """

    @pytest.mark.asyncio
    async def test_commit_send_returns_false_records_not_advanced(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Option 1 (cursor already on it): delta=0, so the FIRST send_keys is the
        # commit Enter — it returns False → pre-commit bail (commit_send_failed).
        monkeypatch.setattr("asyncio.sleep", AsyncMock())
        monkeypatch.setattr(
            "cctelegram.handlers.interactive_ui.resolve_ask_tool_input",
            lambda _wid: None,
        )
        monkeypatch.setattr(
            interactive_ui, "handle_interactive_ui", AsyncMock(return_value=True)
        )
        callback_data, ledger_key = _build_keyed_callback()
        tmux = FakeTmuxManager()
        # Every send_keys returns False (the commit Enter never reaches tmux).
        tmux.send_keys = AsyncMock(return_value=False)
        query = FakeQuery(callback_data)
        authorized = authorize_initial(parse(query.data.encode()), _ctx(query))
        await execute(authorized, _adapters(FakeSessionManager(), tmux))
        assert query.answers == [("Action not registered; refreshing card.", False)]
        entry = auq_ledger.lookup(ledger_key)
        assert entry is not None
        assert entry.state == "not_advanced"
        assert entry.failed_reason == "commit_send_failed"
        assert entry.dispatched_at is None
        # Exactly one send_keys call (the commit Enter); delta=0 sends no nav keys.
        assert tmux.send_keys.await_count == 1

    @pytest.mark.asyncio
    async def test_nav_send_returns_false_records_not_advanced(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Option 3 with the cursor on option 1: delta=2 → the FIRST send_keys is a
        # ``Down`` nav step. It returns False → pre-commit bail (nav_send_failed);
        # the Enter is never reached.
        monkeypatch.setattr("asyncio.sleep", AsyncMock())
        monkeypatch.setattr(
            "cctelegram.handlers.interactive_ui.resolve_ask_tool_input",
            lambda _wid: None,
        )
        monkeypatch.setattr(
            interactive_ui, "handle_interactive_ui", AsyncMock(return_value=True)
        )
        callback_data, ledger_key = _build_keyed_callback(
            option_number=3, label="Defer Wave 0"
        )
        tmux = FakeTmuxManager()
        sent: list[str] = []

        async def _send_keys(
            window_id: str, keys: str, enter: bool = True, literal: bool = True
        ) -> bool:
            sent.append(keys)
            return False  # the first nav step fails

        tmux.send_keys = AsyncMock(side_effect=_send_keys)
        query = FakeQuery(callback_data)
        authorized = authorize_initial(parse(query.data.encode()), _ctx(query))
        await execute(authorized, _adapters(FakeSessionManager(), tmux))
        assert query.answers == [("Action not registered; refreshing card.", False)]
        entry = auq_ledger.lookup(ledger_key)
        assert entry is not None
        assert entry.state == "not_advanced"
        assert entry.failed_reason == "nav_send_failed"
        assert entry.dispatched_at is None
        # Bailed on the first nav step — never reached Enter.
        assert sent == ["Down"]
