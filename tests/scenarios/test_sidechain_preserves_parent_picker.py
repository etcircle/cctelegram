"""Scenario: a SIDECHAIN / background-agent block must NOT tear down the
PARENT route's genuinely-live interactive surface.

Bug — 2026-06-23, DiCopilot @4 / thread 378 (pid 84741). A parent session
posted an AskUserQuestion ("How should Wave C proceed? 1. Floor-only
2. Defer Wave C 3. Full re-architecture") and BLOCKED on it for ~49 minutes
(tool_use 11:28:51Z → tool_result 12:17:55Z; the owner picked opt=3 via live
cursor nav, so the picker was genuinely live the whole time). Meanwhile the
topic ran background Workflows whose sub-agents streamed output ("Ruff clean
across all changed files…", Bash calls). The monitor emits each sidechain
block as a ``NewMessage`` with ``session_id=parent_session_id`` and a non-None
``subagent_key`` ("sub:<parent>:…", session_monitor.py:1599-1614), so it
routes to the PARENT's route.

``bot.handle_new_message``'s interactive-HANDLING branch (bot.py:936) is gated
on ``msg.subagent_key is None``, but the destructive interactive-TEARDOWN
branch (bot.py:1046, ``if has_interactive_surface(...): clear_interactive_msg();
forget_ask_tool_input()``) was NOT — a day-one (v0.1.0) asymmetry. So every
sidechain block ``topic_delete``-d the live picker and popped the by-window
``_auq_context_posted`` dedup marker (interactive_ui.py:443). The 1Hz poller
then re-detected the still-live pane AUQ and re-posted the ctx card + picker —
~28× over 18 minutes.

Fix: gate BOTH the teardown (bot.py:1046) and the AUQ-``tool_result``
invalidation + ledger-release (bot.py:1016) on ``msg.subagent_key is None``,
mirroring the handling branch. A sidechain block must never tear down the
parent's foreground prompt; a genuine PARENT block still clears it.
"""

from __future__ import annotations

import time

import pytest

from cctelegram import bot as bot_module
from cctelegram import md_capture
from cctelegram.handlers import auq_ledger, interactive_ui
from cctelegram.session_monitor import NewMessage
from tests.conftest import ScenarioHarness


pytestmark = pytest.mark.scenario


_SIDECHAIN_KEY = "sub:sess-1:wf_0f5ecd88:agent-a3292e2cc1d6be872"
_SIDECHAIN_TEXT = (
    "Ruff clean across all changed files. Let me also run the broader ruff check."
)


@pytest.fixture
def cc_tmp(tmp_path, monkeypatch):
    """Isolate ``app_dir()`` to a tmp dir so the md_capture EPM-plan marker
    reads/writes never touch the real ~/.cc-telegram (``app_dir()`` reads the
    env on each call). Mirrors test_bug2_prose_before_picker.py's fixture."""
    monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
    return tmp_path


def _seed_live_auq_surface(scenario: ScenarioHarness, wid: str, thread_id: int) -> None:
    """Seed a published live AUQ picker + its '📋 full details' ctx marker for a
    route, exactly as ``handle_interactive_ui`` would leave them."""
    interactive_ui._interactive_mode[(scenario.user_id, thread_id)] = wid
    interactive_ui._interactive_msgs[(scenario.user_id, thread_id)] = 99999
    interactive_ui._auq_context_posted[wid] = "form:75c6b3397e34850c"


@pytest.mark.asyncio
async def test_sidechain_text_block_preserves_live_auq_surface_and_ctx_marker(
    scenario: ScenarioHarness,
) -> None:
    """A background sub-agent's text block routed to the parent route while a
    parent AUQ picker is live must NOT clear the picker or pop the ctx marker.

    Pre-fix: bot.py:1046 fires unconditionally → the picker is topic_delete-d
    and ``forget_ask_tool_input`` pops ``_auq_context_posted`` → the poller
    re-posts (the ~28× duplication). Post-fix: the ``subagent_key is None`` gate
    keeps the surface + marker intact.
    """
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(
        thread_id=42,
        window_id=wid,
        display_name="repo",
        cwd="/repo",
        session_id="sess-1",
    )
    _seed_live_auq_surface(scenario, wid, 42)

    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",  # sidechain blocks carry the PARENT session id
            text=_SIDECHAIN_TEXT,
            content_type="text",
            role="assistant",
            subagent_key=_SIDECHAIN_KEY,
        ),
        scenario.bot,
    )

    assert interactive_ui.has_interactive_surface(scenario.user_id, 42) is True, (
        "a sidechain block must NOT tear down the parent's live interactive "
        "surface (the picker was deleted by the ungated bot.py:1046 teardown)"
    )
    assert interactive_ui._auq_context_posted.get(wid) is not None, (
        "a sidechain block must NOT pop the by-window ctx dedup marker — popping "
        "it re-arms the poller's ctx-card re-post (the ~28× duplication trigger)"
    )


@pytest.mark.asyncio
async def test_parent_text_block_still_clears_live_auq_surface(
    scenario: ScenarioHarness,
) -> None:
    """Regression pin: a GENUINE parent non-interactive block (subagent_key is
    None) — e.g. narration the instant a bypassPermissions auto-resolution moved
    past the picker — must STILL clear the live card + drop the ctx marker. The
    gate must only exclude sidechain blocks, never parent blocks.
    """
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(
        thread_id=42,
        window_id=wid,
        display_name="repo",
        cwd="/repo",
        session_id="sess-1",
    )
    _seed_live_auq_surface(scenario, wid, 42)

    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="Done — moving on.",
            content_type="text",
            role="assistant",
            subagent_key=None,  # the parent's own block
        ),
        scenario.bot,
    )

    assert interactive_ui.has_interactive_surface(scenario.user_id, 42) is False, (
        "a genuine parent block must still tear down a resolved card"
    )
    assert interactive_ui._auq_context_posted.get(wid) is None, (
        "a genuine parent block must still drop the ctx marker"
    )


@pytest.mark.asyncio
async def test_sidechain_auq_tool_result_does_not_release_parent_ledger(
    scenario: ScenarioHarness,
) -> None:
    """Latent twin (bot.py:1016): a SUB-AGENT that itself runs AskUserQuestion
    emits a tool_result block carrying ``tool_name='AskUserQuestion'`` (plumbed
    through at session_monitor.py:1606). Pre-fix this trips the parent's
    AUQ-tool_result invalidation → ``auq_ledger_release_window`` writes
    ``released`` over the PARENT window's rows (unmasking a dispatched-but-
    unresolved single-use brake) and clears the parent cache. Post-fix the
    ``subagent_key is None`` gate keeps the parent ledger AND the parent's
    pending-AUQ cache untouched (both halves of the bot.py:1016 invalidation —
    Hermes review P3-1).
    """
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(
        thread_id=42,
        window_id=wid,
        display_name="repo",
        cwd="/repo",
        session_id="sess-1",
    )

    auq_ledger.record(
        "rh:fp:2",
        state="accepted",
        user_id=scenario.user_id,
        window_id=wid,
        full_fingerprint="ff" * 20,
        option_number=2,
        option_label="alpha",
    )
    auq_ledger.record("rh:fp:2", state="dispatched")

    # The parent has a genuinely-pending AUQ whose tool_input is cached. A
    # subagent's own AUQ resolution must NOT invalidate this parent cache (the
    # forget_ask_tool_input half of the bot.py:1016 block).
    interactive_ui.remember_ask_tool_input(
        wid, {"questions": [{"question": "parent Q", "options": []}]}, "t-parent-auq"
    )
    assert interactive_ui._last_completed_ask_tool_input.get(wid) is not None

    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="**AskUserQuestion**(subagent question) Answered.",
            content_type="tool_result",
            tool_use_id="t-sub-auq-1",
            tool_name="AskUserQuestion",
            role="assistant",
            subagent_key=_SIDECHAIN_KEY,
        ),
        scenario.bot,
    )

    row = auq_ledger.lookup("rh:fp:2")
    assert row is not None and row.state == "dispatched", (
        "a SUB-AGENT's own AskUserQuestion tool_result must NOT release the "
        "PARENT window's ledger rows"
    )
    assert interactive_ui._last_completed_ask_tool_input.get(wid) is not None, (
        "a SUB-AGENT's own AskUserQuestion tool_result must NOT invalidate the "
        "PARENT's still-pending AUQ tool_input cache"
    )
    assert interactive_ui._last_auq_tool_use_id.get(wid) == "t-parent-auq", (
        "the parent's cached tool_use_id must survive a sidechain AUQ tool_result"
    )


@pytest.mark.asyncio
async def test_sidechain_block_preserves_live_epm_surface_and_plan_marker(
    scenario: ScenarioHarness, cc_tmp
) -> None:
    """ExitPlanMode is equally vulnerable to bot.py:1046 (``has_interactive_surface``
    is UI-type-agnostic) and is fixed by the same gate. A sidechain block must
    not delete the live EPM picker nor reap the ``epm_plan_shown_live`` marker
    (``forget_ask_tool_input`` → ``md_capture.teardown_session``), which would
    re-enable a duplicate '📋 Plan' body re-post on the next poll re-render.
    """
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(
        thread_id=42,
        window_id=wid,
        display_name="repo",
        cwd="/repo",
        session_id="sess-1",
    )
    # A live EPM picker (no AUQ ctx marker — EPM has none).
    interactive_ui._interactive_mode[(scenario.user_id, 42)] = wid
    interactive_ui._interactive_msgs[(scenario.user_id, 42)] = 12345
    # Its plan body was already posted live before the card.
    md_capture.msg_display_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    md_capture.record_epm_plan_shown_live(
        "sess-1", norm_hash="planhash123", shown_at=time.time()
    )
    assert md_capture.was_epm_plan_shown_live("sess-1", "planhash123") is True

    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text=_SIDECHAIN_TEXT,
            content_type="text",
            role="assistant",
            subagent_key=_SIDECHAIN_KEY,
        ),
        scenario.bot,
    )

    assert interactive_ui.has_interactive_surface(scenario.user_id, 42) is True, (
        "a sidechain block must NOT delete the parent's live ExitPlanMode picker"
    )
    assert md_capture.was_epm_plan_shown_live("sess-1", "planhash123") is True, (
        "a sidechain block must NOT reap the EPM plan marker (teardown_session) — "
        "reaping it re-enables a duplicate '📋 Plan' re-post on the next re-render"
    )


@pytest.mark.asyncio
async def test_sidechain_block_preserves_all_sibling_window_surfaces(
    scenario: ScenarioHarness,
) -> None:
    """Multi-window (double-`--resume`) fan-out: one session_id can resolve to
    >1 route. A sidechain block fanned out across sibling windows must leave
    EVERY route's live surface intact post-fix (the teardown is per-route inside
    the handle_new_message loop, so the gate must hold for each).
    """
    wid_a = scenario.add_window(window_name="repo-a", cwd="/repo")
    wid_b = scenario.add_window(window_name="repo-b", cwd="/repo")
    scenario.bind_thread(
        thread_id=42,
        window_id=wid_a,
        display_name="repo-a",
        cwd="/repo",
        session_id="sess-1",
    )
    scenario.bind_thread(
        thread_id=43,
        window_id=wid_b,
        display_name="repo-b",
        cwd="/repo",
        session_id="sess-1",
    )
    _seed_live_auq_surface(scenario, wid_a, 42)
    _seed_live_auq_surface(scenario, wid_b, 43)

    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text=_SIDECHAIN_TEXT,
            content_type="text",
            role="assistant",
            subagent_key=_SIDECHAIN_KEY,
        ),
        scenario.bot,
    )

    assert interactive_ui.has_interactive_surface(scenario.user_id, 42) is True
    assert interactive_ui.has_interactive_surface(scenario.user_id, 43) is True
    assert interactive_ui._auq_context_posted.get(wid_a) is not None
    assert interactive_ui._auq_context_posted.get(wid_b) is not None


@pytest.mark.asyncio
async def test_sidechain_block_preserves_live_permission_surface(
    scenario: ScenarioHarness,
) -> None:
    """The generic teardown is UI-type-agnostic (``has_interactive_surface`` is
    keyed only by route), so the gate must protect a live Permission-prompt card
    too — aligning the test proof with the AUQ/EPM/Permission doc wording
    (Hermes review P3-2). A permission card registers ``_interactive_msgs`` with
    no AUQ ctx marker; a sidechain block must not delete it.
    """
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(
        thread_id=42,
        window_id=wid,
        display_name="repo",
        cwd="/repo",
        session_id="sess-1",
    )
    # A live permission-prompt card (no AUQ ctx marker — permission has none).
    interactive_ui._interactive_mode[(scenario.user_id, 42)] = wid
    interactive_ui._interactive_msgs[(scenario.user_id, 42)] = 54321

    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text=_SIDECHAIN_TEXT,
            content_type="text",
            role="assistant",
            subagent_key=_SIDECHAIN_KEY,
        ),
        scenario.bot,
    )

    assert interactive_ui.has_interactive_surface(scenario.user_id, 42) is True, (
        "a sidechain block must NOT delete the parent's live permission-prompt card"
    )
