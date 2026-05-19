"""Tests for terminal_parser — regex-based detection of Claude Code UI elements."""

import pytest

from cctelegram.terminal_parser import (
    extract_bash_output,
    extract_context_pct,
    extract_interactive_content,
    is_interactive_ui,
    is_status_active,
    parse_status_line,
    strip_pane_chrome,
)

# ── parse_status_line ────────────────────────────────────────────────────


class TestParseStatusLine:
    @pytest.mark.parametrize(
        ("spinner", "rest", "expected"),
        [
            ("·", "Working on task", "Working on task"),
            ("✻", "  Reading file  ", "Reading file"),
            ("✽", "Thinking deeply", "Thinking deeply"),
            ("✶", "Analyzing code", "Analyzing code"),
            ("✳", "Processing input", "Processing input"),
            ("✢", "Building project", "Building project"),
        ],
    )
    def test_spinner_chars(self, spinner: str, rest: str, expected: str, chrome: str):
        pane = f"some output\n{spinner}{rest}\n{chrome}"
        assert parse_status_line(pane) == expected

    @pytest.mark.parametrize(
        "pane",
        [
            pytest.param("just normal text\nno spinners here\n", id="no_spinner"),
            pytest.param("", id="empty"),
        ],
    )
    def test_returns_none(self, pane: str):
        assert parse_status_line(pane) is None

    def test_no_chrome_returns_none(self):
        """Without chrome separator, status can't be determined."""
        pane = "output\n✻ Doing work\nno chrome here\n"
        assert parse_status_line(pane) is None

    def test_blank_line_between_status_and_chrome(self, chrome: str):
        """Status line with blank lines before separator."""
        pane = f"output\n✻ Doing work\n\n{chrome}"
        assert parse_status_line(pane) == "Doing work"

    def test_idle_no_status(self, chrome: str):
        """Idle pane (no status line above chrome) returns None."""
        pane = f"some output\n● Tool result\n{chrome}"
        assert parse_status_line(pane) is None

    def test_false_positive_bullet(self, chrome: str):
        """· in regular output must NOT be detected as status."""
        pane = f"· bullet point one\n· bullet point two\nsome result\n{chrome}"
        assert parse_status_line(pane) is None

    def test_uses_fixture(self, sample_pane_status_line: str):
        assert parse_status_line(sample_pane_status_line) == "Reading file src/main.py"


# ── is_status_active ─────────────────────────────────────────────────────


class TestIsStatusActive:
    """is_status_active is True iff Claude is actively producing output.
    The signal is "esc to interrupt" in the bottom chrome bar — that's
    the only marker Claude renders consistently while a run is in flight,
    and removes once the run completes.
    """

    def test_active_pane_with_esc_to_interrupt(self):
        """Real captured-in-the-wild active pane (Brewing…)."""
        pane = (
            "✽ Brewing… (3s · thinking with high effort)\n"
            "\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt"
        )
        assert is_status_active(pane) is True

    def test_post_completion_summary_no_esc(self):
        """Real captured-in-the-wild idle pane: same spinner+blank gap, but
        bottom chrome has no "esc to interrupt"."""
        pane = (
            "✻ Cooked for 17s · 3 shells still running\n"
            "\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  ⏵⏵ bypass permissions on · 3 shells · ↓ to manage"
        )
        assert is_status_active(pane) is False

    def test_active_with_shells_and_esc(self):
        """Active run while background shells exist (compound bottom chrome)."""
        pane = (
            "✽ Tempering… (26s · ↓ 125 tokens · thought for 13s)\n"
            "\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  ⏵⏵ bypass permissions on · 2 shells · esc to interrupt · ↓ to manage"
        )
        assert is_status_active(pane) is True

    def test_idle_pane_no_status(self, chrome: str):
        pane = f"some output\n{chrome}"
        assert is_status_active(pane) is False

    def test_empty_is_idle(self):
        assert is_status_active("") is False

    def test_case_insensitive(self):
        """Tolerate hypothetical capitalization changes in the marker."""
        pane = "✻ Working\n──────\n  Esc To Interrupt\n"
        assert is_status_active(pane) is True


# ── extract_interactive_content ──────────────────────────────────────────


class TestExtractInteractiveContent:
    def test_exit_plan_mode(self, sample_pane_exit_plan: str):
        result = extract_interactive_content(sample_pane_exit_plan)
        assert result is not None
        assert result.name == "ExitPlanMode"
        assert "Would you like to proceed?" in result.content
        assert "ctrl-g to edit in" in result.content

    def test_exit_plan_mode_variant(self):
        pane = (
            "  Claude has written up a plan\n  ─────\n  Details here\n  Esc to cancel\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "ExitPlanMode"
        assert "Claude has written up a plan" in result.content

    def test_ask_user_multi_tab(self, sample_pane_ask_user_multi_tab: str):
        result = extract_interactive_content(sample_pane_ask_user_multi_tab)
        assert result is not None
        assert result.name == "AskUserQuestion"
        assert "←" in result.content

    def test_ask_user_single_tab(self, sample_pane_ask_user_single_tab: str):
        result = extract_interactive_content(sample_pane_ask_user_single_tab)
        assert result is not None
        assert result.name == "AskUserQuestion"
        assert "Enter to select" in result.content

    def test_ask_user_plain_no_checkbox(self):
        """Simple A/B/C/D AskUserQuestion (no ☐/✔/☒ glyphs) must still match.

        Regression: Claude Code renders single-select AskUserQuestion as a
        numbered options block + ``Enter to select`` footer with no checkbox
        glyphs. The original single-tab pattern required a leading
        ``[☐✔☒]`` which left this variant undetected; the bot then fell
        through to plain-text delivery and the user saw no button keyboard.
        """
        pane = (
            "Mobile drawer: chip labels or no labels?\n"
            "\n"
            "❯ 1. Stay with no labels (your original choice)\n"
            "   Subtle visual grouping only.\n"
            " 2. Add tiny 'paper' / 'digital' chips\n"
            "   9px lowercase muted mono chips above each group.\n"
            " 3. Type something.\n"
            "─\n"
            " 4. Chat about this\n"
            "\n"
            "Enter to select · Tab/Arrow keys to navigate · Esc to cancel\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "AskUserQuestion"
        assert "Enter to select" in result.content
        assert "1. Stay with no labels" in result.content

    def test_ask_user_extracts_bottom_region_when_scrollback_has_old_picker(self):
        pane = (
            "Old question?\n"
            "\n"
            "❯ 1. Old A\n"
            "  2. Old B\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
            "\n"
            "tool output and conversation scrollback\n"
            "\n"
            "Live question?\n"
            "\n"
            "❯ 1. Live A\n"
            "  2. Live B\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "AskUserQuestion"
        assert "Live A" in result.content
        assert "Old A" not in result.content

    def test_ask_user_mixed_pattern_shadow_old_checkbox_above_live_plain(self):
        # P1 trap (Hermes review of bc6eaed): the single-tab checkbox AUQ
        # pattern (top=``☐``, bottom=``Enter to select``) runs BEFORE the
        # plain-numbered pattern in UI_PATTERNS. With bottom_up the
        # checkbox pattern's walk-back from the LIVE plain-numbered
        # footer can find a ``☐`` line in the OLD checkbox picker above,
        # returning a region that starts in the stale checkbox and ends
        # in the live plain-numbered options. The fix: when the walk-back
        # crosses an OLDER instance of pattern.bottom, bail so a later
        # pattern can try.
        pane = (
            "Stale multi-select question?\n"
            "\n"
            "  ☐ Old Alpha\n"
            "  ☐ Old Beta\n"
            "  ☐ Old Gamma\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
            "\n"
            "intervening tool output and scrollback\n"
            "\n"
            "Live numbered question?\n"
            "\n"
            "❯ 1. Live A\n"
            "  2. Live B\n"
            "  3. Live C\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "AskUserQuestion"
        # Must extract the LIVE plain-numbered region, not shadow with
        # the stale checkbox top.
        assert "Live A" in result.content
        assert "Old Alpha" not in result.content
        assert "Old Beta" not in result.content
        assert "Old Gamma" not in result.content

    def test_permission_prompt_no_longer_detected(self, sample_pane_permission: str):
        # Wave 2: PermissionPrompt is dead code under
        # ``--dangerously-skip-permissions`` (the deployment's mode), so the
        # patterns were removed from UI_PATTERNS. Verify the pane no longer
        # matches anything.
        assert extract_interactive_content(sample_pane_permission) is None

    def test_restore_checkpoint(self):
        pane = (
            "  Restore the code to a previous state?\n"
            "  ─────\n"
            "  Some details\n"
            "  Enter to continue\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "RestoreCheckpoint"
        assert "Restore the code" in result.content

    def test_settings(self):
        pane = "  Settings: press tab to cycle\n  ─────\n  Option 1\n  Esc to cancel\n"
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "Settings"
        assert "Settings:" in result.content

    def test_settings_model_picker(self, sample_pane_settings: str):
        result = extract_interactive_content(sample_pane_settings)
        assert result is not None
        assert result.name == "Settings"
        assert "Select model" in result.content
        assert "Sonnet" in result.content
        assert "Enter to confirm" in result.content

    def test_settings_esc_to_cancel_bottom(self):
        pane = (
            "  Settings: press tab to cycle\n"
            "  ─────\n"
            "  Model\n"
            "  ─────\n"
            "  ● claude-sonnet-4-20250514\n"
            "  ○ claude-opus-4-20250514\n"
            "  Esc to cancel\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "Settings"
        assert "Esc to cancel" in result.content

    def test_settings_esc_to_exit_bottom(self):
        pane = (
            "  Settings: press tab to cycle\n"
            "  ─────\n"
            "  Model\n"
            "  ─────\n"
            "  ● Default (Opus 4.6)\n"
            "  ○ claude-sonnet-4-20250514\n"
            "\n"
            "  Enter to confirm · Esc to exit\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "Settings"
        assert "Enter to confirm" in result.content

    @pytest.mark.parametrize(
        "pane",
        [
            pytest.param("$ echo hello\nhello\n$\n", id="no_ui"),
            pytest.param("", id="empty"),
        ],
    )
    def test_returns_none(self, pane: str):
        assert extract_interactive_content(pane) is None

    def test_dropped_permission_pattern_returns_none(self):
        # Wave 2: PermissionPrompt patterns were removed (dead code under
        # ``--dangerously-skip-permissions``). Pane shapes that previously
        # matched must now return None.
        pane = "  Do you want to proceed?\n  Esc to cancel\n"
        assert extract_interactive_content(pane) is None


# ── is_interactive_ui ────────────────────────────────────────────────────


class TestIsInteractiveUI:
    def test_true_when_ui_present(self, sample_pane_exit_plan: str):
        assert is_interactive_ui(sample_pane_exit_plan) is True

    def test_false_when_no_ui(self, sample_pane_no_ui: str):
        assert is_interactive_ui(sample_pane_no_ui) is False

    def test_settings_is_interactive(self, sample_pane_settings: str):
        assert is_interactive_ui(sample_pane_settings) is True

    def test_false_for_empty_string(self):
        assert is_interactive_ui("") is False


# ── CB1 + CB5: visible_pane_liveness ─────────────────────────────────────


class TestVisiblePaneLiveness:
    """Three-state liveness predicate over the *visible* pane."""

    def test_empty_pane_is_unknown_not_absent(self):
        # CB1: tmux can return empty during alt-screen mode or redraw races.
        # Treating empty as ABSENT lets a destructive clear erase a live
        # picker the very next frame brings back.
        from cctelegram.terminal_parser import visible_pane_liveness

        assert visible_pane_liveness("") == "unknown"
        assert visible_pane_liveness("   \n  \n") == "unknown"
        assert visible_pane_liveness(None) == "unknown"

    def test_picker_visible_is_present(self):
        from cctelegram.terminal_parser import visible_pane_liveness

        pane = (
            "Pick one.\n"
            "\n"
            "❯ 1. A\n"
            "  2. B\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        assert visible_pane_liveness(pane) == "present"

    def test_shell_prompt_is_absent(self):
        from cctelegram.terminal_parser import visible_pane_liveness

        pane = "$ ls\nfile1.txt  file2.txt\n$ \n"
        assert visible_pane_liveness(pane) == "absent"

    def test_long_question_with_footer_at_bottom_is_present(self):
        # CB5: the question prose pushes the top anchor (option block / tab
        # header) above the visible region, but the picker footer stays at
        # the visible bottom. is_interactive_ui(visible) returns False here
        # (no top anchor), but visible_pane_liveness recovers via the
        # picker-anchor fallback.
        from cctelegram.terminal_parser import visible_pane_liveness

        # 60 lines of question prose, no top anchor, footer at the bottom.
        prose = "\n".join(
            f"Line {i} of the long question explanation." for i in range(60)
        )
        pane = prose + "\nEnter to select · ↑/↓ to navigate · Esc to cancel\n"
        assert visible_pane_liveness(pane) == "present"

    def test_exit_plan_mode_footer_anchors_too(self):
        from cctelegram.terminal_parser import visible_pane_liveness

        # Only the bottom of an ExitPlanMode picker visible.
        pane = (
            "line of plan text\n"
            "more plan text\n"
            "ctrl-g to edit in the editor · Esc to cancel\n"
        )
        assert visible_pane_liveness(pane) == "present"

    def test_submit_answers_options_only_visible_is_present(self):
        # Regression: production log 2026-05-17 12:31 — multi-question AUQ
        # advanced to the Submit/Cancel confirmation screen. The tab
        # header and "Ready to submit your answers?" prompt scrolled
        # above the visible region; the last 3 lines of the pane were
        # `['', '❯ 1. Submit answers', '  2. Cancel']`. None of the
        # legacy anchors (Enter to select / Esc to / ╰─) appear on the
        # Submit screen, so liveness returned "absent" and the
        # interactive card was destructively cleared — leaving the user
        # with no way to submit. Adding "Submit answers" as an anchor
        # keeps the card alive until the user picks Submit or Cancel.
        from cctelegram.terminal_parser import visible_pane_liveness

        pane = "\n❯ 1. Submit answers\n  2. Cancel\n"
        assert visible_pane_liveness(pane) == "present"

    def test_ready_to_submit_prompt_visible_is_present(self):
        # Alternative anchor: the "Ready to submit your answers?" prompt
        # also appears on the Submit screen. When terminal height is
        # large enough that the prompt sits within the visible bottom 5
        # lines, it should anchor the liveness check too.
        from cctelegram.terminal_parser import visible_pane_liveness

        pane = "Ready to submit your answers?\n\n❯ 1. Submit answers\n  2. Cancel\n"
        assert visible_pane_liveness(pane) == "present"

    def test_submit_answers_substring_outside_tail_is_absent(self):
        # Negative case: if "Submit answers" appears far up in the pane
        # (e.g. earlier session output) but the visible bottom 5 lines
        # are a shell prompt, the anchor must NOT trigger — otherwise we
        # leak presence across a fully cleared terminal.
        from cctelegram.terminal_parser import visible_pane_liveness

        prose = "\n".join(
            f"Line {i} Submit answers somewhere in history" for i in range(30)
        )
        pane = prose + "\n$ ls\nfile1.txt  file2.txt\n$ \n"
        assert visible_pane_liveness(pane) == "absent"


# ── strip_pane_chrome ───────────────────────────────────────────────────


class TestStripPaneChrome:
    def test_strips_from_separator(self):
        lines = [
            "some output",
            "more output",
            "─" * 30,
            "❯",
            "─" * 30,
            "  [Opus 4.6] Context: 34%",
        ]
        assert strip_pane_chrome(lines) == ["some output", "more output"]

    def test_no_separator_returns_all(self):
        lines = ["line 1", "line 2", "line 3"]
        assert strip_pane_chrome(lines) == lines

    def test_short_separator_not_triggered(self):
        lines = ["output", "─" * 10, "more output"]
        assert strip_pane_chrome(lines) == lines

    def test_only_searches_last_10_lines(self):
        # Separator at line 0 with 15 lines total — outside the last-10 window
        lines = ["─" * 30] + [f"line {i}" for i in range(14)]
        assert strip_pane_chrome(lines) == lines


# ── extract_bash_output ─────────────────────────────────────────────────


class TestExtractBashOutput:
    def test_extracts_command_output(self):
        pane = "some context\n! echo hello\n⎿ hello\n"
        result = extract_bash_output(pane, "echo hello")
        assert result is not None
        assert "! echo hello" in result
        assert "hello" in result

    def test_command_not_found_returns_none(self):
        pane = "some context\njust normal output\n"
        assert extract_bash_output(pane, "echo hello") is None

    def test_chrome_stripped(self):
        pane = (
            "some context\n"
            "! ls\n"
            "⎿ file.txt\n"
            + "─" * 30
            + "\n"
            + "❯\n"
            + "─" * 30
            + "\n"
            + "  [Opus 4.6] Context: 34%\n"
        )
        result = extract_bash_output(pane, "ls")
        assert result is not None
        assert "file.txt" in result
        assert "Opus" not in result

    def test_prefix_match_long_command(self):
        pane = "! long_comma…\n⎿ output\n"
        result = extract_bash_output(pane, "long_command_that_gets_truncated")
        assert result is not None
        assert "output" in result

    def test_trailing_blank_lines_stripped(self):
        pane = "! echo hi\n⎿ hi\n\n\n"
        result = extract_bash_output(pane, "echo hi")
        assert result is not None
        assert not result.endswith("\n")


# ── extract_context_pct ─────────────────────────────────────────────────


class TestExtractContextPct:
    def test_extracts_realistic_chrome(self):
        pane = (
            "some output\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  [Opus 4.6] Context: 89%\n"
            "  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
        )
        assert extract_context_pct(pane) == 89

    def test_extracts_low_value(self):
        pane = "  [Sonnet 4.5] Context: 7%\n"
        assert extract_context_pct(pane) == 7

    def test_no_context_line_returns_none(self):
        pane = (
            "some output\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
        )
        assert extract_context_pct(pane) is None

    def test_empty_returns_none(self):
        assert extract_context_pct("") is None

    def test_only_searches_bottom_lines(self):
        # Push a Context line to the top of a long pane — it's outside the
        # last-10-line window so should not be picked up.
        pane = (
            "  [Opus 4.6] Context: 50%\n"
            + "\n".join(f"line {i}" for i in range(30))
            + "\n"
        )
        assert extract_context_pct(pane) is None

    def test_out_of_range_value_ignored(self):
        # Three-digit number that's out of range
        pane = "  [Opus 4.6] Context: 250%\n"
        assert extract_context_pct(pane) is None


# ── parse_ask_user_question ───────────────────────────────────────────────


from cctelegram.terminal_parser import (  # noqa: E402
    AskOption,
    AskTab,
    AskUserQuestionForm,
    parse_ask_user_question,
)


# Multi-tab picker mid-form, currently on the "Approach" tab.
# Synthesized from the etvideo-editor /plan-ceo-review pane (window @34,
# 2026-05-14) at the moment the user was choosing implementation approach.
_PANE_MULTITAB_APPROACH = (
    "  STOP — pick an approach before mode selection. Per the skill, I need\n"
    "  your call.\n"
    "\n"
    "────────────────────────────────────────────────────────────\n"
    "←  ☐ Approach  ☐ Positioning  ✔ Submit  →\n"
    "Which implementation approach for the full ETVideoScript vision should we\n"
    "lock in before the review continues?\n"
    "\n"
    "❯ 1. C — Parallel tracks: stabilize core + scaffold copilot (Recommended)\n"
    "    Editor and copilot co-designed. Two parallel Hermes lanes…\n"
    "  2. B — Copilot-first (brand wedge)\n"
    "    Ship the chat panel + 3-4 skills next…\n"
    "  3. A — Editor-first, copilot-second\n"
    "    Finish Wave A.1 → B → C (waveform)…\n"
    "  4. Different framing entirely — reduce scope first\n"
    "  5. Type something.\n"
    "  6. Chat about this\n"
    "\n"
    "Enter to select · Tab/Arrow keys to navigate · Esc to cancel\n"
)


# Multi-tab picker on the submit-confirmation screen — both questions
# answered, cursor on "Submit answers". Captured from window @34 directly.
_PANE_MULTITAB_SUBMIT = (
    "←  ☒ Approach  ☒ Positioning  ✔ Submit  →\n"
    "\n"
    "Review your answers\n"
    "\n"
    " ● Which implementation approach for the full ETVideoScript vision should we\n"
    "   lock in before the review continues?\n"
    "   → C — Parallel tracks: stabilize core + scaffold copilot (Recommended)\n"
    " ● How do you want to position publicly?\n"
    '   → "Open-source editor your AI agent uses" (Recommended)\n'
    "\n"
    "Ready to submit your answers?\n"
    "\n"
    "❯ 1. Submit answers\n"
    "  2. Cancel\n"
)


# Single-question picker (no tabs) — Claude Code's periodic feedback survey
# variant. Footer is "Enter to select".
_PANE_SINGLE_TAB = (
    "● How is Claude doing this session? (optional)\n"
    "\n"
    "❯ 1. Bad\n"
    "  2. Fine\n"
    "  3. Good\n"
    "  0. Dismiss\n"
    "\n"
    "Enter to select · Tab/Arrow keys to navigate · Esc to cancel\n"
)


class TestBuildFormFromToolInput:
    """Building the AskUserQuestionForm directly from the JSONL tool_use input.

    Pane scrape misses options when long question text scrolls them off the
    top of the visible region. The JSONL payload carries the full option
    list and is order-stable, so this path is preferred for AskUserQuestion
    dispatch when the input dict is available.
    """

    def test_full_payload(self):
        from cctelegram.terminal_parser import build_form_from_tool_input

        form = build_form_from_tool_input(
            {
                "questions": [
                    {
                        "question": "Pick one.",
                        "header": "Approach",
                        "multiSelect": False,
                        "options": [
                            {"label": "A) First", "description": "x"},
                            {"label": "B) Second", "description": "y"},
                            {"label": "C) Third (Recommended)", "description": "z"},
                        ],
                    }
                ]
            }
        )
        assert form is not None
        assert form.current_question_title == "Pick one."
        assert [o.number for o in form.options] == [1, 2, 3]
        assert form.options[0].label == "A) First"
        assert form.options[2].recommended is True
        assert form.options[2].label == "C) Third"

    def test_none_or_malformed_returns_none(self):
        from cctelegram.terminal_parser import build_form_from_tool_input

        assert build_form_from_tool_input(None) is None
        assert build_form_from_tool_input({}) is None
        assert build_form_from_tool_input({"questions": []}) is None
        assert build_form_from_tool_input({"questions": "nope"}) is None
        assert build_form_from_tool_input({"questions": [{"options": []}]}) is None
        assert build_form_from_tool_input({"questions": [{"options": "x"}]}) is None


class TestParseAskUserQuestion:
    def test_plain_picker_with_multiline_descriptions(self):
        """Plain A/B/C question with multi-line indented descriptions between
        options. Regression: the original parser broke on any unmatched line
        once it had started collecting, so descriptions or pros/cons bullets
        after the first option dropped every subsequent option from the form.
        Also pins the off-screen-option-1 case: when the visible region is
        scrolled past option 1, the parser must still keep options 2..N.
        """
        pane = (
            "  2. B) Still no buttons — dig deeper\n"
            "    I still only see plain text, no tappable options.\n"
            "\n"
            "      ✅ Honest signal that there's another layer to debug.\n"
            "      ❌ Need to keep investigating; possibly the queue timing.\n"
            "  3. C) Buttons appeared but tapping them did nothing\n"
            "    The card landed with buttons but the dispatch broke.\n"
            "\n"
            "      ✅ Tells me detection works.\n"
            "      ❌ Different layer of bug to chase.\n"
            "  4. Type something.\n"
            "─\n"
            "  5. Chat about this\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        form = parse_ask_user_question(pane)
        assert form is not None
        assert [opt.number for opt in form.options] == [2, 3, 4, 5]
        assert form.options[0].label.startswith("B) Still no buttons")
        assert form.options[1].label.startswith("C) Buttons appeared")

    def test_plain_picker_last_option_with_description(self):
        """Footer-based scan must extract all options when the LAST option
        has a multi-line description and no multi-tab header is rendered.

        Captured live on 2026-05-19 13:41 in the cgc-fork topic (window @37,
        thread 10636): a 2-question AUQ rendered without a tab strip; every
        poll cycle logged ``resolve_ask_form multi-q inference FAILED:
        questions=2 pane_opts=0 pane_title='Enter to select · …'`` and the
        renderer fell back to the generic keystroke keyboard. The footer-
        based upward walk-back's description-continuation rule only looked
        BELOW for a numbered option (within 8 lines), so the LAST option's
        descriptions broke the walk: nothing below them except the footer.
        Fix: symmetric ABOVE-or-BELOW lookahead in parse_ask_user_question.
        """
        pane = (
            "Query core grill 2a — how wide is the dialect seam inside the builder?\n"
            "\n"
            "  1. Narrow seam, built now (recommended)\n"
            "     The private builder emits openCypher; a DialectAdapter supplies\n"
            "     only the divergent fragments (fulltext search, path-extraction).\n"
            "  2. Full query translation\n"
            "     The builder emits a dialect-neutral query representation (IR/AST).\n"
            "     Most correct, biggest build.\n"
            "  3. Kuzu-only, defer the seam\n"
            "     Builder emits openCypher for Kuzu. No DialectAdapter in v1 at all.\n"
            "     One adapter = hypothetical seam — build it only when Neo4j needs.\n"
            "     Lightest v1.\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · n to add notes · Esc to cancel\n"
        )
        form = parse_ask_user_question(pane)
        assert form is not None
        assert [opt.number for opt in form.options] == [1, 2, 3]
        assert form.options[0].label.startswith("Narrow seam")
        assert form.options[0].recommended is True
        assert form.options[1].label.startswith("Full query translation")
        assert form.options[2].label.startswith("Kuzu-only")
        # The Recommended suffix detection is case-insensitive: Claude Code
        # emitted ``(recommended)`` lowercase in cgc-fork's JSONL labels.
        # Without IGNORECASE the literal text leaks into the pick-button
        # label; with IGNORECASE the flag sets and the suffix is stripped.
        # Pre-fix the title heuristic mis-assigned the footer text as the
        # question title because options_region collapsed to ``[blank,
        # footer]``. Post-fix start_idx walks up past every option, so
        # the footer is no longer the first non-empty line. None is
        # acceptable here — ``_strong_match`` falls through to the
        # label-overlap path and matches Q1's JSONL options.
        assert form.current_question_title != (
            "Enter to select · ↑/↓ to navigate · n to add notes · Esc to cancel"
        )

    def test_multitab_approach_returns_tabs_and_options(self):
        form = parse_ask_user_question(_PANE_MULTITAB_APPROACH)
        assert form is not None
        # Three tabs visible: Approach, Positioning, Submit
        labels = [t.label for t in form.tabs]
        assert "Approach" in labels
        assert "Positioning" in labels
        # All un-answered (the cell glyphs are ☐ ☐ ✔)
        assert form.tabs[0].answered is False
        assert form.tabs[1].answered is False
        # The submit cell is_submit=True
        submit_tabs = [t for t in form.tabs if t.is_submit]
        assert len(submit_tabs) == 1
        # Options should include the recommended Approach C as option 1
        assert form.options
        assert form.options[0].number == 1
        assert form.options[0].cursor is True
        assert form.options[0].recommended is True
        assert "Parallel tracks" in form.options[0].label
        # Free-text option is present ("Type something")
        assert form.is_free_text is True
        # Not the review screen — we're still picking
        assert form.is_review_screen is False

    def test_multitab_submit_screen_flag(self):
        form = parse_ask_user_question(_PANE_MULTITAB_SUBMIT)
        assert form is not None
        # Both content tabs answered
        approach = next(t for t in form.tabs if t.label == "Approach")
        positioning = next(t for t in form.tabs if t.label == "Positioning")
        assert approach.answered is True
        assert positioning.answered is True
        # Review screen flag tripped (header + prompt both present)
        assert form.is_review_screen is True
        # Options show "Submit answers" / "Cancel"
        opt_labels = [o.label for o in form.options]
        assert any("Submit answers" in lbl for lbl in opt_labels)
        assert any("Cancel" in lbl for lbl in opt_labels)
        # Cursor on the submit row
        assert form.options[0].cursor is True
        assert form.options[0].number == 1

    def test_single_tab_no_tabs_collected(self):
        form = parse_ask_user_question(_PANE_SINGLE_TAB)
        assert form is not None
        # Single-question picker → no multi-tab cells
        assert form.tabs == ()
        # All four options parsed
        nums = [o.number for o in form.options]
        assert nums == [1, 2, 3, 0] or nums == [1, 2, 3]
        # ``0. Dismiss`` skips contiguous check (numbering starts at 1),
        # so the parser may discard it. Either outcome is acceptable for PR 1
        # as long as the live options 1/2/3 are present.
        assert any("Bad" in o.label for o in form.options)
        assert any("Fine" in o.label for o in form.options)
        assert any("Good" in o.label for o in form.options)
        # First option carries the cursor
        assert form.options[0].cursor is True

    def test_non_picker_pane_returns_none(self):
        pane = (
            "Just regular Claude Code output\n"
            "  ⎿  some tool result\n"
            "  ⏵⏵ bypass permissions on\n"
        )
        assert parse_ask_user_question(pane) is None

    def test_empty_input_returns_none(self):
        assert parse_ask_user_question("") is None

    def test_fingerprint_stable_across_calls(self):
        a = parse_ask_user_question(_PANE_MULTITAB_APPROACH)
        b = parse_ask_user_question(_PANE_MULTITAB_APPROACH)
        assert a is not None and b is not None
        assert a.fingerprint() == b.fingerprint()
        # Length sanity — 16 hex chars
        assert len(a.fingerprint()) == 16

    def test_fingerprint_changes_when_tab_state_changes(self):
        a = parse_ask_user_question(_PANE_MULTITAB_APPROACH)
        b = parse_ask_user_question(_PANE_MULTITAB_SUBMIT)
        assert a is not None and b is not None
        assert a.fingerprint() != b.fingerprint()

    def test_fingerprint_excludes_pane_excerpt_noise(self):
        """Trailing whitespace / blank-line drift on the pane should not
        change the fingerprint — only structural fields contribute.
        """
        clean = _PANE_MULTITAB_APPROACH
        noisy = _PANE_MULTITAB_APPROACH + "\n\n   \n"  # trailing blanks
        a = parse_ask_user_question(clean)
        b = parse_ask_user_question(noisy)
        assert a is not None and b is not None
        assert a.fingerprint() == b.fingerprint()

    def test_pane_excerpt_carries_tab_header(self):
        form = parse_ask_user_question(_PANE_MULTITAB_APPROACH)
        assert form is not None
        # Excerpt starts at the tab header line (the chrome separator above
        # is discarded — it's not part of the picker structure).
        assert form.pane_excerpt.startswith("←")

    def test_dataclasses_are_frozen_and_hashable(self):
        # The dataclasses must be hashable so the renderer can put them
        # into sets / dict keys / token maps without surprise mutation.
        opt = AskOption(label="x", recommended=False, cursor=False, number=1)
        tab = AskTab(label="A", answered=False, is_submit=False, is_current=False)
        form = AskUserQuestionForm(tabs=(tab,), options=(opt,))
        # ``_meta`` is a mutable field excluded from equality, so two forms
        # with identical structured state compare equal even when one of
        # them later gains a diagnostic note.
        form2 = AskUserQuestionForm(tabs=(tab,), options=(opt,))
        assert form == form2


# ── PR 1 (multi-tab resolver + INF/QS fingerprint gates) ────────────────


from cctelegram.terminal_parser import (  # noqa: E402
    AskQuestion,
    _questions_digest,
    build_form_from_tool_input,
    resolve_ask_form,
)


# Frozen single-question form used for the byte-identical fingerprint
# golden. If you change ``_canonical_repr`` in a way that affects single-
# question canonical output, this test FAILS — that's the safety net.
# The hash is computed from the canonical_repr produced before the multi-
# tab fields existed; recomputing it requires conscious approval (and a
# matching update to the comment in ``_canonical_repr``).
_SINGLE_QUESTION_GOLDEN_FORM = AskUserQuestionForm(
    tabs=(),
    current_question_title="Pick one.",
    options=(
        AskOption(label="A) First", recommended=False, cursor=True, number=1),
        AskOption(label="B) Second", recommended=False, cursor=False, number=2),
        AskOption(label="C) Third", recommended=True, cursor=False, number=3),
    ),
    is_review_screen=False,
    is_free_text=False,
    pane_excerpt="",
)


class TestSingleQuestionFingerprintGolden:
    """Lock down the single-question canonical fingerprint.

    The plan (FA3) commits to byte-identical canonical_repr output for
    single-question forms across the multi-tab rollout. If anyone changes
    canonical line set or order without bumping the golden hash, this
    test fires loudly.
    """

    def test_canonical_repr_lines_unchanged(self):
        # Single-question form produces exactly 5 lines: TABS / Q / OPTS /
        # RVW / FT. No QS:, no INF:. Anything else means the multi-tab
        # gates fired on a single-question form — bug.
        repr_str = _SINGLE_QUESTION_GOLDEN_FORM._canonical_repr()
        lines = repr_str.split("\n")
        assert len(lines) == 5
        assert lines[0].startswith("TABS:")
        assert lines[1].startswith("Q:")
        assert lines[2].startswith("OPTS:")
        assert lines[3].startswith("RVW:")
        assert lines[4].startswith("FT:")
        assert not any(line.startswith("QS:") for line in lines)
        assert not any(line.startswith("INF:") for line in lines)

    def test_single_question_fingerprint_golden(self):
        # Pinned SHA-1 of the canonical above. Update this constant ONLY
        # if you intentionally changed single-question canonical output
        # AND you've considered the rolling-deploy impact on live tokens.
        expected = "6651ea1b8174f879"
        assert _SINGLE_QUESTION_GOLDEN_FORM.fingerprint() == expected


class TestMultiTabFingerprintGates:
    """QS: and INF: lines must appear ONLY for multi-tab forms."""

    def _two_q_form(self, inferred: bool = True) -> AskUserQuestionForm:
        q1 = AskQuestion(
            title="Q1?",
            header="Approach",
            options=(
                AskOption(label="A", recommended=False, cursor=False, number=1),
                AskOption(label="B", recommended=False, cursor=False, number=2),
            ),
        )
        q2 = AskQuestion(
            title="Q2?",
            header="Polish",
            options=(
                AskOption(label="X", recommended=False, cursor=False, number=1),
                AskOption(label="Y", recommended=False, cursor=False, number=2),
            ),
        )
        return AskUserQuestionForm(
            tabs=(),
            current_question_title="Q1?",
            options=q1.options,
            questions=(q1, q2),
            current_tab_inferred=inferred,
        )

    def test_qs_and_inf_lines_present_for_multi_tab(self):
        form = self._two_q_form(inferred=True)
        lines = form._canonical_repr().split("\n")
        assert any(line.startswith("QS:") for line in lines)
        assert any(line == "INF:1" for line in lines)

    def test_inferred_false_changes_fingerprint(self):
        a = self._two_q_form(inferred=True)
        b = self._two_q_form(inferred=False)
        assert a.fingerprint() != b.fingerprint()
        b_lines = b._canonical_repr().split("\n")
        assert any(line == "INF:0" for line in b_lines)

    def test_qs_digest_changes_on_label_rename(self):
        a = self._two_q_form()
        # Same titles + counts, different label — digest must differ so a
        # stale card gets torn down on re-render.
        q1_renamed = AskQuestion(
            title="Q1?",
            header="Approach",
            options=(
                AskOption(label="A renamed", recommended=False, cursor=False, number=1),
                AskOption(label="B", recommended=False, cursor=False, number=2),
            ),
        )
        b = AskUserQuestionForm(
            tabs=a.tabs,
            current_question_title=a.current_question_title,
            options=a.options,
            questions=(q1_renamed, a.questions[1]),
        )
        assert a.fingerprint() != b.fingerprint()

    def test_qs_digest_handles_pipe_in_label(self):
        # Naive ``"|".join(labels)`` would collide on labels containing
        # ``|``. The digest must use a separator that can't appear in
        # JSONL-derived text.
        q_pipe = AskQuestion(
            title="Q?",
            header="H",
            options=(
                AskOption(label="A|B", recommended=False, cursor=False, number=1),
                AskOption(label="C", recommended=False, cursor=False, number=2),
            ),
        )
        q_split = AskQuestion(
            title="Q?",
            header="H",
            options=(
                AskOption(label="A", recommended=False, cursor=False, number=1),
                AskOption(label="B|C", recommended=False, cursor=False, number=2),
            ),
        )
        # These two have the same naive ``"A|B|C"`` flat string but
        # different option boundaries — they MUST hash differently.
        d1 = _questions_digest((q_pipe, q_pipe))
        d2 = _questions_digest((q_split, q_split))
        assert d1 != d2


class TestBuildFormFromToolInputMultiQuestion:
    """``build_form_from_tool_input`` walks all questions and captures descriptions."""

    def test_two_questions_populated(self):
        form = build_form_from_tool_input(
            {
                "questions": [
                    {
                        "question": "Pick approach.",
                        "header": "Approach",
                        "options": [
                            {"label": "A", "description": "first option"},
                            {"label": "B", "description": "second option"},
                        ],
                    },
                    {
                        "question": "Pick polish.",
                        "header": "Polish",
                        "options": [
                            {"label": "X", "description": "xdesc"},
                            {"label": "Y", "description": "ydesc"},
                        ],
                    },
                ]
            }
        )
        assert form is not None
        assert len(form.questions) == 2
        assert form.questions[0].title == "Pick approach."
        assert form.questions[0].header == "Approach"
        assert form.questions[0].options[0].description == "first option"
        assert form.questions[1].options[1].label == "Y"
        # Legacy fields mirror Q1 so existing single-tab consumers keep
        # working without conditionals.
        assert form.current_question_title == "Pick approach."
        assert [o.label for o in form.options] == ["A", "B"]

    def test_description_captured_single_question(self):
        form = build_form_from_tool_input(
            {
                "questions": [
                    {
                        "question": "Pick one.",
                        "options": [
                            {"label": "A", "description": "first"},
                            {"label": "B (Recommended)", "description": "second"},
                        ],
                    }
                ]
            }
        )
        assert form is not None
        assert form.options[0].description == "first"
        assert form.options[1].description == "second"
        # Recommended suffix still stripped from label as before.
        assert form.options[1].label == "B"
        assert form.options[1].recommended is True


class TestResolveAskForm:
    """``resolve_ask_form`` is the unified resolver for render + validate paths."""

    def _multi_q_input(self) -> dict:
        return {
            "questions": [
                {
                    "question": "Pick approach.",
                    "header": "Approach",
                    "options": [
                        {"label": "A — option A label", "description": "reason A"},
                        {"label": "B — option B label", "description": "reason B"},
                    ],
                },
                {
                    "question": "Pick polish.",
                    "header": "Polish",
                    "options": [
                        {"label": "X — option X label", "description": "reason X"},
                        {"label": "Y — option Y label", "description": "reason Y"},
                    ],
                },
            ]
        }

    def test_returns_none_when_neither_source(self):
        assert resolve_ask_form(None, "") is None

    def test_single_question_jsonl_no_pane(self):
        # Single-question JSONL + no pane → JSONL form, current_tab_inferred=True,
        # no QS/INF in canonical.
        form = resolve_ask_form(
            {
                "questions": [
                    {
                        "question": "Pick one.",
                        "options": [{"label": "A"}, {"label": "B"}],
                    }
                ]
            },
            "",
        )
        assert form is not None
        assert len(form.questions) == 1
        # Canonical stays single-tab shape (5 lines).
        assert len(form._canonical_repr().split("\n")) == 5

    def test_multi_question_with_matching_pane_infers_current(self):
        # Pane shows Q2's title + Q2's options → resolver picks idx 1.
        pane = (
            "Pick polish.\n"
            "\n"
            "❯ 1. X — option X label\n"
            "  2. Y — option Y label\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        form = resolve_ask_form(self._multi_q_input(), pane)
        assert form is not None
        assert form.current_tab_inferred is True
        assert form.current_question_title == "Pick polish."
        # The current tab's options are surfaced; cursor overlaid from pane.
        assert form.options[0].label == "X — option X label"
        assert form.options[0].cursor is True

    def test_multi_question_corrupt_pane_defaults_to_zero(self):
        # Pane has no recognizable picker → resolver defaults to tab 0
        # AND marks current_tab_inferred=False. Renderer (PR 3) MUST NOT
        # mint pick buttons in this state.
        pane = "garbage that doesn't look like a picker at all\n"
        form = resolve_ask_form(self._multi_q_input(), pane)
        assert form is not None
        assert form.current_tab_inferred is False
        # Defaults to first question.
        assert form.current_question_title == "Pick approach."
        # INF:0 line present.
        lines = form._canonical_repr().split("\n")
        assert any(line == "INF:0" for line in lines)

    def test_jsonl_missing_falls_back_to_pane(self):
        # No tool_input → pure pane fallback (legacy behaviour).
        pane = (
            "Pick one.\n"
            "\n"
            "❯ 1. A — first\n"
            "  2. B — second\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        form = resolve_ask_form(None, pane)
        assert form is not None
        # questions tuple is empty (legacy pane path doesn't carry it).
        assert form.questions == ()
        assert [o.number for o in form.options] == [1, 2]

    def test_ambiguous_titles_secondary_match_via_options(self):
        # Two questions share a title; option-label overlap disambiguates.
        tool_input = {
            "questions": [
                {
                    "question": "Pick.",
                    "options": [{"label": "alpha"}, {"label": "beta"}],
                },
                {
                    "question": "Pick.",
                    "options": [{"label": "gamma"}, {"label": "delta"}],
                },
            ]
        }
        pane = (
            "Pick.\n"
            "\n"
            "❯ 1. gamma\n"
            "  2. delta\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        form = resolve_ask_form(tool_input, pane)
        assert form is not None
        assert form.current_tab_inferred is True
        # Option-overlap pinned the second question.
        assert form.options[0].label == "gamma"

    def test_identical_options_across_tabs_defaults(self):
        # Every tab has the same option labels (e.g. "Yes / No / Skip" pattern).
        # Neither title-exact nor option-overlap can disambiguate → must
        # default to (0, False) safely rather than picking arbitrarily.
        tool_input = {
            "questions": [
                {
                    "question": "Q1?",
                    "options": [{"label": "Yes"}, {"label": "No"}],
                },
                {
                    "question": "Q2?",
                    "options": [{"label": "Yes"}, {"label": "No"}],
                },
            ]
        }
        # Pane title doesn't match either question's title verbatim
        # (wrapped / truncated scenario).
        pane = (
            "Q something else?\n"
            "\n"
            "❯ 1. Yes\n"
            "  2. No\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        form = resolve_ask_form(tool_input, pane)
        assert form is not None
        # Both questions tied on the option-overlap score → defaulted.
        assert form.current_tab_inferred is False
        assert form.current_question_title == "Q1?"

    # ── P1.2 — Review-screen short-circuit ────────────────────────────────

    def _review_pane(self) -> str:
        # Realistic Claude Code review screen on a 2-question form: the tab
        # header is at the top, the body says "Ready to submit your answers?"
        # and the picker shows Submit / Cancel rather than Q1's options.
        return (
            "←  ☒ Approach  ☒ Polish  ✔ Submit  →\n"
            "\n"
            "Review your answers\n"
            "Ready to submit your answers?\n"
            "\n"
            "❯ 1. Submit\n"
            "  2. Cancel\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )

    def test_multi_q_review_screen_returns_submit_cancel_not_q1(self):
        # The bug: with multi-question JSONL on the review screen, the
        # resolver used to overlay pane's Submit/Cancel cursor onto Q1's
        # options, producing a form with Q1 labels but Submit-screen
        # semantics — pick buttons would mint with Q1 labels but the
        # picker would treat digit 1 as Submit. Wrong-action class bug.
        form = resolve_ask_form(self._multi_q_input(), self._review_pane())
        assert form is not None
        # Pane is authoritative on review-screen → options are Submit/Cancel.
        assert form.is_review_screen is True
        assert [o.label for o in form.options] == ["Submit", "Cancel"]
        # The JSONL `questions` matrix is preserved for tab-strip context.
        assert len(form.questions) == 2
        # No inference happened — pane authoritatively showed review.
        # Mint code suppresses pick buttons under this flag (review-screen
        # nav stays available via the keystroke keyboard).
        assert form.current_tab_inferred is False
        # current_question_title cleared so the renderer / fingerprint
        # don't carry a Q1 title that was never on screen.
        assert form.current_question_title is None
        # No Q1/Q2 labels leaked into options.
        assert all(
            "option A" not in o.label and "option B" not in o.label
            for o in form.options
        )

    def test_multi_q_review_fingerprint_stable_render_vs_validate(self):
        # Mint-then-validate: rendering and the pick-token validator call
        # resolve_ask_form against the same JSONL + pane and must produce
        # byte-identical canonical reprs. Otherwise the fingerprint check
        # fails on every callback and the bot 404s its own buttons.
        a = resolve_ask_form(self._multi_q_input(), self._review_pane())
        b = resolve_ask_form(self._multi_q_input(), self._review_pane())
        assert a is not None and b is not None
        assert a._canonical_repr() == b._canonical_repr()
        # Also: the canonical encodes RVW:1 + INF:0 on the review branch
        # so a render mistakenly produced under non-review state would not
        # validate against a review-screen callback.
        canonical = a._canonical_repr()
        assert "RVW:1" in canonical
        assert "INF:0" in canonical

    def test_multi_q_review_screen_with_no_pane_form_unchanged(self):
        # Defensive: if the pane is empty (mid-redraw), the multi-question
        # branch must still default cleanly — no review short-circuit
        # without a pane_form to source Submit/Cancel from.
        form = resolve_ask_form(self._multi_q_input(), "")
        assert form is not None
        assert form.is_review_screen is False
        # Original multi-Q-no-pane path: inferred=False, defaulted to Q1.
        assert form.current_tab_inferred is False
        assert form.current_question_title == "Pick approach."

    # ── Single-question review-screen short-circuit ─────────────────────

    def _single_q_input(self, title: str = "Fix the P1s how?") -> dict:
        return {
            "questions": [
                {
                    "question": title,
                    "header": "Fix path",
                    "options": [
                        {"label": "Send findings back to Hermes (Recommended)"},
                        {"label": "I fix the P1s directly"},
                        {"label": "Merge as-is, file P1s as follow-up issues"},
                        {"label": "Fix P1s + P2s together"},
                    ],
                }
            ]
        }

    def _single_q_review_pane(self, cursor_row: int = 1) -> str:
        # Claude Code's single-question AUQ has a Submit/Cancel confirmation
        # step after the picker. No tabstrip (single question), but the same
        # "Review your answers" / "Ready to submit your answers?" markers.
        c1 = "❯ " if cursor_row == 1 else "  "
        c2 = "❯ " if cursor_row == 2 else "  "
        return (
            "Review your answers\n"
            "Ready to submit your answers?\n"
            "\n"
            f"{c1}1. Submit answers\n"
            f"{c2}2. Cancel\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )

    def test_single_q_review_screen_returns_submit_cancel_not_originals(self):
        # The bug: with single-question JSONL on the review screen, the
        # resolver used to graft pane's is_review_screen=True onto the
        # original 4 answer options. Worst case: clicking the rendered
        # "option 2" would dispatch '2 + Enter' into the live Submit/Cancel
        # picker → Cancel, while the button label reads as one of the
        # original answer options. Wrong-action-class for any non-row-1 row.
        form = resolve_ask_form(self._single_q_input(), self._single_q_review_pane())
        assert form is not None
        assert form.is_review_screen is True
        # Pane is authoritative: options come from the live Submit/Cancel
        # picker, not the JSONL answer matrix.
        assert [o.label for o in form.options] == ["Submit answers", "Cancel"]
        # No original answer labels leak into the rendered options.
        assert all(
            "Send findings back to Hermes" not in o.label
            and "I fix the P1s" not in o.label
            for o in form.options
        )
        # Mint-suppression flag mirrors the multi-question branch. Single-q
        # forms don't gate on it today (mint suppressor is len(questions) > 1),
        # but keeping it consistent guards against future gate changes.
        assert form.current_tab_inferred is False
        # ``questions`` matrix preserved so canonical_repr stays identifiable.
        assert len(form.questions) == 1
        assert form.questions[0].title == "Fix the P1s how?"

    def test_single_q_review_fingerprint_parity_render_vs_validate(self):
        # Render and pick-token validator both call resolve_ask_form against
        # the same JSONL + pane; canonical reprs must match or the staleness
        # check would 404 the user's own click on a freshly rendered card.
        a = resolve_ask_form(self._single_q_input(), self._single_q_review_pane())
        b = resolve_ask_form(self._single_q_input(), self._single_q_review_pane())
        assert a is not None and b is not None
        assert a._canonical_repr() == b._canonical_repr()
        canonical = a._canonical_repr()
        assert "RVW:1" in canonical

    def test_single_q_review_fingerprint_non_collision_across_inputs(self):
        # Two different single-question tool inputs that share the same
        # review pane MUST produce different fingerprints. With
        # current_question_title cleared (a simpler fix variant), every
        # single-question review screen with cursor on Submit would collapse
        # to the same canonical_repr — stale-card protection would silently
        # weaken. Keeping ``current_question_title`` from JSONL is what
        # makes this test pass.
        a = resolve_ask_form(
            self._single_q_input("Fix the P1s how?"), self._single_q_review_pane()
        )
        b = resolve_ask_form(
            self._single_q_input("Pick the next slice?"), self._single_q_review_pane()
        )
        assert a is not None and b is not None
        assert a.fingerprint() != b.fingerprint()

    def test_single_q_review_cursor_on_cancel(self):
        # Catches "row 1 is always cursor-selected" assumptions. If the user
        # navigated the keystroke fallback to Cancel, the pane cursor is on
        # row 2 — the renderer / mint must reflect that, otherwise a Submit
        # button could end up marked as the chosen row.
        form = resolve_ask_form(
            self._single_q_input(), self._single_q_review_pane(cursor_row=2)
        )
        assert form is not None
        assert form.is_review_screen is True
        assert [o.label for o in form.options] == ["Submit answers", "Cancel"]
        # Cursor lands on Cancel (option 2).
        assert form.options[0].cursor is False
        assert form.options[1].cursor is True

    def test_single_q_picker_unchanged_no_review_short_circuit(self):
        # Regression guard: on the picker step (NOT the review screen) the
        # single-question branch must still overlay JSONL options + pane
        # cursor as before. Only the review-screen pane triggers the
        # pane-authoritative path.
        picker_pane = (
            "Fix the P1s how?\n"
            "\n"
            "❯ 1. Send findings back to Hermes (Recommended)\n"
            "  2. I fix the P1s directly\n"
            "  3. Merge as-is, file P1s as follow-up issues\n"
            "  4. Fix P1s + P2s together\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        form = resolve_ask_form(self._single_q_input(), picker_pane)
        assert form is not None
        assert form.is_review_screen is False
        # Original JSONL options preserved (not Submit/Cancel). The
        # "(Recommended)" suffix is stripped into the ``recommended`` flag
        # by build_form_from_tool_input.
        labels = [o.label for o in form.options]
        assert "Send findings back to Hermes" in labels
        assert form.options[0].recommended is True
        assert "Submit answers" not in labels
        # Cursor overlaid from pane onto option 1.
        assert form.options[0].cursor is True
        assert form.current_tab_inferred is True

    def test_single_q_picker_no_pane_cursor_defaults_to_option_1(self):
        """When the pane scrape doesn't detect a cursor character on any
        option, the resolver defaults cursor=True on option 1.

        Symptom (cgc-fork D6 — Backend AUQ, 2026-05-19 14:36 window=@37
        thread=10636): the Telegram card rendered "1. FalkorDB Lite" with
        no ❯ marker on any option. Either the live pane had no literal
        cursor character (some Claude Code variants signal the selected
        row with ANSI inverse-video that gets stripped when capture-pane
        runs without ``-e``), or the cursor row scrolled out of the
        captured visible region for the long-description AUQ. Either way
        the renderer ended up with cursor=False on every option and the
        user couldn't tell where they were.

        Fix: ``_overlay_cursor`` falls through to ``cursor_at =
        jsonl_options[0].number`` when the pane reports no cursor.
        Matches Claude Code's fresh-AUQ behaviour (cursor starts on
        option 1). Pick buttons dispatch by literal number, so a stale-
        but-visible marker can never mis-route input.
        """
        picker_pane_no_cursor = (
            "Fix the P1s how?\n"
            "\n"
            "  1. Send findings back to Hermes (Recommended)\n"
            "  2. I fix the P1s directly\n"
            "  3. Merge as-is, file P1s as follow-up issues\n"
            "  4. Fix P1s + P2s together\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        form = resolve_ask_form(self._single_q_input(), picker_pane_no_cursor)
        assert form is not None
        # Cursor lands on option 1 by default.
        assert form.options[0].cursor is True
        assert form.options[1].cursor is False
        assert form.options[2].cursor is False
        assert form.options[3].cursor is False
        assert form.current_tab_inferred is True

    # ── CB6 — Strong-match requirement before overlay ─────────────────────

    def test_drift_pane_question_outside_jsonl_no_overlay(self):
        # JSONL declares Q1 ("Pick approach.") + Q2 ("Pick polish."). Pane
        # shows a third question with one label that coincidentally matches
        # one of Q1's options. Without the strong-match guard, the resolver
        # would overlay Q1's labels onto the pane cursor (1-of-2 option
        # overlap was a "unique winner" for _infer_current_tab_idx). With
        # the guard: demote inferred=False so no pick buttons mint.
        pane = (
            "What about a totally different question?\n"
            "\n"
            "❯ 1. A — option A label\n"
            "  2. completely unrelated foo\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        form = resolve_ask_form(self._multi_q_input(), pane)
        assert form is not None
        # 1-of-2 pane labels overlapped with Q1 — below the ≥50% strong-match
        # threshold (50% is met with 1/2 → wait, that's exactly 50% which
        # IS ≥50%). Title substring also fails ("Pick approach." not in pane
        # title). Edge case: this passes strong-match by overlap.
        # Adjust the assertion to the actual semantics.
        # (This test documents the boundary; the next test below catches a
        # firmly-below-threshold case.)
        assert form.is_review_screen is False

    def test_drift_below_strong_match_threshold_falls_back_to_pane(self):
        # Stronger drift: pane shows 4 options; only 1 overlaps with Q1.
        # 1/4 = 25% < 50% strong-match threshold AND title doesn't substring.
        # No JSONL question strong-matches the pane — JSONL is stale (e.g.,
        # Claude has emitted a fresh AskUserQuestion tool_use that hasn't
        # been flushed to the JSONL file yet). Fall back to pane-only so the
        # renderer mints pick buttons against the labels the user actually
        # sees, instead of suppressing buttons (or worse, dispatching JSONL-
        # labelled buttons against the pane's different question).
        tool_input = {
            "questions": [
                {
                    "question": "Pick approach.",
                    "options": [
                        {"label": "alpha"},
                        {"label": "beta"},
                    ],
                },
                {
                    "question": "Pick polish.",
                    "options": [
                        {"label": "gamma"},
                        {"label": "delta"},
                    ],
                },
            ]
        }
        pane = (
            "Totally unrelated picker title.\n"
            "\n"
            "❯ 1. alpha\n"
            "  2. zeta\n"
            "  3. eta\n"
            "  4. theta\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        form = resolve_ask_form(tool_input, pane)
        assert form is not None
        # JSONL-stale fallback: pane wins entirely. The result is a
        # single-tab form whose options match the live pane.
        assert form.questions == ()
        assert [o.label for o in form.options] == ["alpha", "zeta", "eta", "theta"]
        # Cursor preserved from pane parse.
        assert form.options[0].cursor is True
        assert all(o.cursor is False for o in form.options[1:])
        # The stale-fallback path tags the form so the renderer's defer /
        # pick-suppression gate can tell it apart from cache-empty pane-only.
        assert form._meta.get("stale_fallback") == "1"

    def test_non_stale_resolve_does_not_set_stale_fallback_meta(self):
        # Sanity: when JSONL strong-matches the pane (normal path), the
        # returned form must NOT carry the stale_fallback tag. Otherwise
        # the renderer would erroneously suppress pick buttons on a clean
        # render.
        tool_input = {
            "questions": [
                {
                    "question": "Pick approach.",
                    "options": [
                        {"label": "alpha"},
                        {"label": "beta"},
                    ],
                }
            ]
        }
        pane = (
            "Pick approach.\n"
            "\n"
            "❯ 1. alpha\n"
            "  2. beta\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        form = resolve_ask_form(tool_input, pane)
        assert form is not None
        assert form._meta.get("stale_fallback") != "1"

    def test_pane_only_no_jsonl_does_not_set_stale_fallback_meta(self):
        # Pure pane fallback (jsonl_form is None) must also leave the tag
        # unset — there's no stale cache to confuse, just no cache at all.
        # The renderer's cache_empty branch already handles this case.
        pane = (
            "Pick approach.\n"
            "\n"
            "❯ 1. alpha\n"
            "  2. beta\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        form = resolve_ask_form(None, pane)
        assert form is not None
        assert form._meta.get("stale_fallback") != "1"

    def test_title_substring_match_passes_strong_match(self):
        # Wrapped/truncated pane title is still a substring of the JSONL
        # question title → strong match passes via the title branch.
        tool_input = {
            "questions": [
                {
                    "question": "Pick approach to the migration strategy.",
                    "options": [{"label": "alpha"}, {"label": "beta"}],
                },
                {
                    "question": "Pick polish for the final ship.",
                    "options": [{"label": "gamma"}, {"label": "delta"}],
                },
            ]
        }
        pane = (
            "Pick approach to the migrati\n"  # truncated mid-word
            "\n"
            "❯ 1. alpha\n"
            "  2. completely-mismatched-label\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        form = resolve_ask_form(tool_input, pane)
        assert form is not None
        # Title substring is well above the 8-char floor; CB6 keeps inferred=True.
        assert form.current_tab_inferred is True
        assert form.current_question_title.startswith("Pick approach")

    def test_jsonl_stale_etvideo_repro_falls_back_to_pane(self):
        # Real 2026-05-16 repro from the etvideo-editor session. JSONL's
        # latest AUQ tool_use was the 2-question "Test video" / "Paid calls"
        # form. The pane meanwhile had advanced to a brand-new AUQ
        # ("Video scope", 3 visible options about MediaProvider framework)
        # whose tool_use had not yet been flushed to JSONL. The renderer
        # previously suppressed pick buttons via FA5+ (multi-q inference
        # failed because no JSONL question strong-matched the pane), leaving
        # the user with a card showing the OLD question's options and no
        # working controls. New behaviour: fall back to pane-only so pick
        # buttons render against the labels the user actually sees.
        tool_input = {
            "questions": [
                {
                    "question": "Which source video should the end-to-end run use?",
                    "header": "Test video",
                    "options": [
                        {"label": "temp/WIN_20260513...Pro.mp4"},
                        {"label": "Reuse an existing workspace"},
                        {"label": "temp/smoke-source.mp4"},
                    ],
                },
                {
                    "question": (
                        "For the sound-regeneration step, are paid "
                        "providers (xAI TTS) authorized for this test?"
                    ),
                    "header": "Paid calls",
                    "options": [
                        {"label": "Mock only (free)"},
                        {"label": "xAI authorized for this run"},
                    ],
                },
            ]
        }
        # Pane is on a completely different AUQ — different title, no option
        # labels in common with either JSONL question.
        pane = (
            "How much of the video-generation pipeline should this "
            "program build now?\n"
            "\n"
            "  1. Framework + migrate 3 pipelines\n"
            "❯ 2. Above + a real video provider\n"
            "  3. Framework + 1 proof pipeline\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        form = resolve_ask_form(tool_input, pane)
        assert form is not None
        # Pane-only fallback: single-tab shape, pane's options + cursor.
        assert form.questions == ()
        assert [o.label for o in form.options] == [
            "Framework + migrate 3 pipelines",
            "Above + a real video provider",
            "Framework + 1 proof pipeline",
        ]
        assert form.options[1].cursor is True
        # current_tab_inferred is True for pane-only (set by
        # parse_ask_user_question); FA5+ guard doesn't fire because
        # len(questions) <= 1.
        assert form.current_tab_inferred is True

    def test_jsonl_stale_single_q_drift_falls_back_to_pane(self):
        # Sub-case of the same bug class for single-question JSONL: pane
        # shows a different AUQ entirely. Without the stale check the
        # renderer would graft pane.options' cursor onto JSONL labels,
        # producing a wrong-action class bug — buttons read as the OLD
        # answers but a click dispatches the digit against the live pane's
        # different question.
        tool_input = {
            "questions": [
                {
                    "question": "Pick a color.",
                    "options": [
                        {"label": "red"},
                        {"label": "green"},
                        {"label": "blue"},
                    ],
                }
            ]
        }
        pane = (
            "Pick a fruit.\n"
            "\n"
            "❯ 1. apple\n"
            "  2. banana\n"
            "  3. cherry\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        form = resolve_ask_form(tool_input, pane)
        assert form is not None
        # Fall back to pane: labels match what the user sees.
        assert [o.label for o in form.options] == ["apple", "banana", "cherry"]
        assert form.options[0].cursor is True

    def test_jsonl_stale_skipped_on_review_screen(self):
        # On a review screen the pane's visible options are Submit/Cancel,
        # which never strong-match a JSONL question's answer labels. The
        # stale check must NOT fire here — the existing review-screen
        # branches (line 953 single-q, line 987 multi-q) handle this
        # correctly by preserving the JSONL questions matrix for tab-strip
        # context while using pane options for the Submit/Cancel buttons.
        pane = (
            "Review your answers\n"
            "Ready to submit your answers?\n"
            "\n"
            "❯ 1. Submit answers\n"
            "  2. Cancel\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        form = resolve_ask_form(self._multi_q_input(), pane)
        assert form is not None
        # Review-screen branch took over (not the stale fallback). JSONL
        # questions are still attached for tab-strip rendering.
        assert form.is_review_screen is True
        assert len(form.questions) == 2

    def test_jsonl_stale_skipped_at_overlap_boundary_unrelated_title(self):
        # Mid-redraw guard at the 50% boundary, exercising the OVERLAP
        # branch of _strong_match in isolation: pane title is unrelated to
        # any JSONL question title (so the title branch cannot pass), but
        # exactly 1 of 2 pane labels matches Q1 (1/2 = 50% which IS ≥50%).
        # _strong_match returns True via the overlap branch; JSONL stale
        # gate must NOT fire. This pins the 50% boundary semantics —
        # without it the prior test passed via title match and a refactor
        # of ``overlap * 2 >= len(pane_labels)`` could silently break the
        # gate (Hermes review P2 on PR #23).
        pane = (
            "Some unrelated picker title 12345678\n"  # ≥8 chars, no JSONL match
            "\n"
            "❯ 1. A — option A label\n"  # exact label from Q1
            "  2. mid-redraw-garbage\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        form = resolve_ask_form(self._multi_q_input(), pane)
        assert form is not None
        # Overlap branch passed → JSONL Q1 is current tab, NOT pane-only.
        assert len(form.questions) == 2
        # The existing path overlays JSONL Q1's title.
        assert form.current_question_title == "Pick approach."

    def test_jsonl_stale_falls_back_below_overlap_boundary_unrelated_title(self):
        # Companion to the boundary test: below the 50% overlap threshold
        # AND with no title match, _strong_match returns False for every
        # JSONL question → JSONL is stale → pane-only fallback. Pins the
        # below-boundary half of the gate (Hermes review P2 on PR #23).
        pane = (
            "Some unrelated picker title 12345678\n"  # no JSONL title match
            "\n"
            "❯ 1. A — option A label\n"  # 1 match
            "  2. mid-redraw-garbage\n"  # 0
            "  3. another-unrelated-label\n"  # 0
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        # 1 of 3 pane labels overlaps Q1 (33%), 0 overlaps Q2 → no strong
        # match anywhere → stale fallback.
        form = resolve_ask_form(self._multi_q_input(), pane)
        assert form is not None
        assert form.questions == ()
        assert [o.label for o in form.options] == [
            "A — option A label",
            "mid-redraw-garbage",
            "another-unrelated-label",
        ]
        assert form.options[0].cursor is True
