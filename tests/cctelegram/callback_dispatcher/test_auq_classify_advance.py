"""RED-first contract for the v2.1.168 confirm-advance + verify helpers.

The bot no longer trusts a sent keystroke to mean "committed". After it presses
``Enter`` it re-parses the pane and only records the ledger ``dispatched`` lock
when the form provably made the EXACT expected transition. These are the pure
predicates that decide it (the orchestrator-authored verification contract;
GREEN implements them to pass — implementer != verifier):

  - ``callback_dispatcher.interactive._classify_advance(committed, entry, aform, resolved)``
  - ``terminal_parser._loose_label_match(live, minted)``
  - ``terminal_parser._pane_looks_like_picker(pane)``

Every multi-question row is asserted through the REAL ``terminal_parser`` over the
committed multi-Q fixtures (side-file sourced so ``questions`` is populated, as the
live confirm-advance re-parse is); the 3+Q skip / duplicate-option-set rows use
synthetic forms because no 3-question fixture exists.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cctelegram.terminal_parser import resolve_ask_form

# RED until GREEN adds them:
from cctelegram.callback_dispatcher.interactive import _classify_advance  # noqa: E402
from cctelegram.terminal_parser import (  # noqa: E402
    _loose_label_match,
    _pane_looks_like_picker,
)

_FX = Path(__file__).parents[1] / "fixtures"

# The inline 2-question tool_input (mirrors the multi-Q scenario side file) so the
# parsed forms carry the ordered ``questions`` list the predicate's identity check
# needs — exactly the source shape the live confirm-advance re-parse uses.
_TOOL_INPUT = {
    "questions": [
        {
            "question": (
                "Which implementation approach should we take for the new "
                "caching layer?"
            ),
            "header": "Approach",
            "multiSelect": False,
            "options": [
                {"label": "Write-through cache with Redis backend"},
                {"label": "Write-back cache with periodic flush"},
                {"label": "No cache, optimize queries instead"},
            ],
        },
        {
            "question": "How should we roll this out to production users?",
            "header": "Rollout",
            "multiSelect": False,
            "options": [
                {"label": "Immediate full rollout to everyone"},
                {"label": "Gradual canary over one week"},
                {"label": "Feature-flagged opt-in only"},
            ],
        },
    ]
}


def _form(name: str):
    return resolve_ask_form(_TOOL_INPUT, (_FX / name).read_text())


def _entry(*, option_number: int = 1, is_review_submit: bool = False):
    return SimpleNamespace(
        option_number=option_number, is_review_submit=is_review_submit
    )


@pytest.fixture
def forms():
    return {
        "Q1": _form("auq_multiq_q1_pane.txt"),
        "Q2": _form("auq_multiq_q2_after_pick_pane.txt"),
        "SUB": _form("auq_multiq_submit_pane.txt"),
    }


# ── _classify_advance — real fixtures ──────────────────────────────────────


def test_q1_pick_advancing_to_q2_is_dispatched(forms) -> None:
    assert _classify_advance(forms["Q1"], _entry(option_number=1), forms["Q2"], False)


def test_q1_pick_landing_on_submit_is_over_advance_not_dispatched(forms) -> None:
    # The exact over-advance class: a non-final pick must NOT reach review.
    assert not _classify_advance(
        forms["Q1"], _entry(option_number=1), forms["SUB"], False
    )


def test_same_question_redraw_is_not_advanced(forms) -> None:
    assert not _classify_advance(
        forms["Q1"], _entry(option_number=1), forms["Q1"], False
    )


def test_final_question_pick_reaching_review_is_dispatched(forms) -> None:
    assert _classify_advance(forms["Q2"], _entry(option_number=1), forms["SUB"], False)


def test_submit_resolved_is_dispatched(forms) -> None:
    assert _classify_advance(
        forms["SUB"], _entry(option_number=1, is_review_submit=True), None, True
    )


def test_submit_landing_on_a_picker_is_not_dispatched(forms) -> None:
    # Confirm-parse produced a form (a picker is still up) → NOT a resolution.
    assert not _classify_advance(
        forms["SUB"], _entry(option_number=1, is_review_submit=True), forms["Q2"], False
    )


def test_multiq_nonfinal_unexpected_resolution_is_not_dispatched(forms) -> None:
    # A non-final pick should advance to the next question, not resolve the tool.
    assert not _classify_advance(forms["Q1"], _entry(option_number=1), None, True)


def test_cancel_leaving_review_is_dispatched(forms) -> None:
    assert _classify_advance(forms["SUB"], _entry(option_number=2), None, True)


def test_cancel_still_on_review_is_not_dispatched(forms) -> None:
    assert not _classify_advance(
        forms["SUB"], _entry(option_number=2), forms["SUB"], False
    )


# ── _classify_advance — synthetic 3-question forms (skip / duplicate) ───────


def _stub_form(answered, shown_opts, questions, *, review=False, title="q"):
    Opt = lambda label: SimpleNamespace(label=label, cursor=False)  # noqa: E731
    Tab = lambda label, ans, sub=False: SimpleNamespace(  # noqa: E731
        label=label, answered=ans, is_submit=sub, is_current=False
    )
    tabs = [Tab(f"H{i}", a) for i, a in enumerate(answered)] + [
        Tab("Submit", False, True)
    ]
    return SimpleNamespace(
        tabs=tabs,
        options=[Opt(x) for x in shown_opts],
        questions=questions,
        is_review_screen=review,
        current_question_title=title,
    )


def _q(opts):
    return SimpleNamespace(options=[SimpleNamespace(label=x) for x in opts], title="t")


def test_3q_skip_to_later_question_fails_closed() -> None:
    qs = [_q(["a1", "a2"]), _q(["b1", "b2"]), _q(["c1", "c2"])]
    committed = _stub_form([False, False, False], ["a1", "a2"], qs)
    # only the committed tab flipped, but the SHOWN question is Q3's options.
    after_skip = _stub_form([True, False, False], ["c1", "c2"], qs)
    assert not _classify_advance(committed, _entry(option_number=1), after_skip, False)


def test_3q_immediate_next_is_dispatched() -> None:
    qs = [_q(["a1", "a2"]), _q(["b1", "b2"]), _q(["c1", "c2"])]
    committed = _stub_form([False, False, False], ["a1", "a2"], qs)
    after_ok = _stub_form([True, False, False], ["b1", "b2"], qs)
    assert _classify_advance(committed, _entry(option_number=1), after_ok, False)


def test_3q_duplicate_option_set_next_fails_closed() -> None:
    # Q2 and Q3 share an option set → the next question is not uniquely identified.
    qs = [_q(["a1", "a2"]), _q(["d1", "d2"]), _q(["d1", "d2"])]
    committed = _stub_form([False, False, False], ["a1", "a2"], qs)
    after_dup = _stub_form([True, False, False], ["d1", "d2"], qs)
    assert not _classify_advance(committed, _entry(option_number=1), after_dup, False)


def test_3q_extra_tab_flip_fails_closed() -> None:
    # The UNIQUE catcher for the ``after != expected`` over-advance gate (no
    # is_review_screen shortcut): committed Q1 of three, but TWO tabs flipped to
    # answered while the shown screen is Q2's picker. Only the answered-vector
    # delta check rejects this — guards the over-advance gate itself.
    qs = [_q(["a1", "a2"]), _q(["b1", "b2"]), _q(["c1", "c2"])]
    committed = _stub_form([False, False, False], ["a1", "a2"], qs)
    after_two_flipped = _stub_form([True, True, False], ["b1", "b2"], qs)
    assert not _classify_advance(
        committed, _entry(option_number=1), after_two_flipped, False
    )


# ── _loose_label_match ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("live", "minted", "expected"),
    [
        ("Foo", "Foo", True),
        ("[✔] Foo (Recommended)", "Foo", True),  # checkbox + suffix stripped
        ("Approve with cond", "Approve with conditions", True),  # truncation
        ("Approve with conditions", "Approve", False),  # semantic EXTENSION rejected
        ("Bar", "Foo", False),
        ("", "Foo", False),  # empty live rejected
        ("Foo", "", False),  # empty minted rejected
    ],
)
def test_loose_label_match(live: str, minted: str, expected: bool) -> None:
    assert _loose_label_match(live, minted) is expected


# ── _pane_looks_like_picker ────────────────────────────────────────────────


def test_pane_with_picker_markers_is_picker(forms) -> None:
    assert _pane_looks_like_picker((_FX / "auq_multiq_q1_pane.txt").read_text())


def test_resolved_prompt_pane_is_not_picker() -> None:
    assert not _pane_looks_like_picker("user@host repo % \n")


def test_pane_with_cursor_glyph_but_no_footer_is_picker() -> None:
    # The footer/header markers can scroll off / be clipped while the picker is
    # STILL up; a cursor-glyph numbered-option row alone must prove "picker
    # present" so a post-Enter parse-fail there is AMBIGUOUS (commit_unconfirmed),
    # never a false "resolved" → false "dispatched" lock.
    pane = "some preamble\n❯ 1. Pick this option\n  2. Or this one\n"
    assert _pane_looks_like_picker(pane)


def test_pane_with_plain_numbered_lines_no_cursor_is_not_picker() -> None:
    # A non-picker pane that merely contains numbered text (no selection cursor
    # glyph, no markers) must NOT be misread as a live picker.
    assert not _pane_looks_like_picker("Steps:\n1. did a thing\n2. did another\n")


def test_loose_label_match_checkbox_without_trailing_space() -> None:
    # The checkbox strip is whitespace-OPTIONAL: ``[✔]Foo`` (no space) normalizes
    # like ``[✔] Foo``.
    assert _loose_label_match("[✔]Foo", "Foo")
