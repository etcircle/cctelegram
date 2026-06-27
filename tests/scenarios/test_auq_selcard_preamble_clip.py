"""Scenario coverage for the selection-card preamble cap (DISPLAY-ONLY).

A long AskUserQuestion question (the side-file render path puts the WHOLE
``questions[i].question`` into ``current_question_title``) rendered verbatim
above the options in the picker card, pushing the tappable choices to the very
bottom (and risking ``_clip_card_body``'s tail clip cutting them off). The fix
caps the question/preamble shown in the SELECTION card via ``_clip_card_title``.

DISPLAY-only: the full question still lives in the separate "📋 full details"
card (verified here), the option labels stay listed, and the form is never
mutated so tap-dispatch is unaffected.

Drives the public seam (``handle_interactive_ui``) with a fake bot / fake tmux
and asserts on the rendered cards — no monkeypatch of handler internals.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from cctelegram.handlers import auq_source, interactive_ui
from cctelegram.utils import app_dir
from tests.conftest import ScenarioHarness

pytestmark = pytest.mark.scenario

_SESSION_ID = "55555555-5555-4555-8555-555555555555"

_Q_HEAD = "Which database migration strategy should we commit to"
_Q_TAIL = "FINALSENTINEL_only_in_the_full_question_body"
_LONG_QUESTION = (
    _Q_HEAD + " given the tradeoffs across availability, cost, rollback risk, and team "
    "familiarity? "
    + (
        "There is a great deal of nuance to weigh here, and this preamble is "
        "deliberately long so the options would otherwise be pushed off the "
        "bottom of the card. "
    )
    * 4
    + _Q_TAIL
)

_LABELS = [
    "A) Online schema migration (gh-ost)",
    "B) Blue-green database swap",
    "C) Dual-write with backfill",
    "D) Maintenance-window cutover",
]


def _tool_input() -> dict[str, Any]:
    return {
        "questions": [
            {
                "question": _LONG_QUESTION,
                "header": "Migration",
                "multiSelect": False,
                "options": [
                    {"label": label, "description": f"desc for {label}"}
                    for label in _LABELS
                ],
            }
        ]
    }


def _complete_pane(labels: list[str]) -> str:
    """A COMPLETE single-tab picker pane (contiguous from option 1) so the
    resolver returns ``side_file_ok`` against a consistent fresh side file."""
    lines: list[str] = []
    for number, label in enumerate(labels, start=1):
        prefix = "❯" if number == 1 else " "
        lines.append(f"{prefix} {number}. {label}")
        lines.append(f"     description for option {number}")
    next_num = len(labels) + 1
    lines.append(f"  {next_num}. Type something.")
    lines.append("─" * 40)
    lines.append(f"  {next_num + 1}. Chat about this")
    lines.append("")
    lines.append("Enter to select · ↑/↓ to navigate · Esc to cancel")
    return "\n".join(lines) + "\n"


def _bind(scenario: ScenarioHarness, pane: str) -> str:
    wid = scenario.add_window(window_name="repo", cwd="/repo", pane_text=pane)
    scenario.bind_thread(
        42, wid, display_name="repo", cwd="/repo", session_id=_SESSION_ID
    )
    return wid


def _write_side_file_fresh(tool_input: dict[str, Any]) -> Path:
    pending = app_dir() / "auq_pending"
    pending.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = pending / f"{_SESSION_ID}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": _SESSION_ID,
                "tool_use_id": "tool-use-preamble-clip",
                "written_at": time.time(),
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
    return text.replace("\\", "")


def _picker_text(scenario: ScenarioHarness) -> str:
    for sent in reversed(scenario.bot.sent):
        if sent.kwargs.get("reply_markup") is not None:
            return _unescape(str(sent.kwargs.get("text") or ""))
    raise AssertionError("no picker card recorded")


def _context_text(scenario: ScenarioHarness) -> str:
    for sent in scenario.bot.sent:
        text = _unescape(str(sent.kwargs.get("text") or ""))
        if text.startswith("📋 AskUserQuestion — full details"):
            return text
    raise AssertionError("no AUQ context message recorded")


@pytest.mark.asyncio
async def test_long_question_preamble_is_clipped_in_selection_card(
    scenario: ScenarioHarness,
) -> None:
    """The owner case: a fresh consistent side file with a very long question →
    ``side_file_ok`` renders the side-file form. The selection card's preamble
    is CLIPPED (head + ellipsis; the question tail is gone) yet ALL option
    labels are listed; the "📋 full details" card still carries the FULL
    question (clip scoped to the selection card only).
    """
    pane = _complete_pane(_LABELS)
    wid = _bind(scenario, pane)
    _write_side_file_fresh(_tool_input())

    # Premise guard: the trusted side-file render path (the side-file form, with
    # the long question as its title, is what gets rendered).
    r = auq_source.resolve_auq_source_for_render(wid, pane)
    assert r.decision == "side_file_ok"

    await _render(scenario, wid)

    picker = _picker_text(scenario)
    # Preamble clipped: the head is shown, the tail sentinel is NOT, ellipsis present.
    assert _Q_HEAD in picker
    assert _Q_TAIL not in picker  # RED on bare branch base (full question rendered)
    assert "…" in picker
    # All four options remain listed (never pushed off the bottom).
    for label in _LABELS:
        assert label in picker

    # The FULL question (incl. the tail sentinel) still lives in the details card.
    context = _context_text(scenario)
    assert _Q_TAIL in context
