"""Scenario coverage for the partial-bail ctx-recovery fix (v5 plan §6.1).

The long-open busy-topic AUQ bug (DiCopilot @4): a PreToolUse side file aged
past the 300s render read-TTL + a heavily-scrolled pane that hides option 1 →
``resolve_auq_source_for_render`` returns a PARTIAL-pane bail
(``decision=="bail"``, ``dispatch_trusted is False``,
``reason.startswith("bail_partial")``). On bare main the ctx-source selector
hard-maps ``bail → ctx_source=None``, so the full-details card (which carries
ALL options, incl. the scrolled-off option 1, from the side-file dict) is
suppressed and the user sees only the partial picker.

The fix posts that full-details card from the read-TTL-free side-file dict ONLY
on a CONSISTENT partial-pane bail that clears the helper-local evidence floor
(Leg A reliable ≥8-char title OR Leg B ≥2 distinct numbered slot-matches). It
never mints a dispatchable pick token (``dispatch_trusted`` stays False), so it
is DISPLAY-ONLY — never a wrong dispatch.

These tests drive the public seam (``handle_interactive_ui``) with a fake bot /
fake tmux and assert on ``scenario.bot.sent`` — no monkeypatch of handler
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

_SESSION_ID = "33333333-3333-4333-8333-333333333333"


# ── builders ─────────────────────────────────────────────────────────────────


def _single_q_input(labels: list[str], *, title: str) -> dict[str, Any]:
    """A single-question tool_input with the given option labels + a question."""
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
    ``extra_scrollback`` is prepended so the dedup-hash churn test can vary the
    surrounding scrollback while keeping the picker structurally identical.
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
    tool_input: dict[str, Any], *, tool_use_id: str = "tool-use-aged-recover"
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


def _context_texts(scenario: ScenarioHarness) -> list[str]:
    return [
        _unescape(str(sent.kwargs.get("text") or ""))
        for sent in scenario.bot.sent
        if _unescape(str(sent.kwargs.get("text") or "")).startswith(
            "📋 AskUserQuestion — full details"
        )
    ]


def _context_text(scenario: ScenarioHarness) -> str:
    texts = _context_texts(scenario)
    if not texts:
        raise AssertionError("no AUQ context message recorded")
    return texts[0]


def _picker_text(scenario: ScenarioHarness) -> str:
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


# ── 1. THE GENUINE main-RED seam test ────────────────────────────────────────


@pytest.mark.asyncio
async def test_partial_consistent_bail_posts_ctx_with_option_1(
    scenario: ScenarioHarness,
) -> None:
    """THE GENUINE main-RED seam test (the only one RED against bare main).

    A partial pane (option 1 scrolled off, slots 2,3 + affordances visible) +
    an aged consistent side file holding all 3 options incl. option 1. Premise
    guard: ``resolve_auq_source_for_render`` → ``decision=="bail"``,
    ``dispatch_trusted is False``, ``reason.startswith("bail_partial")``.

    Load-bearing assertion (RED on main: ``_context_text`` raises "no AUQ
    context message recorded"; GREEN after): exactly ONE full-details ctx card
    whose body carries "A) Review the 66 proposals" + the other labels; the
    picker still renders the partial pane with picks suppressed.
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

    # Load-bearing: exactly ONE full-details card carrying option 1.
    assert len(_context_texts(scenario)) == 1
    context = _context_text(scenario)
    assert "A) Review the 66 proposals" in context
    assert "B) Draft the synthesis doc" in context
    assert "C) Defer to next session" in context

    # The picker still renders the partial live pane, picks suppressed.
    picker = _picker_text(scenario)
    assert "2. B) Draft the synthesis doc" in picker
    assert _aqp_tokens(scenario) == []


# ── 2. churn / once-only with the stable side-file dedup key ──────────────────


@pytest.mark.asyncio
async def test_partial_bail_recover_posts_ctx_once_across_churning_renders(
    scenario: ScenarioHarness,
) -> None:
    """N=4 renders, structurally-identical partial picker with DIFFERENT
    surrounding scrollback each tick → exactly ONE ctx card; the persisted
    marker is the SIDE-FILE canonical fingerprint (NOT the churning pane fp,
    NOT a form: key).
    """
    tool_input = _single_q_input(_DICO_LABELS, title=_DICO_TITLE)
    _write_side_file_aged(tool_input)
    side_fp = auq_source._canonical_dict_fingerprint(tool_input)

    first_pane = _partial_pane(
        [(2, _DICO_LABELS[1]), (3, _DICO_LABELS[2])],
        cursor_number=2,
        extra_scrollback="trace 0: alpha churn line",
    )
    wid = _bind(scenario, first_pane)

    pane_texts: list[str] = []
    pane_fps: list[str] = []
    for i in range(4):
        pane = _partial_pane(
            [(2, _DICO_LABELS[1]), (3, _DICO_LABELS[2])],
            cursor_number=2,
            extra_scrollback="\n".join(
                f"DIFFERENT churn {i} line {j}: beta gamma" for j in range(5 * (i + 1))
            ),
        )
        scenario.tmux.set_pane(wid, pane)
        pane_texts.append(pane)
        pane_fps.append(
            auq_source.resolve_auq_source_for_render(wid, pane).source_fingerprint
        )
        await _render(scenario, wid)

    # Vacuity guard: the scrollback genuinely varied each render's pane text
    # (the pane fingerprint is deliberately scrollback-invariant — the loop-kill
    # property — so the churn is real even though the pane fp is stable).
    assert len(set(pane_texts)) > 1

    # Exactly ONE ctx card across all four churning renders.
    assert len(_context_texts(scenario)) == 1

    # The marker is the STABLE side-file canonical fingerprint — NOT the pane fp
    # (which would re-mint a fresh key per tick), NOT a form: key.
    marker = interactive_ui._auq_context_posted.get(wid)
    assert marker == f"pretool:{side_fp[:16]}"
    assert not marker.startswith("form:")
    for pane_fp in pane_fps:
        assert marker != f"pretool:{pane_fp[:16]}"


# ── 3. titleless single coincidence — GREEN-must-stay floor guard ─────────────


@pytest.mark.asyncio
async def test_titleless_one_coincidence_no_ctx_card_via_handle_interactive_ui(
    scenario: ScenarioHarness,
) -> None:
    """GREEN-must-stay floor guard (NOT a RED repro; passes on main AND after
    fix). A titleless partial pane with ONE visible numbered option + an aged
    STALE/different side file whose slot coincidentally shares that label → NO
    ctx card. Passes on main because main posts no ctx on any bail; its value is
    pinning that the FIX does not post the WRONG card.
    """
    stale_input = _single_q_input(
        ["X) Stale option one", "Y) Stale option two", "Z) Stale option three"],
        title="A totally different stale question entirely",
    )
    _write_side_file_aged(stale_input)
    pane = _partial_pane([(2, "Y) Stale option two")], cursor_number=2)
    wid = _bind(scenario, pane)

    await _render(scenario, wid)

    assert _context_texts(scenario) == []


# ── 4. N=2 false-negative — EXPECTED, fail-closed (residual §11(b)) ───────────


@pytest.mark.asyncio
async def test_n2_false_negative_single_surviving_option_no_card(
    scenario: ScenarioHarness,
) -> None:
    """INTENTIONAL safety-over-recall false-negative (residual §11(b)).

    A REAL 2-option single-tab AUQ where only option 2 survives the scroll AND
    the title is unparseable (``current_question_title is None`` — single-tab
    panes store the title in ``pane_walkback_title``, forbidden for identity) →
    1 numbered match (< 2 for Leg B) AND Leg A dead → NO ctx card. The partial
    picker still renders option 2.

    This is FAIL-CLOSED: the user never sees a wrong-question card; option 1 is
    recoverable by scrolling. It degrades to today's main behavior (no ctx card
    on any bail) → strictly no worse than main. Lowering
    ``_CTX_EVIDENCE_MIN_NUMBERED_MATCHES`` to 1 to "fix" it would re-open the
    single-coincidence wrong-question regression on EVERY titleless 1-coincidence
    pane — DO NOT undo this trade-off without re-confirming the wrong-question
    guard.
    """
    tool_input = _single_q_input(
        ["A) Keep the legacy path", "B) Migrate to the new pipeline"],
        title="Choose the migration approach for this service",
    )
    _write_side_file_aged(tool_input)
    pane = _partial_pane([(2, "B) Migrate to the new pipeline")], cursor_number=2)
    wid = _bind(scenario, pane)

    # Premise guard: the single-tab partial pane has no parseable title.
    from cctelegram import terminal_parser

    pane_form = terminal_parser.resolve_ask_form(None, pane)
    assert pane_form is not None
    assert pane_form.current_question_title is None

    await _render(scenario, wid)

    assert _context_texts(scenario) == []
    picker = _picker_text(scenario)
    assert "2. B) Migrate to the new pipeline" in picker


# ── 5. coincidental 2-option stale display — DISPLAY-ONLY (residual §11(a)) ────


@pytest.mark.asyncio
async def test_coincidental_two_option_stale_side_file_posts_display_only_no_pick_token(
    scenario: ScenarioHarness,
) -> None:
    """Pins accepted residual §11(a) (round-3 Codex P2).

    A STALE, not-overwritten side file for a DIFFERENT question whose two labels
    are generic ("1. Yes" / "2. No") shares both labels with a titleless partial
    pane showing those same two generic labels → ``_record_consistent_with_pane``
    accepts + Leg B (N=2) passes → the helper recovers + posts the stale
    full-details ctx card.

    Bounded DISPLAY-ONLY contract: (a) the ctx card DOES post (the residual is
    real — a stale wrong-question card), AND (b) ``dispatch_trusted`` stays False
    → the suppressed bail picker mints NO ``aqp:`` pick buttons (so
    ``_aqp_tokens`` is ``[]``) — proving it is never a wrong DISPATCH / dead-tap,
    only a bounded wrong DISPLAY. Bounded because the side file is overwritten on
    every new AUQ; requires a titleless pane + 2 coincidental generic labels + a
    stale unoverwritten side file simultaneously.
    """
    stale_input = _single_q_input(
        ["Yes", "No"], title="An entirely different stale yes/no decision"
    )
    _write_side_file_aged(stale_input)
    # A titleless partial pane (drop option 1 so it bails partial) whose visible
    # slots 2,3 coincidentally carry the generic stale labels.
    pane = _partial_pane([(2, "Yes"), (3, "No")], cursor_number=2)
    # Make the side file's labels line up by slot: pad to 3 so slots 2,3 match.
    stale_input = _single_q_input(
        ["Maybe later", "Yes", "No"],
        title="An entirely different stale yes/no decision",
    )
    _write_side_file_aged(stale_input)
    wid = _bind(scenario, pane)

    await _render(scenario, wid)

    # (a) the stale full-details card DOES post (the residual is real).
    assert len(_context_texts(scenario)) == 1
    # (b) DISPLAY-ONLY: a partial bail mints NO aqp: pick buttons (dispatch_trusted
    # False), so there is no dispatchable token at all — the strongest
    # no-wrong-dispatch pin.
    assert _aqp_tokens(scenario) == []


# ── 6. JSONL cache precedence (the cache DOES win) ────────────────────────────


@pytest.mark.asyncio
async def test_jsonl_cache_takes_precedence_over_recovered_bail(
    scenario: ScenarioHarness,
) -> None:
    """A completed-JSONL cache + tool_use_id wins over the recovered bail
    branch (the recovered branch sits BELOW ``dict_via_jsonl``).
    """
    pane = _partial_pane([(2, _DICO_LABELS[1]), (3, _DICO_LABELS[2])], cursor_number=2)
    wid = _bind(scenario, pane)
    _write_side_file_aged(_single_q_input(_DICO_LABELS, title=_DICO_TITLE))

    # Seed a completed-JSONL cache for a DIFFERENT question (the cache wins).
    jsonl_input = _single_q_input(
        ["J) JSONL-only option one", "K) JSONL-only option two"],
        title="A JSONL-cached question that should win",
    )
    interactive_ui._last_completed_ask_tool_input[wid] = jsonl_input
    interactive_ui._last_auq_tool_use_id[wid] = "toolu_stale_jsonl"

    await _render(scenario, wid)

    context = _context_text(scenario)
    # The JSONL-cache content wins; the recovered option-1 needle is absent.
    assert "J) JSONL-only option one" in context
    assert "A) Review the 66 proposals" not in context


# ── 7. prior FORM-source marker blocks the recovered DICT card (green guard) ───


@pytest.mark.asyncio
async def test_prior_form_ctx_marker_blocks_recovered_dict_card(
    scenario: ScenarioHarness,
) -> None:
    """Window-presence once-only gate (Codex P2 §5a): a prior ctx marker on the
    window blocks the later recovered card. Proves the target bug starts from no
    prior marker.
    """
    pane = _partial_pane([(2, _DICO_LABELS[1]), (3, _DICO_LABELS[2])], cursor_number=2)
    wid = _bind(scenario, pane)
    _write_side_file_aged(_single_q_input(_DICO_LABELS, title=_DICO_TITLE))

    # Pre-commit a marker for the window (simulates an already-posted ctx card).
    token = interactive_ui.claim_auq_context_post_in_memory(wid, "form:preexisting")
    assert token is not None
    interactive_ui.commit_auq_context_post(
        wid,
        token,
        (9001,),
        text="📋 AskUserQuestion — full details\n\nprior form card",
        source={"questions": [{"question": "prior", "options": []}]},
        user_id=scenario.user_id,
        chat_id=scenario.chat_id,
        thread_id=42,
        session_id=_SESSION_ID,
    )

    before = len(_context_texts(scenario))
    await _render(scenario, wid)
    # No ADDITIONAL ctx send (the once-only gate blocks it).
    assert len(_context_texts(scenario)) == before


# ── 8. multi-question partial bail — all-questions card, no picker token ──────


@pytest.mark.asyncio
async def test_multi_question_partial_bail_posts_all_questions_ctx_no_picker_token_on_ambiguity(
    scenario: ScenarioHarness,
) -> None:
    """P2 §9.1 accepted-residual pin. A 2-question side file + a partial multi-Q
    pane with ≥2 matching labels → (a) the ctx card enumerates ALL questions;
    (b) the bail picker mints NO ``aqp:`` pick buttons (so ``_aqp_tokens`` is
    ``[]``).
    """
    tool_input = _multi_q_input()
    _write_side_file_aged(tool_input)
    # Question 1 has 3 options; the pane shows its slots 2,3 (slot 1 scrolled
    # off, so it is NOT contiguous-from-1 → a PARTIAL bail) which gives Leg B
    # two distinct numbered slot-matches (N=2).
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

    # (a) the ctx card enumerates ALL questions.
    context = _context_text(scenario)
    assert "Which migration strategy should we use?" in context
    assert "Which rollout cadence do you prefer?" in context
    # (b) DISPLAY-ONLY: a partial bail mints NO aqp: pick buttons (dispatch_trusted
    # False), so there is no dispatchable token at all — the strongest
    # no-wrong-dispatch pin.
    assert _aqp_tokens(scenario) == []
