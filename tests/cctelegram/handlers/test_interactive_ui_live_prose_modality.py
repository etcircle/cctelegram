"""PR-1 prose-ORDER: ``_maybe_post_live_prose`` selects the emission ANCHOR (and
its eps/lookback tolerances) by modality.

AUQ → ``auq_source.peek_side_file_written_at`` + the AUQ constants; never reads
the EPM poller stamp. ExitPlanMode → ``status_polling.peek_epm_surface_emitted_at``
+ the EPM constants; never reads the ``auq_pending`` side file. The resolved
anchor is threaded into ``select_fresh_prose`` as the additive-OR upper-bound
source.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from cctelegram import md_capture
from cctelegram.handlers import (
    auq_source,
    interactive_ui,
    message_queue,
    status_polling,
)

_SID = "feedface-0000-1111-2222-333344445555"


@pytest.fixture(autouse=True)
def _clean_state():
    message_queue._route_user_turn_at.clear()
    interactive_ui._interactive_msgs.clear()
    status_polling._epm_surface_first_seen_at.clear()
    yield
    message_queue._route_user_turn_at.clear()
    interactive_ui._interactive_msgs.clear()
    status_polling._epm_surface_first_seen_at.clear()


@pytest.fixture
def spies(monkeypatch):
    """Spy the two anchor sources + select_fresh_prose; return the recorders."""
    rec = {"auq": [], "epm": [], "sfp_kwargs": None}

    def fake_written_at(session_id):
        rec["auq"].append(session_id)
        return 11111.0

    def fake_epm(user_id, thread_id, window_id):
        rec["epm"].append((user_id, thread_id, window_id))
        return 22222.0

    def fake_sfp(session_id, **kwargs):
        rec["sfp_kwargs"] = kwargs
        return None  # no candidate → no posting; the retry loop is bounded

    monkeypatch.setattr(auq_source, "peek_side_file_written_at", fake_written_at)
    monkeypatch.setattr(status_polling, "peek_epm_surface_emitted_at", fake_epm)
    monkeypatch.setattr(md_capture, "select_fresh_prose", fake_sfp)
    monkeypatch.setattr(interactive_ui, "session_id_for_window", lambda _wid: _SID)
    return rec


@pytest.mark.asyncio
async def test_auq_uses_written_at_and_auq_constants(spies):
    await interactive_ui._maybe_post_live_prose(
        AsyncMock(),
        user_id=1,
        thread_id=100,
        chat_id=42,
        window_id="@0",
        ui_name="AskUserQuestion",
    )
    kw = spies["sfp_kwargs"]
    assert kw is not None
    assert kw["emitted_at"] == 11111.0
    assert kw["emit_anchor_eps_s"] == md_capture._EMIT_ANCHOR_EPS_S
    assert kw["emit_anchor_lookback_s"] == md_capture._EMIT_ANCHOR_LOOKBACK_S
    assert spies["auq"] == [_SID]
    # AUQ path must NOT consult the EPM poller stamp.
    assert spies["epm"] == []


@pytest.mark.asyncio
async def test_epm_uses_poller_stamp_and_epm_constants(spies):
    await interactive_ui._maybe_post_live_prose(
        AsyncMock(),
        user_id=1,
        thread_id=100,
        chat_id=42,
        window_id="@0",
        ui_name="ExitPlanMode",
    )
    kw = spies["sfp_kwargs"]
    assert kw is not None
    assert kw["emitted_at"] == 22222.0
    assert kw["emit_anchor_eps_s"] == md_capture._EMIT_ANCHOR_EPS_EPM_S
    assert kw["emit_anchor_lookback_s"] == md_capture._EMIT_ANCHOR_LOOKBACK_EPM_S
    assert spies["epm"] == [(1, 100, "@0")]
    # EPM EPS must be DISTINCT from the AUQ EPS source: the AUQ side-file
    # accessor must never be consulted for an ExitPlanMode surface.
    assert spies["auq"] == []
