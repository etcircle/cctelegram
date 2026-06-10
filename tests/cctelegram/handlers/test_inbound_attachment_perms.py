"""Review finding 27: attachment dirs/files must follow the 0700/0600 posture.

``images/`` and ``files/`` were created at import with umask defaults (0755
dirs, 0644 downloads) while every other sensitive store in the project is
0700/0600. The fix is a create-and-repair helper (mkdir then ALWAYS chmod
0o700 — mkdir's ``mode`` is a no-op on an existing dir, so upgraded installs
must be repaired) plus a 0o600 chmod after each download. OSError posture:
log WARNING + continue — never silent, never fail the download.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cctelegram.handlers import inbound_telegram as inbound_module


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


# --- _ensure_private_media_dir -------------------------------------------


def test_ensure_private_media_dir_fresh_create_is_0700(tmp_path: Path) -> None:
    target = tmp_path / "images"
    result = inbound_module._ensure_private_media_dir(target)
    assert result == target
    assert target.is_dir()
    assert _mode(target) == 0o700


def test_ensure_private_media_dir_repairs_preexisting_loose_dir(
    tmp_path: Path,
) -> None:
    """An upgraded install's 0755 dir must be REPAIRED to 0700 (chmod always runs)."""
    target = tmp_path / "files"
    target.mkdir()
    os.chmod(target, 0o755)
    assert _mode(target) == 0o755

    inbound_module._ensure_private_media_dir(target)

    assert _mode(target) == 0o700


def test_ensure_private_media_dir_oserror_warns_and_continues(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    target = tmp_path / "images"
    with (
        caplog.at_level(logging.WARNING, logger=inbound_module.logger.name),
        patch.object(inbound_module.os, "chmod", side_effect=OSError("denied")),
    ):
        result = inbound_module._ensure_private_media_dir(target)

    assert result == target
    assert target.is_dir()  # mkdir succeeded; chmod failure didn't propagate
    assert any(
        r.levelno == logging.WARNING and "0700" in r.getMessage()
        for r in caplog.records
    )


# --- _restrict_download_perms ---------------------------------------------


def test_restrict_download_perms_sets_0600(tmp_path: Path) -> None:
    f = tmp_path / "download.jpg"
    f.write_bytes(b"image")
    os.chmod(f, 0o644)

    inbound_module._restrict_download_perms(f)

    assert _mode(f) == 0o600


def test_restrict_download_perms_oserror_warns_and_continues(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    f = tmp_path / "download.jpg"
    f.write_bytes(b"image")
    with (
        caplog.at_level(logging.WARNING, logger=inbound_module.logger.name),
        patch.object(inbound_module.os, "chmod", side_effect=OSError("denied")),
    ):
        inbound_module._restrict_download_perms(f)  # must not raise

    assert f.exists()
    assert any(
        r.levelno == logging.WARNING and "0600" in r.getMessage()
        for r in caplog.records
    )


# --- handler-level: downloads land at 0600; chmod failure never fails the
# --- download --------------------------------------------------------------


class _DownloadedFile:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def download_to_drive(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.payload)


def _make_photo_update(*, thread_id: int = 99) -> MagicMock:
    photo = MagicMock()
    photo.file_unique_id = "perm-photo"
    photo.get_file = AsyncMock(return_value=_DownloadedFile(b"new photo"))

    message = MagicMock()
    message.photo = [photo]
    message.document = None
    message.caption = "cap"
    message.media_group_id = None
    message.message_thread_id = thread_id
    message.message_id = 123
    message.chat = MagicMock()
    message.chat.id = -100123
    message.chat.type = "supergroup"
    message.chat.send_action = AsyncMock()
    message.quote = None
    message.reply_to_message = None

    update = MagicMock()
    update.message = message
    update.callback_query = None
    update.effective_user = MagicMock()
    update.effective_user.id = 1
    update.effective_chat = message.chat
    return update


def _make_document_update(*, thread_id: int = 99) -> MagicMock:
    document = MagicMock()
    document.file_unique_id = "perm-doc"
    document.file_name = "report.txt"
    document.file_size = 11
    document.get_file = AsyncMock(return_value=_DownloadedFile(b"new doc"))

    message = MagicMock()
    message.photo = None
    message.document = document
    message.caption = "cap"
    message.media_group_id = None
    message.message_thread_id = thread_id
    message.message_id = 124
    message.chat = MagicMock()
    message.chat.id = -100123
    message.chat.type = "supergroup"
    message.chat.send_action = AsyncMock()
    message.quote = None
    message.reply_to_message = None

    update = MagicMock()
    update.message = message
    update.callback_query = None
    update.effective_user = MagicMock()
    update.effective_user.id = 1
    update.effective_chat = message.chat
    return update


def _unbound_topic_patches(media_dir_name: str, tmp_path: Path):
    media_dir = tmp_path / media_dir_name
    return media_dir, (
        patch.object(inbound_module, "is_user_allowed", return_value=True),
        patch.object(inbound_module.session_manager, "set_group_chat_id"),
        patch.object(
            inbound_module.session_manager,
            "get_window_for_thread",
            return_value=None,
        ),
        patch.object(
            inbound_module,
            "_list_unbound_windows",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch.object(
            inbound_module,
            "build_directory_browser",
            return_value=("picker", MagicMock(), []),
        ),
        patch.object(inbound_module, "safe_reply", new_callable=AsyncMock),
    )


@pytest.mark.asyncio
async def test_photo_download_lands_at_0600(tmp_path: Path) -> None:
    context = MagicMock()
    context.user_data = {}
    update = _make_photo_update()
    media_dir, patches = _unbound_topic_patches("images", tmp_path)

    with patch.object(inbound_module, "_IMAGES_DIR", media_dir):
        for p in patches:
            p.start()
        try:
            await inbound_module.photo_handler(update, context)
        finally:
            for p in patches:
                p.stop()

    pending = context.user_data["_pending_thread_attachments"]
    downloaded = Path(pending[0].path)
    assert downloaded.exists()
    assert _mode(downloaded) == 0o600


@pytest.mark.asyncio
async def test_document_download_lands_at_0600(tmp_path: Path) -> None:
    context = MagicMock()
    context.user_data = {}
    update = _make_document_update()
    media_dir, patches = _unbound_topic_patches("files", tmp_path)

    with patch.object(inbound_module, "_FILES_DIR", media_dir):
        for p in patches:
            p.start()
        try:
            await inbound_module.document_handler(update, context)
        finally:
            for p in patches:
                p.stop()

    pending = context.user_data["_pending_thread_attachments"]
    downloaded = Path(pending[0].path)
    assert downloaded.exists()
    assert _mode(downloaded) == 0o600


@pytest.mark.asyncio
async def test_photo_download_succeeds_when_chmod_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A chmod OSError logs a WARNING but never fails the download itself."""
    context = MagicMock()
    context.user_data = {}
    update = _make_photo_update()
    media_dir, patches = _unbound_topic_patches("images", tmp_path)

    with (
        caplog.at_level(logging.WARNING, logger=inbound_module.logger.name),
        patch.object(inbound_module, "_IMAGES_DIR", media_dir),
        patch.object(inbound_module.os, "chmod", side_effect=OSError("denied")),
    ):
        for p in patches:
            p.start()
        try:
            await inbound_module.photo_handler(update, context)
        finally:
            for p in patches:
                p.stop()

    pending = context.user_data["_pending_thread_attachments"]
    downloaded = Path(pending[0].path)
    assert downloaded.exists()
    assert any(r.levelno == logging.WARNING for r in caplog.records)
