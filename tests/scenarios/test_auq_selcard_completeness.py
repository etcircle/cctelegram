"""Scenario coverage for the selection-card completeness fix (DISPLAY-ONLY).

The long-open busy-topic AUQ bug (DiCopilot @4): on a scrolled/aged
AskUserQuestion the resolver returns a PARTIAL-pane bail
(``decision=="bail"``, ``dispatch_trusted is False``,
``reason.startswith("bail_partial")``). The Telegram SELECTION card (the picker
card with the inline keyboard) renders its body from the resolver's PANE form,
which lost its top options to scroll → the card shows "Only options 2-3 are
visible" and omits option 1.

The already-shipped helper ``auq_source.recover_consistent_side_file_for_ctx``
recovers the COMPLETE option list from the PreToolUse side file on exactly this
bail (it already drives the full-details "📋" ctx card). This fix extends that
SAME recovery to the selection card's BODY render — DISPLAY ONLY. Dispatch is
untouched (and already suppressed on a bail: ``dispatch_trusted`` False → no
``aqp:`` pick buttons minted).

These tests drive the public seam (``handle_interactive_ui``) with a fake bot /
fake tmux and assert on the rendered picker card — no monkeypatch of handler
internals in test bodies.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from cctelegram.handlers import auq_source, interactive_ui
from cctelegram.handlers.callback_data import CB_ASK_PICK
from cctelegram.utils import app_dir
from tests.conftest import ScenarioHarness

pytestmark = pytest.mark.scenario

_SESSION_ID = "44444444-4444-4444-8444-444444444444"


# ── builders ─────────────────────────────────────────────────────────────────


def _single_q_input(labels: list[str], *, title: str) -> dict[str, Any]:
    """A single-question single-select tool_input with the given option labels."""
    return {
        "questions": [
            {
                "question": title,
                "header": "Scope",
                "multiSelect": False,
                "options": [{"label": label, "description": ""} for label in labels],
            }
        ]
    }


def _multi_select_input(labels: list[str], *, title: str) -> dict[str, Any]:
    """A single-question MULTI-select tool_input (identical to ``_single_q_input``
    but ``multiSelect: True``) — used to pin that a multi-select side file does
    NOT get swapped into the body."""
    return {
        "questions": [
            {
                "question": title,
                "header": "Scope",
                "multiSelect": True,
                "options": [{"label": label, "description": ""} for label in labels],
            }
        ]
    }


def _multi_q_input() -> dict[str, Any]:
    return {
        "questions": [
            {
                "question": "Which migration strategy should we use?",
                "header": "Strategy",
                "multiSelect": False,
                "options": [
                    {"label": "P) Lift and shift", "description": "desc P"},
                    {"label": "Q) Rewrite incrementally", "description": "desc Q"},
                    {"label": "R) Hybrid approach", "description": "desc R"},
                ],
            },
            {
                "question": "Which rollout cadence do you prefer?",
                "header": "Cadence",
                "multiSelect": False,
                "options": [
                    {"label": "S) Big bang", "description": "desc S"},
                    {"label": "T) Canary", "description": "desc T"},
                ],
            },
        ]
    }


def _partial_pane(
    rows: list[tuple[int, str]],
    *,
    cursor_number: int | None = None,
    affordances: bool = True,
    extra_scrollback: str = "",
) -> str:
    """A partial single-tab picker pane (no ``←…→`` tab header → titleless).

    Drops option 1: the first row starts at slot 2, so the form is NOT
    contiguous-from-1 and NOT a complete picker → the resolver bails partial.
    """
    lines: list[str] = []
    if extra_scrollback:
        lines.append(extra_scrollback)
    for number, label in rows:
        prefix = "❯" if number == cursor_number else " "
        lines.append(f"{prefix} {number}. {label}")
        lines.append(f"     description for option {number}")
    if affordances:
        next_num = rows[-1][0] + 1
        lines.append(f"  {next_num}. Type something.")
        lines.append("─" * 40)
        lines.append(f"  {next_num + 1}. Chat about this")
    lines.append("")
    lines.append("Enter to select · ↑/↓ to navigate · Esc to cancel")
    return "\n".join(lines) + "\n"


def _bind(scenario: ScenarioHarness, pane: str) -> str:
    wid = scenario.add_window(window_name="repo", cwd="/repo", pane_text=pane)
    scenario.bind_thread(
        42,
        wid,
        display_name="repo",
        cwd="/repo",
        session_id=_SESSION_ID,
    )
    return wid


def _write_side_file_aged(
    tool_input: dict[str, Any], *, tool_use_id: str = "tool-use-aged-selcard"
) -> Path:
    """Write a side file aged past the 300s ``_PRETOOL_TTL_SECONDS`` read-TTL."""
    pending = app_dir() / "auq_pending"
    pending.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = pending / f"{_SESSION_ID}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": _SESSION_ID,
                "tool_use_id": tool_use_id,
                "written_at": time.time() - 1000,
                "tool_input": tool_input,
            }
        )
    )
    return path


async def _render(scenario: ScenarioHarness, wid: str) -> None:
    assert await interactive_ui.handle_interactive_ui(
        scenario.bot,
        scenario.user_id,
        wid,
        42,
        tmux_mgr=scenario.tmux,
        session_mgr=scenario.session_manager,
    )


def _unescape(text: str) -> str:
    """Strip MarkdownV2 backslash escapes so substring needles (which carry
    ``)`` / ``.``) match the rendered card text the fake bot stores."""
    return text.replace("\\", "")


def _picker_text(scenario: ScenarioHarness) -> str:
    """The picker card body — the last sent message carrying a reply_markup."""
    for sent in reversed(scenario.bot.sent):
        if sent.kwargs.get("reply_markup") is not None:
            return _unescape(str(sent.kwargs.get("text") or ""))
    raise AssertionError("no picker card recorded")


def _pick_callbacks(scenario: ScenarioHarness) -> list[str]:
    for sent in reversed(scenario.bot.sent):
        markup = sent.kwargs.get("reply_markup")
        if markup is not None:
            return [b.callback_data for row in markup.inline_keyboard for b in row]
    raise AssertionError("no reply markup recorded")


def _aqp_tokens(scenario: ScenarioHarness) -> list[str]:
    return [
        cb.removeprefix(CB_ASK_PICK).split(":")[-1]
        for cb in _pick_callbacks(scenario)
        if cb.startswith(CB_ASK_PICK)
    ]


_DICO_LABELS = [
    "A) Review the 66 proposals",
    "B) Draft the synthesis doc",
    "C) Defer to next session",
]
_DICO_TITLE = "What should we do next with the proposals?"


# ── 1. THE owner case (RED on bare main) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_partial_bail_single_select_selection_card_lists_all_options(
    scenario: ScenarioHarness,
) -> None:
    """The owner case: a partial pane (option 1 scrolled off) + an aged
    consistent SINGLE-question SINGLE-select side file holding all 3 options.

    On bare main the selection card renders the pane body ("Only options 2-3 are
    visible"; option 1 absent). After the fix the card lists ALL options (incl.
    option 1) with the new manual-nav notice — DISPLAY-ONLY (no pick tokens).
    """
    pane = _partial_pane([(2, _DICO_LABELS[1]), (3, _DICO_LABELS[2])], cursor_number=2)
    wid = _bind(scenario, pane)
    _write_side_file_aged(_single_q_input(_DICO_LABELS, title=_DICO_TITLE))

    # Premise guard: this is the partial-pane bail shape.
    r = auq_source.resolve_auq_source_for_render(wid, pane)
    assert r.decision == "bail"
    assert r.dispatch_trusted is False
    assert r.reason.startswith("bail_partial")

    await _render(scenario, wid)

    picker = _picker_text(scenario)
    # The selection card now lists ALL options, INCLUDING the scrolled-off
    # option 1 (the line RED on bare main).
    assert "A) Review the 66 proposals" in picker
    assert "B) Draft the synthesis doc" in picker
    assert "C) Defer to next session" in picker
    # The new notice wording is present; the old wording is gone.
    assert "Tap-to-select is off on a scrolled screen" in picker
    assert "Only options" not in picker
    # DISPLAY-ONLY: a partial bail mints NO aqp: pick buttons.
    assert _aqp_tokens(scenario) == []


# ── 1b. cursor overlay — the swapped body keeps the live pane ❯ (Hermes P2) ───


@pytest.mark.asyncio
async def test_partial_bail_swap_preserves_pane_cursor(
    scenario: ScenarioHarness,
) -> None:
    """Hermes P2 fold: the swapped full-options body keeps the live pane cursor
    (``❯``) on the highlighted option (matched by NUMBER), so the manual
    ↑/↓/Tab nav the notice points at is not blind. The scrolled-off option 1 is
    listed WITHOUT a cursor.
    """
    pane = _partial_pane([(2, _DICO_LABELS[1]), (3, _DICO_LABELS[2])], cursor_number=2)
    wid = _bind(scenario, pane)
    _write_side_file_aged(_single_q_input(_DICO_LABELS, title=_DICO_TITLE))

    await _render(scenario, wid)

    picker = _picker_text(scenario)
    # The pane's cursor (option 2) is overlaid onto the swapped body.
    assert "❯ 2. B) Draft the synthesis doc" in picker
    # All options listed; option 1 (scrolled off, no pane cursor) has NO cursor.
    assert "1. A) Review the 66 proposals" in picker
    assert "❯ 1. A) Review the 66 proposals" not in picker
    assert "❯ 3. C) Defer to next session" not in picker


# ── 2. multi-QUESTION partial bail — the single-question gate blocks the swap ──


@pytest.mark.asyncio
async def test_multi_question_partial_bail_single_question_gate_blocks_swap(
    scenario: ScenarioHarness,
) -> None:
    """P1 negative (passes on main AND after fix). A 2-question side file + a
    partial pane showing Q1's slots 2,3 (option 1 "P) Lift and shift" scrolled
    off → a partial bail).

    Without the single-QUESTION gate the ungated swap would render
    ``build_form_from_tool_input(multi_q_payload)``, which defaults to
    ``questions[0]`` and would inject "P) Lift and shift". The gate blocks the
    swap (the side file has >1 question), so option 1's label is NOT injected.
    This test pins the gate.
    """
    _write_side_file_aged(_multi_q_input())
    pane = _partial_pane(
        [(2, "Q) Rewrite incrementally"), (3, "R) Hybrid approach")],
        cursor_number=2,
    )
    wid = _bind(scenario, pane)

    # Premise guard: this is a partial-pane bail.
    r = auq_source.resolve_auq_source_for_render(wid, pane)
    assert r.decision == "bail"
    assert r.dispatch_trusted is False

    await _render(scenario, wid)

    picker = _picker_text(scenario)
    # The single-question gate blocks the swap: Q1's option 1 is NOT injected.
    assert "P) Lift and shift" not in picker


# ── 3. multi-SELECT partial bail — the select_mode gate keeps the pane state ──


@pytest.mark.asyncio
async def test_multi_select_partial_bail_keeps_pane_state(
    scenario: ScenarioHarness,
) -> None:
    """P2 negative. A single-QUESTION MULTI-select side file does NOT get
    swapped into the body (the ``candidate.select_mode == "single"`` gate blocks
    it — multi-select side-file options carry ``selected=None`` → ``·``
    everything, destroying the pane's real ☑/☐ checkbox state).

    The PREMISE proves this test targets the select_mode gate (not an earlier
    one): the recovery succeeds, the candidate form is non-None, the payload is
    single-QUESTION, and the candidate's ``select_mode == "multi"`` — so only
    the select_mode gate can block the swap.
    """
    _write_side_file_aged(_multi_select_input(_DICO_LABELS, title=_DICO_TITLE))
    pane = _partial_pane([(2, _DICO_LABELS[1]), (3, _DICO_LABELS[2])], cursor_number=2)
    wid = _bind(scenario, pane)

    # PREMISE (required): the recovery succeeds + the gate that blocks the swap
    # is the select_mode gate, NOT an earlier one.
    recovered = auq_source.recover_consistent_side_file_for_ctx(wid, pane)
    assert recovered is not None
    from cctelegram.terminal_parser import build_form_from_tool_input

    cand = build_form_from_tool_input(recovered.payload)
    assert cand is not None
    assert len(recovered.payload.get("questions", [])) == 1
    assert cand.select_mode == "multi"

    await _render(scenario, wid)

    picker = _picker_text(scenario)
    # No swap happened: the new notice (which appears only on a successful swap)
    # is absent, and option 1 was NOT injected from the side file. The card
    # keeps today's pane body.
    assert "Tap-to-select is off on a scrolled screen" not in picker
    assert "A) Review the 66 proposals" not in picker
