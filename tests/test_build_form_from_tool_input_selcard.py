"""Unit guards for ``build_form_from_tool_input`` as used by the selection-card
completeness fix (DISPLAY-ONLY body swap on a partial-pane bail).

The swap calls ``build_form_from_tool_input(recovered.payload)`` then renders it
ONLY when the result is non-None AND ``select_mode == "single"``. These guards
pin the three None-returning shapes (so the swap fails closed to the pane form),
the single-question single-select happy path (the option labels survive), and
the multi-select mapping (so the ``select_mode`` gate has something to reject).
"""

from __future__ import annotations

from cctelegram.terminal_parser import build_form_from_tool_input


def test_single_question_single_select_builds_form_with_all_options() -> None:
    form = build_form_from_tool_input(
        {
            "questions": [
                {
                    "question": "q",
                    "header": "h",
                    "multiSelect": False,
                    "options": [
                        {"label": "A", "description": ""},
                        {"label": "B", "description": ""},
                    ],
                }
            ]
        }
    )
    assert form is not None
    assert form.select_mode == "single"
    assert len(form.questions) == 1
    labels = [o.label for o in form.options]
    assert "A" in labels
    assert "B" in labels


def test_none_input_returns_none() -> None:
    assert build_form_from_tool_input(None) is None


def test_empty_questions_returns_none() -> None:
    assert build_form_from_tool_input({"questions": []}) is None


def test_question_with_no_parseable_options_returns_none() -> None:
    assert (
        build_form_from_tool_input({"questions": [{"question": "q", "options": []}]})
        is None
    )


def test_multiselect_single_question_maps_to_multi() -> None:
    form = build_form_from_tool_input(
        {
            "questions": [
                {
                    "question": "q",
                    "header": "h",
                    "multiSelect": True,
                    "options": [
                        {"label": "A", "description": ""},
                        {"label": "B", "description": ""},
                    ],
                }
            ]
        }
    )
    assert form is not None
    assert form.select_mode == "multi"
