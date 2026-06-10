"""Wave C unit tests — the cross-topic dashboard (handlers/dashboard.py).

Covers the busy-signal plan's Wave C contract (v2 C1–C5, v3 C2a/C3a/C-misc,
v4 pre-C fixes 1–3):

  - Renderer: needs-attention-first grouping; 🔔 via WAITING_ON_USER; 🔔 via
    the unanswered-turn wall-clock pair; never-unanswered when either stamp is
    None; ages; empty state.
  - /dashboard command: allowed-user gate, DM/General reject, claim
    posts+persists, re-run elsewhere MOVES (old deleted), pin opt-in with
    persist-only-on-success.
  - Concurrency: per-(chat, owner) lock — concurrent double-/dashboard yields
    exactly one persisted msg_id; loser cleanup after post-send revalidation.
  - SessionManager persistence: dashboards survive bind/unbind/_save_state
    cycles and a fresh load (the fixed-dict regression).
  - Update driver: repaint on run-state transition AND bind/unbind/rename
    (content hash); no-change tick does not edit; MESSAGE_NOT_MODIFIED is
    success; edit-404 self-heals + persists the new msg_id; minute-coarsened
    ages keep the hash stable within the minute.
  - Host-topic death: topic-shaped failure clears the record (no self-heal
    loop into a dead topic); clear_topic_state on the host thread clears it.
  - Multi-user: two owners in one chat get independent, owner-filtered
    dashboards.
  - Boundary: dashboard.py never touches message_queue internals.
"""

from __future__ import annotations

import asyncio
import inspect
import time

import pytest
from telegram.error import BadRequest

from cctelegram import route_runtime
from cctelegram.route_runtime import RunState, TranscriptLifecycleEvent
from cctelegram.handlers import dashboard
from cctelegram.session import SessionManager, session_manager
from tests.conftest import FakeBot, make_context, make_update_command

UID = 12345  # in ALLOWED_USERS per the root conftest env bootstrap
CHAT = -1001234567890


@pytest.fixture(autouse=True)
def _fresh(fresh_handler_state):
    yield


def _evt(
    role: str = "assistant",
    block: str = "text",
    *,
    tool_use_id: str | None = None,
    tool_name: str | None = None,
    stop_reason: str | None = None,
    timestamp: float | None = None,
) -> TranscriptLifecycleEvent:
    return TranscriptLifecycleEvent(
        role=role,  # type: ignore[arg-type]
        block_type=block,  # type: ignore[arg-type]
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        stop_reason=stop_reason,
        timestamp=timestamp,
    )


def _bind(owner: int, thread_id: int, wid: str, name: str) -> None:
    session_manager.bind_thread(owner, thread_id, wid, name)


async def _make_state(route, state: str) -> None:
    """Drive route_runtime into a named run state for the renderer tests."""
    if state == "running":
        await route_runtime.ingest_transcript_event(route, _evt("user", "text"))
    elif state == "running_tool":
        await route_runtime.ingest_transcript_event(route, _evt("user", "text"))
        await route_runtime.ingest_transcript_event(
            route, _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash")
        )
    elif state == "waiting":
        await route_runtime.ingest_transcript_event(route, _evt("user", "text"))
        await route_runtime.ingest_transcript_event(
            route,
            _evt(
                "assistant",
                "tool_use",
                tool_use_id="q1",
                tool_name="AskUserQuestion",
            ),
        )
    elif state == "idle":
        await route_runtime.ingest_transcript_event(route, _evt("user", "text"))
        await route_runtime.ingest_transcript_event(
            route, _evt("assistant", "text", stop_reason="end_turn")
        )
    else:  # pragma: no cover
        raise AssertionError(state)


# ── renderer ─────────────────────────────────────────────────────────────


async def test_renderer_empty_state():
    text = dashboard.render_dashboard(UID)
    assert "No bound topics." in text


async def test_renderer_groups_needs_attention_first():
    _bind(UID, 10, "@1", "idle-repo")
    _bind(UID, 11, "@2", "busy-repo")
    _bind(UID, 12, "@3", "ask-repo")
    await _make_state((UID, 10, "@1"), "idle")
    await _make_state((UID, 11, "@2"), "running")
    await _make_state((UID, 12, "@3"), "waiting")

    text = dashboard.render_dashboard(UID)
    lines = [ln for ln in text.splitlines() if ln.startswith(("🔔", "🟡", "⚪"))]
    assert len(lines) == 3
    assert lines[0].startswith("🔔") and "ask-repo" in lines[0]
    assert lines[1].startswith("🟡") and "busy-repo" in lines[1]
    assert lines[2].startswith("⚪") and "idle-repo" in lines[2]
    assert "waiting on you" in lines[0]
    assert "running" in lines[1]
    assert "idle" in lines[2]


async def test_renderer_waiting_on_user_is_attention():
    _bind(UID, 10, "@1", "repo")
    await _make_state((UID, 10, "@1"), "waiting")
    text = dashboard.render_dashboard(UID)
    assert "🔔 repo — waiting on you" in text


async def test_renderer_unanswered_turn_is_attention():
    """Idle route whose assistant turn ended AFTER the last user turn → 🔔."""
    route = (UID, 10, "@1")
    _bind(UID, 10, "@1", "repo")
    route_runtime.stamp_user_turn(route, 1000.0)
    await route_runtime.ingest_transcript_event(
        route, _evt("assistant", "text", stop_reason="end_turn", timestamp=1500.0)
    )
    snap = route_runtime.snapshot(route)
    assert snap.run_state in (RunState.IDLE_RECENT, RunState.IDLE_CLEARED)
    text = dashboard.render_dashboard(UID)
    assert "🔔 repo — waiting on you" in text


async def test_renderer_answered_turn_is_idle():
    """ended <= user_turn → NOT unanswered (the fast-transcript race shape)."""
    route = (UID, 10, "@1")
    _bind(UID, 10, "@1", "repo")
    await route_runtime.ingest_transcript_event(
        route, _evt("assistant", "text", stop_reason="end_turn", timestamp=1500.0)
    )
    route_runtime.stamp_user_turn(route, 2000.0)
    # Pane idle reconciliation leaves the route idle with both stamps set.
    await route_runtime.mark_pane_idle(route)
    text = dashboard.render_dashboard(UID)
    assert "⚪ repo — idle" in text
    assert "🔔" not in text


async def test_renderer_missing_stamp_never_classifies_unanswered():
    route = (UID, 10, "@1")
    _bind(UID, 10, "@1", "repo")
    # Only the assistant stamp (no user stamp) — restart shape.
    await route_runtime.ingest_transcript_event(
        route, _evt("assistant", "text", stop_reason="end_turn", timestamp=1500.0)
    )
    assert route_runtime.snapshot(route).last_user_turn_at is None
    assert "🔔" not in dashboard.render_dashboard(UID)

    route_runtime.reset_for_tests()
    # Only the user stamp (no assistant stamp).
    route_runtime.stamp_user_turn(route, 1000.0)
    assert "🔔" not in dashboard.render_dashboard(UID)


async def test_renderer_ages_are_minute_coarse():
    route = (UID, 10, "@1")
    _bind(UID, 10, "@1", "repo")
    await _make_state(route, "running")
    base = route_runtime.snapshot(route).last_event_at
    t1 = dashboard.render_dashboard(UID, now_mono=base + 125.0)
    assert "2m" in t1
    # Within the same minute bucket → identical render (hash-stable).
    t2 = dashboard.render_dashboard(UID, now_mono=base + 170.0)
    assert t1 == t2
    # Hours past 60 minutes.
    t3 = dashboard.render_dashboard(UID, now_mono=base + 2 * 3600 + 30.0)
    assert "2h" in t3


async def test_renderer_running_tool_marks_tool():
    route = (UID, 10, "@1")
    _bind(UID, 10, "@1", "repo")
    await _make_state(route, "running_tool")
    text = dashboard.render_dashboard(UID)
    assert "🟡 repo — running (tool" in text


async def test_renderer_filters_to_owner():
    _bind(UID, 10, "@1", "mine")
    _bind(99999, 20, "@2", "theirs")
    text = dashboard.render_dashboard(UID)
    assert "mine" in text
    assert "theirs" not in text


# ── /dashboard command ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dashboard_command_rejects_disallowed_user():
    bot = FakeBot()
    update = make_update_command("dashboard", thread_id=7, user_id=99999)
    await dashboard.dashboard_command(update, make_context(bot=bot, user_id=99999))
    assert bot.sent == []
    assert session_manager.get_dashboard(CHAT, 99999) is None


@pytest.mark.asyncio
async def test_dashboard_command_rejects_dm_and_general():
    bot = FakeBot()
    update = make_update_command("dashboard", thread_id=None)
    await dashboard.dashboard_command(update, make_context(bot=bot))
    update.message.reply_text.assert_awaited()
    assert session_manager.get_dashboard(CHAT, UID) is None
    assert all(s.method != "send_message" for s in bot.sent)


@pytest.mark.asyncio
async def test_dashboard_claim_posts_and_persists():
    bot = FakeBot()
    update = make_update_command("dashboard", thread_id=7)
    await dashboard.dashboard_command(update, make_context(bot=bot))

    sends = [s for s in bot.sent if s.method == "send_message"]
    assert len(sends) == 1
    assert sends[0].kwargs.get("message_thread_id") == 7
    rec = session_manager.get_dashboard(CHAT, UID)
    assert rec is not None
    assert rec["thread_id"] == 7
    assert rec["msg_id"] == sends[0].message_id
    assert rec["pinned"] is False


@pytest.mark.asyncio
async def test_dashboard_rerun_elsewhere_moves_and_deletes_old():
    bot = FakeBot()
    await dashboard.dashboard_command(
        make_update_command("dashboard", thread_id=7), make_context(bot=bot)
    )
    old = session_manager.get_dashboard(CHAT, UID)
    assert old is not None

    await dashboard.dashboard_command(
        make_update_command("dashboard", thread_id=9), make_context(bot=bot)
    )
    rec = session_manager.get_dashboard(CHAT, UID)
    assert rec is not None
    assert rec["thread_id"] == 9
    assert rec["msg_id"] != old["msg_id"]
    deletes = [s for s in bot.sent if s.method == "delete_message"]
    assert any(s.kwargs["message_id"] == old["msg_id"] for s in deletes)


@pytest.mark.asyncio
async def test_dashboard_pin_persists_only_on_success():
    bot = FakeBot()
    await dashboard.dashboard_command(
        make_update_command("dashboard", thread_id=7), make_context(bot=bot)
    )
    await dashboard.dashboard_command(
        make_update_command("dashboard", args="pin", thread_id=7),
        make_context(bot=bot),
    )
    rec = session_manager.get_dashboard(CHAT, UID)
    assert rec is not None and rec["pinned"] is True
    assert any(s.method == "pin_chat_message" for s in bot.sent)


@pytest.mark.asyncio
async def test_dashboard_pin_failure_does_not_persist():
    bot = FakeBot()
    await dashboard.dashboard_command(
        make_update_command("dashboard", thread_id=7), make_context(bot=bot)
    )

    async def _boom(**kwargs):
        raise BadRequest("not enough rights to pin a message")

    bot.pin_chat_message = _boom  # type: ignore[assignment]
    update = make_update_command("dashboard", args="pin", thread_id=7)
    await dashboard.dashboard_command(update, make_context(bot=bot))
    rec = session_manager.get_dashboard(CHAT, UID)
    assert rec is not None and rec["pinned"] is False
    update.message.reply_text.assert_awaited()


@pytest.mark.asyncio
async def test_dashboard_pin_without_dashboard_replies_hint():
    bot = FakeBot()
    update = make_update_command("dashboard", args="pin", thread_id=7)
    await dashboard.dashboard_command(update, make_context(bot=bot))
    assert session_manager.get_dashboard(CHAT, UID) is None
    update.message.reply_text.assert_awaited()


# ── concurrency / loser cleanup ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_double_dashboard_single_record():
    """Two concurrent /dashboard claims serialize on the per-(chat, owner)
    lock — exactly one persisted record and exactly one live message."""
    bot = FakeBot()
    await asyncio.gather(
        dashboard.dashboard_command(
            make_update_command("dashboard", thread_id=7), make_context(bot=bot)
        ),
        dashboard.dashboard_command(
            make_update_command("dashboard", thread_id=9), make_context(bot=bot)
        ),
    )
    rec = session_manager.get_dashboard(CHAT, UID)
    assert rec is not None
    sends = [s for s in bot.sent if s.method == "send_message"]
    deletes = [s for s in bot.sent if s.method == "delete_message"]
    # One live message: every send but the final winner was deleted.
    assert len(sends) - len(deletes) == 1
    live_ids = {s.message_id for s in sends} - {s.kwargs["message_id"] for s in deletes}
    assert live_ids == {rec["msg_id"]}


@pytest.mark.asyncio
async def test_loser_cleanup_deletes_own_message(monkeypatch):
    """If a concurrent winner persisted a different msg_id while our send was
    in flight (cross-process shape), the loser deletes its own message and
    leaves the winner's record alone."""
    bot = FakeBot()
    real_topic_send = dashboard.topic_send

    async def race_topic_send(*args, **kwargs):
        sent, outcome = await real_topic_send(*args, **kwargs)
        # A competing writer lands while our Telegram I/O is in flight.
        session_manager.set_dashboard(CHAT, UID, 99, 424242)
        return sent, outcome

    monkeypatch.setattr(dashboard, "topic_send", race_topic_send)
    await dashboard.dashboard_command(
        make_update_command("dashboard", thread_id=7), make_context(bot=bot)
    )
    rec = session_manager.get_dashboard(CHAT, UID)
    assert rec == {"thread_id": 99, "msg_id": 424242, "pinned": False}
    sends = [s for s in bot.sent if s.method == "send_message"]
    deletes = [s for s in bot.sent if s.method == "delete_message"]
    assert len(sends) == 1
    assert any(d.kwargs["message_id"] == sends[0].message_id for d in deletes)


# ── SessionManager persistence ───────────────────────────────────────────


def test_dashboard_state_survives_bind_unbind_save_cycles():
    session_manager.set_dashboard(CHAT, UID, 7, 1234)
    session_manager.bind_thread(UID, 50, "@9", "repo")
    session_manager.unbind_thread(UID, 50)
    # Fresh load from the same state file (the unknown-key-dropping rewrite
    # regression): the dashboards key must round-trip through _save_state.
    sm2 = SessionManager()
    rec = sm2.get_dashboard(CHAT, UID)
    assert rec == {"thread_id": 7, "msg_id": 1234, "pinned": False}


def test_dashboard_mutation_api():
    session_manager.set_dashboard(CHAT, UID, 7, 1234)
    session_manager.update_dashboard_msg_id(CHAT, UID, 5678)
    assert session_manager.get_dashboard(CHAT, UID)["msg_id"] == 5678
    session_manager.set_dashboard_pinned(CHAT, UID, True)
    assert session_manager.get_dashboard(CHAT, UID)["pinned"] is True
    session_manager.clear_dashboard(CHAT, UID)
    assert session_manager.get_dashboard(CHAT, UID) is None


def test_iter_dashboards_yields_parsed_keys():
    session_manager.set_dashboard(CHAT, UID, 7, 1234)
    rows = list(session_manager.iter_dashboards())
    assert rows == [(CHAT, UID, {"thread_id": 7, "msg_id": 1234, "pinned": False})]


# ── update driver ────────────────────────────────────────────────────────


async def _claim(bot: FakeBot, thread_id: int = 7) -> dict:
    await dashboard.dashboard_command(
        make_update_command("dashboard", thread_id=thread_id),
        make_context(bot=bot),
    )
    rec = session_manager.get_dashboard(CHAT, UID)
    assert rec is not None
    return rec


def _edits(bot: FakeBot) -> list:
    return [s for s in bot.sent if s.method == "edit_message_text"]


@pytest.mark.asyncio
async def test_driver_repaints_on_run_state_transition():
    bot = FakeBot()
    _bind(UID, 10, "@1", "repo")
    rec = await _claim(bot)
    await dashboard.maybe_refresh_dashboards(bot)
    baseline = len(_edits(bot))

    # No change → no edit.
    await dashboard.maybe_refresh_dashboards(bot)
    assert len(_edits(bot)) == baseline

    # Run-state transition → repaint.
    await _make_state((UID, 10, "@1"), "running")
    await dashboard.maybe_refresh_dashboards(bot)
    edits = _edits(bot)
    assert len(edits) == baseline + 1
    assert edits[-1].kwargs["message_id"] == rec["msg_id"]
    assert "running" in edits[-1].kwargs["text"]


@pytest.mark.asyncio
async def test_driver_repaints_on_bind_unbind_and_rename():
    bot = FakeBot()
    _bind(UID, 10, "@1", "repo")
    await _claim(bot)
    await dashboard.maybe_refresh_dashboards(bot)
    baseline = len(_edits(bot))

    # Bind a new topic — no run-state transition anywhere.
    _bind(UID, 11, "@2", "second")
    await dashboard.maybe_refresh_dashboards(bot)
    assert len(_edits(bot)) == baseline + 1
    assert "second" in _edits(bot)[-1].kwargs["text"]

    # Rename.
    session_manager.update_display_name("@2", "renamed")
    await dashboard.maybe_refresh_dashboards(bot)
    assert len(_edits(bot)) == baseline + 2
    assert "renamed" in _edits(bot)[-1].kwargs["text"]

    # Unbind.
    session_manager.unbind_thread(UID, 11)
    await dashboard.maybe_refresh_dashboards(bot)
    assert len(_edits(bot)) == baseline + 3
    assert "renamed" not in _edits(bot)[-1].kwargs["text"]


class _EditRaisesBot(FakeBot):
    def __init__(self, error_message: str) -> None:
        super().__init__()
        self._error_message = error_message

    async def edit_message_text(self, *, chat_id, message_id, **kwargs):
        self._record(
            "edit_message_text_attempt",
            {"chat_id": chat_id, "message_id": message_id, **kwargs},
        )
        raise BadRequest(self._error_message)


@pytest.mark.asyncio
async def test_driver_message_not_modified_is_success():
    bot = _EditRaisesBot("Message is not modified")
    _bind(UID, 10, "@1", "repo")
    rec = await _claim(bot)
    # Content change so the driver attempts the edit, which raises the
    # benign "not modified" — must be treated as success.
    await _make_state((UID, 10, "@1"), "running")
    await dashboard.maybe_refresh_dashboards(bot)
    # Treated success: record intact, NO self-heal re-send, hash advanced
    # (the next tick does not retry the edit).
    assert session_manager.get_dashboard(CHAT, UID) == rec
    sends = [s for s in bot.sent if s.method == "send_message"]
    assert len(sends) == 1  # only the original claim
    attempts = [s for s in bot.sent if s.method == "edit_message_text_attempt"]
    await dashboard.maybe_refresh_dashboards(bot)
    assert len([s for s in bot.sent if s.method == "edit_message_text_attempt"]) == len(
        attempts
    )


@pytest.mark.asyncio
async def test_driver_edit_404_self_heals_and_persists_new_msg_id():
    bot = _EditRaisesBot("Message to edit not found")
    _bind(UID, 10, "@1", "repo")
    rec = await _claim(bot)
    # Content change so the driver attempts the (failing) edit this tick.
    await _make_state((UID, 10, "@1"), "running")
    await dashboard.maybe_refresh_dashboards(bot)
    new_rec = session_manager.get_dashboard(CHAT, UID)
    assert new_rec is not None
    assert new_rec["msg_id"] != rec["msg_id"]
    sends = [s for s in bot.sent if s.method == "send_message"]
    assert sends[-1].message_id == new_rec["msg_id"]


@pytest.mark.asyncio
async def test_driver_topic_broken_clears_record_no_loop():
    bot = _EditRaisesBot("Message thread not found")
    _bind(UID, 10, "@1", "repo")
    await _claim(bot)
    # Content change so the driver attempts the (failing) edit this tick.
    await _make_state((UID, 10, "@1"), "running")
    await dashboard.maybe_refresh_dashboards(bot)
    assert session_manager.get_dashboard(CHAT, UID) is None
    # No self-heal send into the dead topic.
    sends = [s for s in bot.sent if s.method == "send_message"]
    assert len(sends) == 1  # only the original claim
    # And the next tick is a no-op (record gone).
    before = len(bot.sent)
    await dashboard.maybe_refresh_dashboards(bot)
    assert len(bot.sent) == before


@pytest.mark.asyncio
async def test_driver_age_tick_hash_stable_within_minute():
    """Ages render minute-coarse, so back-to-back ticks within the minute
    produce a byte-identical body and no edit."""
    bot = FakeBot()
    _bind(UID, 10, "@1", "repo")
    await _make_state((UID, 10, "@1"), "running")
    await _claim(bot)
    await dashboard.maybe_refresh_dashboards(bot)
    n = len(_edits(bot))
    await dashboard.maybe_refresh_dashboards(bot)
    await dashboard.maybe_refresh_dashboards(bot)
    assert len(_edits(bot)) == n


# ── host-topic death / cleanup wiring ────────────────────────────────────


@pytest.mark.asyncio
async def test_clear_topic_state_clears_hosted_dashboard():
    from cctelegram.handlers.cleanup import clear_topic_state

    bot = FakeBot()
    await _claim(bot, thread_id=7)
    await clear_topic_state(UID, 7, bot)
    assert session_manager.get_dashboard(CHAT, UID) is None


@pytest.mark.asyncio
async def test_clear_topic_state_keeps_dashboard_in_other_thread():
    from cctelegram.handlers.cleanup import clear_topic_state

    bot = FakeBot()
    await _claim(bot, thread_id=7)
    await clear_topic_state(UID, 8, bot)
    assert session_manager.get_dashboard(CHAT, UID) is not None


# ── multi-user isolation ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_two_owners_same_chat_independent_dashboards(monkeypatch):
    other = 67890
    monkeypatch.setattr(dashboard, "is_user_allowed", lambda _uid: True)
    bot = FakeBot()
    _bind(UID, 10, "@1", "mine")
    _bind(other, 20, "@2", "theirs")

    await dashboard.dashboard_command(
        make_update_command("dashboard", thread_id=7, user_id=UID),
        make_context(bot=bot, user_id=UID),
    )
    await dashboard.dashboard_command(
        make_update_command("dashboard", thread_id=8, user_id=other),
        make_context(bot=bot, user_id=other),
    )

    rec_a = session_manager.get_dashboard(CHAT, UID)
    rec_b = session_manager.get_dashboard(CHAT, other)
    assert rec_a is not None and rec_b is not None
    assert rec_a["msg_id"] != rec_b["msg_id"]

    sends = {
        s.message_id: s.kwargs["text"] for s in bot.sent if s.method == "send_message"
    }
    assert "mine" in sends[rec_a["msg_id"]]
    assert "theirs" not in sends[rec_a["msg_id"]]
    assert "theirs" in sends[rec_b["msg_id"]]
    assert "mine" not in sends[rec_b["msg_id"]]


# ── boundary ─────────────────────────────────────────────────────────────


def test_dashboard_module_never_touches_message_queue_internals():
    src = inspect.getsource(dashboard)
    assert "message_queue" not in src
    assert "_status_msg_info" not in src
    assert "register_" not in src  # no observer/callback registration


def test_reset_for_tests_clears_module_state():
    dashboard._last_render_hash[(1, 2)] = "x"
    dashboard._dashboard_locks[(1, 2)] = asyncio.Lock()
    dashboard.reset_for_tests()
    assert dashboard._last_render_hash == {}
    assert dashboard._dashboard_locks == {}


def test_renderer_is_pure_no_now_flake():
    """now_mono is injectable; default falls back to time.monotonic."""
    _bind(UID, 10, "@1", "repo")
    t = dashboard.render_dashboard(UID, now_mono=time.monotonic())
    assert "repo" in t
