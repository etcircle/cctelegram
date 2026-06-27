"""Unit guards for ``_clip_card_title`` — the selection-card preamble cap.

The selection (picker) card lists option labels only; the full question +
descriptions live in the separate "📋 full details" card. A long question
(the side-file render path puts the whole ``questions[i].question`` into
``current_question_title``) would otherwise render verbatim above the options
and push the tappable choices off the bottom. ``_clip_card_title`` caps the
LOCAL render string only — it never mutates the form (dispatch/fingerprint stay
byte-identical), removing a long preamble as a cause of option clipping (a
pathological many/long-options card can still overflow ``_clip_card_body`` — a
separate, pre-existing limit this helper does not address).
"""

from __future__ import annotations

from cctelegram.handlers.interactive_ui import (
    _SELCARD_TITLE_MAX_CHARS,
    _clip_card_title,
    _render_ask_user_question,
)
from cctelegram.terminal_parser import build_form_from_tool_input


def test_short_title_unchanged() -> None:
    t = "Which deployment strategy should we use for the next release?"
    assert _clip_card_title(t) == t
    assert "…" not in _clip_card_title(t)


def test_title_exactly_at_limit_unchanged() -> None:
    t = "x" * _SELCARD_TITLE_MAX_CHARS
    assert _clip_card_title(t) == t


def test_none_returns_empty_string() -> None:
    assert _clip_card_title(None) == ""


def test_empty_returns_empty_string() -> None:
    assert _clip_card_title("") == ""


def test_long_title_clipped_with_ellipsis_and_bounded() -> None:
    t = (
        "This is a deliberately long migration question that goes on about "
        "availability and cost and rollback risk and team familiarity, "
    ) * 6
    out = _clip_card_title(t)
    # ends with the ellipsis, bounded to the cap (+ the single ellipsis char)
    assert out.endswith("…")
    assert len(out) <= _SELCARD_TITLE_MAX_CHARS + 1
    # the kept body is a genuine prefix of the original
    assert t.startswith(out[:-1])
    # cut on a WORD boundary: the original char at the cut index is a space
    assert t[len(out) - 1] == " "


def test_long_title_without_spaces_hard_cut() -> None:
    t = "x" * 500
    out = _clip_card_title(t)
    assert out.endswith("…")
    # no nearby space → hard cut at the cap, plus the ellipsis
    assert len(out) == _SELCARD_TITLE_MAX_CHARS + 1


# ── shared title-render path: the preamble is clipped on the multi-select and
#    multi-question branches too (the title render precedes the select-mode
#    split). Render-level so it does not depend on resolver/pane consistency. ──

_MULTI_TAIL = "TAILSENTINEL_multi_select_full_question"
_MQ_TAIL = "TAILSENTINEL_multi_question_full_q1"


def test_multi_select_render_clips_preamble_and_keeps_options() -> None:
    long_q = (
        "Which features should ship in the very first release? "
        + "There is plenty of detail to weigh across each of them. " * 6
        + _MULTI_TAIL
    )
    form = build_form_from_tool_input(
        {
            "questions": [
                {
                    "question": long_q,
                    "header": "Features",
                    "multiSelect": True,
                    "options": [
                        {"label": f"Feature {c}", "description": ""} for c in "ABCDE"
                    ],
                }
            ]
        }
    )
    assert form is not None
    assert form.select_mode == "multi"
    body = _render_ask_user_question(form)
    assert "Which features should ship" in body  # head shown
    assert _MULTI_TAIL not in body  # tail clipped
    assert "…" in body
    for c in "ABCDE":
        assert f"Feature {c}" in body  # all options still listed


def test_multi_question_render_clips_q1_preamble() -> None:
    long_q1 = (
        "Which migration strategy should we commit to right now? "
        + "Weighing availability, cost, and rollback risk in detail. " * 6
        + _MQ_TAIL
    )
    form = build_form_from_tool_input(
        {
            "questions": [
                {
                    "question": long_q1,
                    "header": "Strategy",
                    "multiSelect": False,
                    "options": [
                        {"label": "S-A) Lift and shift", "description": ""},
                        {"label": "S-B) Rewrite", "description": ""},
                    ],
                },
                {
                    "question": "Which rollout cadence?",
                    "header": "Cadence",
                    "multiSelect": False,
                    "options": [
                        {"label": "C-A) Big bang", "description": ""},
                        {"label": "C-B) Canary", "description": ""},
                    ],
                },
            ]
        }
    )
    assert form is not None
    assert len(form.questions) == 2
    body = _render_ask_user_question(form)
    assert "Which migration strategy should we commit to" in body  # head shown
    assert _MQ_TAIL not in body  # Q1 tail clipped
    assert "…" in body
    # Q1's options (the rendered tab) still listed.
    assert "S-A) Lift and shift" in body
    assert "S-B) Rewrite" in body
