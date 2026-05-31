"""Scenario: interactive picker safety against wrong-user clicks.

When two users see the same interactive picker card (e.g. shared topic),
a click from a *non-owner* user must:
  - be answered with "This control isn't yours.",
  - NOT consume the legitimate owner's token (CB3 fix — the older
    consume-then-reject path destroyed tokens on wrong-user clicks).

The legitimate owner's subsequent click on the same token then still
lands. This scenario exercises the public keyed
``aqp:<route_hash>:<fp8>:<opt>:<token>`` callback path.

The aqp token map is private state inside ``interactive_ui``; the test
seeds it via the same internal helpers the production keyboard builder
uses. That seam is the natural fixture point for the wrong-user safety
invariant — the assertions themselves operate at the public callback
seam.
"""

from __future__ import annotations

import time

import pytest

from cctelegram import bot as bot_module
from cctelegram.handlers import auq_ledger, interactive_ui, pick_token
from cctelegram.handlers.callback_data import CB_ASK_PICK
from tests.conftest import ScenarioHarness, make_update_callback


pytestmark = pytest.mark.scenario


_OWNER_ID = 12345  # default test user
_INTRUDER_ID = 99999
_FINGERPRINT = "fp-test"
_OPT = 1


def _keyed_callback(
    *, owner_id: int, thread_id: int, window_id: str, token: str
) -> str:
    """Build the Wave 3 keyed ``aqp:<route_hash>:<fp8>:<opt>:<token>`` shape.

    The route_hash/fp8/opt triplet keys the restart-safe ledger and is the
    only callback shape the dispatcher parses since the legacy
    ``aqp:<token>`` shape was retired.
    """
    route_hash = auq_ledger.make_route_hash(owner_id, thread_id, window_id)
    fp8 = _FINGERPRINT[:8]
    return f"{CB_ASK_PICK}{route_hash}:{fp8}:{_OPT}:{token}"


def _seed_pick_token(*, owner_id: int, thread_id: int, window_id: str) -> str:
    entry = pick_token.PickTokenEntry(
        window_id=window_id,
        user_id=owner_id,
        thread_id=thread_id,
        fingerprint=_FINGERPRINT,
        option_number=_OPT,
        option_label="Yes",
        is_review_submit=False,
        expires_at=time.monotonic() + 300.0,
        source_kind="pane",
        source_fingerprint="sfp",
        row_generation=1,
    )
    return pick_token.mint(entry)


@pytest.mark.asyncio
async def test_wrong_user_click_is_rejected_without_consuming_token(
    scenario: ScenarioHarness,
) -> None:
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")
    token = _seed_pick_token(owner_id=_OWNER_ID, thread_id=42, window_id=wid)
    callback_data = _keyed_callback(
        owner_id=_OWNER_ID, thread_id=42, window_id=wid, token=token
    )

    intruder_update = make_update_callback(
        callback_data, thread_id=42, user_id=_INTRUDER_ID
    )
    await bot_module.callback_handler(intruder_update, scenario.context)

    intruder_update.callback_query.answer.assert_awaited()
    answer_text = intruder_update.callback_query.answer.await_args.args[0]
    assert answer_text == "This control isn't yours."
    # Critical CB3 invariant: the wrong-user click did NOT consume the token.
    assert pick_token.peek(token) is not None
    # No tmux keystroke was sent.
    assert scenario.tmux.sent_keys == []


@pytest.mark.asyncio
async def test_stale_token_refreshes_card(
    scenario: ScenarioHarness,
) -> None:
    """A click against an unknown / expired token answers 'Card expired, refreshing.'."""
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")
    # Mark the user in interactive mode so the refresh path has a window to read.
    interactive_ui.set_interactive_mode(_OWNER_ID, wid, 42)

    update = make_update_callback(
        _keyed_callback(
            owner_id=_OWNER_ID, thread_id=42, window_id=wid, token="deadbeefdead"
        ),
        thread_id=42,
        user_id=_OWNER_ID,
    )
    await bot_module.callback_handler(update, scenario.context)

    update.callback_query.answer.assert_awaited()
    answer_text = update.callback_query.answer.await_args.args[0]
    assert "Card expired" in answer_text


@pytest.mark.asyncio
async def test_owner_click_on_stale_form_refreshes_without_keystroke(
    scenario: ScenarioHarness,
) -> None:
    """Owner click whose fingerprint no longer matches the pane → refresh, no tmux send."""
    wid = scenario.add_window(window_name="repo", cwd="/repo", pane_text="no form here")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")
    interactive_ui.set_interactive_mode(_OWNER_ID, wid, 42)
    token = _seed_pick_token(owner_id=_OWNER_ID, thread_id=42, window_id=wid)

    update = make_update_callback(
        _keyed_callback(owner_id=_OWNER_ID, thread_id=42, window_id=wid, token=token),
        thread_id=42,
        user_id=_OWNER_ID,
    )
    await bot_module.callback_handler(update, scenario.context)

    update.callback_query.answer.assert_awaited()
    answer_text = update.callback_query.answer.await_args.args[0]
    # Either "Form changed, refreshing." or similar — the keystroke never fired.
    assert "refreshing" in answer_text.lower() or "expired" in answer_text.lower()
    # No tmux digit keystroke was sent.
    digit_sends = [
        (sent_wid, keys)
        for sent_wid, keys, _, _ in scenario.tmux.sent_keys
        if sent_wid == wid and keys.isdigit()
    ]
    assert digit_sends == []
