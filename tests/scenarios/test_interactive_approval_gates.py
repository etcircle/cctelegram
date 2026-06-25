"""Scenario: interactive approval gates (Permission / Workflow) — PR-1 display.

Public-Telegram-seam scenario tests for the PR-1 (display-only) gate cards:

  - a live Permission / Workflow gate pane → the bot posts a card with the
    manual ↑/↓/⏎/Esc nav keyboard AND the honest "un-verified live keystrokes"
    notice (P2-1), with NO semantic option-pick buttons;
  - the route promotes RUNNING → WAITING_ON_USER (the existing pane-confirmed
    poller promotion is UI-name-agnostic — it keys on ``ui_content``);
  - the redundant generic "🔔 needs a decision" card is DISMISSED once the
    actionable gate surface publishes (the §3 dismiss-on-surface wiring);
  - flag OFF → NO card, NO ``WAITING_ON_USER`` promotion (the detector-level
    kill-switch, S-9), proving a flag-OFF deploy adds zero new behavior.

Modeled on ``tests/scenarios/test_auq_waiting_indicator.py`` (the closest
existing harness for a live-pane interactive surface + the notification card).
The gate fixtures are the committed Wave-0 v2.1.190 captures.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from cctelegram import route_runtime, terminal_parser
from cctelegram.route_runtime import RunState
from cctelegram.handlers import attention, interactive_ui, status_polling
from cctelegram.tmux_manager import tmux_manager as real_tmux
from cctelegram.utils import app_dir
from tests.conftest import ScenarioHarness

pytestmark = pytest.mark.scenario

_SESSION_ID = "44444444-4444-4444-8444-444444444444"
_THREAD_ID = 77
_FIXTURES = Path(__file__).parent.parent / "cctelegram" / "fixtures"


def _load(name: str) -> str:
    return (_FIXTURES / name).read_text()


@pytest.fixture
def gate_on():
    """Enable gate detection for the test body (root reset clears it after)."""
    terminal_parser.set_permission_prompts_enabled(True)
    yield
    terminal_parser.set_permission_prompts_enabled(False)


def _bind(scenario: ScenarioHarness, pane: str, *, name: str = "repo") -> str:
    wid = scenario.add_window(window_name=name, cwd="/repo", pane_text=pane)
    scenario.bind_thread(
        _THREAD_ID, wid, display_name=name, cwd="/repo", session_id=_SESSION_ID
    )
    return wid


async def _render(scenario: ScenarioHarness, wid: str) -> bool:
    return bool(
        await interactive_ui.handle_interactive_ui(
            scenario.bot,
            scenario.user_id,
            wid,
            _THREAD_ID,
            tmux_mgr=scenario.tmux,
            session_mgr=scenario.session_manager,
        )
    )


async def _poll(scenario: ScenarioHarness, wid: str, n: int = 1) -> None:
    for _ in range(n):
        await status_polling.update_status_message(
            scenario.bot,
            user_id=scenario.user_id,
            window_id=wid,
            thread_id=_THREAD_ID,
        )


def _route(scenario: ScenarioHarness, wid: str) -> route_runtime.Route:
    return (scenario.user_id, _THREAD_ID, wid)


def _write_notify_side_file(wid: str, *, generation: str = "gen-1") -> Path:
    """Write a window-keyed Notification-hook side file (no message text)."""
    d = app_dir() / "notify_pending"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = d / f"{_SESSION_ID}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": _SESSION_ID,
                "ts": time.time(),
                "window_key": f"{real_tmux.session_name}:{wid}",
                "generation": generation,
                "kind": "permission",
            }
        )
    )
    return path


def _last_interactive_card_text(scenario: ScenarioHarness) -> str | None:
    """The text of the most-recent interactive (tool) card the bot sent."""
    for s in reversed(scenario.bot.sent):
        if s.method in ("send_message", "edit_message_text"):
            text = s.kwargs.get("text") or ""
            if interactive_ui._GATE_NAV_NOTICE in text or "❯" in text:
                return text
    return None


# ── Permission gate: card + notice + WAITING promotion ────────────────────


@pytest.mark.asyncio
async def test_permission_gate_card_posts_with_honest_notice(
    scenario: ScenarioHarness, gate_on
) -> None:
    """A live permission gate posts a display-only card carrying the honest
    un-verified-keystroke notice (P2-1), and promotes the route to WAITING."""
    wid = _bind(scenario, _load("permission_bash_v2.1.190.txt"))
    route = _route(scenario, wid)
    await route_runtime.mark_inbound_sent(route)  # RUNNING
    assert route_runtime.snapshot(route).run_state is RunState.RUNNING

    assert await _render(scenario, wid)
    await _poll(scenario, wid, 2)

    # The card published with the honest notice (P2-1) and the question text.
    card = _last_interactive_card_text(scenario)
    assert card is not None
    assert interactive_ui._GATE_NAV_NOTICE in card
    assert "Do you want to proceed?" in card

    # WAITING promotion (UI-name-agnostic poller path), typing off.
    snap = route_runtime.snapshot(route)
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.interactive_pending is True
    assert snap.typing_eligible is False


@pytest.mark.asyncio
async def test_permission_gate_card_has_no_pick_buttons(
    scenario: ScenarioHarness, gate_on
) -> None:
    """PR-1 is DISPLAY-ONLY: the gate card carries the manual nav keyboard but
    NO semantic option-pick (``aqp:``/``aqt:``) buttons (S-1)."""
    wid = _bind(scenario, _load("permission_webfetch_v2.1.190.txt"))
    await route_runtime.mark_inbound_sent(_route(scenario, wid))
    assert await _render(scenario, wid)

    pick_datas: list[str] = []
    for s in scenario.bot.sent:
        markup = s.kwargs.get("reply_markup")
        if markup is None:
            continue
        for row in markup.inline_keyboard:
            for btn in row:
                if btn.callback_data:
                    pick_datas.append(btn.callback_data)
    assert pick_datas, "expected the manual nav keyboard"
    assert not any(d.startswith(("aqp:", "aqt:")) for d in pick_datas), pick_datas


# ── Workflow gate: phases + token warning in the body ─────────────────────


@pytest.mark.asyncio
async def test_workflow_gate_card_surfaces_phases_and_warning(
    scenario: ScenarioHarness, gate_on
) -> None:
    """The Workflow gate card body shows the phases + the token-cost warning so
    the user taps informed, plus the honest notice (P2-1)."""
    wid = _bind(scenario, _load("workflow_dynamic_launch_v2.1.190.txt"))
    route = _route(scenario, wid)
    await route_runtime.mark_inbound_sent(route)
    assert await _render(scenario, wid)
    await _poll(scenario, wid, 2)

    card = _last_interactive_card_text(scenario)
    assert card is not None
    assert interactive_ui._GATE_NAV_NOTICE in card
    assert "Run a dynamic workflow?" in card
    assert "Dynamic workflows can use a lot of tokens" in card  # warning
    assert "phases" in card  # the phase list
    assert route_runtime.snapshot(route).run_state is RunState.WAITING_ON_USER


# ── Decision-card dismiss-on-surface (§3) ─────────────────────────────────


@pytest.mark.asyncio
async def test_decision_card_dismissed_once_gate_surface_publishes(
    scenario: ScenarioHarness, gate_on
) -> None:
    """The generic "🔔 needs a decision" card posted from the Notification hook
    is DISMISSED once the actionable gate card owns the surface (§3)."""
    wid = _bind(scenario, _load("permission_bash_v2.1.190.txt"))
    route = _route(scenario, wid)
    await route_runtime.mark_inbound_sent(route)

    # Tick 1: notification fires BEFORE any gate surface exists → the generic
    # decision card posts (has_interactive_surface is False).
    _write_notify_side_file(wid)
    # Consume the notification at the top of the poll path WITHOUT a gate
    # surface yet (no prior render) — the generic decision card is posted.
    await status_polling._consume_notification_signal(
        scenario.bot, scenario.user_id, _THREAD_ID, wid
    )
    assert route_runtime.snapshot(route).notification_pending is True
    assert not interactive_ui.has_interactive_surface(scenario.user_id, _THREAD_ID)
    # A decision card is live.
    key = (scenario.user_id, _THREAD_ID)
    state = attention._attention_state.get(key)
    assert state is not None and state.kind == status_polling.NOTIFY_DECISION_KIND
    assert state.state == "waiting"

    # Tick 2: the poller renders the gate card (surface publishes) AND the
    # decision card is dismissed on the same/next reconcile.
    await _poll(scenario, wid, 2)
    assert interactive_ui.has_interactive_surface(scenario.user_id, _THREAD_ID)
    state = attention._attention_state.get(key)
    assert state is not None
    # Dismissed → idle (the gate card supersedes the generic nudge).
    assert state.state == "idle"
    # The actionable gate card carries the honest notice.
    card = _last_interactive_card_text(scenario)
    assert card is not None and interactive_ui._GATE_NAV_NOTICE in card


# ── Flag OFF: no card, no promotion (S-9) ─────────────────────────────────


@pytest.mark.asyncio
async def test_flag_off_no_card_no_promotion(scenario: ScenarioHarness) -> None:
    """With the gate flag OFF (default), a permission gate pane is NOT detected:
    no card, and the route is NOT promoted to WAITING_ON_USER — a flag-OFF
    deploy adds zero new behavior (S-9, detector-level kill-switch)."""
    assert terminal_parser.permission_prompts_enabled() is False
    wid = _bind(scenario, _load("permission_bash_v2.1.190.txt"))
    route = _route(scenario, wid)
    await route_runtime.mark_inbound_sent(route)  # RUNNING

    assert not await _render(scenario, wid)  # liveness 'absent' / no detect
    await _poll(scenario, wid, 3)

    snap = route_runtime.snapshot(route)
    assert snap.run_state is RunState.RUNNING  # NOT promoted
    assert snap.interactive_pending is False
    assert not interactive_ui.has_interactive_surface(scenario.user_id, _THREAD_ID)


# ── S-8 fail-closed: quoted-prompt shapes post NO card (P1b, flag ON) ──────
#
# The dangerous false-positive shapes the strict-parse + bottom-terminal gate
# must reject — proven at the public seam (no card AND no WAITING promotion)
# even with the flag ON. RED-first against HEAD (where the loose UIPattern
# match alone lit a card).

# (a) "Claude wants to ..." + footer, NO numbered options.
_NEG_CLAUDE_WANTS_NO_OPTIONS = (
    "When Claude wants to fetch a URL it shows a prompt.\n"
    "\n"
    " Claude wants to fetch content from example.com\n"
    " Some prose explaining what that means, with no option block at all.\n"
    " Esc to cancel . Tab to amend\n"
)
# (b) A permission question + numbered prose + footer, NOT at the pane bottom
# (assistant prose follows the footer).
_NEG_PERMISSION_NOT_AT_BOTTOM = (
    "For reference, the prompt looks like this:\n"
    "\n"
    " Do you want to proceed?\n"
    " 1. Yes\n"
    "   2. Yes, and always allow\n"
    "   3. No\n"
    " Esc to cancel . Tab to amend\n"
    "\n"
    "So you would normally pick option 1. But I have already finished, so\n"
    "there is nothing for you to approve right now.\n"
)
# (c) "Run a dynamic workflow?" / "Dynamic workflows can use ..." quoted, footer,
# NO live option block.
_NEG_WORKFLOW_NO_OPTIONS = (
    "A dynamic workflow gate normally shows:\n"
    "\n"
    " Run a dynamic workflow?\n"
    " Dynamic workflows can use a lot of tokens quickly by running subagents.\n"
    " Esc to cancel . Tab to amend\n"
)
# (d) A COMPLETE quoted Workflow block FOLLOWED BY trailing assistant prose.
_NEG_WORKFLOW_COMPLETE_THEN_PROSE = (
    "Here is what the workflow gate looks like when it appears:\n"
    "\n"
    " Run a dynamic workflow?\n"
    " This dynamic workflow will spin up subagents.\n"
    " Dynamic workflows can use a lot of tokens quickly.\n"
    " 1. Yes, run it\n"
    "   2. View raw script\n"
    "   3. No\n"
    " Esc to cancel . Tab to amend\n"
    "\n"
    "As you can see, you would tap option 1 to proceed. Tell me what to do.\n"
)


# (e) Round-2 Codex P1: a COMPLETE quoted Permission gate (real options +
# ``(esc)``) in scrollback, FOLLOWED BY the live pane's normal input box +
# status bar. A live gate REPLACES that chrome (the bgshells fixture proves it),
# so a gate WITH ready-for-input chrome below the footer is a quoted false
# positive — no card.
_NEG_PERMISSION_QUOTED_THEN_INPUTBOX = (
    " Do you want to allow Claude to fetch this content?\n"
    " ❯ 1. Yes\n"
    "   2. Yes, and don't ask again for example.com\n"
    "   3. No, and tell Claude what to do differently (esc)\n"
    "\n"
    "────────────────────────────────────────────────────────────────────────\n"
    "❯ \n"
    "────────────────────────────────────────────────────────────────────────\n"
    "  ? for shortcuts · ← for agents\n"
)
# (f) A COMPLETE quoted Workflow block then input box + status bar.
_NEG_WORKFLOW_QUOTED_THEN_INPUTBOX = (
    " Run a dynamic workflow?\n"
    " This dynamic workflow will spin up subagents.\n"
    " Dynamic workflows can use a lot of tokens quickly.\n"
    " ❯ 1. Yes, run it\n"
    "   2. View raw script\n"
    "   3. No\n"
    " Esc to cancel · Tab to amend\n"
    " ctrl+g to edit script in $EDITOR\n"
    "\n"
    "────────────────────────────────────────────────────────────────────────\n"
    "❯ \n"
    "  Opus 4.8 (1M context) · Context left: 42% · ↓ to manage\n"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pane",
    [
        _NEG_CLAUDE_WANTS_NO_OPTIONS,
        _NEG_PERMISSION_NOT_AT_BOTTOM,
        _NEG_WORKFLOW_NO_OPTIONS,
        _NEG_WORKFLOW_COMPLETE_THEN_PROSE,
        _NEG_PERMISSION_QUOTED_THEN_INPUTBOX,
        _NEG_WORKFLOW_QUOTED_THEN_INPUTBOX,
    ],
)
async def test_quoted_gate_shapes_post_no_card(
    scenario: ScenarioHarness, gate_on, pane: str
) -> None:
    """With the flag ON, a quoted / explained / non-bottom gate — INCLUDING a
    complete-but-quoted gate followed by the input box + status bar (round-2
    Codex P1) — posts NO card and does NOT promote the route to WAITING_ON_USER
    (S-8 fail-closed, P1b)."""
    wid = _bind(scenario, pane)
    route = _route(scenario, wid)
    await route_runtime.mark_inbound_sent(route)  # RUNNING

    assert not await _render(scenario, wid)  # no detection → no card
    await _poll(scenario, wid, 3)

    assert _last_interactive_card_text(scenario) is None
    assert not interactive_ui.has_interactive_surface(scenario.user_id, _THREAD_ID)
    snap = route_runtime.snapshot(route)
    assert snap.run_state is RunState.RUNNING  # NOT promoted
    assert snap.interactive_pending is False


@pytest.mark.asyncio
async def test_live_gate_with_bg_shells_posts_card(
    scenario: ScenarioHarness, gate_on
) -> None:
    """Round-2 Hermes P2 refuted by data: a LIVE WebFetch gate captured WITH 2
    background shells running (``permission_webfetch_bgshells_v2.1.190.txt``)
    still posts the card + promotes to WAITING — the footer is the bottom, so
    the tightened bottom-terminal check does NOT false-negative it."""
    wid = _bind(scenario, _load("permission_webfetch_bgshells_v2.1.190.txt"))
    route = _route(scenario, wid)
    await route_runtime.mark_inbound_sent(route)  # RUNNING

    assert await _render(scenario, wid)
    await _poll(scenario, wid, 2)

    card = _last_interactive_card_text(scenario)
    assert card is not None
    assert interactive_ui._GATE_NAV_NOTICE in card
    assert "Do you want to allow Claude to fetch this content?" in card
    assert route_runtime.snapshot(route).run_state is RunState.WAITING_ON_USER
