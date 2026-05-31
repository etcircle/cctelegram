"""Scenario: a second tap on a dispatched AskUserQuestion pick is rejected.

Wave 3 ledger contract:
  - On a successful dispatch, the ledger writes a row in ``dispatched``
    state keyed by the stable ``(route_hash, fp8, opt)`` triplet.
  - A second tap on the same callback_data (same triplet) finds the
    ``dispatched`` row and answers with "Action already received: <label>".
  - The second tap must NOT send another tmux keystroke. Duplicate dispatch
    was the silent risk this wave exists to close.

The ledger key is restart-safe — even after a process restart, the second
tap still hits the ``dispatched`` row in the JSONL ledger on disk.

Exercises the public bot callback handler against a real ledger backed
by a tmp file.
"""

from __future__ import annotations

import time

import pytest

from cctelegram import bot as bot_module
from cctelegram.handlers import auq_ledger, interactive_ui, pick_token
from cctelegram.handlers.callback_data import CB_ASK_PICK
from tests.conftest import ScenarioHarness, make_update_callback


pytestmark = pytest.mark.scenario


_OWNER_ID = 12345
_WINDOW_NAME = "repo"
_CWD = "/repo"
_THREAD_ID = 42
_FINGERPRINT = "ff" * 20
_OPT = 1
_LABEL = "Yes"


def _seed_keyed_pick_token(
    *, owner_id: int, thread_id: int, window_id: str
) -> tuple[str, str]:
    """Mint a pick token and return (callback_data, ledger_key) for it.

    Mirrors the production mint path in ``_build_pick_button_rows`` so the
    callback handler's parse + key reconstruction match exactly.
    """
    entry = pick_token.PickTokenEntry(
        window_id=window_id,
        user_id=owner_id,
        thread_id=thread_id,
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
    route_hash = auq_ledger.make_route_hash(owner_id, thread_id, window_id)
    fp8 = _FINGERPRINT[:8]
    callback_data = f"{CB_ASK_PICK}{route_hash}:{fp8}:{_OPT}:{token}"
    ledger_key = auq_ledger.make_ledger_key(route_hash, fp8, _OPT)
    return callback_data, ledger_key


@pytest.mark.asyncio
async def test_second_tap_after_dispatch_answers_already_received(
    scenario: ScenarioHarness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    # Scope the ledger to a tmp file so the test doesn't touch real state.
    auq_ledger.reset_for_tests(
        path=tmp_path / "auq_action_ledger.jsonl",
        start_time=time.time(),
    )
    wid = scenario.add_window(window_name=_WINDOW_NAME, cwd=_CWD)
    scenario.bind_thread(
        thread_id=_THREAD_ID, window_id=wid, display_name=_WINDOW_NAME, cwd=_CWD
    )
    interactive_ui.set_interactive_mode(_OWNER_ID, wid, _THREAD_ID)
    callback_data, ledger_key = _seed_keyed_pick_token(
        owner_id=_OWNER_ID, thread_id=_THREAD_ID, window_id=wid
    )

    # Pre-seed the ledger to the post-dispatch state to simulate the
    # outcome of an earlier successful tap. The ledger is the persistent
    # source of truth — the in-memory token table state isn't relevant
    # because the handler short-circuits on the ledger row first.
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

    update = make_update_callback(
        callback_data, thread_id=_THREAD_ID, user_id=_OWNER_ID
    )
    await bot_module.callback_handler(update, scenario.context)

    update.callback_query.answer.assert_awaited()
    answer_text = update.callback_query.answer.await_args.args[0]
    assert answer_text == f"Action already received: {_LABEL}"
    # Critical: the second tap MUST NOT send a tmux keystroke.
    digit_sends = [
        (sent_wid, keys)
        for sent_wid, keys, _, _ in scenario.tmux.sent_keys
        if sent_wid == wid and keys.isdigit()
    ]
    assert digit_sends == []
    # Cleanup so the ledger module's global state doesn't leak.
    auq_ledger.reset_for_tests()
