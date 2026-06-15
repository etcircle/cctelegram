"""Wave B unit tests — Notification side-file consumption in ``status_polling``.

Covers the poller seam (plan v2 B4 + v3 B1d + v4 fix 2):

  - Consumption runs at the TOP of the per-binding path, BEFORE the
    adaptive capture gating — a capture-skipped tick still consumes.
  - A 🔔 transition repaints the digest the SAME tick.
  - The unlink ordering is driven by the ``NotificationMarkResult``:
    committed-live → generation-guarded unlink AFTER the commit;
    redundant / stale → generation-guarded unlink; ignored → NO unlink.
  - Runtime-state TTL: expiry clears even with the side file already
    gone; pending-without-set_at is treated as expired.
  - An on-disk record older than the TTL is treated absent + unlinked.
  - Pane observed RUNNING at a capture sufficiently after
    ``notification_set_at`` clears the bit (generation-guarded unlink of a
    still-present file) — LEVEL + time-qualified, NOT an idle→active edge
    (gate P2-1: the adaptive capture can skip the blocked approval frame
    entirely, so an edge requirement strands the bit when the last capture
    before the notification was already running).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cctelegram import route_runtime
from cctelegram.handlers import attention, status_polling
from cctelegram.handlers.message_sender import TopicSendOutcome
from cctelegram.route_runtime import (
    NOTIFY_TTL_SECONDS,
    RunState,
    TranscriptLifecycleEvent,
)
from cctelegram.session import WindowState, session_manager
from cctelegram.tmux_manager import tmux_manager as real_tmux

_SID = "550e8400-e29b-41d4-a716-446655440000"
_WID = "@5"
_USER = 1
_THREAD = 42
_ROUTE = (_USER, _THREAD, _WID)

_ACTIVE_PANE = (
    "✻ Cooking for 2s\n"
    "──────────────────────────────────────\n"
    "❯ \n"
    "──────────────────────────────────────\n"
    "  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt\n"
)


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    sent = MagicMock()
    sent.message_id = 999
    bot.send_message.return_value = sent
    return bot


@pytest.fixture
def _env(tmp_path, monkeypatch):
    """tmp app_dir + bound window + clean per-route poller state."""
    monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
    session_manager.window_states[_WID] = WindowState(cwd="/tmp/x", session_id=_SID)
    route_runtime.reset_for_tests()
    attention.reset_for_tests()
    status_polling._last_pane_capture.clear()
    status_polling._prev_run_state.clear()
    status_polling._decision_card_eot_grace.clear()
    yield tmp_path
    session_manager.window_states.pop(_WID, None)
    route_runtime.reset_for_tests()
    attention.reset_for_tests()
    status_polling._last_pane_capture.clear()
    status_polling._prev_run_state.clear()
    status_polling._decision_card_eot_grace.clear()


def _write_record(
    cc_dir: Path,
    *,
    ts: float | None = None,
    generation: str = "g1",
) -> Path:
    d = cc_dir / "notify_pending"
    d.mkdir(mode=0o700, exist_ok=True)
    rec = {
        "schema_version": 1,
        "session_id": _SID,
        "ts": ts if ts is not None else time.time(),
        "window_key": f"{real_tmux.session_name}:{_WID}",
        "generation": generation,
        "kind": "permission",
    }
    path = d / f"{_SID}.json"
    path.write_text(json.dumps(rec))
    return path


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


async def _tick(mock_bot, pane_text: str | None = None) -> None:
    """One update_status_message tick against a fake tmux window."""
    window = MagicMock()
    window.window_id = _WID
    with (
        patch.object(status_polling, "tmux_manager") as mock_tmux,
        patch.object(status_polling, "enqueue_status_update", AsyncMock()),
        # The real resolver clears a bound window's session_id when its JSONL
        # is missing (as it is under a tmp app_dir) — keep window_states intact.
        patch.object(
            status_polling.session_manager,
            "resolve_session_for_window",
            AsyncMock(return_value=None),
        ),
    ):
        mock_tmux.find_window_by_id = AsyncMock(return_value=window)
        mock_tmux.capture_pane = AsyncMock(return_value=pane_text)
        await status_polling.update_status_message(
            mock_bot, user_id=_USER, window_id=_WID, thread_id=_THREAD
        )


# ── consumption + unlink ordering by mark result ─────────────────────────


async def test_committed_live_sets_bit_and_unlinks(_env, mock_bot):
    await route_runtime.mark_inbound_sent(_ROUTE)  # RUNNING
    path = _write_record(_env, generation="g1")
    # Within-watchdog capture skip: consumption must STILL run (B4).
    status_polling._last_pane_capture[_ROUTE] = time.monotonic() - 1.0
    await _tick(mock_bot)
    snap = route_runtime.snapshot(_ROUTE)
    assert snap.notification_pending is True
    assert snap.run_state is RunState.WAITING_ON_USER
    assert not path.exists()  # generation-guarded unlink AFTER the commit


async def test_stale_record_unlinked_route_stays_idle(_env, mock_bot):
    await route_runtime.ingest_transcript_event(
        _ROUTE, _evt("assistant", "text", stop_reason="end_turn")
    )
    path = _write_record(_env)
    status_polling._last_pane_capture[_ROUTE] = time.monotonic() - 1.0
    await _tick(mock_bot)
    snap = route_runtime.snapshot(_ROUTE)
    assert snap.notification_pending is False
    assert snap.run_state in (RunState.IDLE_RECENT, RunState.IDLE_CLEARED)
    assert not path.exists()


async def test_redundant_under_transcript_waiting_unlinked(_env, mock_bot):
    await route_runtime.mark_inbound_sent(_ROUTE)
    await route_runtime.ingest_transcript_event(
        _ROUTE,
        _evt("assistant", "tool_use", tool_use_id="auq-1", tool_name="AskUserQuestion"),
    )
    path = _write_record(_env)
    status_polling._last_pane_capture[_ROUTE] = time.monotonic() - 1.0
    await _tick(mock_bot)
    snap = route_runtime.snapshot(_ROUTE)
    assert snap.notification_pending is False
    assert snap.run_state is RunState.WAITING_ON_USER  # transcript WAITING intact
    assert not path.exists()


async def test_unseen_route_ignored_file_survives(_env, mock_bot):
    path = _write_record(_env)
    status_polling._last_pane_capture[_ROUTE] = time.monotonic() - 1.0
    await _tick(mock_bot)
    assert route_runtime.snapshot(_ROUTE).notification_pending is False
    assert path.exists()  # ignored-no-unlink


async def test_already_reflected_generation_not_remarked(_env, mock_bot):
    await route_runtime.mark_inbound_sent(_ROUTE)
    _write_record(_env, generation="g1")
    status_polling._last_pane_capture[_ROUTE] = time.monotonic() - 1.0
    await _tick(mock_bot)
    set_at_1 = route_runtime.snapshot(_ROUTE).notification_set_at
    # Same generation re-written (e.g. unlink raced) → second tick is a no-op.
    _write_record(_env, generation="g1")
    await _tick(mock_bot)
    assert route_runtime.snapshot(_ROUTE).notification_set_at == set_at_1


async def test_on_disk_record_past_ttl_treated_absent(_env, mock_bot):
    await route_runtime.mark_inbound_sent(_ROUTE)
    path = _write_record(_env, ts=time.time() - NOTIFY_TTL_SECONDS - 60)
    status_polling._last_pane_capture[_ROUTE] = time.monotonic() - 1.0
    await _tick(mock_bot)
    snap = route_runtime.snapshot(_ROUTE)
    assert snap.notification_pending is False
    assert snap.run_state is RunState.RUNNING
    assert not path.exists()


# ── runtime-state TTL (v4 fix 2 strand-proofing) ─────────────────────────


async def test_runtime_ttl_expiry_clears_with_file_gone(_env, mock_bot):
    await route_runtime.mark_inbound_sent(_ROUTE)
    await route_runtime.mark_notification_pending(
        _ROUTE, set_at=time.time() - NOTIFY_TTL_SECONDS - 60, generation="g1"
    )
    assert route_runtime.snapshot(_ROUTE).notification_pending is True
    # No side file on disk at all — the TTL check is runtime-state-driven.
    status_polling._last_pane_capture[_ROUTE] = time.monotonic() - 1.0
    await _tick(mock_bot)
    snap = route_runtime.snapshot(_ROUTE)
    assert snap.notification_pending is False
    assert snap.run_state is RunState.RUNNING


async def test_pending_without_set_at_treated_as_expired(_env, mock_bot):
    await route_runtime.mark_inbound_sent(_ROUTE)
    await route_runtime.mark_notification_pending(
        _ROUTE, set_at=time.time(), generation="g1"
    )
    route_runtime._state[_ROUTE].notification_set_at = None  # invariant violation
    status_polling._last_pane_capture[_ROUTE] = time.monotonic() - 1.0
    await _tick(mock_bot)
    assert route_runtime.snapshot(_ROUTE).notification_pending is False


async def test_fresh_notification_survives_tick(_env, mock_bot):
    await route_runtime.mark_inbound_sent(_ROUTE)
    await route_runtime.mark_notification_pending(
        _ROUTE, set_at=time.time(), generation="g1"
    )
    status_polling._last_pane_capture[_ROUTE] = time.monotonic() - 1.0
    await _tick(mock_bot)
    assert route_runtime.snapshot(_ROUTE).notification_pending is True


# ── same-tick digest repaint ─────────────────────────────────────────────


async def test_notification_set_repaints_digest_same_tick(_env, mock_bot):
    await route_runtime.mark_inbound_sent(_ROUTE)
    # Seed the repaint-dedup cache with the PRIOR state so the transition
    # (RUNNING → WAITING via the notification) is detectable this tick.
    status_polling._prev_run_state[_ROUTE] = RunState.RUNNING
    _write_record(_env)
    status_polling._last_pane_capture[_ROUTE] = time.monotonic() - 1.0
    window = MagicMock()
    window.window_id = _WID
    with (
        patch.object(status_polling, "tmux_manager") as mock_tmux,
        patch.object(status_polling, "enqueue_status_update", AsyncMock()),
        patch.object(
            status_polling, "refresh_activity_digest_if_present", AsyncMock()
        ) as mock_refresh,
    ):
        mock_tmux.find_window_by_id = AsyncMock(return_value=window)
        mock_tmux.capture_pane = AsyncMock(return_value=None)
        await status_polling.update_status_message(
            mock_bot, user_id=_USER, window_id=_WID, thread_id=_THREAD
        )
    assert route_runtime.snapshot(_ROUTE).run_state is RunState.WAITING_ON_USER
    mock_refresh.assert_awaited_once()


# ── pane running-after-set_at clear (gate P2-1: level + margin, NOT edge) ─


async def test_pane_running_after_set_at_clears_bit_and_unlinks(_env, mock_bot):
    """A running pane at a capture sufficiently after set_at clears the bit.

    The blocked approval prompt REPLACES the run chrome ("esc to interrupt"
    is rendered only while a run is in flight), so a status-active capture
    strictly after set_at + margin is positive proof the user approved and
    execution resumed — no prior idle observation required.
    """
    await route_runtime.mark_inbound_sent(_ROUTE)
    set_at = time.time() - 30
    await route_runtime.mark_notification_pending(
        _ROUTE, set_at=set_at, generation="g1"
    )
    # The side file (same generation) is still on disk — the clear must
    # unlink it generation-guarded.
    path = _write_record(_env, ts=set_at, generation="g1")
    await _tick(mock_bot, pane_text=_ACTIVE_PANE)
    snap = route_runtime.snapshot(_ROUTE)
    assert snap.notification_pending is False
    assert snap.run_state is RunState.RUNNING
    assert snap.typing_eligible is True
    assert not path.exists()


async def test_stranded_bit_clears_without_idle_frame_ever_captured(_env, mock_bot):
    """The exact gate-P2-1 repro: pane running → notification set with NO
    blocked/idle frame ever captured (Wave A adaptive capture skipped it) →
    pane running again after set_at + margin → the bit MUST clear.

    Under the old idle→active EDGE requirement the first tick seeded
    prev=True, the second tick was True→True, no edge ever fired, and the
    route stranded WAITING_ON_USER / typing-off until newer transcript
    activity or the 30m TTL.
    """
    await route_runtime.mark_inbound_sent(_ROUTE)
    # Tick 1: pane already running BEFORE the notification (the pre-prompt
    # frame) — the only pane observation the poller ever gets pre-approval.
    await _tick(mock_bot, pane_text=_ACTIVE_PANE)
    set_at = time.time() - 30
    await route_runtime.mark_notification_pending(
        _ROUTE, set_at=set_at, generation="g1"
    )
    path = _write_record(_env, ts=set_at, generation="g1")
    assert route_runtime.snapshot(_ROUTE).run_state is RunState.WAITING_ON_USER
    # Tick 2: post-approval — pane running again. The blocked frame between
    # the two ticks was never captured.
    status_polling._last_pane_capture.pop(_ROUTE, None)  # force a capture
    await _tick(mock_bot, pane_text=_ACTIVE_PANE)
    snap = route_runtime.snapshot(_ROUTE)
    assert snap.notification_pending is False
    assert snap.run_state is RunState.RUNNING
    assert snap.typing_eligible is True
    assert not path.exists()


async def test_running_capture_within_margin_does_not_clear(_env, mock_bot):
    """Guard: a running capture at/before set_at + margin must NOT clear —
    it can be the pre-prompt frame captured the same tick the hook fired
    (the run chrome was still on the pane when the prompt began rendering).
    """
    await route_runtime.mark_inbound_sent(_ROUTE)
    set_at = time.time()  # notification just fired
    await route_runtime.mark_notification_pending(
        _ROUTE, set_at=set_at, generation="g1"
    )
    path = _write_record(_env, ts=set_at, generation="g1")
    await _tick(mock_bot, pane_text=_ACTIVE_PANE)
    snap = route_runtime.snapshot(_ROUTE)
    assert snap.notification_pending is True
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.typing_eligible is False
    assert path.exists()  # nothing cleared → nothing unlinked


async def test_slow_capture_observing_within_margin_does_not_clear(
    _env, mock_bot, monkeypatch
):
    """Gate-r2 false-clear race: a capture that STARTS (observes the
    pre-prompt running chrome) inside the margin but RETURNS after it must
    NOT clear. ``capture_wall`` is stamped BEFORE the capture starts — a
    conservative lower bound on the observation time — so a slow capture
    cannot smuggle an inside-the-margin frame past the qualification.
    """
    monkeypatch.setattr(status_polling, "NOTIFY_PANE_CLEAR_MARGIN_S", 0.2)
    await route_runtime.mark_inbound_sent(_ROUTE)
    set_at = time.time()  # notification fires now; capture starts immediately
    await route_runtime.mark_notification_pending(
        _ROUTE, set_at=set_at, generation="g1"
    )
    path = _write_record(_env, ts=set_at, generation="g1")

    async def _slow_capture(_wid):
        # Frame "observed" at call time (inside the margin); return is
        # delayed past the margin — the post-return clock must not be used.
        await asyncio.sleep(0.35)
        return _ACTIVE_PANE

    window = MagicMock()
    window.window_id = _WID
    with (
        patch.object(status_polling, "tmux_manager") as mock_tmux,
        patch.object(status_polling, "enqueue_status_update", AsyncMock()),
        patch.object(
            status_polling.session_manager,
            "resolve_session_for_window",
            AsyncMock(return_value=None),
        ),
    ):
        mock_tmux.find_window_by_id = AsyncMock(return_value=window)
        mock_tmux.capture_pane = AsyncMock(side_effect=_slow_capture)
        await status_polling.update_status_message(
            mock_bot, user_id=_USER, window_id=_WID, thread_id=_THREAD
        )

    snap = route_runtime.snapshot(_ROUTE)
    assert snap.notification_pending is True
    assert snap.run_state is RunState.WAITING_ON_USER
    assert path.exists()


async def test_parser_hostile_active_pane_still_clears_notification(_env, mock_bot):
    """Hermes P2 (2026-06-11 stuck-route follow-up): the Wave B pane clear
    must gate on the ACTIVE MARKER alone, not ``is_running`` (marker +
    parseable status). A parser-hostile active frame — visible "esc to
    interrupt" run chrome but no parseable spinner line — proves execution
    resumed exactly as well as a parseable one; requiring the parsed status
    stranded 🔔 until transcript/TTL while the idle-clear path already
    treated the same frame as running.
    """
    hostile_active_pane = (
        "⏺ Bash(long foreground run)\n"
        "  ⎿  Running…\n"
        "\n" + "─" * 40 + "\n"
        "❯ \n" + "─" * 40 + "\n"
        "  ⏵⏵ bypass permissions on · esc to interrupt\n"
    )
    await route_runtime.mark_inbound_sent(_ROUTE)
    set_at = time.time() - 30
    await route_runtime.mark_notification_pending(
        _ROUTE, set_at=set_at, generation="g1"
    )
    path = _write_record(_env, ts=set_at, generation="g1")
    await _tick(mock_bot, pane_text=hostile_active_pane)
    snap = route_runtime.snapshot(_ROUTE)
    assert snap.notification_pending is False
    assert snap.run_state is RunState.RUNNING
    assert snap.typing_eligible is True
    assert not path.exists()


async def test_idle_pane_after_set_at_does_not_clear(_env, mock_bot):
    """A NON-running capture after set_at preserves the bit — the prompt is
    (presumably) still on the pane; only positive running proof clears."""
    idle_pane = (
        "✻ Cooked for 2s\n"
        "──────────────────────────────────────\n"
        "❯ \n"
        "──────────────────────────────────────\n"
        "  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
    )
    await route_runtime.mark_inbound_sent(_ROUTE)
    await route_runtime.mark_notification_pending(
        _ROUTE, set_at=time.time() - 30, generation="g1"
    )
    await _tick(mock_bot, pane_text=idle_pane)
    assert route_runtime.snapshot(_ROUTE).notification_pending is True


# ── Fix 3b/3c/3d: durable decision card (notify_waiting / dismiss_if_kind) ─
#
# ISSUE-5's missing surface: the 🔔 only ever drove a SILENT digest header.
# It must surface as a persistent, audible "Claude needs a decision" card that
# SURVIVES the workflow's streaming narration (Fix 1) and dismisses on resume.
# These pin the poller card seam: post on COMMITTED_LIVE (gated by the
# double-card guard, Fix 3d), dismiss on the reason-driven clears (Fix 3b/3c).


async def test_committed_live_posts_notification_decision_card(_env, mock_bot):
    await route_runtime.mark_inbound_sent(_ROUTE)  # RUNNING
    _write_record(_env, generation="g1")
    status_polling._last_pane_capture[_ROUTE] = time.monotonic() - 1.0
    with patch(
        "cctelegram.handlers.attention.notify_waiting",
        AsyncMock(return_value=TopicSendOutcome.OK),
    ) as mock_notify:
        await _tick(mock_bot)
    snap = route_runtime.snapshot(_ROUTE)
    assert snap.notification_pending is True
    mock_notify.assert_awaited()
    assert mock_notify.await_args.kwargs.get("kind") == "notification_decision"


async def test_decision_card_retried_while_pending(_env, mock_bot):
    """Fix 3b retry-while-pending (codex-R1-P2c): while notification_pending
    stays True across ticks — even with the side file already consumed/gone —
    the poller RE-ATTEMPTS the decision card. notify_waiting is idempotent (a
    no-op if the card is already up, a genuine retry if a prior post failed on
    a transient), so a lost first post never strands the route on the silent
    digest header alone (the ISSUE-5 symptom)."""
    await route_runtime.mark_inbound_sent(_ROUTE)
    await route_runtime.mark_notification_pending(
        _ROUTE, set_at=time.time(), generation="g1"
    )  # committed; NO side file on disk (already consumed a prior tick)
    assert route_runtime.snapshot(_ROUTE).notification_pending is True
    status_polling._last_pane_capture[_ROUTE] = time.monotonic() - 1.0
    with patch(
        "cctelegram.handlers.attention.notify_waiting",
        AsyncMock(return_value=TopicSendOutcome.OK),
    ) as mock_notify:
        await _tick(mock_bot)
    assert route_runtime.snapshot(_ROUTE).notification_pending is True
    mock_notify.assert_awaited()
    assert mock_notify.await_args.kwargs.get("kind") == "notification_decision"


async def test_decision_card_retry_respects_interactive_surface_guard(_env, mock_bot):
    """The retry-while-pending path stays gated by has_interactive_surface
    (Fix 3d) — no decision card while a real AUQ/EPM surface owns the topic."""
    await route_runtime.mark_inbound_sent(_ROUTE)
    await route_runtime.mark_notification_pending(
        _ROUTE, set_at=time.time(), generation="g1"
    )
    status_polling._last_pane_capture[_ROUTE] = time.monotonic() - 1.0
    with (
        patch(
            "cctelegram.handlers.attention.notify_waiting",
            AsyncMock(return_value=TopicSendOutcome.OK),
        ) as mock_notify,
        patch.object(status_polling, "has_interactive_surface", return_value=True),
    ):
        await _tick(mock_bot)
    mock_notify.assert_not_awaited()


async def test_decision_card_suppressed_when_interactive_surface_live(_env, mock_bot):
    """Fix 3d: no audible decision card while a real AUQ/EPM interactive surface
    already owns the topic (gate on has_interactive_surface, NOT the pane bit).
    The bit still commits; only the redundant card is suppressed."""
    await route_runtime.mark_inbound_sent(_ROUTE)
    _write_record(_env, generation="g1")
    status_polling._last_pane_capture[_ROUTE] = time.monotonic() - 1.0
    with (
        patch(
            "cctelegram.handlers.attention.notify_waiting",
            AsyncMock(return_value=TopicSendOutcome.OK),
        ) as mock_notify,
        patch.object(status_polling, "has_interactive_surface", return_value=True),
    ):
        await _tick(mock_bot)
    assert route_runtime.snapshot(_ROUTE).notification_pending is True
    mock_notify.assert_not_awaited()


async def test_pane_running_clear_dismisses_decision_card(_env, mock_bot):
    """Fix 3b/3c: the PANE_RUNNING clear (user resumed in the terminal)
    dismisses the notification_decision card via the kind-aware dismissal."""
    await route_runtime.mark_inbound_sent(_ROUTE)
    set_at = time.time() - 30
    await route_runtime.mark_notification_pending(
        _ROUTE, set_at=set_at, generation="g1"
    )
    path = _write_record(_env, ts=set_at, generation="g1")
    with patch(
        "cctelegram.handlers.attention.dismiss_if_kind", AsyncMock(), create=True
    ) as mock_dismiss:
        await _tick(mock_bot, pane_text=_ACTIVE_PANE)
    snap = route_runtime.snapshot(_ROUTE)
    assert snap.notification_pending is False
    mock_dismiss.assert_awaited()
    assert mock_dismiss.await_args.kwargs.get("kind") == "notification_decision"
    assert not path.exists()


async def _commit_then_eot_clear() -> None:
    """Commit a notification (card posts) then clear it via a strictly-newer
    end-of-turn → reason END_OF_TURN, route idle, NO background-agent key."""
    await route_runtime.mark_inbound_sent(_ROUTE)
    await route_runtime.mark_notification_pending(
        _ROUTE, set_at=time.time(), generation="g1"
    )
    await route_runtime.ingest_transcript_event(
        _ROUTE,
        _evt("assistant", "text", stop_reason="end_turn", timestamp=time.time() + 10),
    )
    snap = route_runtime.snapshot(_ROUTE)
    assert snap.notification_pending is False
    assert (
        snap.notification_clear_reason
        is route_runtime.NotificationClearReason.END_OF_TURN
    )
    assert snap.background_agents == ()


async def test_eot_gap_grace_holds_card_then_dismisses(_env, mock_bot, monkeypatch):
    """Codex P2 (the EOT-gap race): an END_OF_TURN clear with NO visible
    background key must NOT dismiss the decision card immediately — the monitor
    applies the parent end-of-turn (clearing 🔔) BEFORE the same-batch Workflow
    launch fan-out sets the bg key, so a poller reconcile can land in between.
    The card is HELD for a short grace; only after the grace elapses with still
    no bg key (a genuine no-workflow end-of-turn) is it dismissed."""
    monkeypatch.setattr(status_polling, "DECISION_CARD_EOT_GRACE_S", 5.0)
    await _commit_then_eot_clear()
    status_polling._last_pane_capture[_ROUTE] = time.monotonic() - 1.0
    # Within grace: must NOT dismiss.
    with patch(
        "cctelegram.handlers.attention.dismiss_if_kind", AsyncMock(), create=True
    ) as mock_dismiss:
        await _tick(mock_bot)
    mock_dismiss.assert_not_awaited()
    # Force the grace expired → the genuine end-of-turn dismisses.
    status_polling._decision_card_eot_grace[_ROUTE] = time.monotonic() - 1.0
    status_polling._last_pane_capture[_ROUTE] = time.monotonic() - 1.0
    with patch(
        "cctelegram.handlers.attention.dismiss_if_kind", AsyncMock(), create=True
    ) as mock_dismiss:
        await _tick(mock_bot)
    mock_dismiss.assert_awaited()


async def test_eot_gap_lagging_bg_key_within_grace_keeps_card(
    _env, mock_bot, monkeypatch
):
    """Codex P2 fix: when the lagging Workflow launch fan-out lands DURING the
    grace (the bg key becomes visible), the card is KEPT — the route is
    projected-Busy, exactly the EOT-gap the card is meant to survive."""
    monkeypatch.setattr(status_polling, "DECISION_CARD_EOT_GRACE_S", 5.0)
    await _commit_then_eot_clear()
    status_polling._last_pane_capture[_ROUTE] = time.monotonic() - 1.0
    with patch(
        "cctelegram.handlers.attention.dismiss_if_kind", AsyncMock(), create=True
    ) as mock_dismiss:
        await _tick(mock_bot)  # grace starts; no dismiss
    mock_dismiss.assert_not_awaited()
    # The lagging launch lands → bg key live → projected Busy.
    await route_runtime.mark_background_agent_launched(_ROUTE, "wf-task:wlag01")
    status_polling._last_pane_capture[_ROUTE] = time.monotonic() - 1.0
    with patch(
        "cctelegram.handlers.attention.dismiss_if_kind", AsyncMock(), create=True
    ) as mock_dismiss:
        await _tick(mock_bot)
    mock_dismiss.assert_not_awaited()  # EOT-gap keep (bg key now visible)


async def test_ttl_clear_dismisses_decision_card(_env, mock_bot):
    """Fix 3b/3c: the runtime-TTL clear also dismisses the decision card so a
    silently-degraded 🔔 never leaves a stuck audible card."""
    await route_runtime.mark_inbound_sent(_ROUTE)
    await route_runtime.mark_notification_pending(
        _ROUTE, set_at=time.time() - NOTIFY_TTL_SECONDS - 60, generation="g1"
    )
    status_polling._last_pane_capture[_ROUTE] = time.monotonic() - 1.0
    with patch(
        "cctelegram.handlers.attention.dismiss_if_kind", AsyncMock(), create=True
    ) as mock_dismiss:
        await _tick(mock_bot)
    assert route_runtime.snapshot(_ROUTE).notification_pending is False
    mock_dismiss.assert_awaited()
