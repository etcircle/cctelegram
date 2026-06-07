"""Scenario coverage for AskUserQuestion multi-select rendering and toggles.

Exercises the public Telegram callback seam for the PR-C ``aqt:`` toggle path:
render uses the pane-aware side-file source, toggles send a bare digit without
ledgering or consuming sibling tokens, and Submit/Cancel remains the existing
review-screen ``aqp:`` flow.

Keystroke model: the multi-select TOGGLE path (``aqt:``) still dispatches a BARE
DIGIT (this fix is deferred for toggles), so those assertions stay bare-digit.
The ``aqp:`` Submit/single-select path follows the v2.1.168 model — arrow-navigate
the live cursor to the target, then ``Enter`` (no bare digit). Those review-Submit
dispatch tests drive a cursor-aware advancing fake (``_AdvancingPicker``) so the
post-nav verify + post-Enter confirm observe the real form.
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
from cctelegram.tmux_manager import tmux_manager as _real_tmux
from cctelegram.utils import app_dir
from tests.conftest import ScenarioHarness, make_update_callback, render_cursor

pytestmark = pytest.mark.scenario

_FIXTURES = Path(__file__).parents[1] / "cctelegram" / "fixtures"
_SESSION_ID = "11111111-1111-4111-8111-111111111111"

# A resolved (non-picker) pane: no AUQ marker phrases → the v2.1.168 confirm step
# reads a Submit / single-select pick as positively RESOLVED.
_RESOLVED_PANE = "user@host repo % \n"


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text()


class _AdvancingPicker:
    """Cursor-aware advancing fake for the v2.1.168 ``aqp:`` Submit / single-select
    dispatch (overrides ``send_keys`` + ``capture_pane`` on the scenario's tmux).

    Models ONE picker screen: ``Down``/``Up`` move the cursor over ``n_nav``
    navigable rows (wrapping); ``Enter`` from a real option (1..``n_real``)
    resolves the tool (the picker disappears → ``_RESOLVED_PANE``). ``capture_pane``
    is STATEFUL — it renders ``pane`` with the cursor on its live row until the
    resolving Enter — so the dispatch's post-nav verify and post-Enter confirm both
    observe a consistent live form.

    ``initial_cursor`` seeds the cursor where the live pane already shows ``❯`` so
    the dispatch computes the right nav delta (e.g. cursor-on-Cancel ⇒ 2).
    """

    def __init__(
        self,
        scenario: ScenarioHarness,
        wid: str,
        pane: str,
        *,
        n_real: int,
        n_nav: int,
        initial_cursor: int = 1,
    ) -> None:
        self._fake = scenario.tmux
        self._wid = wid
        self._pane = pane
        self._n_real = n_real
        self._n_nav = n_nav
        self.cursor = initial_cursor
        self.resolved = False

    async def send_keys(
        self, window_id: str, keys: str, enter: bool = True, literal: bool = True
    ) -> bool:
        self._fake.sent_keys.append((window_id, keys, enter, literal))
        if window_id != self._wid or self.resolved:
            return window_id in self._fake.windows
        if keys == "Down":
            self.cursor = self.cursor + 1 if self.cursor < self._n_nav else 1
        elif keys == "Up":
            self.cursor = self.cursor - 1 if self.cursor > 1 else self._n_nav
        elif keys == "Enter":
            if 1 <= self.cursor <= self._n_real:
                self.resolved = True
        return window_id in self._fake.windows

    async def capture_pane(
        self, window_id: str, with_ansi: bool = False, scrollback_lines: int = 0
    ) -> str:
        del with_ansi, scrollback_lines
        if window_id != self._wid:
            return ""
        if self.resolved:
            return _RESOLVED_PANE
        return render_cursor(self._pane, self.cursor)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> _AdvancingPicker:
        for target in (_real_tmux, self._fake):
            monkeypatch.setattr(target, "send_keys", self.send_keys, raising=False)
            monkeypatch.setattr(
                target, "capture_pane", self.capture_pane, raising=False
            )
        return self


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
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    wid = _bind(scenario, _fixture("auq_multiselect_fresh_tmux_capture.txt"))
    side_file = _write_side_file(_SESSION_ID, _safeguards_input())

    await _render(scenario, wid)
    callbacks = _callbacks(scenario)
    toggles = _prefixes(callbacks, CB_ASK_TOGGLE)
    assert len(toggles) == 4
    assert not _prefixes(callbacks, CB_ASK_PICK)
    assert not any(cb.startswith(("aqm:", "aqs:", "aqx:")) for cb in callbacks)

    # Toggle path is UNCHANGED — a bare digit (deferred fast-follow).
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

    # The review Submit is now an ``aqp:`` arrows+Enter dispatch. The ready-to-submit
    # pane has the cursor on Submit (option 1), so the bot sends ONLY "Enter"
    # (delta=0) — never a bare digit on the aqp: path. Install the cursor-aware
    # advancing fake for the Submit step (the resolving Enter clears the picker).
    _AdvancingPicker(
        scenario,
        wid,
        _fixture("auq_multiselect_ready_to_submit_tmux_capture.txt"),
        n_real=2,
        n_nav=2,
        initial_cursor=1,
    ).install(monkeypatch)
    scenario.tmux.sent_keys.clear()
    submit = _prefixes(review_callbacks, CB_ASK_PICK)[0]
    await _tap(scenario, submit)
    assert scenario.tmux.sent_keys[-1:] == [
        (wid, "Enter", False, False),
    ]
    assert not any(lit and k.isdigit() for _w, k, _e, lit in scenario.tmux.sent_keys)
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
async def test_single_select_aqp_navigates_then_enter_no_bare_digit(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
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
    # v2.1.168: a single-select pick navigates the cursor to the target with
    # arrows, then presses Enter — NO bare digit on the aqp: path. The cursor is
    # on option 1 (delta=0), so the dispatch sends ONLY "Enter".
    _AdvancingPicker(scenario, wid, pane, n_real=2, n_nav=2, initial_cursor=1).install(
        monkeypatch
    )
    scenario.tmux.sent_keys.clear()
    await _tap(scenario, picks[0])
    assert scenario.tmux.sent_keys[-1:] == [
        (wid, "Enter", False, False),
    ]
    assert not any(lit and k.isdigit() for _w, k, _e, lit in scenario.tmux.sent_keys)


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
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED pre-fix / GREEN post-fix — the steady-state ``peek_none`` path.

    Render the review screen (cursor on Submit) → capture the ✅ Submit ``aqp:``
    callback the Telegram client still shows. A ↑/↓ nav moves the live pane to
    cursor-on-Cancel; the poller re-render re-mints (FRESH path, new fingerprint
    = new cache_key) and the stale-row hygiene pops the prior Submit token →
    ``peek()`` is None. Tapping the displayed Submit button then hits the
    ``peek_none`` branch and refreshes the card WITHOUT dispatching.

    The assertion is the post-fix behaviour (the tap dispatches arrows+Enter, no
    bare digit). RED on current main: the review fingerprints differ (cursor in
    canonical), so the poller re-mint pops the displayed Submit token
    (``peek_none``), the card refreshes, and NOTHING is sent → the assertion
    fails. GREEN after the fix: cursor-blind fp → cache reuse keeps the SAME token
    alive → dispatch.
    """
    wid = _bind(scenario, _fixture(_REVIEW_SUBMIT_FIXTURE))
    _write_side_file(_SESSION_ID, _two_toggled_input())

    await _render(scenario, wid)
    submit_cb = _prefixes(_callbacks(scenario), CB_ASK_PICK)[0]

    # ↑/↓ nav → poller re-render against the cursor-on-Cancel pane.
    scenario.tmux.set_pane(wid, _fixture(_REVIEW_CANCEL_FIXTURE))
    await _render(scenario, wid)

    # The live pane now has the cursor on Cancel (option 2). Tapping the still-shown
    # Submit (option 1) navigates ``Up`` to reach it, then ``Enter`` — install the
    # cursor-aware advancing fake seeded at cursor-on-Cancel so the post-nav verify
    # sees the cursor land on Submit and the resolving Enter clears the picker.
    _AdvancingPicker(
        scenario,
        wid,
        _fixture(_REVIEW_CANCEL_FIXTURE),
        n_real=2,
        n_nav=2,
        initial_cursor=2,
    ).install(monkeypatch)
    scenario.tmux.sent_keys.clear()
    await _tap(scenario, submit_cb)
    # Post-fix the token survives (cache reuse, byte-identical keyboard) and the
    # tap dispatches arrows+Enter (Up to reach Submit, then Enter — v2.1.168, no
    # bare digit). RED on main: the token was popped (peek_none), card refreshes,
    # and nothing is sent.
    assert scenario.tmux.sent_keys == [
        (wid, "Up", False, False),
        (wid, "Enter", False, False),
    ]
    assert not any(lit and k.isdigit() for _w, k, _e, lit in scenario.tmux.sent_keys)


@pytest.mark.asyncio
async def test_review_submit_sub_poll_window_dispatches(
    scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED pre-fix / GREEN post-fix — the sub-poll ``stale_form`` path.

    Capture the Submit ``aqp:`` callback at cursor-on-Submit, then move the live
    pane to cursor-on-Cancel WITHOUT a re-render (the user taps within ~1s of the
    nav, before the 1 Hz poller re-renders). The minted token is still resident,
    but ``validate_and_consume`` reparses the live pane and the fingerprint no
    longer matches → ``stale_form`` → refresh, no dispatch.

    RED today: cursor-on-Cancel live parse fingerprint != minted cursor-on-Submit
    fingerprint → stale_form → no keystrokes. Post-fix the review fingerprint is
    cursor-blind so the parse matches and the v2.1.168 arrows+Enter dispatch fires.
    """
    wid = _bind(scenario, _fixture(_REVIEW_SUBMIT_FIXTURE))
    _write_side_file(_SESSION_ID, _two_toggled_input())

    await _render(scenario, wid)
    submit_cb = _prefixes(_callbacks(scenario), CB_ASK_PICK)[0]

    # Live pane moved to cursor-on-Cancel; NO re-render (sub-poll-cycle window). The
    # cursor-aware advancing fake is seeded at cursor-on-Cancel so the dispatch
    # navigates ``Up`` to Submit (option 1) then commits with ``Enter``.
    _AdvancingPicker(
        scenario,
        wid,
        _fixture(_REVIEW_CANCEL_FIXTURE),
        n_real=2,
        n_nav=2,
        initial_cursor=2,
    ).install(monkeypatch)

    scenario.tmux.sent_keys.clear()
    await _tap(scenario, submit_cb)
    # Arrows+Enter (Up to Submit, then Enter) — no bare digit on the aqp: path.
    assert scenario.tmux.sent_keys == [
        (wid, "Up", False, False),
        (wid, "Enter", False, False),
    ]
    assert not any(lit and k.isdigit() for _w, k, _e, lit in scenario.tmux.sent_keys)


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
