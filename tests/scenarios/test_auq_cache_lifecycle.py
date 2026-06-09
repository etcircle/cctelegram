"""Scenario: AskUserQuestion ``tool_input`` cache lifecycle across AUQ turns.

Bug — 2026-05-21 09:30:21 incident on @40 / msg 34563. A multi-Q AUQ
(D1 + D2 — PlatformAdapter trait width + engine state across 5 threads)
was answered at 09:29:16 BST. The bot's status_polling hysteresis cleared
the picker card at 09:29:16. The JSONL ``tool_result`` line then flushed
at 09:29:31 with no interactive surface on the route, so the bot's
existing ``forget_ask_tool_input`` call — gated on
``has_interactive_surface`` — never fired. The cache kept pointing at
the completed D1+D2 input. A new AUQ (D3) appeared on the pane at
09:30:21; the renderer overlaid the new pane onto the stale D1+D2
question matrix, ``multi-q inference FAILED`` (``pane_opts=0``),
``current_tab_inferred`` defaulted to False, and the user saw D1's
question text rendered as verbatim with pick buttons suppressed — for
~80 seconds, until they sent ``/screenshot`` to see what was actually
on the terminal.

Fix: ``forget_ask_tool_input`` must also fire whenever an
AskUserQuestion ``tool_result`` is observed, independent of whether a
card is currently published. The cache represents "the latest pending
AUQ", not "the most recent seen AUQ regardless of completion".
"""

from __future__ import annotations

import pytest

from cctelegram import bot as bot_module
from cctelegram.handlers import interactive_ui
from cctelegram.session_monitor import NewMessage
from tests.conftest import ScenarioHarness


pytestmark = pytest.mark.scenario


_AUQ_TOOL_INPUT = {
    "questions": [
        {
            "question": "D1 — How wide should the PlatformAdapter trait be?",
            "header": "Trait width",
            "options": [
                {"label": "Thin trait", "description": "Layer-1 helpers."},
                {"label": "Fat trait", "description": "All 8 methods."},
            ],
        },
        {
            "question": "D2 — How does the engine own its state across 5 threads?",
            "header": "Engine state",
            "options": [
                {"label": "Actor / command channel", "description": "Single owner."},
                {
                    "label": "Shared state + locks",
                    "description": "Reference repo's choice.",
                },
            ],
        },
    ]
}


@pytest.mark.asyncio
async def test_auq_tool_result_drops_cache_when_no_card_was_published(
    scenario: ScenarioHarness,
) -> None:
    """tool_result arrives WITHOUT an active interactive surface.

    Pre-fix: cache stays populated indefinitely, mis-rendering the next AUQ.
    Post-fix: cache is dropped on the tool_result, independent of surface state.
    """
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(
        thread_id=42,
        window_id=wid,
        display_name="repo",
        cwd="/repo",
        session_id="sess-1",
    )

    # AUQ tool_use arrives. The cache is populated by bot.handle_new_message's
    # set-mode block. handle_interactive_ui is called against the fake tmux
    # pane (which the harness leaves empty by default), so it returns False —
    # no card is published. clear_interactive_mode runs (handled=False
    # branch), and has_interactive_surface stays False.
    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="**AskUserQuestion**(D1 — How wide should …)",
            content_type="tool_use",
            tool_use_id="t-auq-1",
            tool_name="AskUserQuestion",
            tool_input=_AUQ_TOOL_INPUT,
            role="assistant",
        ),
        scenario.bot,
    )

    assert interactive_ui._last_completed_ask_tool_input.get(wid) is not None, (
        "tool_use should populate the cache via remember_ask_tool_input"
    )
    assert not interactive_ui.has_interactive_surface(scenario.user_id, 42), (
        "no card should be published — handle_interactive_ui bailed on empty "
        "pane and clear_interactive_mode ran from the handled=False branch"
    )

    # AUQ tool_result arrives. Pre-fix this is a no-op: has_interactive_surface
    # is False, so the existing forget_ask_tool_input call is skipped. The
    # cache leaks into the next AUQ's render and the user sees stale content.
    # The fix adds a second branch that fires on any AUQ tool_result.
    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="**AskUserQuestion**(D1 — How wide should …) Answered.",
            content_type="tool_result",
            tool_use_id="t-auq-1",
            tool_name="AskUserQuestion",
            role="assistant",
        ),
        scenario.bot,
    )

    assert interactive_ui._last_completed_ask_tool_input.get(wid) is None, (
        "tool_result must drop the cache even when no interactive surface "
        "exists — otherwise the stale tool_input mis-renders the next AUQ"
    )


@pytest.mark.asyncio
async def test_auq_tool_result_drops_cache_when_card_is_active(
    scenario: ScenarioHarness,
) -> None:
    """Regression sanity check — the pre-existing cleanup path (with a
    published card) must continue to drop the cache. The fix adds a new
    branch; this test pins the legacy branch behavior so the addition
    doesn't accidentally short-circuit it.
    """
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(
        thread_id=42,
        window_id=wid,
        display_name="repo",
        cwd="/repo",
        session_id="sess-1",
    )

    # Seed both the cache and a fake interactive surface so
    # ``has_interactive_surface`` returns True.
    interactive_ui.remember_ask_tool_input(wid, _AUQ_TOOL_INPUT)
    interactive_ui._interactive_mode[(scenario.user_id, 42)] = wid
    interactive_ui._interactive_msgs[(scenario.user_id, 42)] = 99999

    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="**AskUserQuestion**(D1 — How wide should …) Answered.",
            content_type="tool_result",
            tool_use_id="t-auq-1",
            tool_name="AskUserQuestion",
            role="assistant",
        ),
        scenario.bot,
    )

    assert interactive_ui._last_completed_ask_tool_input.get(wid) is None, (
        "with an active surface, the legacy cleanup branch should fire and "
        "drop the cache"
    )


@pytest.mark.asyncio
async def test_auq_tool_result_forgets_before_card_clear(
    scenario: ScenarioHarness, monkeypatch
) -> None:
    """Codex round-2 P2 fix: the AUQ tool_result invalidation
    (forget_ask_tool_input -> unlink side file) must run BEFORE the awaited
    clear_interactive_msg, so a raise in the card clear cannot orphan the side
    file — which would otherwise strand a DEAD card via the status_polling
    side_file_live_for_session gate (the uptime half of the dead-card class).
    """
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(
        thread_id=42,
        window_id=wid,
        display_name="repo",
        cwd="/repo",
        session_id="sess-1",
    )
    # A live interactive surface so the has_interactive_surface clear branch
    # fires (and is rigged to raise).
    interactive_ui._interactive_mode[(scenario.user_id, 42)] = wid
    interactive_ui._interactive_msgs[(scenario.user_id, 42)] = 12345

    order: list[str] = []
    forgotten: list[str] = []

    def tracking_forget(w):
        order.append("forget")
        forgotten.append(w)

    async def raising_clear(*args, **kwargs):
        order.append("clear")
        raise RuntimeError("simulated Telegram clear failure")

    monkeypatch.setattr(bot_module, "forget_ask_tool_input", tracking_forget)
    monkeypatch.setattr(bot_module, "clear_interactive_msg", raising_clear)

    try:
        await bot_module.handle_new_message(
            NewMessage(
                session_id="sess-1",
                text="**AskUserQuestion**(Q?) Answered.",
                content_type="tool_result",
                tool_use_id="t-auq-1",
                tool_name="AskUserQuestion",
                role="assistant",
            ),
            scenario.bot,
        )
    except RuntimeError:
        pass  # clear raised AFTER forget ran — that's the whole point

    assert "forget" in order, "AUQ tool_result must invalidate even if clear raises"
    assert "clear" in order
    assert order.index("forget") < order.index("clear"), (
        "forget_ask_tool_input must run before the awaited clear_interactive_msg"
    )
    assert wid in forgotten


@pytest.mark.asyncio
async def test_auq_tool_result_releases_window_ledger_rows(
    scenario: ScenarioHarness,
) -> None:
    """Wave 2 fix 3b (P1-1 placement) — the explicit AUQ ``tool_result``
    branch in ``bot.handle_new_message`` is the positive-resolution seam
    that releases the window's action-ledger rows, so a same-day
    byte-identical AUQ (same content-derived ``(route_hash, fp8, opt)``
    key) is dispatchable again instead of permanently answering
    "Action already received". Sibling windows' rows must survive
    (window-scoped release; double-`--resume` siblings keep their cards).
    """
    from cctelegram.handlers import auq_ledger

    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(
        thread_id=42,
        window_id=wid,
        display_name="repo",
        cwd="/repo",
        session_id="sess-1",
    )

    def seed(key: str, window_id: str) -> None:
        auq_ledger.record(
            key,
            state="accepted",
            user_id=scenario.user_id,
            window_id=window_id,
            full_fingerprint="ff" * 20,
            option_number=2,
            option_label="alpha",
        )
        auq_ledger.record(key, state="dispatched")

    seed("rh:fp:2", wid)
    seed("rh-sibling:fp:2", "@sibling")

    await bot_module.handle_new_message(
        NewMessage(
            session_id="sess-1",
            text="**AskUserQuestion**(Q?) Answered.",
            content_type="tool_result",
            tool_use_id="t-auq-1",
            tool_name="AskUserQuestion",
            role="assistant",
        ),
        scenario.bot,
    )

    assert auq_ledger.lookup("rh:fp:2") is None, (
        "the AUQ tool_result (positive resolution proof) must release the "
        "window's ledger rows"
    )
    sibling = auq_ledger.lookup("rh-sibling:fp:2")
    assert sibling is not None and sibling.state == "dispatched", (
        "release is window-scoped — another window's unresolved rows survive"
    )
