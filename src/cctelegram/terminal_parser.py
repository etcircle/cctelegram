"""Terminal output parser — detects Claude Code UI elements in pane text.

Parses captured tmux pane content to detect:
  - Interactive UIs (AskUserQuestion, ExitPlanMode, RestoreCheckpoint,
    Settings) via regex-based UIPattern matching with top/bottom
    delimiters.
  - Status line (spinner characters + working text) by scanning from bottom up.

All Claude Code text patterns live here. To support a new UI type or
a changed Claude Code version, edit UI_PATTERNS / STATUS_SPINNERS.

PermissionPrompt and BashApproval detection has been intentionally removed:
the deployment runs Claude Code with ``--dangerously-skip-permissions``
(YOLO mode), so neither prompt ever renders in the pane and the patterns
were dead code wasting capture cycles. ExitPlanMode and AskUserQuestion
remain because they still appear in the JSONL stream as ``tool_use``
events and are also detected via pane scrape as a redundant safety net.

Key functions: is_interactive_ui(), extract_interactive_content(),
parse_status_line(), strip_pane_chrome(), extract_bash_output().
"""

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Final, Literal

logger = logging.getLogger(__name__)


@dataclass
class InteractiveUIContent:
    """Content extracted from an interactive UI."""

    content: str  # The extracted display content
    name: str = ""  # Pattern name that matched (e.g. "AskUserQuestion")


@dataclass(frozen=True)
class UIPattern:
    """A text-marker pair that delimits an interactive UI region.

    Extraction normally scans lines top-down: the first line matching any
    `top` pattern marks the start, the first subsequent line matching any
    `bottom` pattern marks the end. Both boundary lines are included in the
    extracted content. Patterns with ``bottom_up=True`` scan from the live
    footer/bottom marker upward so old scrollback regions cannot shadow the
    currently visible picker.

    ``top`` and ``bottom`` are tuples of compiled regexes — any single match
    is sufficient. This accommodates wording changes across Claude Code
    versions (e.g. a reworded confirmation prompt).
    """

    name: str  # Descriptive label (not used programmatically)
    top: tuple[re.Pattern[str], ...]
    bottom: tuple[re.Pattern[str], ...]
    min_gap: int = 2  # minimum lines between top and bottom (inclusive)
    bottom_up: bool = False  # scan bottom marker first, then matching top upward
    # Additional pre-top-found bail markers. While walking back from
    # ``bottom_idx`` to find a top anchor, encountering any of these is
    # treated as evidence of a stale picker between the current top
    # candidate (above) and the live bottom (below) — bail with None so
    # the next pattern in UI_PATTERNS can try. The existing
    # ``pattern.bottom``-based bail catches the case where the OLDER
    # picker still has its footer intact; this extra list catches the
    # case where Claude Code has collapsed the older picker into a
    # ``… +N lines (ctrl+o to expand)`` placeholder (cga incident,
    # 2026-05-20 13:38:25: multi-tab AUQ #A's tab header at scrollback
    # line 130 combined with AUQ #B's live ``Enter to select`` near line
    # 220 because AUQ #A's footer had been collapsed; the bot rendered
    # AUQ #A's options on the live card).
    bail_markers: tuple[re.Pattern[str], ...] = ()


# Marks a collapsed Claude Code TUI region — Bash output, file reads, or an
# answered/dismissed AskUserQuestion picker. The token appears at the spot the
# original content used to occupy and is rendered on its OWN line with this
# exact shape: ``     … +17 lines (ctrl+o to expand)``. For a LIVE picker, the
# collapse placeholder never appears as a standalone line inside the picker
# region (the user needs to see options to interact). Anchoring the regex
# with ``^`` … ``$`` rejects matches embedded inside model-supplied option
# descriptions (codex P2, 2026-05-20: a description quoting this text would
# otherwise be misread as a stale-picker boundary and bail detection).
_RE_COLLAPSED_REGION = re.compile(
    r"^\s*(?:…|\.\.\.)\s+\+\d+\s+lines?\s+\(ctrl[+-]o\s+to\s+expand\)\s*$"
)


# ── UI pattern definitions (order matters — first match wins) ────────────

UI_PATTERNS: list[UIPattern] = [
    UIPattern(
        name="ExitPlanMode",
        top=(
            re.compile(r"^\s*Would you like to proceed\?"),
            # v2.1.29+: longer prefix that may wrap across lines
            re.compile(r"^\s*Claude has written up a plan"),
        ),
        bottom=(
            re.compile(r"^\s*ctrl-g to edit in "),
            re.compile(r"^\s*Esc to (cancel|exit)"),
        ),
    ),
    UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*←\s+[☐✔☒]"),),  # Multi-tab: no bottom needed
        bottom=(),
        min_gap=1,
        bottom_up=True,
        bail_markers=(_RE_COLLAPSED_REGION,),
    ),
    UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*[☐✔☒]"),),  # Single-tab: bottom required
        bottom=(re.compile(r"^\s*Enter to select"),),
        min_gap=1,
        bottom_up=True,
        bail_markers=(_RE_COLLAPSED_REGION,),
    ),
    # Plain single-select AskUserQuestion (no checkbox glyphs). Claude Code
    # renders simple A/B/C/D questions as numbered options + ``Enter to select``
    # footer, with no leading ☐/✔/☒. The two patterns above only match the
    # multi-select / multi-tab variants. This pattern catches the rest.
    # Top anchor is a numbered option line; the cursor prefix varies across
    # Claude Code versions (❯, ›, ▶, *, ), >) or may be plain indent.
    UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*[❯›▶*)>]?\s*\d+\.\s+\S"),),
        bottom=(re.compile(r"^\s*Enter to select"),),
        min_gap=0,
        bottom_up=True,
        bail_markers=(_RE_COLLAPSED_REGION,),
    ),
    UIPattern(
        name="RestoreCheckpoint",
        top=(re.compile(r"^\s*Restore the code"),),
        bottom=(re.compile(r"^\s*Enter to continue"),),
    ),
    UIPattern(
        name="Settings",
        top=(
            re.compile(r"^\s*Settings:.*tab to cycle"),
            re.compile(r"^\s*Select model"),
        ),
        bottom=(
            re.compile(r"Esc to cancel"),
            re.compile(r"Esc to exit"),
            re.compile(r"Enter to confirm"),
            re.compile(r"^\s*Type to filter"),
        ),
    ),
]


# ── Post-processing ──────────────────────────────────────────────────────

_RE_LONG_DASH = re.compile(r"^─{5,}$")


def _shorten_separators(text: str) -> str:
    """Replace lines of 5+ ─ characters with exactly ─────."""
    return "\n".join(
        "─────" if _RE_LONG_DASH.match(line) else line for line in text.split("\n")
    )


# ── Core extraction ──────────────────────────────────────────────────────


def _try_extract(lines: list[str], pattern: UIPattern) -> InteractiveUIContent | None:
    """Try to extract content matching a single UI pattern.

    When ``pattern.bottom`` is empty, the region extends from the top marker
    to the last non-empty line (used for multi-tab AskUserQuestion where the
    bottom delimiter varies by tab). ``bottom_up`` patterns find the live
    footer/bottom boundary first and then walk backward to the nearest top
    marker, preventing historic scrollback pickers from shadowing the active
    one after larger AUQ captures.
    """
    top_idx: int | None = None
    bottom_idx: int | None = None

    if pattern.bottom_up:
        if pattern.bottom:
            for i in range(len(lines) - 1, -1, -1):
                if any(p.search(lines[i]) for p in pattern.bottom):
                    bottom_idx = i
                    break
        else:
            for i in range(len(lines) - 1, -1, -1):
                if lines[i].strip():
                    bottom_idx = i
                    break
        if bottom_idx is None:
            return None
        found_top = False
        # Walk back from bottom_idx - 1: the bottom line itself can't be
        # the top, and starting one above lets us bail cleanly when we
        # cross an OLDER instance of the bottom marker (which would
        # indicate that the top we're about to find belongs to a stale
        # picker, not the live one).
        for i in range(bottom_idx - 1, -1, -1):
            if any(p.search(lines[i]) for p in pattern.top):
                top_idx = i
                found_top = True
                continue
            if found_top:
                stripped = lines[i].strip()
                if (
                    not stripped
                    or all(c == "─" for c in stripped)
                    or lines[i].startswith((" ", "\t"))
                ):
                    continue
                break
            # Pre-top-found bail: when walking back from the live footer
            # to find a matching top, encountering an OLDER instance of
            # the same bottom marker means there's a complete prior
            # picker between bottom_idx and any candidate top above. The
            # earlier picker's footer is at lines[i]; whatever top we'd
            # find above it belongs to the OLDER picker, not the live
            # one anchored at bottom_idx. Bail so a later pattern in
            # UI_PATTERNS can try (e.g. plain-numbered after
            # single-tab-checkbox). Without this guard, a checkbox AUQ
            # in scrollback above a live plain-numbered AUQ shadowed
            # the live picker — the checkbox pattern walked past the
            # live plain-numbered options to find an old ☐ top.
            if pattern.bottom and any(p.search(lines[i]) for p in pattern.bottom):
                return None
            # Same bail, broader marker set: Claude Code may collapse an
            # OLDER picker's footer into ``… +N lines (ctrl+o to expand)``
            # so the bottom-pattern bail above can't see it. Detecting the
            # collapse placeholder anywhere on the walk-back path closes
            # that gap (cga incident, 2026-05-20 13:38:25).
            if pattern.bail_markers and any(
                p.search(lines[i]) for p in pattern.bail_markers
            ):
                return None
    else:
        for i, line in enumerate(lines):
            if top_idx is None:
                if any(p.search(line) for p in pattern.top):
                    top_idx = i
            elif pattern.bottom and any(p.search(line) for p in pattern.bottom):
                bottom_idx = i
                break

        if top_idx is not None and not pattern.bottom:
            for i in range(len(lines) - 1, top_idx, -1):
                if lines[i].strip():
                    bottom_idx = i
                    break

    if top_idx is None or bottom_idx is None or bottom_idx - top_idx < pattern.min_gap:
        return None

    content = "\n".join(lines[top_idx : bottom_idx + 1]).rstrip()
    return InteractiveUIContent(content=_shorten_separators(content), name=pattern.name)


# ── Public API ───────────────────────────────────────────────────────────


def extract_interactive_content(pane_text: str) -> InteractiveUIContent | None:
    """Extract content from an interactive UI in terminal output.

    Tries each UI pattern in declaration order; first match wins.
    Returns None if no recognizable interactive UI is found.
    """
    if not pane_text:
        return None

    lines = pane_text.strip().split("\n")
    for pattern in UI_PATTERNS:
        result = _try_extract(lines, pattern)
        if result:
            return result
    return None


def is_interactive_ui(pane_text: str) -> bool:
    """Check if terminal currently shows an interactive UI."""
    return extract_interactive_content(pane_text) is not None


# Picker bottom-border markers that anchor on the *visible pane*. When the
# question prose is long enough to push the picker top anchor off the visible
# slice (~50 lines), ``extract_interactive_content`` over the visible pane
# alone returns None even though the picker IS live. The footer/border lives
# at the picker bottom — which always stays on the visible pane — so checking
# the last few visible lines for these markers is a robust "is the picker
# still on screen right now" predicate.
_PICKER_ANCHOR_MARKERS = (
    re.compile(r"Enter to select"),  # AskUserQuestion / RestoreCheckpoint footer
    re.compile(r"Enter to confirm"),  # Settings footer
    re.compile(r"ctrl-g to edit"),  # ExitPlanMode footer
    re.compile(r"Esc to (cancel|exit)"),  # generic dismiss footer
    re.compile(r"╰─"),  # picker frame bottom-left corner
    # Multi-question AUQ Submit-confirmation screen has none of the above
    # — no Enter/Esc footer, no ╰─ border. When the tab header and the
    # "Ready to submit" prompt scroll above the visible bottom 5 lines,
    # only the numbered Submit/Cancel options stay anchored. Match the
    # ``1. Submit answers`` line itself (cursor-aware) and the prompt
    # above it. Without these anchors, the visible-only liveness check
    # returns "absent" on the Submit screen and the card gets cleared
    # mid-AUQ workflow, leaving the user with no way to commit answers.
    re.compile(r"Ready to submit your answers"),
    re.compile(r"^\s*[❯›▶*)>\s]?\s*\d+\.\s+Submit answers\s*$"),
)


def is_picker_anchor_visible(visible_pane: str, *, window_lines: int = 5) -> bool:
    """True when the last ``window_lines`` of ``visible_pane`` contain a
    picker footer/border anchor.

    Used as the CB5 fallback in liveness checks: when ``is_interactive_ui``
    over the visible pane returns False on a long-question case (top
    anchor pushed off screen), this check still returns True if the picker
    footer sits at the visible bottom.
    """
    if not visible_pane:
        return False
    tail = visible_pane.rstrip("\n").split("\n")[-window_lines:]
    return any(p.search(line) for line in tail for p in _PICKER_ANCHOR_MARKERS)


def visible_pane_liveness(visible_pane: str | None) -> str:
    """Three-state liveness predicate over the *visible* tmux pane (no scrollback).

    Returns one of:
      * ``"present"`` — an interactive UI is on screen now. Safe to dispatch
        nav keystrokes; do not destructively clear.
      * ``"absent"`` — no interactive UI on screen. Safe to clear / refresh /
        bail out of nav dispatch.
      * ``"unknown"`` — empty / whitespace-only capture (alt-screen mode,
        tmux redraw race, terminal cleared mid-cycle). MUST NOT be treated
        as absent: a destructive clear here can erase a live picker the
        very next frame brings back.

    Implementation:
      1. Empty/whitespace → ``"unknown"``.
      2. ``is_interactive_ui(visible)`` → ``"present"``.
      3. ``is_picker_anchor_visible(visible)`` → ``"present"`` (CB5 long-
         question fallback — top anchor scrolled off but footer is visible).
      4. Otherwise → ``"absent"``.
    """
    if not visible_pane or not visible_pane.strip():
        return "unknown"
    if is_interactive_ui(visible_pane):
        return "present"
    if is_picker_anchor_visible(visible_pane):
        return "present"
    return "absent"


# ── AskUserQuestion structured parser ───────────────────────────────────
#
# Background: ``extract_interactive_content`` above answers "is there an
# AskUserQuestion picker on screen?" and returns the raw pane region for
# verbatim relay to Telegram. That's enough to surface the picker, but
# leaves the user with arrow-key buttons on a phone — useless for
# multi-tab forms with 4+ options per question.
#
# This parser produces a structured view of the same region so a future
# renderer (PR 2) can build option buttons matched to each tab and
# question, and a callback handler can validate that the form hasn't
# shifted under it before dispatching keystrokes.
#
# Strict-or-``None`` rule, per peer review: any partial / ambiguous /
# mid-redraw parse returns ``None`` so the existing keystroke fallback
# stays in charge. Hermes flagged this as load-bearing.
#
# Anchor lines (multi-tab):  ``^\s*←\s+[☐☒✔]``  (tab header)
# Anchor lines (single-tab): a numbered-options block ending in
#                            ``Enter to select``.
#
# Pane-text is an unstable adapter — Claude Code reworks its TUI between
# versions. The parser is biased toward returning ``None`` rather than
# guessing when markers shift. Fixture coverage in tests is the safety net.


# Matches a tab cell: state glyph (☐ ☒ ✔) followed by optional label.
# The submit cell is sometimes rendered as ``✔`` with no label, sometimes as
# ``✔ Submit``. Both are valid.
_RE_TAB_CELL = re.compile(r"(?P<state>[☐☒✔])\s*(?P<label>[^☐☒✔→]*?)\s*(?=[☐☒✔]|→|$)")

# Matches the multi-tab header line: ``←  ☐ X  ☒ Y  ✔ Submit  →`` (or similar).
# The trailing ``→`` is required so we don't confuse this with a stray ``←``
# in narrative text.
_RE_TAB_HEADER = re.compile(r"^\s*←\s+(?P<body>.*?)\s*→\s*$")

# Matches a numbered option: ``❯ 1. Some option label`` or ``  2. Another``.
# Cursor markers Claude Code uses: ❯, ›, ▶, * .
_RE_NUMBERED_OPTION = re.compile(
    r"^\s*(?P<cursor>[❯›▶*)>↓]?)\s*(?P<num>\d+)\.\s+(?P<label>.+?)\s*$"
)

# Option-row checkbox — ASCII brackets, NOT ☐/☒ (those are tab-header only).
_RE_OPTION_CHECKBOX = re.compile(r"^\s*[❯›▶*)>↓\s]?\s*\d+\.\s+\[(?P<mark>[ ✔xX])\]\s")

# Matches the picker's "Enter to select / Tab / Esc" footer.
_RE_PICKER_FOOTER = re.compile(r"Enter to select")

# Matches the review-screen footer that asks the user to confirm submission.
_RE_REVIEW_HEADER = re.compile(r"^\s*Review your answers\s*$")
_RE_SUBMIT_PROMPT = re.compile(r"^\s*Ready to submit your answers\?\s*$")

# Literal label of the review-screen's "Submit answers" row (always option 1).
# The single source of the literal that the cursor-blind Submit predicate
# (``AskUserQuestionForm.review_submit_dispatchable``) and the mint-site tags
# anchor on, so a relabeled/reordered review layout SAFELY DECLINES.
REVIEW_SUBMIT_LABEL = "Submit answers"

# Matches a free-text "Type something" option (variant where the user can
# type free text instead of picking a numbered option).
_RE_FREE_TEXT_OPTION = re.compile(r"Type something")
_AFFORDANCE_TRAILING_CHARS = " \t\r\n.!?…。:;,，、"


def is_affordance_label(label: str) -> bool:
    """True for Claude Code picker affordances that are not real options."""
    normalized = label.strip().rstrip(_AFFORDANCE_TRAILING_CHARS).strip()
    return (
        bool(_RE_FREE_TEXT_OPTION.fullmatch(normalized))
        or normalized == "Chat about this"
    )


# Matches ``(Recommended)`` suffix on an option label. Case-insensitive
# because Claude Code (and skill prompts) sometimes emit the tag lowercase
# — observed 2026-05-19 in cgc-fork's "Query core grill 2a" AUQ where the
# JSONL labels carried ``(recommended)``. Without IGNORECASE the flag
# never set and the literal text leaked into the pick-button label.
_RE_RECOMMENDED = re.compile(r"\(Recommended\)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class AskOption:
    """One picker option inside an AskUserQuestion form."""

    label: str  # e.g. "C — Parallel tracks: stabilize core + scaffold copilot"
    recommended: bool  # True if "(Recommended)" suffix present
    cursor: bool  # True if this option is the current selection (❯ / › prefix)
    number: int | None  # 1-9 numeric shortcut, or None when not rendered
    # Per-option reasoning text from the JSONL tool_use.input. Empty for
    # pane-only parses (the pane scrape doesn't reliably attribute description
    # lines to specific options). Used by the renderer to inline reasoning
    # under each label. Excluded from the fingerprint canonical (descriptions
    # can vary cosmetically across redraws and shouldn't invalidate tokens).
    description: str = ""
    # Multi-select display state from pane checkbox glyphs. True = [✔]/[x]/[X],
    # False = [ ], None = unknown/off-screen/non-checkbox single-select.
    # Excluded from equality/canonical/fingerprint: toggles must not stale
    # sibling tokens, and off-screen unknown must not collapse to False.
    selected: bool | None = field(default=None, compare=False)


@dataclass(frozen=True)
class AskQuestion:
    """One question inside a multi-question AskUserQuestion form.

    Mirrors the JSONL ``tool_use.input.questions[i]`` shape. ``options`` here
    is the full ordered list from the structured payload — independent of
    pane visibility.
    """

    title: str  # the human-readable question text (``question`` field in JSONL)
    header: str  # short label used for tab cells (``header`` field in JSONL)
    options: tuple[AskOption, ...]
    multi_select: bool = False


@dataclass(frozen=True)
class AskTab:
    """One question-tab in a multi-question AskUserQuestion form."""

    label: str  # e.g. "Approach" — may be empty for the submit cell
    answered: bool  # ☒ filled (question has an answer)
    is_submit: bool  # ✔ marker — the synthetic "Submit" cell
    is_current: bool  # the tab the user is currently viewing


def _questions_digest(questions: tuple["AskQuestion", ...]) -> str:
    """Stable digest over the multi-question matrix for the fingerprint.

    Covers question titles + per-question ordered option labels + option
    counts. A label rename, an option reorder, or a count change all flip
    the digest → ``handle_interactive_ui`` tears down stale cards and
    re-renders. Descriptions are excluded (cosmetic-only redraws shouldn't
    invalidate live tokens). Uses ``\\x1f`` (unit separator) as a delimiter
    that cannot appear in JSONL-derived text — naive ``"|".join`` would
    collide on labels containing ``|``.
    """
    parts: list[str] = []
    for q in questions:
        labels = "\x1f".join(o.label for o in q.options)
        parts.append(f"{q.title}\x1e{len(q.options)}\x1e{labels}")
    payload = "\x1d".join(parts)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


# AUQ PreToolUse hook content-digest surface.
#
# Used by the PreToolUse hook (``hook.py``) to write a content-only
# fingerprint into the AUQ side file, and by the bot's pretool reader
# (``handlers/interactive_ui.py``) as a logging identifier + a self-
# integrity check on the file (recomputed digest must match the stored
# ``input_fingerprint``).
#
# NOT the acceptance criterion. Side-file acceptance is the projection-
# based predicate in ``_record_consistent_with_pane`` (handlers/
# interactive_ui.py) — it compares projected fields, not hashes, so the
# title-skip / multi-tab-subset edge cases each have a principled answer.
#
# Encoding mirrors ``_questions_digest`` so future readers can compare
# the two surfaces side-by-side.
#
# Separator-collision note (codex P2 round 1): the encoding uses
# ASCII unit/record/group separators ``\x1f`` / ``\x1e`` / ``\x1d``.
# JSON string values CAN legally carry these escaped control bytes —
# i.e. ``("A\x1fB", "C")`` and ``("A", "B\x1fC")`` would produce the
# same encoded payload. In practice, AskUserQuestion labels round-
# trip through Claude Code's TUI renderer which strips control bytes,
# so the collision risk is theoretical, not practical. The digest is
# a logging/cache identifier (NOT the side-file acceptance criterion;
# acceptance is the projection predicate in handlers/interactive_ui.py),
# so even a theoretical collision wouldn't cause wrong-action dispatch.
def questions_content_digest(
    pairs: tuple[tuple[str, tuple[str, ...]], ...],
) -> str:
    """Content-only digest over ordered (question_title, option_labels) pairs."""
    parts: list[str] = []
    for title, labels in pairs:
        joined = "\x1f".join(labels)
        parts.append(f"{title}\x1e{len(labels)}\x1e{joined}")
    payload = "\x1d".join(parts)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def questions_content_pairs_from_tool_input(
    tool_input: Any,
) -> tuple[tuple[str, tuple[str, ...]], ...] | None:
    """Extract content pairs from a JSONL/hook AskUserQuestion ``tool_input``.

    Shape expected: ``{"questions": [{"question": str, "options":
    [{"label": str, "description": str?}, ...]}, ...]}``. Required keys
    (``question`` on each question, ``options`` array on each question,
    ``label`` on each option) must be present AND well-typed; missing
    keys are treated as shape errors, not silently coerced to empty
    strings (codex P2 round 1: tightened to match the docstring contract).
    Returns ``None`` on any shape mismatch.
    """
    if not isinstance(tool_input, dict):
        return None
    raw_questions = tool_input.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        return None
    pairs: list[tuple[str, tuple[str, ...]]] = []
    for q in raw_questions:
        if not isinstance(q, dict):
            return None
        if "question" not in q or not isinstance(q["question"], str):
            return None
        title = q["question"]
        if "options" not in q or not isinstance(q["options"], list):
            return None
        labels: list[str] = []
        for o in q["options"]:
            if not isinstance(o, dict):
                return None
            if "label" not in o or not isinstance(o["label"], str):
                return None
            labels.append(o["label"])
        pairs.append((title, tuple(labels)))
    return tuple(pairs)


def questions_content_pairs_from_form(
    form: "AskUserQuestionForm",
) -> tuple[tuple[str, tuple[str, ...]], ...] | None:
    """Extract content pairs from a parsed ``AskUserQuestionForm``.

    For multi-question forms (``form.questions`` non-empty — set by
    ``resolve_ask_form`` when JSONL is available), emits one pair per
    question.

    For single-question forms (the pane-only parse case, which is what
    the PreToolUse-hook reader sees pre-JSONL), uses
    ``form.current_question_title`` (or empty string if missing) plus
    ``form.options[].label``.

    Returns ``None`` when the form carries no visible options at all.
    """
    if form.questions:
        pairs: list[tuple[str, tuple[str, ...]]] = []
        for q in form.questions:
            pairs.append((q.title, tuple(o.label for o in q.options)))
        return tuple(pairs) if pairs else None
    if not form.options:
        return None
    title = form.current_question_title or ""
    return ((title, tuple(o.label for o in form.options)),)


@dataclass(frozen=True)
class AskUserQuestionForm:
    """Structured snapshot of the AskUserQuestion picker visible in a pane.

    The shape covers three Claude Code variants:

    1. Single-question, numbered options: ``tabs == ()``, ``options`` is
       populated. Footer is ``Enter to select``.
    2. Multi-tab form mid-navigation: ``tabs`` populated, ``options`` is the
       set visible under the current tab.
    3. Multi-tab form on the review screen: ``is_review_screen == True``.
       ``options`` may still be populated with the two submit/cancel rows.

    A parse always carries the raw pane excerpt for verbatim fallback
    rendering. The ``fingerprint`` method gives a stable hash over the
    structured fields so callbacks can verify the form hasn't shifted
    between display and dispatch.
    """

    tabs: tuple[AskTab, ...] = ()
    current_question_title: str | None = None
    options: tuple[AskOption, ...] = ()
    is_review_screen: bool = False
    is_free_text: bool = False
    pane_excerpt: str = ""
    # Multi-question matrix from the JSONL ``tool_use.input.questions`` list.
    # Empty for single-question forms (the existing ``options`` / ``current_question_title``
    # fields carry the same data and the renderer / fingerprint stay on the
    # single-tab path). Populated by ``resolve_ask_form`` when JSONL carries
    # ``len(questions) > 1``.
    questions: tuple[AskQuestion, ...] = ()
    # True when ``current_tab_idx`` was successfully matched from pane content
    # against the JSONL questions matrix. False means the resolver fell through
    # to ``current_tab_idx = 0`` because neither title-match nor option-overlap
    # could pin a tab — typically a corrupt or scrolled-back pane. When False,
    # the renderer MUST NOT mint option-pick buttons (FA5+ safety rule): the
    # pane parse and JSONL render share the same defaulted state, fingerprint
    # parity would pass, and dispatching a digit could answer the wrong tab.
    current_tab_inferred: bool = True
    select_mode: Literal["single", "multi", "unknown"] = "single"
    # Source-of-truth fields used in fingerprinting are above this line.
    # Anything appended below MUST be excluded from ``_canonical_repr`` so
    # adding diagnostic state doesn't break callback tokens minted by
    # earlier renders.
    _meta: dict[str, str] = field(default_factory=dict, compare=False)
    # Display-only question title captured from the pane walk-back when no
    # JSONL data is available. Populated by ``parse_ask_user_question``
    # only — ``resolve_ask_form`` does NOT propagate this through its
    # merged-form constructors because every JSONL overlay path already
    # has the authoritative title in ``current_question_title``. The
    # renderer reads ``current_question_title or pane_walkback_title``
    # so a fresh single-tab picker (before Claude Code flushes the AUQ
    # ``tool_use`` line to JSONL) still gets a header in Telegram.
    # MUST NOT be used by ``_strong_match`` or any other identity check:
    # the walk-back can capture assistant prose or stale scrollback as a
    # title (hermes review 2026-05-21), and substring-matching that
    # against a JSONL question would mis-overlay stale labels onto a
    # live pane (wrong-action class bug).
    pane_walkback_title: str | None = field(default=None, compare=False)
    options_complete: bool = field(default=False, compare=False)

    def _canonical_repr(self) -> str:
        """Stable string form used by ``fingerprint``.

        Excludes ``pane_excerpt`` (carries cursor noise and re-flows on
        redraw) and ``_meta`` (diagnostic). Order is fixed; if you add a
        field that should influence callback freshness, append a new line
        here — don't reorder existing ones.

        Single-question forms (``len(questions) <= 1``) produce the exact
        5-line canonical that pre-multi-tab code did, so callback tokens
        minted against single-question forms keep validating across the
        deploy that introduces ``questions`` / ``current_tab_inferred``.
        The ``QS:`` and ``INF:`` lines only appear for multi-tab forms,
        where there is no live single-question token to invalidate.

        The per-option canonical is **cursor-blind** on every screen
        (review and non-review): on Claude Code v2.1.167 dispatch is a
        bare digit (the option IS the digit, cursor-independent), so the
        terminal cursor ``❯`` position must NOT feed the form identity —
        a cursor move would otherwise rotate the pick token and pop a
        still-live card (peek_none / stale_form). The ``RVW:`` line, not
        the cursor, distinguishes review from non-review forms.
        """
        tabs_str = "|".join(
            f"{t.label}:{'A' if t.answered else 'E'}"
            f":{'C' if t.is_current else '_'}"
            f":{'S' if t.is_submit else '_'}"
            for t in self.tabs
        )
        opts_str = "|".join(
            f"{o.number}:{o.label}:{'R' if o.recommended else '_'}"
            for o in self.options
        )
        lines = [
            f"TABS:{tabs_str}",
            f"Q:{self.current_question_title or ''}",
            f"OPTS:{opts_str}",
            f"RVW:{'1' if self.is_review_screen else '0'}",
            f"FT:{'1' if self.is_free_text else '0'}",
        ]
        if self.select_mode != "single":
            lines.append(f"SEL:{self.select_mode}")
        if len(self.questions) > 1:
            lines.append(f"QS:{_questions_digest(self.questions)}")
            lines.append(f"INF:{'1' if self.current_tab_inferred else '0'}")
        return "\n".join(lines)

    def options_contiguous_from_one(self) -> bool:
        """True when visible option numbers are exactly 1..len(options)."""
        if not self.options:
            return False
        return [o.number for o in self.options] == list(range(1, len(self.options) + 1))

    def fingerprint(self) -> str:
        """Stable 16-char hex digest over the structured form state.

        Used by the (PR 2) renderer to mint callback tokens. On click, the
        handler reparses the pane and compares fingerprints — a mismatch
        means the form changed under us (user navigated, skill advanced,
        Claude Code redrew) and the click must not be dispatched verbatim.
        """
        return hashlib.sha1(self._canonical_repr().encode()).hexdigest()[:16]

    def review_submit_dispatchable(self, option_label: str) -> bool:
        """True iff this is a review screen whose Submit row (option 1) is the literal
        REVIEW_SUBMIT_LABEL AND still matches the minted option_label — CURSOR-BLIND.
        The digit dispatch activates Submit regardless of the terminal cursor (verified
        on Claude Code v2.1.161), so the guard no longer requires the cursor on Submit;
        is_review_screen + option#1 + literal label + minted-label anchors mean a
        non-review screen, a relabeled Submit, or a reordered review layout all SAFELY
        DECLINE (never a wrong dispatch)."""
        return bool(
            self.is_review_screen
            and self.options
            and self.options[0].number == 1
            and self.options[0].label == REVIEW_SUBMIT_LABEL
            and self.options[0].label == option_label
        )


def _parse_tab_header(line: str) -> tuple[AskTab, ...] | None:
    """Parse ``←  ☐ X  ☒ Y  ✔ Submit  →`` into a tuple of ``AskTab``.

    Returns ``None`` if the line doesn't look like a tab header. Empty tab
    list is treated as a parse failure too — a header with no cells is
    indistinguishable from noise.
    """
    m = _RE_TAB_HEADER.match(line)
    if m is None:
        return None
    body = m.group("body")
    cells: list[AskTab] = []
    # _RE_TAB_CELL uses a lookahead so cells are matched left-to-right with
    # no consumption past the next state glyph. ``finditer`` walks the body
    # in order.
    for cm in _RE_TAB_CELL.finditer(body):
        state = cm.group("state")
        label = cm.group("label").rstrip(":").strip()
        cells.append(
            AskTab(
                label=label,
                answered=state == "☒",
                is_submit=state == "✔",
                # ``is_current`` is reconstructed later — the header line
                # alone doesn't say which tab is being viewed (Claude Code
                # marks the current tab by what's rendered below the
                # header, not by the cell glyph).
                is_current=False,
            )
        )
    if not cells:
        return None
    return tuple(cells)


def _checkbox_selected_from_line(line: str) -> bool | None:
    """Return checkbox selected state for an option row, or None if absent."""
    match = _RE_OPTION_CHECKBOX.match(line)
    if match is None:
        return None
    mark = match.group("mark")
    return mark in ("✔", "x", "X")


def _strip_option_checkbox(label: str) -> str:
    """Remove a leading ``[ ]`` / ``[✔]`` checkbox from a parsed option label."""
    return re.sub(r"^\[[ ✔xX]\]\s+", "", label, count=1)


def _normalize_pick_label(label: str) -> str:
    """Canonicalize an option label for the cursor-landing verify compare.

    Lowercase, collapse internal whitespace runs to a single space, strip a
    leading checkbox glyph (``[ ]`` / ``[x]`` / ``[X]`` / ``[✔]``, trailing
    whitespace OPTIONAL so ``[✔]Foo`` normalizes the same as ``[✔] Foo``) and a
    trailing ``(recommended)`` suffix (case-insensitive), then edge-strip. The
    live pane label and the minted label go through the SAME normalization so a
    checkbox redraw, a recommended tag, or trailing whitespace never spuriously
    fails the confirm. The checkbox strip is done locally (not via the shared
    ``_strip_option_checkbox``, whose required trailing whitespace other callers
    depend on) so the no-space ``[✔]Foo`` case strips too.
    """
    stripped = re.sub(r"^\[[ xX✔]\]\s*", "", label.strip(), count=1)
    stripped = re.sub(r"\(recommended\)\s*$", "", stripped, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", stripped).strip().lower()


def _loose_label_match(live: str, minted: str) -> bool:
    """True iff the live cursor's label is the minted label (truncation-tolerant).

    Both sides are normalized via ``_normalize_pick_label``. An empty normalized
    side is rejected (an empty match would accept anything — a wrong-option commit
    hazard). This is the cursor-landing sanity guard alongside the NUMBER +
    FINGERPRINT checks, so it tolerates the .168 picker clipping long option text
    (the minted token may carry the full label while the pane clips the live one),
    while still rejecting an unrelated option.

    Accepts iff (both non-empty) the normalized strings are EQUAL, or the live
    label is a string PREFIX of the minted label (``minted.startswith(live)`` — the
    pane truncated a longer option). This rejects semantic extension
    (live ``"Approve with conditions"`` vs minted ``"Approve"`` → False) and accepts
    truncation (live ``"Approve with cond"`` vs minted ``"Approve with conditions"``
    → True). The asymmetry is deliberate: only the LIVE side is ever clipped by the
    terminal, so the minted label is never the truncated one.
    """
    nl = _normalize_pick_label(live)
    nm = _normalize_pick_label(minted)
    if not nl or not nm:
        return False
    return nl == nm or nm.startswith(nl)


# Raw-pane markers that prove an AskUserQuestion picker / review screen is up.
# Used by the v2.1.168 confirm step to distinguish "picker still rendered but
# unparseable" (AMBIGUOUS — never record ``dispatched``) from "picker positively
# gone" (the tool resolved). Footer phrases + the review-screen headers.
_PICKER_MARKERS: Final[tuple[str, ...]] = (
    "to select",
    "to navigate",
    "to cancel",
    "Review your answers",
    "Ready to submit",
)

# A numbered-option row carrying a real selection cursor glyph (``❯``/``›``/``▶``).
# This is the cursor-glyph fallback for ``_pane_looks_like_picker``: a still-live
# picker whose footer/header markers are scrolled off / truncated / outside the
# captured slice can still be proven up by a cursor-led numbered option. Restricted
# to the genuine cursor glyphs — ``↓`` is the scroll indicator and ``*``/``>``/``)``
# are noise, so they are deliberately excluded.
_RE_PICKER_CURSOR_ROW = re.compile(r"^\s*[❯›▶]\s*\d+\.\s")


def _pane_looks_like_picker(pane: str) -> bool:
    """True iff the raw pane text carries any AskUserQuestion picker marker.

    A coarse raw-text scan (no parse) for the footer phrases and review-screen
    headers an AUQ picker always renders, OR a numbered-option line carrying a
    real selection cursor glyph (``❯``/``›``/``▶`` via ``_RE_PICKER_CURSOR_ROW``)
    — the cursor-glyph fallback covers a still-live picker whose footer/header
    markers are scrolled off / truncated / outside the captured slice. The
    confirm step uses it as the tie-breaker when ``resolve_ask_form`` returns
    None: a match means the picker is still up but the parse failed (AMBIGUOUS →
    ``commit_unconfirmed``, never ``dispatched``); no match means the picker
    positively disappeared (the tool resolved).
    """
    if any(marker in pane for marker in _PICKER_MARKERS):
        return True
    return any(_RE_PICKER_CURSOR_ROW.match(line) for line in pane.splitlines())


def _pane_glyph_signal(lines: list[str]) -> Literal["single", "multi", "unknown"]:
    """Classify pane option rows by checkbox glyph presence."""
    option_rows = []
    for line in lines:
        match = _RE_NUMBERED_OPTION.match(line)
        if match is None:
            continue
        label = _strip_option_checkbox(match.group("label").strip())
        if is_affordance_label(label):
            continue
        option_rows.append(line)
    if not option_rows:
        return "unknown"
    with_checkbox = sum(1 for line in option_rows if _RE_OPTION_CHECKBOX.match(line))
    if with_checkbox == len(option_rows):
        return "multi"
    if with_checkbox == 0:
        return "single"
    return "unknown"


_WARNED_MALFORMED_MULTISELECT = False


def _warn_malformed_multiselect_once() -> None:
    """Warn once for malformed JSONL ``multiSelect`` values."""
    global _WARNED_MALFORMED_MULTISELECT  # noqa: PLW0603
    if _WARNED_MALFORMED_MULTISELECT:
        return
    _WARNED_MALFORMED_MULTISELECT = True
    logger.warning("AskUserQuestion multiSelect must be boolean when present")


def _tool_input_select_mode(
    tool_input: dict[str, Any],
) -> Literal["single", "multi", "unknown"]:
    """Resolve JSONL/side-file select mode from question.multiSelect fields."""
    questions = tool_input.get("questions")
    if not isinstance(questions, list):
        return "single"
    saw_multi = False
    for question in questions:
        if not isinstance(question, dict) or "multiSelect" not in question:
            continue
        value = question.get("multiSelect")
        if not isinstance(value, bool):
            _warn_malformed_multiselect_once()
            return "unknown"
        saw_multi = saw_multi or value
    return "multi" if saw_multi else "single"


def _resolve_select_mode(
    source_mode: Literal["single", "multi", "unknown"] | None,
    pane_signal: Literal["single", "multi", "unknown"],
    *,
    is_review_screen: bool,
) -> Literal["single", "multi", "unknown"]:
    """Apply the PR-B source-vs-pane select-mode decision table."""
    if is_review_screen:
        return "single"
    if source_mode == "unknown" or pane_signal == "unknown":
        return "unknown" if source_mode is not None else pane_signal
    if source_mode is None:
        return pane_signal
    if source_mode != pane_signal:
        return "unknown"
    return source_mode


def _parse_numbered_options(lines: list[str]) -> tuple[AskOption, ...]:
    """Walk lines top-down collecting consecutive numbered options.

    Stops at the first non-option, non-blank line so a description line
    following an option doesn't get folded into the next option's label.
    Returns ``()`` if no numbered options are found or numbering has a
    gap (a gap usually means we're mid-redraw — caller should treat as a
    parse failure).
    """
    options: list[AskOption] = []
    # True when the live cursor ``❯`` is parked on a free-text affordance row
    # ("Type something" / "Chat about this"). Affordances ALWAYS trail the real
    # options, so an affordance cursor is the bottom-most ``❯`` on screen — i.e.
    # the live one — which means every ``❯`` on a real option above it is stale
    # scrollback. We track this so the bottom-most-cursor dedup below can clear
    # the surviving stale real-option cursor instead of painting a phantom.
    affordance_cursor_seen = False
    for line in lines:
        m = _RE_NUMBERED_OPTION.match(line)
        if m is None:
            if options:
                stripped = line.strip()
                if not stripped:
                    continue
                # Picker footer ends the option block.
                if _RE_PICKER_FOOTER.search(line) or _RE_TAB_HEADER.match(line):
                    break
                # Anything else (description text, separator runs, pros/cons
                # bullets) is treated as continuation of the previous option
                # and silently skipped. Earlier the loop broke on any
                # non-numbered line, which dropped every option past the
                # first when Claude Code rendered multi-line descriptions.
                continue
            continue
        try:
            num = int(m.group("num"))
        except ValueError:
            return ()
        label = m.group("label").strip()
        selected = _checkbox_selected_from_line(line)
        label = _strip_option_checkbox(label)
        # Free-text affordances ("Type something", "Chat about this") render as
        # numbered rows in the TUI but are NOT real picker options. The
        # side-file source and the pane-signal classifiers (``_pane_glyph_signal``,
        # ``auq_source._record_consistent_with_pane``) already exclude them, so
        # including them here gave a pure-pane parse N+1 options vs the side
        # file's N → fingerprint mismatch → silent toggle reject. Skip them so a
        # render→tap source flip keeps the fingerprint stable. Affordances always
        # trail the real options, so skipping them preserves the 1-based numbering
        # and the contiguity guard below stays satisfied. We still note when the
        # live cursor sits on a (dropped) affordance so the dedup below doesn't
        # promote a stale scrollback cursor on a real option to "live".
        if is_affordance_label(label):
            if m.group("cursor").strip() in ("❯", "›", "▶", "*"):
                affordance_cursor_seen = True
            continue
        # ``↓`` is the picker's scroll-more indicator, NOT a selection cursor.
        # Claude Code paints it at the left edge of the top visible option when
        # earlier options have scrolled off the viewport. Empirically (live
        # ``tmux capture-pane -S -500`` of a scrolled picker): the real ``❯``
        # cursor sits in the frozen scrollback rows while the live viewport's
        # top row carries ``↓``. It stays in ``_RE_NUMBERED_OPTION``'s cursor
        # char-class so the row still parses as an option, but it must not set
        # ``cursor`` — doing so painted a phantom ❯ on the scroll-boundary row.
        cursor = m.group("cursor").strip() in ("❯", "›", "▶", "*")
        recommended = bool(_RE_RECOMMENDED.search(label))
        if recommended:
            label = _RE_RECOMMENDED.sub("", label).rstrip()
        options.append(
            AskOption(
                label=label,
                recommended=recommended,
                cursor=cursor,
                number=num,
                selected=selected,
            )
        )
    # Contiguity guard: keep only the longest monotonic +1 prefix starting at
    # whichever number the first option uses. The pane's visible region can
    # scroll past option 1 (questions with long descriptions push earlier
    # options off the top), so anchoring strictly at 1 dropped the entire
    # block. Trailing special rows like ``0. Dismiss`` (Claude Code's feedback
    # survey) still break the numeric run and get dropped from the structured
    # view; the keystroke fallback (Enter/digit keys) still reaches them.
    if not options or options[0].number is None:
        return ()
    kept: list[AskOption] = []
    expected: int = options[0].number
    for opt in options:
        if opt.number != expected:
            break
        kept.append(opt)
        expected += 1
    # Bottom-most-cursor dedup. Claude Code can leave MORE than one ``❯`` in a
    # captured pane, from two sources that the renderer must collapse to a
    # single live cursor:
    #
    #   1. Stale scrollback — a ``tmux capture-pane -S -<n>`` of a SCROLLED
    #      picker retains the pre-scroll top rows, INCLUDING a frozen ``❯`` on
    #      whatever option was the cursor before the viewport scrolled. (Long
    #      AUQs need the ``-S`` capture so off-screen options are recovered.)
    #   2. Decorative Recommended marker — older Claude Code TUIs painted a
    #      second ``❯`` on the ``(Recommended)`` row as well as the live cursor
    #      row (this no longer occurs in Claude Code v2.1.x, which puts the
    #      recommendation on a description line and never decorates with ``❯``).
    #
    # In BOTH cases the spurious ``❯`` is physically ABOVE the live cursor row:
    # scrollback history sits above the live viewport, and the Recommended row
    # is reordered to the top. So the live cursor is unambiguously the
    # BOTTOM-MOST ``❯`` (closest to the footer). When >1 cursor survives, keep
    # only the last and clear the rest; this also satisfies the "≥1 cursor
    # visible" renderer invariant (we never clear the sole survivor).
    #
    # This MUST run as the final cursor authority — an earlier recommended-only
    # dedup would strip the live cursor when it lands on a Recommended option
    # below a stale scrollback ``❯`` (reported the card as frozen on option 1).
    # Validated against live 80x24 captures at cursor positions 1-5 (both nav
    # directions) and the legacy Bug-C dual-cursor / restore cases, which all
    # resolve to the bottom-most ``❯``.
    cursor_idxs = [i for i, o in enumerate(kept) if o.cursor]
    # When the live cursor is on a (dropped) trailing affordance, every real
    # option ``❯`` is stale scrollback above it — clear them all so no real
    # option is mislabelled as the cursor. Otherwise keep only the bottom-most
    # real-option ``❯`` (the live cursor) and clear the stale ones above it.
    if affordance_cursor_seen:
        clear_idxs = list(cursor_idxs)
    elif len(cursor_idxs) > 1:
        clear_idxs = cursor_idxs[:-1]
    else:
        clear_idxs = []
    for i in clear_idxs:
        opt = kept[i]
        kept[i] = AskOption(
            label=opt.label,
            recommended=opt.recommended,
            cursor=False,
            number=opt.number,
            description=opt.description,
            selected=opt.selected,
        )
    return tuple(kept)


def _parse_question_options(options_input: Any) -> tuple[AskOption, ...]:
    """Build a tuple of ``AskOption`` from one JSONL ``question.options`` list.

    Skips entries that aren't strings or dicts, and drops entries whose label
    is empty. The returned tuple preserves source order; ``number`` is the
    1-based index. ``description`` carries the per-option reasoning text
    when the JSONL payload provides it; ``""`` otherwise.
    """
    if not isinstance(options_input, list):
        return ()
    options: list[AskOption] = []
    for idx, opt in enumerate(options_input, start=1):
        if isinstance(opt, str):
            label, description = opt, ""
        elif isinstance(opt, dict):
            raw_label = opt.get("label")
            label = raw_label if isinstance(raw_label, str) else ""
            raw_desc = opt.get("description")
            description = raw_desc if isinstance(raw_desc, str) else ""
        else:
            continue
        label = label.strip()
        if not label:
            continue
        recommended = bool(_RE_RECOMMENDED.search(label))
        if recommended:
            label = _RE_RECOMMENDED.sub("", label).rstrip()
        options.append(
            AskOption(
                label=label,
                recommended=recommended,
                cursor=False,
                number=idx,
                description=description.strip(),
                selected=None,
            )
        )
    return tuple(options)


def build_form_from_tool_input(
    tool_input: dict[str, Any] | None,
) -> AskUserQuestionForm | None:
    """Build an ``AskUserQuestionForm`` directly from a JSONL ``tool_use`` input.

    The tmux pane scrape captures only the visible region, so long question
    text pushes earlier options off the top of the screen — the user sees
    options 2..N and option 1 is gone. The structured ``tool_use.input`` in
    the session JSONL carries the complete option list and is order-stable.
    Prefer this over ``parse_ask_user_question`` for AskUserQuestion dispatch
    when the input dict is available.

    Returns ``None`` when the input is missing, malformed, or contains no
    parseable options. Callers should fall back to the pane parser.

    The structured payload Claude Code emits for AskUserQuestion is shaped:

        {
          "questions": [
            {"question": "...", "header": "...", "multiSelect": false,
             "options": [{"label": "...", "description": "..."}, ...]},
            ...
          ]
        }

    Multi-question forms populate ``form.questions`` with the full matrix.
    The legacy single-question fields (``current_question_title``, ``options``)
    mirror ``questions[0]`` so the existing renderer + fingerprint paths
    keep working without conditionals at every call site — ``resolve_ask_form``
    overlays the correct current-tab focus on top for multi-question forms.

    The picker UI also appends a "Type something" / "Chat about this" pair
    at the bottom — those are picker-internal and not part of the tool_use
    payload. We mint pick buttons only for the structured options; the
    keystroke fallback still reaches the picker-internal entries.
    """
    if not isinstance(tool_input, dict):
        return None
    questions_raw = tool_input.get("questions")
    if not isinstance(questions_raw, list) or not questions_raw:
        return None

    parsed_questions: list[AskQuestion] = []
    multiselect_present = any(
        isinstance(q, dict) and "multiSelect" in q for q in questions_raw
    )
    select_mode = _tool_input_select_mode(tool_input)
    for q in questions_raw:
        if not isinstance(q, dict):
            continue
        title = q.get("question") or q.get("header") or ""
        header = q.get("header") or ""
        options = _parse_question_options(q.get("options"))
        if not options:
            # A question without parseable options is dropped — same as v1
            # behaviour for the single-question case. The render still
            # surfaces the other tabs; an empty tab would just produce a
            # body with no actionable options.
            continue
        parsed_questions.append(
            AskQuestion(
                title=title.strip() if isinstance(title, str) else "",
                header=header.strip() if isinstance(header, str) else "",
                options=options,
                multi_select=q.get("multiSelect") is True,
            )
        )

    if not parsed_questions:
        return None

    first = parsed_questions[0]
    return AskUserQuestionForm(
        tabs=(),
        current_question_title=first.title or None,
        options=first.options,
        is_review_screen=False,
        is_free_text=False,
        pane_excerpt="",
        questions=tuple(parsed_questions),
        # No pane context here — defer to ``resolve_ask_form`` to decide
        # whether the current tab can be inferred. When this helper is
        # called in isolation (tests, legacy single-question callers),
        # default to True for back-compat with the single-question render
        # path (which never gates on this flag).
        current_tab_inferred=True,
        select_mode=select_mode,
        options_complete=True,
        _meta={"multiselect_present": "1" if multiselect_present else "0"},
    )


def parse_ask_user_question(pane_text: str) -> AskUserQuestionForm | None:
    """Structured parse of the AskUserQuestion picker in ``pane_text``.

    PR 1 surface: pure parser, no caller change. Returns ``None`` when the
    pane does not contain a recognizable AskUserQuestion picker, or when
    the parse is ambiguous (mid-redraw, unknown variant, gaps in
    numbering). The keystroke-keyboard fallback in ``handle_interactive_ui``
    stays in charge for ``None`` returns.

    Detection is anchored on one of:
      * a multi-tab header line (``← ☐ X  ☒ Y  ✔ Submit →``)
      * a numbered-options block followed by ``Enter to select``

    Returns ``AskUserQuestionForm`` with whichever fields were extractable.
    Empty / partial fields are preserved (e.g. mid-redraw tab header with
    no visible options yet → ``options=()`` rather than ``None``) so the
    fingerprint can still detect that the form is on a particular tab.
    """
    if not pane_text:
        return None

    lines = pane_text.split("\n")

    # Locate the lowest-on-screen tab header (most recent redraw wins).
    # We scan bottom-up so a stale header earlier in the scrollback does
    # not shadow the live one.
    tab_header_idx: int | None = None
    for i in range(len(lines) - 1, -1, -1):
        if _RE_TAB_HEADER.match(lines[i]):
            tab_header_idx = i
            break

    # Locate the picker footer ("Enter to select") near the bottom of the pane.
    # The single-tab options block sits immediately above this line. Scan
    # bottom-up so a stale footer earlier in the scrollback can't shadow
    # the live one, and search the entire captured buffer (scrollback may
    # extend far above the visible region for long question text).
    footer_idx: int | None = None
    for i in range(len(lines) - 1, -1, -1):
        if _RE_PICKER_FOOTER.search(lines[i]):
            footer_idx = i
            break
    has_footer = footer_idx is not None

    # Review-screen + free-text markers stay scoped to the last 25 lines —
    # they only matter for the live picker state, not historic scrollback.
    recent_tail = lines[-25:]
    is_review = any(_RE_REVIEW_HEADER.match(line) for line in recent_tail) and any(
        _RE_SUBMIT_PROMPT.match(line) for line in recent_tail
    )
    is_free_text = any(_RE_FREE_TEXT_OPTION.search(line) for line in recent_tail)

    if tab_header_idx is None and not has_footer and not is_review:
        return None

    tabs: tuple[AskTab, ...] = ()
    if tab_header_idx is not None:
        parsed_tabs = _parse_tab_header(lines[tab_header_idx])
        if parsed_tabs is None:
            return None
        tabs = parsed_tabs

    # Walk-back (single-tab path) captures a display-only question-title
    # candidate from the line above the options block. Tracked separately
    # from ``current_question_title`` (which goes into the fingerprint and
    # into ``_strong_match`` for JSONL-stale detection): the walk-back is
    # a heuristic guess that can accidentally pick up assistant prose or
    # stale scrollback, so feeding it into the matcher would risk
    # mis-overlaying stale JSONL labels onto a live pane (wrong-action
    # class). The renderer falls back to ``pane_walkback_title`` when
    # ``current_question_title`` is None — this gives the user context
    # for fresh pickers before Claude Code has flushed the AUQ
    # ``tool_use`` line to JSONL (2026-05-21 D5 incident at 22:49).
    walkback_stop_idx: int | None = None
    walkback_blank_gap: int = 0

    # Collect options below the tab header (multi-tab) or in the picker
    # region above the footer (single-tab). For multi-tab, options live
    # between the header and the next separator / next tab header /
    # picker footer.
    if tab_header_idx is not None:
        end_idx = len(lines)
        for j in range(tab_header_idx + 1, len(lines)):
            line = lines[j]
            if _RE_TAB_HEADER.match(line):
                end_idx = j
                break
            stripped = line.strip()
            if stripped and all(c == "─" for c in stripped):
                end_idx = j
                break
        options_region = lines[tab_header_idx + 1 : end_idx]
    elif footer_idx is not None:
        # Scan upward from the footer to find the contiguous numbered-options
        # block. Walk backward until we hit a line that's clearly not part
        # of the options block (anything other than a numbered option, a
        # description continuation, a blank line, or a separator). This
        # captures option 1 even when the question text is long enough to
        # push it well above the last 25 lines.
        start_idx = footer_idx
        for j in range(footer_idx - 1, -1, -1):
            line = lines[j]
            stripped = line.strip()
            if not stripped:
                start_idx = j
                walkback_blank_gap += 1
                continue
            if _RE_NUMBERED_OPTION.match(line):
                start_idx = j
                walkback_blank_gap = 0
                continue
            # Separator line (only ─ chars).
            if all(c == "─" for c in stripped):
                start_idx = j
                walkback_blank_gap = 0
                continue
            # Description continuation — non-empty indented text within
            # ~7 lines (in either direction) of a numbered option. The
            # symmetric scan handles the LAST option's descriptions,
            # which only have a numbered option ABOVE them in file
            # order (the footer is below). Without the upward arm the
            # walk-back terminated at the last desc line, leaving
            # ``pane_opts=0`` and forcing ``_build_pick_button_rows``'s
            # ``fa5_guard`` to suppress option buttons on multi-Q AUQs
            # that Claude Code renders without a multi-tab header.
            # Bounded distance still rejects stale indented scrollback
            # that has no nearby option.
            if line.startswith(("  ", "\t")) and (
                any(
                    _RE_NUMBERED_OPTION.match(lines[k])
                    for k in range(j + 1, min(j + 8, footer_idx + 1))
                )
                or any(
                    _RE_NUMBERED_OPTION.match(lines[k]) for k in range(max(0, j - 7), j)
                )
            ):
                start_idx = j
                walkback_blank_gap = 0
                continue
            # Non-pattern line — title-display candidate. Only set
            # ``walkback_stop_idx`` here so the for-loop falling off
            # the top of the buffer (no break) keeps it at None: a
            # buffer that is entirely pattern lines has no title to
            # capture. Also reject indented lines as title candidates
            # — Claude Code's question text is rendered at column 0,
            # and indented lines above the topmost option are
            # invariably scrollback noise (hermes review, 2026-05-21).
            if not line.startswith(("  ", "\t")):
                walkback_stop_idx = j
            break
        options_region = lines[start_idx : footer_idx + 1]
    else:
        options_region = recent_tail

    options = _parse_numbered_options(options_region)
    pane_signal = _pane_glyph_signal(options_region)
    select_mode = _resolve_select_mode(
        None,
        pane_signal,
        is_review_screen=is_review,
    )

    # Multi-tab in-region title scan — sets the authoritative
    # ``current_question_title`` for layouts where Claude Code prints
    # the question text between the tab header and the first option.
    # Inputs to ``_strong_match`` and the fingerprint canonical come
    # from this field, so we only populate it from a region anchored
    # by the tab header (a strong "this is the picker" signal).
    current_question_title: str | None = None
    if tab_header_idx is not None:
        for line in options_region:
            stripped = line.strip()
            if not stripped:
                continue
            if _RE_NUMBERED_OPTION.match(line):
                break
            if _RE_TAB_HEADER.match(line):
                continue
            if all(c == "─" for c in stripped):
                continue
            current_question_title = stripped
            break

    # ``pane_walkback_title`` (display only): walked-back title for the
    # single-tab path. Bounded gap (≤2 blanks between candidate and
    # topmost option) keeps us from pulling in pre-picker scrollback.
    # Multi-line wraps capped at 3 physical lines so an entire stray
    # paragraph cannot get glued together and accidentally match a
    # JSONL substring (hermes review, 2026-05-21). The renderer falls
    # back to this field when ``current_question_title`` is None.
    pane_walkback_title: str | None = None
    if walkback_stop_idx is not None and walkback_blank_gap <= 2:
        parts: list[str] = [lines[walkback_stop_idx].strip()]
        for k in range(walkback_stop_idx - 1, -1, -1):
            if len(parts) >= 3:
                break
            prev_line = lines[k]
            prev_stripped = prev_line.strip()
            if not prev_stripped:
                break
            if _RE_NUMBERED_OPTION.match(prev_line):
                break
            if all(c == "─" for c in prev_stripped):
                break
            if prev_line.startswith(("  ", "\t")):
                # Indented prior content is either an option-description
                # continuation or unrelated bullet text — not part of the
                # title. (Tmux's pane capture does not re-indent
                # soft-wrapped lines, so a wrapped title's continuation
                # would start at column 0.)
                break
            parts.append(prev_stripped)
        pane_walkback_title = " ".join(reversed(parts))

    # Build a pane excerpt for verbatim fallback rendering. We pin it to the
    # tab header (if any) or the last ~25 lines otherwise — the renderer in
    # PR 2 won't use the full pane scrollback.
    excerpt_start = (
        tab_header_idx if tab_header_idx is not None else max(0, len(lines) - 25)
    )
    pane_excerpt = "\n".join(lines[excerpt_start:]).rstrip()

    options_contiguous = bool(options) and [o.number for o in options] == list(
        range(1, len(options) + 1)
    )
    # A pure-pane picker is "complete" when we can see option 1 (contiguous
    # from 1 = top of the list present) AND an affordance OPTION ROW
    # ("Type something" / "Chat about this") was actually parsed in the option
    # block. Claude Code always renders those affordance rows at the BOTTOM of
    # the option list, so a parsed affordance row proves we captured the whole
    # list rather than a scrolled tail. We require an affordance *row in the
    # option block* — NOT the weaker ``is_free_text`` tail-substring scan, which
    # could be tripped by question text or an option description containing the
    # phrase "Type something" (hermes review 2026-05-31). Conservative: if no
    # affordance row is in-block or numbering doesn't start at 1,
    # options_complete stays False (toggle buttons suppressed → keystroke-nav
    # fallback), never a wrong dispatch.
    affordance_row_in_block = any(
        (_m := _RE_NUMBERED_OPTION.match(line)) is not None
        and is_affordance_label(_strip_option_checkbox(_m.group("label").strip()))
        for line in options_region
    )
    options_complete = options_contiguous and affordance_row_in_block

    return AskUserQuestionForm(
        tabs=tabs,
        current_question_title=current_question_title,
        options=options,
        is_review_screen=is_review,
        is_free_text=is_free_text,
        pane_excerpt=pane_excerpt,
        pane_walkback_title=pane_walkback_title,
        select_mode=select_mode,
        options_complete=options_complete,
    )


def _infer_current_tab_idx(
    questions: tuple[AskQuestion, ...],
    pane_form: AskUserQuestionForm | None,
) -> tuple[int, bool]:
    """Match pane-visible content against the JSONL questions matrix.

    Returns ``(idx, inferred)``. ``inferred`` is True when at least one
    matcher pinned a single tab; False when every matcher tied or no signal
    was available, in which case ``idx`` defaults to 0 and the caller must
    suppress option-pick buttons (FA5+ safety: dispatching a digit against a
    defaulted tab can answer the wrong tab in the live TUI).

    Match order:
      1. Primary — exact title match (pane's ``current_question_title`` ==
         a question's ``title`` OR ``header``). Falls through on ambiguity
         (two questions share the same title) or on truncated/wrapped pane
         titles.
      2. Secondary — option-label overlap. Score each question by how many
         of its option labels appear in the pane form's options. Unique
         winner wins; tie → fall through.
      3. Fallback — return ``(0, False)``.
    """
    if pane_form is None or not questions:
        return 0, False

    # Primary: exact title match.
    pane_title = (pane_form.current_question_title or "").strip()
    if pane_title:
        title_matches: list[int] = []
        for i, q in enumerate(questions):
            if pane_title == q.title.strip() or pane_title == q.header.strip():
                title_matches.append(i)
        if len(title_matches) == 1:
            return title_matches[0], True

    # Secondary: option-label overlap. The pane carries the visible labels
    # for the current tab only; whichever question has the most labels in
    # the pane form's options is the active one.
    pane_labels = {o.label for o in pane_form.options if o.label}
    if pane_labels:
        scored: list[tuple[int, int]] = []
        for i, q in enumerate(questions):
            q_labels = {o.label for o in q.options if o.label}
            scored.append((i, len(pane_labels & q_labels)))
        # Drop zero scores so a pane with no overlap with any question
        # doesn't accidentally pick idx 0 as the "winner".
        scored = [(i, s) for (i, s) in scored if s > 0]
        if scored:
            scored.sort(key=lambda pair: pair[1], reverse=True)
            top_score = scored[0][1]
            top = [i for (i, s) in scored if s == top_score]
            if len(top) == 1:
                return top[0], True

    return 0, False


def resolve_ask_form(
    tool_input: dict[str, Any] | None,
    pane_text: str,
) -> AskUserQuestionForm | None:
    """Unified AskUserQuestion form resolution.

    Used by both the render path (``handle_interactive_ui``) and the
    pick-token callback validator. Returning byte-identical forms from
    both call sites is what makes the fingerprint staleness check sound
    for multi-tab forms — if render uses the JSONL overlay but validate
    re-parses only the pane, fingerprints will never match on multi-tab.

    Inputs:
      * ``tool_input``: JSONL ``tool_use.input`` dict, or None when the
        cache has been evicted (post-restart, post-tool_result).
      * ``pane_text``: live tmux pane capture.

    Output shapes:

    1. Single-question JSONL: returns the legacy single-tab form (same
       canonical_repr as today). Pane is consulted only for cursor /
       free-text / review-screen flags.
    2. Multi-question JSONL + pane parses: ``questions`` matrix populated
       from JSONL; ``current_question_title`` + ``options`` overlay the
       matched tab; ``current_tab_inferred`` reflects whether matching
       succeeded.
    3. Multi-question JSONL + pane fails: ``current_tab_idx = 0`` and
       ``current_tab_inferred = False`` — the renderer MUST NOT mint pick
       buttons under this state.
    4. JSONL missing: fall back to ``parse_ask_user_question(pane_text)``
       — preserves the pane-only path for sessions where the JSONL cache
       was lost.
    5. Both missing: returns None.
    """
    pane_form = parse_ask_user_question(pane_text) if pane_text else None

    jsonl_form = build_form_from_tool_input(tool_input)
    if jsonl_form is None:
        # No JSONL — pure pane fallback.
        return pane_form

    # JSONL-stale detection. Claude buffers an assistant turn before
    # writing it to JSONL, so a fresh AskUserQuestion tool_use can be
    # live on the pane while ``tool_input`` still points at the
    # *previous* AUQ. The render then overlays a pane that doesn't
    # reconcile with the cached questions:
    #
    #   * single-q stale → wrong-action class: pick buttons render
    #     JSONL labels but a click dispatches the digit against the
    #     pane's different question (e.g. clicking "1. Old answer A"
    #     submits "Option 1 of the new question").
    #   * multi-q stale → FA5+ guard suppresses pick buttons (correct
    #     defense, but the user is stuck with no working surface).
    #
    # Detection: pane has non-empty options AND no JSONL question
    # ``_strong_match``-es the pane. Skip on review screens (pane is
    # already authoritative there and the existing branches preserve
    # the JSONL questions matrix for tab-strip context). Falling back
    # to ``pane_form`` gives the renderer a clean single-tab shape
    # whose option labels match the live pane — pick buttons dispatch
    # against the right question, and the cursor overlay works.
    if (
        pane_form is not None
        and not pane_form.is_review_screen
        and pane_form.options
        and not any(_strong_match(q, pane_form) for q in jsonl_form.questions)
    ):
        logger.info(
            "resolve_ask_form JSONL STALE: pane has %d options that don't "
            "match any of %d JSONL questions; falling back to pane-only. "
            "pane_title=%r jsonl_titles=%r",
            len(pane_form.options),
            len(jsonl_form.questions),
            (pane_form.current_question_title or "<none>")[:80],
            [q.title[:80] for q in jsonl_form.questions],
        )
        # Tag the form so the renderer can distinguish "pane-only
        # because no JSONL was ever cached" (cache_empty) from
        # "pane-only because the JSONL cache held a DIFFERENT question"
        # (cache stale). The contiguous-from-1 mint gate downstream
        # protects both cases when the pane shows only a tail of the
        # option list, but the tag remains useful for diagnostic logs
        # and the callback-rerender notice path. ``_meta`` is
        # ``compare=False`` and excluded from ``_canonical_repr`` /
        # ``fingerprint``, so this tag doesn't invalidate live pick-token
        # callbacks minted against earlier renders.
        pane_form._meta["stale_fallback"] = "1"
        return pane_form

    if len(jsonl_form.questions) <= 1:
        # Single-question review screen: pane is authoritative, same as the
        # multi-question short-circuit below. Claude Code's single-question
        # AUQ TUI has two steps — picker then Submit/Cancel confirmation;
        # the picker's JSONL options are the original answers, but the
        # confirmation step's pane shows ``1. Submit answers`` /
        # ``2. Cancel``. Without this branch, the single-question resolver
        # always returned the original answer options grafted onto
        # ``is_review_screen=True``, producing a mislabelled card AND a
        # wrong-action-class bug: clicking the rendered "option 2" would
        # dispatch ``2 + Enter`` against the live Submit/Cancel picker
        # (Cancel) while the button reads as one of the original answers.
        # ``current_question_title`` stays from JSONL so single-question
        # review fingerprints don't collapse onto a single canonical repr
        # (``_canonical_repr`` omits QS:/INF: for len(questions) <= 1, so
        # the title is the only remaining identity carrier here).
        if pane_form is not None and pane_form.is_review_screen:
            return AskUserQuestionForm(
                tabs=pane_form.tabs,
                current_question_title=jsonl_form.current_question_title,
                options=pane_form.options,
                is_review_screen=True,
                is_free_text=pane_form.is_free_text,
                pane_excerpt=pane_form.pane_excerpt,
                questions=jsonl_form.questions,
                current_tab_inferred=False,
                select_mode="single",
                options_complete=True,
            )
        # Single-question: keep the JSONL-derived shape but graft live pane
        # state (cursor on the right option, free-text / review-screen
        # flags). Without the pane overlay the form would always claim
        # cursor on option 1, breaking the existing single-tab behaviour.
        if pane_form is not None:
            return AskUserQuestionForm(
                tabs=jsonl_form.tabs,
                current_question_title=jsonl_form.current_question_title,
                options=_overlay_cursor_and_selection(
                    jsonl_form.options, pane_form.options
                ),
                is_review_screen=pane_form.is_review_screen,
                is_free_text=pane_form.is_free_text,
                pane_excerpt=pane_form.pane_excerpt,
                questions=jsonl_form.questions,
                current_tab_inferred=True,
                select_mode=_jsonl_resolved_select_mode(jsonl_form, pane_form),
                options_complete=True,
            )
        return jsonl_form

    # Multi-question: detect review screen FIRST. On a review screen, the
    # pane's visible options are Submit/Cancel — not Q1's options — and
    # overlaying them onto Q1's labels mints buttons whose label disagrees
    # with the action that the cursor will dispatch (wrong-action class).
    # Pane is authoritative for the review screen's options + cursor; the
    # JSONL `questions` matrix stays for tab-strip context only.
    if pane_form is not None and pane_form.is_review_screen:
        return AskUserQuestionForm(
            tabs=pane_form.tabs,
            current_question_title=None,
            options=pane_form.options,
            is_review_screen=True,
            is_free_text=pane_form.is_free_text,
            pane_excerpt=pane_form.pane_excerpt,
            questions=jsonl_form.questions,
            # No inference happened — the pane authoritatively says "review".
            # The mint gate has a review-screen EXCEPTION: it still mints the
            # Submit/Cancel pick buttons from the pane's own options (these are
            # the real review-screen labels, not JSONL Q-labels), so the user
            # can submit / cancel via the Telegram keyboard as well as keystroke
            # nav. `current_tab_inferred=False` only marks that no tab inference
            # ran here.
            current_tab_inferred=False,
            select_mode="single",
            options_complete=True,
        )

    # Multi-question: infer the current tab from pane content.
    current_idx, inferred = _infer_current_tab_idx(jsonl_form.questions, pane_form)
    # Strong-match requirement before overlay: even if _infer_current_tab_idx
    # returned (idx, True) on a single matching option, demote to inferred=False
    # unless we have a non-trivial title substring match OR ≥50% option-label
    # overlap. This prevents minting Q1's buttons when the pane is actually
    # showing Q2 with one coincidentally-shared option label.
    if inferred and pane_form is not None:
        if not _strong_match(jsonl_form.questions[current_idx], pane_form):
            inferred = False
    current_q = jsonl_form.questions[current_idx]
    # Overlay the live cursor onto the chosen tab's options only when the
    # match is strong. On weak/no inference we keep JSONL options as-is so
    # the validator and renderer see a stable shape; pick buttons are
    # suppressed downstream because current_tab_inferred is False.
    options = (
        _overlay_cursor_and_selection(current_q.options, pane_form.options)
        if pane_form is not None and inferred
        else current_q.options
    )
    # Diagnostic: when inference fails on a multi-question form, the FA5+
    # guard in ``_build_pick_button_rows`` suppresses pick buttons and the
    # user is left with keystroke nav only. Log the inputs so future repros
    # tell us whether (a) pane_form was None, (b) options weren't extracted,
    # (c) title didn't match, or (d) strong-match demoted. Only log on the
    # failure path to keep noise low; the success path is the common case.
    if not inferred:
        pane_title = (
            (pane_form.current_question_title or "<none>") if pane_form else "<no pane>"
        )
        pane_opts = len(pane_form.options) if pane_form else -1
        jsonl_titles = [q.title for q in jsonl_form.questions]
        logger.info(
            "resolve_ask_form multi-q inference FAILED: questions=%d pane_opts=%d "
            "pane_title=%r jsonl_titles=%r",
            len(jsonl_form.questions),
            pane_opts,
            pane_title[:80] if isinstance(pane_title, str) else pane_title,
            [t[:80] for t in jsonl_titles],
        )
    return AskUserQuestionForm(
        tabs=pane_form.tabs if pane_form is not None else (),
        current_question_title=current_q.title or None,
        options=options,
        is_review_screen=pane_form.is_review_screen if pane_form is not None else False,
        is_free_text=pane_form.is_free_text if pane_form is not None else False,
        pane_excerpt=pane_form.pane_excerpt if pane_form is not None else "",
        questions=jsonl_form.questions,
        current_tab_inferred=inferred,
        select_mode=_jsonl_resolved_select_mode(jsonl_form, pane_form),
        options_complete=True,
    )


def _strong_match(q: AskQuestion, pane_form: AskUserQuestionForm) -> bool:
    """Stricter inference check than ``_infer_current_tab_idx``.

    The inference helper accepts any unique winner, including the degenerate
    case "one label happened to match." That can mint Q1's buttons against
    a pane showing Q2 if Q1 and Q2 share one option label. Require:

      * the question title is a non-trivial substring of the pane title
        (or vice versa) — case-insensitive, ≥8 chars or full title length, OR
      * ≥50% of the pane's option labels appear in the question's options.

    Reject if neither holds. Caller demotes ``inferred`` to False; the mint
    code then suppresses pick buttons and keystroke nav stays available.
    """
    q_title = q.title.strip().lower()
    pane_title = (pane_form.current_question_title or "").strip().lower()
    if q_title and pane_title:
        # Substring match in either direction; reject trivially-short overlaps
        # (e.g., "Pick." in any pane title would otherwise pass).
        shorter = min(q_title, pane_title, key=len)
        threshold = min(8, len(shorter))
        if threshold > 0 and (
            (q_title in pane_title or pane_title in q_title)
            and len(shorter) >= threshold
        ):
            return True

    pane_labels = {o.label for o in pane_form.options if o.label}
    if not pane_labels:
        return False
    q_labels = {o.label for o in q.options if o.label}
    overlap = len(pane_labels & q_labels)
    # ≥50% of pane labels recognized in this question's option set.
    return overlap * 2 >= len(pane_labels)


def _overlay_cursor_and_selection(
    jsonl_options: tuple[AskOption, ...],
    pane_options: tuple[AskOption, ...],
) -> tuple[AskOption, ...]:
    """Apply pane cursor and visible checkbox selection to JSONL options by number.

    Cursor follows the existing overlay rule: if no cursor is visible, default
    to option 1. Selection is stricter: only visible pane rows are known; JSONL
    options not present in the pane get ``selected=None`` rather than False.
    """
    cursor_at: int | None = None
    selected_by_num: dict[int, bool | None] = {}
    for opt in pane_options:
        if opt.number is None:
            continue
        selected_by_num[opt.number] = opt.selected
        # Prefer the LAST cursor flag in pane order. ``_parse_numbered_options``
        # already dedups to a single (bottom-most) cursor, but a raw or future
        # pane_options tuple could still carry a stale-scrollback ``❯`` above
        # the live one — the live cursor is always the bottom-most. Overwrite
        # rather than first-wins so the overlay tracks the live cursor.
        if opt.cursor:
            cursor_at = opt.number
    if cursor_at is None and jsonl_options:
        cursor_at = jsonl_options[0].number
    if cursor_at is None:
        return jsonl_options
    return tuple(
        AskOption(
            label=o.label,
            recommended=o.recommended,
            cursor=(o.number == cursor_at),
            number=o.number,
            description=o.description,
            selected=selected_by_num.get(o.number) if o.number is not None else None,
        )
        for o in jsonl_options
    )


def _jsonl_resolved_select_mode(
    jsonl_form: AskUserQuestionForm,
    pane_form: AskUserQuestionForm | None,
) -> Literal["single", "multi", "unknown"]:
    """Resolve select mode when a JSONL/side-file source is present."""
    if pane_form is None:
        return jsonl_form.select_mode
    source_mode: Literal["single", "multi", "unknown"] | None = jsonl_form.select_mode
    if jsonl_form._meta.get("multiselect_present") == "0":
        source_mode = None
    return _resolve_select_mode(
        source_mode,
        pane_form.select_mode,
        is_review_screen=pane_form.is_review_screen,
    )


# ── Status line parsing ─────────────────────────────────────────────────

# Spinner characters Claude Code uses in its status line
STATUS_SPINNERS = frozenset(["·", "✻", "✽", "✶", "✳", "✢"])


def _find_chrome_separator(lines: list[str]) -> int | None:
    """Locate the topmost ``──`` chrome separator in the last 10 lines."""
    search_start = max(0, len(lines) - 10)
    for i in range(search_start, len(lines)):
        stripped = lines[i].strip()
        if len(stripped) >= 20 and all(c == "─" for c in stripped):
            return i
    return None


def has_pane_chrome(pane_text: str) -> bool:
    """Return True iff the frame contains Claude Code's bottom-chrome anchor.

    The anchor is the chrome separator — a full line of ``─`` (≥20 chars) in
    the last 10 lines — the SAME structural anchor ``parse_status_line`` and
    ``strip_pane_chrome`` already trust to locate the bottom chrome. Its
    presence is positive evidence the capture is a fully-rendered live
    Claude Code pane (not an empty/truncated/mid-redraw frame). Used by
    ``status_polling._process_idle_clear_only`` as the positive half of its
    "confirmed idle" predicate (chrome present AND not ``is_status_active``).
    """
    if not pane_text:
        return False
    return _find_chrome_separator(pane_text.split("\n")) is not None


def parse_status_line(pane_text: str) -> str | None:
    """Extract the Claude Code status line from terminal output.

    The status line (spinner + working text) appears above the chrome
    separator (a full line of ``─`` characters). We locate the separator
    first, then check the lines just above it — this avoids false
    positives from ``·`` bullets in Claude's regular output.

    Returns the text after the spinner, or None if no status line found.
    Note: blank lines between the spinner and the chrome are tolerated
    here (the post-completion summary case). To distinguish "Claude is
    actively running" from "post-completion summary", use
    ``is_status_active`` instead.
    """
    if not pane_text:
        return None

    lines = pane_text.split("\n")
    chrome_idx = _find_chrome_separator(lines)
    if chrome_idx is None:
        return None  # No chrome visible — can't determine status

    # Check lines just above the separator (skip blanks, up to 4 lines)
    for i in range(chrome_idx - 1, max(chrome_idx - 5, -1), -1):
        line = lines[i].strip()
        if not line:
            continue
        if line[0] in STATUS_SPINNERS:
            return line[1:].strip()
        # First non-empty line above separator isn't a spinner → no status
        return None
    return None


def is_status_active(pane_text: str) -> bool:
    """Return True iff Claude is actively producing output.

    The reliable signal is the literal ``esc to interrupt`` in the bottom
    chrome bar — Claude only renders that hint while a run is in flight.
    The spinner glyph and the spinner-line text are NOT reliable: Claude
    keeps the spinner+summary line ("✻ Cooked for 2s") visible after a
    run completes, and the gap above the top chrome is the same in both
    active and idle states (Claude always inserts a blank line there).

    Examples:

        Actively running (returns True)::

            ✽ Brewing… (3s · thinking with high effort)

            ──────────────────────────────────
            ❯
            ──────────────────────────────────
              ⏵⏵ bypass permissions on · esc to interrupt

        Post-completion summary (returns False)::

            ✻ Cooked for 2s

            ──────────────────────────────────
            ❯
            ──────────────────────────────────
              ⏵⏵ bypass permissions on (shift+tab to cycle)
    """
    if not pane_text:
        return False

    # Search the last 8 lines so we catch the bottom chrome bar without
    # paying for a full pane scan on every poll.
    last_lines = pane_text.split("\n")[-8:]
    return any("esc to interrupt" in line.lower() for line in last_lines)


# ── Context-window indicator ─────────────────────────────────────────────

# Matches Claude Code's chrome footer line, e.g.
#   "  [Opus 4.6] Context: 89%"
#   "  [Sonnet 4.5] Context: 7%"
_RE_CONTEXT_PCT = re.compile(r"\bContext:\s*(\d{1,3})%")


def extract_context_pct(pane_text: str) -> int | None:
    """Extract the Context-window percentage from Claude Code's chrome.

    Scans the bottom 10 lines for a ``[<model>] Context: NN%`` pattern.
    Returns the integer (0-100) or ``None`` if no match is found or the
    parsed value is out of range. Pure parser — no I/O, no caching.
    """
    if not pane_text:
        return None
    lines = pane_text.split("\n")
    for line in lines[-10:]:
        match = _RE_CONTEXT_PCT.search(line)
        if match:
            try:
                pct = int(match.group(1))
            except ValueError:
                continue
            if 0 <= pct <= 100:
                return pct
    return None


# ── Pane chrome stripping & bash output extraction ─────────────────────


def strip_pane_chrome(lines: list[str]) -> list[str]:
    """Strip Claude Code's bottom chrome (prompt area + status bar).

    The bottom of the pane looks like::

        ────────────────────────  (separator)
        ❯                        (prompt)
        ────────────────────────  (separator)
          [Opus 4.6] Context: 34%
          ⏵⏵ bypass permissions…

    This function finds the topmost ``────`` separator in the last 10 lines
    and strips everything from there down.
    """
    search_start = max(0, len(lines) - 10)
    for i in range(search_start, len(lines)):
        stripped = lines[i].strip()
        if len(stripped) >= 20 and all(c == "─" for c in stripped):
            return lines[:i]
    return lines


def extract_bash_output(pane_text: str, command: str) -> str | None:
    """Extract ``!`` command output from a captured tmux pane.

    Searches from the bottom for the ``! <command>`` echo line, then
    returns that line and everything below it (including the ``⎿`` output).
    Returns *None* if the command echo wasn't found.
    """
    lines = strip_pane_chrome(pane_text.splitlines())

    # Find the last "! <command>" echo line (search from bottom).
    # Match on the first 10 chars of the command in case the line is truncated.
    cmd_idx: int | None = None
    match_prefix = command[:10]
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if stripped.startswith(f"! {match_prefix}") or stripped.startswith(
            f"!{match_prefix}"
        ):
            cmd_idx = i
            break

    if cmd_idx is None:
        return None

    # Include the command echo line and everything after it
    raw_output = lines[cmd_idx:]

    # Strip trailing empty lines
    while raw_output and not raw_output[-1].strip():
        raw_output.pop()

    if not raw_output:
        return None

    return "\n".join(raw_output).strip()


# ── Usage modal parsing ──────────────────────────────────────────────────────────


@dataclass
class UsageInfo:
    """Parsed output from Claude Code's /usage modal."""

    raw_text: str  # Full captured pane text
    parsed_lines: list[str]  # Cleaned content lines from the modal


def parse_usage_output(pane_text: str) -> UsageInfo | None:
    """Extract usage information from Claude Code's /usage settings tab.

    The /usage modal shows a Settings overlay with a "Usage" tab containing
    progress bars and reset times.  This parser looks for the Settings header
    line, then collects all content until "Esc to cancel".

    Returns UsageInfo with cleaned lines, or None if not detected.
    """
    if not pane_text:
        return None

    lines = pane_text.strip().split("\n")

    # Find the Settings header that indicates we're in the usage modal
    start_idx: int | None = None
    end_idx: int | None = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if start_idx is None:
            # The usage tab header line
            if "Settings:" in stripped and "Usage" in stripped:
                start_idx = i + 1  # skip the header itself
        else:
            if stripped.startswith("Esc to"):
                end_idx = i
                break

    if start_idx is None:
        return None
    if end_idx is None:
        end_idx = len(lines)

    # Collect content lines, stripping progress bar characters and whitespace
    cleaned: list[str] = []
    for line in lines[start_idx:end_idx]:
        # Strip the line but preserve meaningful content
        stripped = line.strip()
        if not stripped:
            continue
        # Remove progress bar block characters but keep the rest
        # Progress bars are like: █████▋   38% used
        # Strip leading block chars, keep the percentage
        stripped = re.sub(r"^[\u2580-\u259f\s]+", "", stripped).strip()
        if stripped:
            cleaned.append(stripped)

    if cleaned:
        return UsageInfo(raw_text=pane_text, parsed_lines=cleaned)

    return None
