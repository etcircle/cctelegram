"""GH #43 — pane-derived "background jobs" decoration (plan v2 W4).

A turn that ends with a backgrounded shell still executing renders as plain
idle today: the user watches "1 shell still running" in the terminal while
the topic shows nothing pending. The fix surfaces the pane's shell count as
a pull-only DECORATION — the collapsed done-card gains ``⏳ N background
job(s)`` and /dashboard shows ⏳ instead of ⚪ — never a run-state mutation,
never typing (user decision recorded on the issue).

Pieces under test:
  - W4a ``terminal_parser.parse_background_jobs`` — chrome-region anchored
    (status-bar ``· N shell`` primary, churn-line ``· N shell(s) still
    running`` fallback, MAX on conflict; 0 = chrome present but no token;
    None = no chrome / untrusted frame). Fixture is a REAL v2.1.168 frame.
  - W4b ``handlers/pane_signals`` — bounded in-memory leaf store with
    staleness (3× the 10s capture watchdog) and route/topic teardown.
  - W4c renderers — collapsed done-card suffix; dashboard ⏳ glyph with 🔔
    precedence.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from cctelegram import route_runtime
from cctelegram.route_runtime import RunState, TranscriptLifecycleEvent
from cctelegram.terminal_parser import parse_background_jobs

FIXTURE = Path(__file__).parent / "fixtures" / "gh43_bg_shell_frame.txt"

ROUTE: route_runtime.Route = (1, 42, "@7")


def _evt(
    role: str = "assistant",
    block: str = "text",
    *,
    tool_use_id: str | None = None,
    tool_name: str | None = None,
    stop_reason: str | None = None,
) -> TranscriptLifecycleEvent:
    return TranscriptLifecycleEvent(
        role=role,  # type: ignore[arg-type]
        block_type=block,  # type: ignore[arg-type]
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        stop_reason=stop_reason,
    )


@pytest.fixture(autouse=True)
def _reset():
    from cctelegram.handlers import pane_signals

    route_runtime.reset_for_tests()
    pane_signals.reset_for_tests()
    yield
    route_runtime.reset_for_tests()
    pane_signals.reset_for_tests()


# ── W4a: parser ──────────────────────────────────────────────────────────


def test_parser_real_fixture_reads_one_shell():
    """The captured v2.1.168 frame carries the token twice (churn line +
    status bar) — both say 1."""
    frame = FIXTURE.read_text()
    assert parse_background_jobs(frame) == 1


def test_parser_no_chrome_returns_none():
    assert parse_background_jobs("") is None
    assert parse_background_jobs("just some text\nno chrome here") is None


def test_parser_chrome_but_no_token_returns_zero():
    idle = (
        "$ echo done\n"
        "done\n"
        "──────────────────────────────────────\n"
        "❯ \n"
        "──────────────────────────────────────\n"
        "  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
    )
    assert parse_background_jobs(idle) == 0


def test_parser_ignores_shell_tokens_in_body_prose():
    """`· N shell` strings in Claude's OUTPUT (above the chrome region) must
    not be counted — the scan is anchored to the chrome region only."""
    frame = (
        "Claude says: run with · 3 shells · for parallelism\n"
        "also `2 shells still running` is a string in a doc\n"
        "──────────────────────────────────────\n"
        "❯ \n"
        "──────────────────────────────────────\n"
        "  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
    )
    assert parse_background_jobs(frame) == 0


def test_parser_status_bar_only():
    frame = (
        "some output\n"
        "──────────────────────────────────────\n"
        "❯ \n"
        "──────────────────────────────────────\n"
        "  ⏵⏵ bypass permissions on · 1 shell · ← for agents\n"
    )
    assert parse_background_jobs(frame) == 1


def test_parser_churn_line_only_plural():
    frame = (
        "✻ Churned for 2h 45m 18s · 2 shells still running\n"
        "\n"
        "──────────────────────────────────────\n"
        "❯ \n"
        "──────────────────────────────────────\n"
        "  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
    )
    assert parse_background_jobs(frame) == 2


def test_parser_conflicting_tokens_take_max():
    frame = (
        "✻ Brewed for 6s · 2 shells still running\n"
        "\n"
        "──────────────────────────────────────\n"
        "❯ \n"
        "──────────────────────────────────────\n"
        "  ⏵⏵ bypass permissions on · 1 shell · ← for agents\n"
    )
    assert parse_background_jobs(frame) == 2


# ── W4b: pane_signals leaf ───────────────────────────────────────────────


def test_pane_signals_record_and_peek_fresh():
    from cctelegram.handlers import pane_signals

    now = 1000.0
    assert pane_signals.record_background_jobs(ROUTE, 1, now=now) is True
    assert pane_signals.peek_background_jobs(ROUTE, now=now + 5.0) == 1


def test_pane_signals_staleness_hides_count():
    from cctelegram.handlers import pane_signals

    pane_signals.record_background_jobs(ROUTE, 1, now=1000.0)
    assert (
        pane_signals.peek_background_jobs(
            ROUTE, now=1000.0 + pane_signals.BG_JOBS_MAX_AGE_S + 0.1
        )
        is None
    )


def test_pane_signals_record_returns_changed():
    from cctelegram.handlers import pane_signals

    assert pane_signals.record_background_jobs(ROUTE, 1, now=1000.0) is True
    assert pane_signals.record_background_jobs(ROUTE, 1, now=1001.0) is False
    assert pane_signals.record_background_jobs(ROUTE, 0, now=1002.0) is True


def test_pane_signals_teardown_seams():
    from cctelegram.handlers import pane_signals

    other: route_runtime.Route = (1, 42, "@9")
    pane_signals.record_background_jobs(ROUTE, 1, now=1000.0)
    pane_signals.record_background_jobs(other, 2, now=1000.0)
    pane_signals.clear_route(ROUTE)
    assert pane_signals.peek_background_jobs(ROUTE, now=1001.0) is None
    assert pane_signals.peek_background_jobs(other, now=1001.0) == 2
    pane_signals.clear_routes_for_topic(1, 42)
    assert pane_signals.peek_background_jobs(other, now=1001.0) is None


# ── W4c: collapsed done-card suffix ──────────────────────────────────────


async def _idle_route(route: route_runtime.Route) -> None:
    await route_runtime.ingest_transcript_event(route, _evt("user", "text"))
    await route_runtime.ingest_transcript_event(
        route, _evt("assistant", "text", stop_reason="end_turn")
    )


async def test_collapsed_done_card_gains_bg_jobs_suffix():
    from cctelegram.handlers import message_queue, pane_signals

    await _idle_route(ROUTE)
    pane_signals.record_background_jobs(ROUTE, 1, now=time.time())
    state = message_queue.ActivityDigestState(
        message_id=1,
        window_id="@7",
        tool_count=14,
        completed_count=14,
        done=True,
        started_at=100.0,
        finalized_at=321.0,
    )
    text = message_queue._render_activity_digest(
        state, route=ROUTE, collapse_done=True
    )
    assert "⏳ 1 background job" in text
    assert text.startswith("✅ Done")


async def test_collapsed_done_card_no_suffix_when_zero_or_stale():
    from cctelegram.handlers import message_queue, pane_signals

    await _idle_route(ROUTE)
    state = message_queue.ActivityDigestState(
        message_id=1, window_id="@7", done=True
    )
    # No record at all.
    text = message_queue._render_activity_digest(
        state, route=ROUTE, collapse_done=True
    )
    assert "background job" not in text
    # Zero recorded.
    pane_signals.record_background_jobs(ROUTE, 0, now=time.time())
    text = message_queue._render_activity_digest(
        state, route=ROUTE, collapse_done=True
    )
    assert "background job" not in text
    # Stale record.
    pane_signals.record_background_jobs(
        ROUTE, 2, now=time.time() - pane_signals.BG_JOBS_MAX_AGE_S - 1.0
    )
    text = message_queue._render_activity_digest(
        state, route=ROUTE, collapse_done=True
    )
    assert "background job" not in text


async def test_running_route_never_renders_bg_suffix():
    """The decoration is idle-only — a running route's card is untouched
    even with a fresh count (typing/Busy already say 'work in flight')."""
    from cctelegram.handlers import message_queue, pane_signals

    await route_runtime.ingest_transcript_event(ROUTE, _evt("user", "text"))
    pane_signals.record_background_jobs(ROUTE, 1, now=time.time())
    state = message_queue.ActivityDigestState(
        message_id=1, window_id="@7", done=True
    )
    text = message_queue._render_activity_digest(
        state, route=ROUTE, collapse_done=True
    )
    assert "background job" not in text


# ── W4c: dashboard glyph ─────────────────────────────────────────────────


async def test_dashboard_idle_with_bg_jobs_shows_hourglass(fresh_handler_state):
    from cctelegram.handlers import dashboard, pane_signals
    from cctelegram.session import session_manager

    uid = 12345  # allowed user per conftest env bootstrap
    chat = -1001234567890
    session_manager.bind_thread(uid, 42, "@7", "di-copilot-2")
    session_manager.set_group_chat_id(uid, 42, chat)
    route = (uid, 42, "@7")
    await _idle_route(route)
    pane_signals.record_background_jobs(route, 1, now=time.time())
    text = dashboard.render_dashboard(uid, chat)
    assert "⏳ di-copilot-2" in text
    assert "1 background job" in text
    assert "⚪ di-copilot-2" not in text


async def test_dashboard_unanswered_bell_wins_over_bg_jobs(fresh_handler_state):
    from cctelegram.handlers import dashboard, pane_signals
    from cctelegram.session import session_manager

    uid = 12345
    chat = -1001234567890
    session_manager.bind_thread(uid, 42, "@7", "di-copilot-2")
    session_manager.set_group_chat_id(uid, 42, chat)
    route = (uid, 42, "@7")
    # Unanswered turn: assistant ended after the user's last delivery.
    route_runtime.stamp_user_turn(route, time.time() - 60.0)
    await route_runtime.ingest_transcript_event(route, _evt("user", "text"))
    await route_runtime.ingest_transcript_event(
        route,
        TranscriptLifecycleEvent(
            role="assistant",
            block_type="text",
            tool_use_id=None,
            tool_name=None,
            stop_reason="end_turn",
            timestamp=time.time(),
        ),
    )
    pane_signals.record_background_jobs(route, 1, now=time.time())
    text = dashboard.render_dashboard(uid, chat)
    assert "🔔 di-copilot-2" in text  # the bell outranks the hourglass
    assert "⏳ di-copilot-2" not in text


async def test_dashboard_idle_no_bg_jobs_stays_white(fresh_handler_state):
    from cctelegram.handlers import dashboard, pane_signals
    from cctelegram.session import session_manager

    uid = 12345
    chat = -1001234567890
    session_manager.bind_thread(uid, 42, "@7", "di-copilot-2")
    session_manager.set_group_chat_id(uid, 42, chat)
    route = (uid, 42, "@7")
    await _idle_route(route)
    pane_signals.record_background_jobs(route, 0, now=time.time())
    text = dashboard.render_dashboard(uid, chat)
    assert "⚪ di-copilot-2" in text
    assert "⏳" not in text
