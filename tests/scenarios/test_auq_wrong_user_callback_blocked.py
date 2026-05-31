"""Scenario: a non-owner clicking a dispatched AUQ pick is blocked.

Wave 3 §7.2 P1 contract (wrong-user replay):
  - Owner taps an AUQ pick button → ledger writes ``dispatched`` for the
    stable key.
  - Intruder in the same topic taps the SAME callback_data. The intruder
    has no live pick token for this key, so the collision-defense peek
    can't promote them.
  - Handler must answer ``WRONG_USER_PICK_TEXT`` and MUST NOT leak the
    owner's option label via the "Action already received" path.
  - The owner's ledger row stays put; subsequent owner-side retries
    still see "Action already received: <label>".

This is the wave's authorization invariant. A leak here would let a
shared-topic intruder learn which option an owner picked just by
clicking the same callback_data.
"""

from __future__ import annotations

import time

import pytest

from cctelegram import bot as bot_module
from cctelegram.handlers import auq_ledger
from cctelegram.handlers.callback_data import CB_ASK_PICK
from cctelegram.handlers import pick_token
from tests.conftest import ScenarioHarness, make_update_callback


pytestmark = pytest.mark.scenario


_OWNER_ID = 12345
_INTRUDER_ID = 99999
_WINDOW_NAME = "repo"
_CWD = "/repo"
_THREAD_ID = 42
_FINGERPRINT = "ff" * 20
_OPT = 1
_LABEL = "Yes"


@pytest.mark.asyncio
async def test_intruder_click_after_dispatch_blocked_without_label_leak(
    scenario: ScenarioHarness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    auq_ledger.reset_for_tests(
        path=tmp_path / "auq_action_ledger.jsonl",
        start_time=time.time(),
    )
    wid = scenario.add_window(window_name=_WINDOW_NAME, cwd=_CWD)
    scenario.bind_thread(
        thread_id=_THREAD_ID, window_id=wid, display_name=_WINDOW_NAME, cwd=_CWD
    )

    # Owner mints a pick token and the ledger lands in ``dispatched``.
    entry = pick_token.PickTokenEntry(
        window_id=wid,
        user_id=_OWNER_ID,
        thread_id=_THREAD_ID,
        fingerprint=_FINGERPRINT,
        option_number=_OPT,
        option_label=_LABEL,
        is_review_submit=False,
        expires_at=time.monotonic() + 300.0,
        source_kind="pane",
        source_fingerprint="sfp",
        row_generation=1,
    )
    token = pick_token.mint(entry)
    route_hash = auq_ledger.make_route_hash(_OWNER_ID, _THREAD_ID, wid)
    fp8 = _FINGERPRINT[:8]
    callback_data = f"{CB_ASK_PICK}{route_hash}:{fp8}:{_OPT}:{token}"
    ledger_key = auq_ledger.make_ledger_key(route_hash, fp8, _OPT)
    auq_ledger.record(
        ledger_key,
        state="accepted",
        user_id=_OWNER_ID,
        window_id=wid,
        full_fingerprint=_FINGERPRINT,
        option_number=_OPT,
        option_label=_LABEL,
    )
    auq_ledger.record(ledger_key, state="dispatched")

    # Intruder taps the same callback_data.
    update = make_update_callback(
        callback_data, thread_id=_THREAD_ID, user_id=_INTRUDER_ID
    )
    await bot_module.callback_handler(update, scenario.context)

    update.callback_query.answer.assert_awaited()
    answer_text = update.callback_query.answer.await_args.args[0]
    show_alert = update.callback_query.answer.await_args.kwargs.get("show_alert")
    assert answer_text == "This control isn't yours."
    assert show_alert is True
    # CRITICAL: option label must NOT leak.
    assert _LABEL not in answer_text
    # No tmux keystroke sent.
    digit_sends = [
        (sent_wid, keys)
        for sent_wid, keys, _, _ in scenario.tmux.sent_keys
        if sent_wid == wid and keys.isdigit()
    ]
    assert digit_sends == []
    # Owner's ledger row stays untouched.
    row = auq_ledger.lookup(ledger_key)
    assert row is not None
    assert row.state == "dispatched"
    assert row.user_id == _OWNER_ID
    auq_ledger.reset_for_tests()
