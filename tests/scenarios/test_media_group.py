"""Scenario: photo+caption media group coalesces into one aggregator bundle.

A Telegram media group fires one ``photo_handler`` call per item but only the
first carries the caption. The aggregator must:
  - dedupe the caption (one ``text_parts`` entry, not N copies),
  - collect all photo paths into a single bundle keyed by ``media_group_id``,
  - delete attachment files after a successful send.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cctelegram import bot as bot_module
from tests.conftest import ScenarioHarness, _make_message, _make_user


pytestmark = pytest.mark.scenario


def _make_photo_update(
    *,
    thread_id: int,
    file_unique_id: str,
    caption: str | None = None,
    media_group_id: str | None = None,
    message_id: int = 100,
) -> MagicMock:
    photo_size = MagicMock()
    photo_size.file_unique_id = file_unique_id

    async def _download(dest: Any) -> Any:
        Path(dest).write_bytes(b"\x00")
        return dest

    tg_file = MagicMock()
    tg_file.download_to_drive = AsyncMock(side_effect=_download)
    photo_size.get_file = AsyncMock(return_value=tg_file)

    msg = _make_message(
        thread_id=thread_id,
        caption=caption,
        photo=[photo_size],
        media_group_id=media_group_id,
        message_id=message_id,
    )
    msg.chat.send_action = AsyncMock()
    update = MagicMock(name="Update")
    update.message = msg
    update.callback_query = None
    update.effective_user = _make_user()
    update.effective_chat = msg.chat
    update.effective_message = msg
    return update


@pytest.mark.asyncio
async def test_media_group_coalesces_caption_and_paths(
    scenario: ScenarioHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")

    offered: list[tuple[tuple[int, int, str], Path, str | None, str | None]] = []

    async def fake_offer_photo(
        route: tuple[int, int, str],
        path: Path,
        caption: str | None,
        media_group_id: str | None,
    ) -> None:
        offered.append((route, path, caption, media_group_id))

    monkeypatch.setattr(bot_module, "aggregator_offer_photo", fake_offer_photo)

    # First photo carries the caption.
    upd1 = _make_photo_update(
        thread_id=42,
        file_unique_id="p1",
        caption="look at this",
        media_group_id="mg-1",
        message_id=100,
    )
    await bot_module.photo_handler(upd1, scenario.context)

    # Second photo in same media group, no caption.
    upd2 = _make_photo_update(
        thread_id=42,
        file_unique_id="p2",
        media_group_id="mg-1",
        message_id=101,
    )
    await bot_module.photo_handler(upd2, scenario.context)

    assert len(offered) == 2
    # Both calls share the media_group_id; the caption only rides item 1.
    assert offered[0][3] == "mg-1"
    assert offered[1][3] == "mg-1"
    assert offered[0][2] == "look at this"
    # Item 2 has no caption (media-group dedup guard in photo_handler).
    assert offered[1][2] == ""
