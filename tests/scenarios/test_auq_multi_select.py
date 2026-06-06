"""Scenario coverage for AskUserQuestion multi-select rendering and toggles.

Exercises the public Telegram callback seam for the PR-C ``aqt:`` toggle path:
render uses the pane-aware side-file source, toggles send a bare digit without
ledgering or consuming sibling tokens, and Submit/Cancel remains the existing
review-screen ``aqp:`` flow.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cctelegram import bot as bot_module, terminal_parser
from cctelegram.callback_dispatcher import DispatcherAdapters, dispatch_callback
from cctelegram.handlers import auq_ledger, interactive_ui, pick_token
from cctelegram.handlers.callback_data import CB_ASK_PICK, CB_ASK_TOGGLE
from cctelegram.session_monitor import NewMessage
from cctelegram.utils import app_dir
from tests.conftest import ScenarioHarness, make_update_callback

pytestmark = pytest.mark.scenario

_FIXTURES = Path(__file__).parents[1] / "cctelegram" / "fixtures"
_SESSION_ID = "11111111-1111-4111-8111-111111111111"


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text()


def _safeguards_input(*, multi: bool = True) -> dict[str, Any]:
    return {
        "questions": [
            {
                "question": "Pick the implementation safeguards to include.",
                "header": "Safeguards",
                "multiSelect": multi,
                "options": [
                    {"label": "A) Verify cursor row from tmux pane before Space"},
                    {"label": "B) Keep PreToolUse side file alive across toggles"},
                    {"label": "C) Suppress tabbed multi-question forms"},
                    {"label": "D) Add Submit and Cancel buttons"},
                ],
            }
        ]
    }


def _compressed_input() -> dict[str, Any]:
    return {
        "questions": [
            {
                "question": "Pick evidence.",
                "header": "Evidence",
                "multiSelect": True,
                "options": [
                    {"label": "A) Parser fixture parity"},
                    {"label": "B) Callback dispatch parity"},
                    {"label": "C) Unknown-mode suppression"},
                    {"label": "D) Tool-result cleanup proof"},
                    {"label": "E) Review-screen submit reuse"},
                ],
            }
        ]
    }


def _two_toggled_input() -> dict[str, Any]:
    return {
        "questions": [
            {
                "question": "Pick two cc-telegram safeguards.",
                "header": "Safeguards",
                "multiSelect": True,
                "options": [
                    {"label": "A) Verify cursor row before Space"},
                    {"label": "B) Preserve side file through toggles"},
                    {"label": "C) Suppress tabbed multi-question forms"},
                    {"label": "D) Use submit ledger for final Enter"},
                ],
            }
        ]
    }


def _multi_question_input() -> dict[str, Any]:
    data = _safeguards_input()
    data["questions"].append(
        {
            "question": "Second question",
            "header": "Second",
            "multiSelect": True,
            "options": [{"label": "A) Second"}, {"label": "B) Other"}],
        }
    )
    return data


def _bind(
    scenario: ScenarioHarness, pane: str, *, session_id: str = _SESSION_ID
) -> str:
    wid = scenario.add_window(window_name="repo", cwd="/repo", pane_text=pane)
    scenario.bind_thread(
        42,
        wid,
        display_name="repo",
        cwd="/repo",
        session_id=session_id,
    )
    return wid


def _write_side_file(session_id: str, tool_input: dict[str, Any]) -> Path:
    pending = app_dir() / "auq_pending"
    pending.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = pending / f"{session_id}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": session_id,
                "tool_use_id": "tool-use-1",
                "written_at": time.time(),
                "tool_input": tool_input,
            }
        )
    )
    return path


def _adapters(scenario: ScenarioHarness) -> DispatcherAdapters:
    return DispatcherAdapters(
        session_manager=scenario.session_manager,
        tmux_manager=scenario.tmux,
        bot=scenario.bot,
        route_runtime=SimpleNamespace(),
        config=SimpleNamespace(),
        terminal_parser=terminal_parser,
    )


def _last_markup(scenario: ScenarioHarness) -> Any:
    for sent in reversed(scenario.bot.sent):
        markup = sent.kwargs.get("reply_markup")
        if markup is not None:
            return markup
    raise AssertionError("no reply markup recorded")


def _callbacks(scenario: ScenarioHarness) -> list[str]:
    markup = _last_markup(scenario)
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _texts(scenario: ScenarioHarness) -> str:
    return "\n---\n".join(scenario.bot.texts())


async def _render(scenario: ScenarioHarness, wid: str) -> None:
    assert await interactive_ui.handle_interactive_ui(
        scenario.bot,
        scenario.user_id,
        wid,
        42,
        tmux_mgr=scenario.tmux,
        session_mgr=scenario.session_manager,
    )


async def _tap(
    scenario: ScenarioHarness, callback_data: str, *, user_id: int | None = None
) -> None:
    update = make_update_callback(
        callback_data,
        thread_id=42,
        user_id=user_id or scenario.user_id,
        chat_id=scenario.chat_id,
    )
    await dispatch_callback(
        update,
        scenario.context,
        _adapters(scenario),
        is_user_allowed_func=lambda _uid: True,
    )


def _prefixes(callbacks: list[str], prefix: str) -> list[str]:
    return [cb for cb in callbacks if cb.startswith(prefix)]


@pytest.mark.asyncio
async def test_happy_path_toggle_tab_review_submit_and_tool_result_cleanup(
    scenario: ScenarioHarness,
) -> None:
    wid = _bind(scenario, _fixture("auq_multiselect_fresh_tmux_capture.txt"))
    side_file = _write_side_file(_SESSION_ID, _safeguards_input())

    await _render(scenario, wid)
    callbacks = _callbacks(scenario)
    toggles = _prefixes(callbacks, CB_ASK_TOGGLE)
    assert len(toggles) == 4
    assert not _prefixes(callbacks, CB_ASK_PICK)
    assert not any(cb.startswith(("aqm:", "aqs:", "aqx:")) for cb in callbacks)

    await _tap(scenario, toggles[1])
    assert scenario.tmux.sent_keys == [(wid, "2", False, True)]
    assert side_file.exists(), "aqt toggles must not clean side files"

    scenario.tmux.set_pane(wid, _fixture("auq_multiselect_2_toggled_tmux_capture.txt"))
    await _render(scenario, wid)
    assert "☑ 2. B)" in _texts(scenario)

    tab = next(cb for cb in _callbacks(scenario) if cb.startswith("aq:tab:"))
    scenario.tmux.set_pane(
        wid, _fixture("auq_multiselect_ready_to_submit_tmux_capture.txt")
    )
    await _tap(scenario, tab)
    review_callbacks = _callbacks(scenario)
    assert _prefixes(review_callbacks, CB_ASK_PICK)
    assert not _prefixes(review_callbacks, CB_ASK_TOGGLE)

    submit = _prefixes(review_callbacks, CB_ASK_PICK)[0]
    await _tap(scenario, submit)
    assert scenario.tmux.sent_keys[-2:] == [
        (wid, "1", False, True),
        (wid, "Enter", False, False),
    ]
    assert side_file.exists(), "review aqp dispatch is not the cleanup event"

    await bot_module.handle_new_message(
        NewMessage(
            session_id=_SESSION_ID,
            text="AskUserQuestion answered",
            content_type="tool_result",
            tool_use_id="tool-use-1",
            tool_name="AskUserQuestion",
            role="assistant",
        ),
        scenario.bot,
    )
    assert not side_file.exists()


@pytest.mark.asyncio
async def test_toggle_off_retap_sends_same_digit_twice(
    scenario: ScenarioHarness,
) -> None:
    wid = _bind(scenario, _fixture("auq_multiselect_fresh_tmux_capture.txt"))
    _write_side_file(_SESSION_ID, _safeguards_input())
    await _render(scenario, wid)
    opt2 = _prefixes(_callbacks(scenario), CB_ASK_TOGGLE)[1]
    await _tap(scenario, opt2)
    scenario.tmux.set_pane(wid, _fixture("auq_multiselect_fresh_tmux_capture.txt"))
    await _tap(scenario, opt2)
    assert [k for _, k, _, _ in scenario.tmux.sent_keys] == ["2", "2"]


@pytest.mark.asyncio
async def test_compressed_pane_with_side_file_renders_unknowns_and_toggles(
    scenario: ScenarioHarness,
) -> None:
    wid = _bind(
        scenario,
        _fixture("auq_multiselect_compressed_long_cursor_only_tmux_capture.txt"),
    )
    side_file = _write_side_file(_SESSION_ID, _compressed_input())
    await _render(scenario, wid)
    text = _texts(scenario)
    assert "☐ 3. C) Unknown-mode suppression" in text
    assert "· 1. A) Parser fixture parity" in text
    assert "· 5. E) Review-screen submit reuse" in text
    toggles = _prefixes(_callbacks(scenario), CB_ASK_TOGGLE)
    assert len(toggles) == 5
    await _tap(scenario, toggles[3])
    assert scenario.tmux.sent_keys == [(wid, "4", False, True)]
    assert side_file.exists()


@pytest.mark.asyncio
async def test_overlay_correctness_with_visible_selected_and_offscreen_unknowns(
    scenario: ScenarioHarness,
) -> None:
    pane = """←  ☒ Safeguards  ✔ Submit  →

Pick evidence.

  1. [ ] A) Parser fixture parity
❯ 2. [✔] B) Callback dispatch parity
  3. [ ] C) Unknown-mode suppression
Enter to select · ↑/↓ to navigate · Esc to cancel
"""
    wid = _bind(scenario, pane)
    _write_side_file(_SESSION_ID, _compressed_input())
    await _render(scenario, wid)
    text = _texts(scenario)
    assert "☐ 1. A) Parser fixture parity" in text
    assert "☑ 2. B) Callback dispatch parity" in text
    assert "☐ 3. C) Unknown-mode suppression" in text
    assert "· 4. D) Tool-result cleanup proof" in text
    assert "· 5. E) Review-screen submit reuse" in text


@pytest.mark.asyncio
async def test_hook_missing_compressed_suppresses_full_pane_mints_toggles(
    scenario: ScenarioHarness,
) -> None:
    # Compressed capture (options scrolled past 1, non-contiguous-from-1 → the
    # full list is NOT captured): no toggles + "full list unavailable" notice.
    wid = _bind(
        scenario,
        _fixture("auq_multiselect_compressed_long_cursor_only_tmux_capture.txt"),
    )
    await _render(scenario, wid)
    assert not _prefixes(_callbacks(scenario), CB_ASK_TOGGLE)
    assert "full list unavailable" in _texts(scenario)

    # Full fresh capture, hook still missing (pure-pane source): the picker is
    # now "complete" — options contiguous from 1 AND the "Type something"
    # free-text affordance is visible at the bottom of the list, proving the
    # whole list was captured. The fix deliberately mints toggles here so a
    # render→tap AUQ-source flip (side file → pure pane) keeps the fast buttons
    # working instead of silently rejecting the tap. (Pre-fix the pure-pane
    # path hardcoded options_complete=False and this minted no toggles.)
    scenario.bot.sent.clear()
    scenario.tmux.set_pane(wid, _fixture("auq_multiselect_fresh_tmux_capture.txt"))
    await _render(scenario, wid)
    assert len(_prefixes(_callbacks(scenario), CB_ASK_TOGGLE)) == 4


@pytest.mark.asyncio
async def test_unknown_partial_glyphs_suppresses_toggles(
    scenario: ScenarioHarness,
) -> None:
    pane = """←  ☐ Safeguards  ✔ Submit  →

Pick.

❯ 1. [ ] A) One
  2. B) Two
Enter to select · ↑/↓ to navigate · Esc to cancel
"""
    wid = _bind(scenario, pane)
    await _render(scenario, wid)
    assert not _prefixes(_callbacks(scenario), CB_ASK_TOGGLE)


@pytest.mark.asyncio
async def test_staleness_mid_toggle_refreshes_without_dispatch_or_cleanup(
    scenario: ScenarioHarness,
) -> None:
    wid = _bind(scenario, _fixture("auq_multiselect_fresh_tmux_capture.txt"))
    side_file = _write_side_file(_SESSION_ID, _safeguards_input())
    await _render(scenario, wid)
    opt2 = _prefixes(_callbacks(scenario), CB_ASK_TOGGLE)[1]
    scenario.tmux.set_pane(
        wid, _fixture("auq_multiselect_ready_to_submit_tmux_capture.txt")
    )
    await _tap(scenario, opt2)
    assert not scenario.tmux.sent_keys
    assert side_file.exists()


@pytest.mark.asyncio
async def test_toggle_survives_degraded_pane_at_tap_via_source_stickiness(
    scenario: ScenarioHarness,
) -> None:
    # Source-stickiness regression (Part E). Render mints the toggle against the
    # PreToolUse SIDE FILE (fresh pane). Then the pane is DEGRADED at tap — the
    # question-title region is obscured to "?" while the option block (cursor on
    # 1, nothing selected) is unchanged. On that degraded pane a plain
    # resolve_auq_source FLIPS side_file→pane (resolve_record rejects on the
    # title mismatch), and the pure-pane form fingerprint diverges from the
    # minted one — pre-fix that silently rejected the tap (dead button). With the
    # pin (peek_sticky_source returns the unchanged side file), the tap
    # re-resolves the SAME side-file source, the form fingerprint matches the
    # minted entry, and the digit IS dispatched.
    wid = _bind(scenario, _fixture("auq_multiselect_fresh_tmux_capture.txt"))
    side_file = _write_side_file(_SESSION_ID, _safeguards_input())
    await _render(scenario, wid)
    toggles = _prefixes(_callbacks(scenario), CB_ASK_TOGGLE)
    assert len(toggles) == 4

    degraded_pane = """←  ☐ Safeguards  ✔ Submit  →

?

❯ 1. [ ] A) Verify cursor row from tmux pane before Space
  2. [ ] B) Keep PreToolUse side file alive across toggles
  3. [ ] C) Suppress tabbed multi-question forms
  4. [ ] D) Add Submit and Cancel buttons
  5. [ ] Type something
     Submit
  6. Chat about this

Enter to select · ↑/↓ to navigate · Esc to cancel
"""
    scenario.tmux.set_pane(wid, degraded_pane)
    await _tap(scenario, toggles[1])
    # The toggle dispatched (digit 2 sent), proving the pin survived the flip.
    assert scenario.tmux.sent_keys == [(wid, "2", False, True)]
    assert side_file.exists(), "aqt toggles must not clean side files"


@pytest.mark.asyncio
async def test_failed_digit_no_ledger_and_side_file_survives(
    scenario: ScenarioHarness,
) -> None:
    wid = _bind(scenario, _fixture("auq_multiselect_fresh_tmux_capture.txt"))
    side_file = _write_side_file(_SESSION_ID, _safeguards_input())
    await _render(scenario, wid)
    opt2 = _prefixes(_callbacks(scenario), CB_ASK_TOGGLE)[1]
    scenario.tmux.send_keys_response = False
    await _tap(scenario, opt2)
    assert scenario.tmux.sent_keys == [(wid, "2", False, True)]
    assert side_file.exists()
    assert not (app_dir() / auq_ledger.LEDGER_FILENAME).exists()


@pytest.mark.asyncio
async def test_wrong_user_toggle_does_not_dispatch_or_cleanup(
    scenario: ScenarioHarness,
) -> None:
    wid = _bind(scenario, _fixture("auq_multiselect_fresh_tmux_capture.txt"))
    side_file = _write_side_file(_SESSION_ID, _safeguards_input())
    await _render(scenario, wid)
    await _tap(scenario, _prefixes(_callbacks(scenario), CB_ASK_TOGGLE)[1], user_id=999)
    assert not scenario.tmux.sent_keys
    assert side_file.exists()


@pytest.mark.asyncio
async def test_status_poll_rerender_after_toggle_keeps_side_file_and_options_complete(
    scenario: ScenarioHarness,
) -> None:
    wid = _bind(scenario, _fixture("auq_multiselect_fresh_tmux_capture.txt"))
    side_file = _write_side_file(_SESSION_ID, _safeguards_input())
    await _render(scenario, wid)
    await _tap(scenario, _prefixes(_callbacks(scenario), CB_ASK_TOGGLE)[1])
    await _render(scenario, wid)
    assert side_file.exists()
    assert len(_prefixes(_callbacks(scenario), CB_ASK_TOGGLE)) == 4


@pytest.mark.asyncio
async def test_multi_question_suppresses_toggle_buttons(
    scenario: ScenarioHarness,
) -> None:
    wid = _bind(scenario, _fixture("auq_multiselect_fresh_tmux_capture.txt"))
    _write_side_file(_SESSION_ID, _multi_question_input())
    await _render(scenario, wid)
    assert not _prefixes(_callbacks(scenario), CB_ASK_TOGGLE)


@pytest.mark.asyncio
async def test_single_select_unaffected_still_aqp_digit_enter(
    scenario: ScenarioHarness,
) -> None:
    pane = """Pick one.

❯ 1. A) One
  2. B) Two
Enter to select · ↑/↓ to navigate · Esc to cancel
"""
    wid = _bind(scenario, pane)
    await _render(scenario, wid)
    picks = _prefixes(_callbacks(scenario), CB_ASK_PICK)
    assert len(picks) == 2
    assert not _prefixes(_callbacks(scenario), CB_ASK_TOGGLE)
    await _tap(scenario, picks[0])
    assert scenario.tmux.sent_keys[:2] == [
        (wid, "1", False, True),
        (wid, "Enter", False, False),
    ]


def test_aqt_prefix_is_registered() -> None:
    from cctelegram.callback_dispatcher.registry import lookup

    entry = lookup("aqt:route:fp:2:token")
    assert entry is not None
    assert entry.executor_name == "execute_interactive_callback"


# ── Review-screen "Submit answers" stale-token-after-nav race (RED gate) ────
#
# The two review fixtures are the SAME live Claude Code v2.1.161 multi-select
# review screen; only the terminal cursor ``❯`` moved between
# ``1. Submit answers`` and ``2. Cancel`` (a ↑/↓ nav). On current main the
# cursor is in the form fingerprint, so a nav rotates the Submit pick token out
# from under the still-displayed button. The fix makes the review fingerprint
# cursor-blind (token survives) + relaxes the Submit guard + makes the
# is_review_submit tag cursor-blind.

_REVIEW_SUBMIT_FIXTURE = "auq_multiselect_review_cursor_submit.txt"
_REVIEW_CANCEL_FIXTURE = "auq_multiselect_review_cursor_cancel.txt"


def _token_of(callback_data: str) -> str:
    # aqp:<route_hash>:<fp8>:<opt>:<token> — token is the final colon segment.
    return callback_data.split(":")[-1]


@pytest.mark.asyncio
async def test_review_submit_after_poller_rerender_dispatches(
    scenario: ScenarioHarness,
) -> None:
    """RED pre-fix / GREEN post-fix — the steady-state ``peek_none`` path.

    Render the review screen (cursor on Submit) → capture the ✅ Submit ``aqp:``
    callback the Telegram client still shows. A ↑/↓ nav moves the live pane to
    cursor-on-Cancel; the poller re-render re-mints (FRESH path, new fingerprint
    = new cache_key) and the stale-row hygiene pops the prior Submit token →
    ``peek()`` is None. Tapping the displayed Submit button then hits the
    ``peek_none`` branch and refreshes the card WITHOUT dispatching.

    The assertion is the post-fix behaviour (the tap dispatches ``"1"``+``Enter``).
    RED on current main: the review fingerprints differ (cursor in canonical),
    so the poller re-mint pops the displayed Submit token (``peek_none``), the
    card refreshes, and NOTHING is sent → the assertion fails. GREEN after the
    fix: cursor-blind fp → cache reuse keeps the SAME token alive → dispatch.
    """
    wid = _bind(scenario, _fixture(_REVIEW_SUBMIT_FIXTURE))
    _write_side_file(_SESSION_ID, _two_toggled_input())

    await _render(scenario, wid)
    submit_cb = _prefixes(_callbacks(scenario), CB_ASK_PICK)[0]

    # ↑/↓ nav → poller re-render against the cursor-on-Cancel pane.
    scenario.tmux.set_pane(wid, _fixture(_REVIEW_CANCEL_FIXTURE))
    await _render(scenario, wid)

    scenario.tmux.sent_keys.clear()
    await _tap(scenario, submit_cb)
    # Post-fix the token survives (cache reuse, byte-identical keyboard) and the
    # tap dispatches "1" + Enter. RED on main: the token was popped (peek_none),
    # the card refreshes, and nothing is sent.
    assert scenario.tmux.sent_keys[-2:] == [
        (wid, "1", False, True),
        (wid, "Enter", False, False),
    ]


@pytest.mark.asyncio
async def test_review_submit_sub_poll_window_dispatches(
    scenario: ScenarioHarness,
) -> None:
    """RED pre-fix / GREEN post-fix — the sub-poll ``stale_form`` path.

    Capture the Submit ``aqp:`` callback at cursor-on-Submit, then move the live
    pane to cursor-on-Cancel WITHOUT a re-render (the user taps within ~1s of the
    nav, before the 1 Hz poller re-renders). The minted token is still resident,
    but ``validate_and_consume`` reparses the live pane and the fingerprint no
    longer matches → ``stale_form`` → refresh, no dispatch.

    RED today: cursor-on-Cancel live parse fingerprint != minted cursor-on-Submit
    fingerprint → stale_form → no keystrokes. Post-fix the review fingerprint is
    cursor-blind so the parse matches and ``"1"``+``Enter`` dispatch.
    """
    wid = _bind(scenario, _fixture(_REVIEW_SUBMIT_FIXTURE))
    _write_side_file(_SESSION_ID, _two_toggled_input())

    await _render(scenario, wid)
    submit_cb = _prefixes(_callbacks(scenario), CB_ASK_PICK)[0]

    # Live pane moved to cursor-on-Cancel; NO re-render (sub-poll-cycle window).
    scenario.tmux.set_pane(wid, _fixture(_REVIEW_CANCEL_FIXTURE))

    scenario.tmux.sent_keys.clear()
    await _tap(scenario, submit_cb)
    assert scenario.tmux.sent_keys[-2:] == [
        (wid, "1", False, True),
        (wid, "Enter", False, False),
    ]


@pytest.mark.asyncio
async def test_review_first_render_cursor_on_cancel_tags_submit_and_checkmark(
    scenario: ScenarioHarness,
) -> None:
    """RED pre-fix / GREEN post-fix — the cursor-blind ``is_review_submit`` tag.

    A card whose FIRST render already has the terminal cursor on Cancel must
    still tag option 1 ("Submit answers") as ``is_review_submit=True`` and show
    the ✅ prefix on its button — otherwise the relaxed Submit guard is skipped
    and the ✅ affordance never appears.

    This is the MEANINGFUL RED assertion: on current main the tag is
    cursor-derived (``form.is_review_screen and opt.cursor and opt.number == 1``)
    so with the cursor on Cancel the Submit row tags ``is_review_submit=False``
    and the button text is "1. Submit answers" (no ✅). A dispatch-only assertion
    would NOT be RED here (a False tag SKIPS the guard, so a tap would dispatch).
    """
    wid = _bind(scenario, _fixture(_REVIEW_CANCEL_FIXTURE))
    _write_side_file(_SESSION_ID, _two_toggled_input())

    await _render(scenario, wid)

    markup = _last_markup(scenario)
    buttons = [b for row in markup.inline_keyboard for b in row]
    pick_buttons = [b for b in buttons if b.callback_data.startswith(CB_ASK_PICK)]
    assert pick_buttons, "expected aqp: review pick buttons"

    # Option 1 (Submit answers) is the :1: callback.
    submit_btn = next(b for b in pick_buttons if b.callback_data.split(":")[3] == "1")

    # RED on main: cursor-derived tag is False → no ✅ prefix, no review-submit tag.
    assert "✅" in submit_btn.text
    submit_entry = pick_token.peek(_token_of(submit_btn.callback_data))
    assert submit_entry is not None
    assert submit_entry.is_review_submit is True
