"""Scenario: document upload downloads + forwards to aggregator.

The document path downloads to ``_FILES_DIR``, then either stashes it as a
pending attachment (unbound topic) or offers it to the per-route aggregator
(bound topic). Substrate boundaries (Telegram file download) are stubbed;
the handler stack is real.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from cctelegram import bot as bot_module
from tests.conftest import ScenarioHarness, _make_message, _make_user


pytestmark = pytest.mark.scenario


def _make_document_update(
    *,
    thread_id: int,
    file_size: int = 1024,
    caption: str | None = None,
    media_group_id: str | None = None,
    file_name: str = "notes.txt",
    download_dest: Path | None = None,
) -> MagicMock:
    document = MagicMock(name="Document")
    document.file_size = file_size
    document.file_name = file_name
    document.file_unique_id = "uid42"

    async def _download(dest: Any) -> Any:  # noqa: ANN001 — MagicMock signature
        if download_dest is None:
            Path(dest).write_bytes(b"\x00")
        else:
            Path(download_dest).write_bytes(b"\x00")
        return dest

    tg_file = MagicMock()
    tg_file.download_to_drive = AsyncMock(side_effect=_download)
    document.get_file = AsyncMock(return_value=tg_file)

    msg = _make_message(
        thread_id=thread_id,
        caption=caption,
        document=document,
        media_group_id=media_group_id,
    )
    msg.chat.send_action = AsyncMock()
    update = MagicMock(name="Update")
    update.message = msg
    update.callback_query = None
    update.effective_user = _make_user()
    update.effective_chat = msg.chat
    update.effective_message = msg
    return update


# Patch type annotation reference
from typing import Any  # noqa: E402


@pytest.mark.asyncio
async def test_document_in_bound_topic_offers_to_aggregator(
    scenario: ScenarioHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")

    offered: list[tuple[tuple[int, int, str], Path, str | None, str | None]] = []

    async def fake_offer_document(
        route: tuple[int, int, str],
        path: Path,
        caption: str | None,
        media_group_id: str | None,
    ) -> None:
        offered.append((route, path, caption, media_group_id))

    monkeypatch.setattr(bot_module, "aggregator_offer_document", fake_offer_document)

    update = _make_document_update(thread_id=42, caption="check this")
    await bot_module.document_handler(update, scenario.context)

    assert len(offered) == 1
    route, _path, caption, _mg = offered[0]
    assert route == (scenario.user_id, 42, wid)
    assert caption == "check this"


@pytest.mark.asyncio
async def test_document_in_unbound_topic_stashes_as_pending(
    scenario: ScenarioHarness,
) -> None:
    """Unbound topic + document → directory browser + attachment stashed."""
    update = _make_document_update(thread_id=42, caption="readme")
    await bot_module.document_handler(update, scenario.context)

    # Browser shown.
    update.message.reply_text.assert_awaited()
    assert "reply_markup" in update.message.reply_text.await_args.kwargs
    # Attachment recorded in user_data.
    attachments = scenario.user_data.get("_pending_thread_attachments")
    assert attachments and len(attachments) == 1
    assert scenario.user_data["_pending_thread_id"] == 42


@pytest.mark.asyncio
async def test_oversized_document_is_rejected(
    scenario: ScenarioHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bot_module.config, "max_attachment_size_bytes", 1024)
    update = _make_document_update(thread_id=42, file_size=10 * 1024 * 1024)
    await bot_module.document_handler(update, scenario.context)

    update.message.reply_text.assert_awaited()
    reply_text = update.message.reply_text.await_args.args[0]
    assert "too large" in reply_text.lower()
