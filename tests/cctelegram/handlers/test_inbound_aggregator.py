"""Tests for the §2.8 inbound aggregator."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from cctelegram.config import config
from cctelegram.handlers import inbound_aggregator


@pytest.fixture(autouse=True)
def _clear_aggregator_state():
    inbound_aggregator._route_pending.clear()
    inbound_aggregator._route_locks.clear()
    yield
    inbound_aggregator._route_pending.clear()
    inbound_aggregator._route_locks.clear()


@pytest.fixture(autouse=True)
def _short_debounce():
    """Use a tiny debounce so tests don't hang waiting on the real 1.5s."""
    original = config.aggregator_debounce_seconds
    config.aggregator_debounce_seconds = 0.05
    yield
    config.aggregator_debounce_seconds = original


@pytest.fixture
def captured_sends():
    sends: list[tuple[str, str]] = []

    async def fake_send(window_id: str, text: str) -> tuple[bool, str]:
        sends.append((window_id, text))
        return True, "ok"

    with patch.object(
        inbound_aggregator.session_manager,
        "send_to_window",
        side_effect=fake_send,
    ):
        yield sends


async def _wait_until_flushed(sends: list, expected: int = 1, timeout: float = 1.0):
    """Poll the sends list until ``expected`` calls land or timeout."""
    elapsed = 0.0
    step = 0.01
    while len(sends) < expected and elapsed < timeout:
        await asyncio.sleep(step)
        elapsed += step


@pytest.mark.asyncio
async def test_single_text_flushes_after_debounce(captured_sends):
    route = (1, 100, "@0")
    await inbound_aggregator.aggregator_offer_text(route, "hello world")
    await _wait_until_flushed(captured_sends, expected=1)
    assert captured_sends == [("@0", "hello world")]


@pytest.mark.asyncio
async def test_consecutive_text_coalesces(captured_sends):
    route = (1, 100, "@0")
    await inbound_aggregator.aggregator_offer_text(route, "first")
    await inbound_aggregator.aggregator_offer_text(route, "second")
    await _wait_until_flushed(captured_sends, expected=1)
    assert len(captured_sends) == 1
    flushed_text = captured_sends[0][1]
    assert "first" in flushed_text
    assert "second" in flushed_text


@pytest.mark.asyncio
async def test_media_group_coalesces_to_one_flush(captured_sends):
    route = (1, 100, "@0")
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/img1.jpg"), "look at these", "mg-1"
    )
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/img2.jpg"), None, "mg-1"
    )
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/img3.jpg"), None, "mg-1"
    )
    await _wait_until_flushed(captured_sends, expected=1)
    assert len(captured_sends) == 1
    flushed = captured_sends[0][1]
    assert flushed.count("look at these") == 1
    assert "/tmp/img1.jpg" in flushed
    assert "/tmp/img2.jpg" in flushed
    assert "/tmp/img3.jpg" in flushed
    # Path arrival order preserved.
    assert (
        flushed.index("img1.jpg")
        < flushed.index("img2.jpg")
        < flushed.index("img3.jpg")
    )
    # Single grouped block: only one "(attachments:" header.
    assert flushed.count("(attachments:") == 1


@pytest.mark.asyncio
async def test_media_group_no_caption_groups_paths(captured_sends):
    route = (1, 100, "@0")
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/a.jpg"), None, "mg-2"
    )
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/b.jpg"), None, "mg-2"
    )
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/c.jpg"), None, "mg-2"
    )
    await _wait_until_flushed(captured_sends, expected=1)
    assert len(captured_sends) == 1
    flushed = captured_sends[0][1]
    assert flushed.startswith("(attachments:")
    assert "/tmp/a.jpg" in flushed
    assert "/tmp/b.jpg" in flushed
    assert "/tmp/c.jpg" in flushed


@pytest.mark.asyncio
async def test_media_group_then_followup_text_appends_once(captured_sends):
    route = (1, 100, "@0")
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/x1.jpg"), "shared caption", "mg-3"
    )
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/x2.jpg"), None, "mg-3"
    )
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/x3.jpg"), None, "mg-3"
    )
    await inbound_aggregator.aggregator_offer_text(route, "and one more thing")
    await _wait_until_flushed(captured_sends, expected=1)
    assert len(captured_sends) == 1
    flushed = captured_sends[0][1]
    assert flushed.count("shared caption") == 1
    assert "and one more thing" in flushed
    # Caption appears before follow-up; both before the (attachments: …)
    # block, all three paths grouped.
    assert flushed.index("shared caption") < flushed.index("and one more thing")
    assert flushed.index("and one more thing") < flushed.index("(attachments:")
    assert flushed.count("(attachments:") == 1
    for p in ("/tmp/x1.jpg", "/tmp/x2.jpg", "/tmp/x3.jpg"):
        assert p in flushed


@pytest.mark.asyncio
async def test_photo_then_fast_follow_text_coalesces(captured_sends):
    route = (1, 100, "@0")
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/p.jpg"), None, None
    )
    await inbound_aggregator.aggregator_offer_text(route, "describe this please")
    await _wait_until_flushed(captured_sends, expected=1)
    assert len(captured_sends) == 1
    flushed = captured_sends[0][1]
    assert "describe this please" in flushed
    assert "/tmp/p.jpg" in flushed


@pytest.mark.asyncio
async def test_distinct_media_groups_force_flush_at_boundary(captured_sends):
    """Two media-groups inside the debounce window must NOT merge.

    Caption from group-2 leaking into group-1's bundle was the §2.8 bug:
    the boundary check force-flushes the in-progress bundle when a new
    mg-id arrives.
    """
    route = (1, 100, "@0")
    config.aggregator_debounce_seconds = 5.0
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/g1a.jpg"), "first album", "mg-1"
    )
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/g1b.jpg"), None, "mg-1"
    )
    # Boundary: different mg-id → previous bundle force-flushes.
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/g2a.jpg"), "second album", "mg-2"
    )
    await _wait_until_flushed(captured_sends, expected=1)
    # Force the new bundle out so we can inspect it.
    await inbound_aggregator.aggregator_flush_route(route)
    assert len(captured_sends) == 2
    first, second = captured_sends[0][1], captured_sends[1][1]
    assert "first album" in first
    assert "second album" not in first
    assert "/tmp/g1a.jpg" in first and "/tmp/g1b.jpg" in first
    assert "/tmp/g2a.jpg" not in first
    assert "second album" in second
    assert "/tmp/g2a.jpg" in second


@pytest.mark.asyncio
async def test_ungrouped_attachment_does_not_reset_media_group_boundary(captured_sends):
    """An mg=None attachment between two groups must not erase the boundary memory."""
    route = (1, 100, "@0")
    config.aggregator_debounce_seconds = 5.0
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/g1.jpg"), "first album", "mg-1"
    )
    # Non-grouped photo joins the in-progress bundle. Must NOT reset
    # current_media_group_id to None, else the next group's boundary
    # check would skip and merge the two albums.
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/loose.jpg"), None, None
    )
    # Boundary: arrival of mg-2 must force-flush g1 + loose together,
    # then start a fresh bundle for mg-2.
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/g2.jpg"), "second album", "mg-2"
    )
    await _wait_until_flushed(captured_sends, expected=1)
    await inbound_aggregator.aggregator_flush_route(route)
    assert len(captured_sends) == 2
    first, second = captured_sends[0][1], captured_sends[1][1]
    assert "first album" in first
    assert "/tmp/g1.jpg" in first and "/tmp/loose.jpg" in first
    assert "/tmp/g2.jpg" not in first
    assert "second album" not in first
    assert "second album" in second
    assert "/tmp/g2.jpg" in second
    assert "/tmp/g1.jpg" not in second


@pytest.mark.asyncio
async def test_caption_dedup_within_media_group(captured_sends):
    """Telegram repeats the same caption on every media-group item; we dedup."""
    route = (1, 100, "@0")
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/d1.jpg"), "same caption", "mg-d"
    )
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/d2.jpg"), "same caption", "mg-d"
    )
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/d3.jpg"), "same caption", "mg-d"
    )
    await _wait_until_flushed(captured_sends, expected=1)
    flushed = captured_sends[0][1]
    assert flushed.count("same caption") == 1


@pytest.mark.asyncio
async def test_max_attachments_triggers_immediate_flush(captured_sends):
    route = (1, 100, "@0")
    original_max = config.aggregator_max_attachments
    config.aggregator_max_attachments = 10
    # Use a large debounce so we can prove the cap, not the timer, fired.
    config.aggregator_debounce_seconds = 5.0
    try:
        for i in range(11):
            await inbound_aggregator.aggregator_offer_photo(
                route, Path(f"/tmp/m{i}.jpg"), None, None
            )
        # No need to wait long: flush should have fired synchronously inside
        # aggregator_offer_photo when the cap was hit.
        await asyncio.sleep(0)
        # The 10th photo trips the cap → first 10 flush. The 11th lands in
        # a fresh bundle and does NOT yet flush (debounce is 5s).
        assert len(captured_sends) == 1
        flushed = captured_sends[0][1]
        # 10 photos in the flushed bundle (m0..m9).
        for i in range(10):
            assert f"/tmp/m{i}.jpg" in flushed
        assert "/tmp/m10.jpg" not in flushed
    finally:
        config.aggregator_max_attachments = original_max


@pytest.mark.asyncio
async def test_force_flush_drains_before_slash_command(captured_sends):
    route = (1, 100, "@0")
    config.aggregator_debounce_seconds = 5.0
    await inbound_aggregator.aggregator_offer_text(route, "pre-command text")
    # No flush yet (long debounce).
    assert captured_sends == []
    await inbound_aggregator.aggregator_flush_route(route)
    assert captured_sends == [("@0", "pre-command text")]


@pytest.mark.asyncio
async def test_teardown_cancels_pending_flush(captured_sends):
    route = (1, 100, "@0")
    config.aggregator_debounce_seconds = 5.0
    await inbound_aggregator.aggregator_offer_text(route, "should not be sent")
    assert inbound_aggregator.has_pending(route)
    inbound_aggregator.aggregator_clear_route(route)
    # Even after waiting past where the debounce would have landed, nothing
    # was sent.
    await asyncio.sleep(0.05)
    assert captured_sends == []
    assert not inbound_aggregator.has_pending(route)


@pytest.mark.asyncio
async def test_voice_offer_treated_as_text(captured_sends):
    route = (1, 100, "@0")
    await inbound_aggregator.aggregator_offer_voice(route, "transcribed audio body")
    await _wait_until_flushed(captured_sends, expected=1)
    assert captured_sends == [("@0", "transcribed audio body")]


@pytest.mark.asyncio
async def test_unbound_topic_pending_then_directory_pick_flushes(captured_sends):
    """Surrogate for the user-flow: pending photos pile up, then a route is
    bound and the bot feeds them to the aggregator + force-flushes.

    The bot.py flow (``_create_and_bind_window`` and the window-picker
    bind path) calls ``aggregator_offer_text`` + ``aggregator_offer_photo``
    + ``aggregator_flush_route`` in that order. Verify the resulting flush
    is the §2.8.2 single-text + grouped-paths shape.
    """
    route = (1, 100, "@0")
    pending_text = "first message in the new topic"
    pending_photos = [
        ("/tmp/u1.jpg", "stash caption", "mg-x"),
        ("/tmp/u2.jpg", "", "mg-x"),
    ]

    await inbound_aggregator.aggregator_offer_text(route, pending_text)
    for path_str, caption, media_group_id in pending_photos:
        await inbound_aggregator.aggregator_offer_photo(
            route, Path(path_str), caption, media_group_id
        )
    await inbound_aggregator.aggregator_flush_route(route)

    assert len(captured_sends) == 1
    flushed = captured_sends[0][1]
    assert "first message in the new topic" in flushed
    assert "stash caption" in flushed
    assert "/tmp/u1.jpg" in flushed
    assert "/tmp/u2.jpg" in flushed
    assert flushed.count("(attachments:") == 1


@pytest.mark.asyncio
async def test_send_to_window_failure_is_logged_not_raised():
    """A send_to_window failure must not crash the flush path."""
    route = (1, 100, "@0")

    async def failing_send(window_id: str, text: str) -> tuple[bool, str]:
        return False, "tmux missing"

    with patch.object(
        inbound_aggregator.session_manager,
        "send_to_window",
        side_effect=failing_send,
    ):
        await inbound_aggregator.aggregator_offer_text(route, "x")
        # Force the flush so we don't depend on the debounce timer.
        await inbound_aggregator.aggregator_flush_route(route)
    # No exception, bundle is gone.
    assert not inbound_aggregator.has_pending(route)


@pytest.mark.asyncio
async def test_send_to_window_exception_is_swallowed():
    """A send_to_window crash must not leak; we log & move on."""
    route = (1, 100, "@0")

    async def crashing_send(window_id: str, text: str) -> tuple[bool, str]:
        raise RuntimeError("boom")

    with patch.object(
        inbound_aggregator.session_manager,
        "send_to_window",
        side_effect=crashing_send,
    ):
        await inbound_aggregator.aggregator_offer_text(route, "x")
        await inbound_aggregator.aggregator_flush_route(route)
    assert not inbound_aggregator.has_pending(route)


def test_session_manager_mock_protocol():
    """Sanity check: ``send_to_window`` is the public API the aggregator uses."""
    assert hasattr(inbound_aggregator.session_manager, "send_to_window")
    # AsyncMock works as a stand-in.
    session_manager_mock = AsyncMock()
    session_manager_mock.send_to_window = AsyncMock(return_value=(True, "ok"))
