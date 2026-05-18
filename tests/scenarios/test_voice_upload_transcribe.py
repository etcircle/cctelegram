"""Scenario: voice message gets transcribed and forwarded.

The voice path downloads OGG bytes from Telegram, asks the OpenAI substrate
(``transcribe_voice``) to convert them to text, and offers the transcription
to the per-route inbound aggregator. The user also gets an echo bubble
with the raw transcription text. Substrate boundaries (Telegram file
download + OpenAI transcription) are stubbed; the handler stack is real.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from cctelegram import bot as bot_module
from tests.conftest import ScenarioHarness, _make_message, _make_user


pytestmark = pytest.mark.scenario


def _make_voice_update(*, thread_id: int) -> MagicMock:
    voice = MagicMock(name="Voice")
    voice_file = MagicMock(name="VoiceFile")
    voice_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"\x00\x01"))
    voice.get_file = AsyncMock(return_value=voice_file)

    msg = _make_message(thread_id=thread_id, voice=voice)
    msg.chat.send_action = AsyncMock()
    update = MagicMock(name="Update")
    update.message = msg
    update.callback_query = None
    update.effective_user = _make_user()
    update.effective_chat = msg.chat
    update.effective_message = msg
    return update


@pytest.mark.asyncio
async def test_voice_message_transcribes_and_offers_to_aggregator(
    scenario: ScenarioHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")
    # OpenAI substrate stub: return a known transcription.
    monkeypatch.setattr(
        bot_module, "transcribe_voice", AsyncMock(return_value="hello voice")
    )
    # Aggregator offer (substrate to inbound aggregator) — record the call.
    offered: list[tuple[tuple[int, int, str], str]] = []

    async def fake_offer(route: tuple[int, int, str], text: str) -> None:
        offered.append((route, text))

    monkeypatch.setattr(bot_module, "aggregator_offer_voice", fake_offer)
    # Pretend we have an OpenAI key configured.
    monkeypatch.setattr(bot_module.config, "openai_api_key", "sk-fake")

    update = _make_voice_update(thread_id=42)
    await bot_module.voice_handler(update, scenario.context)

    assert offered == [((scenario.user_id, 42, wid), "hello voice")]
    # Echo bubble was sent.
    update.message.reply_text.assert_awaited()
    echo_text = update.message.reply_text.await_args.args[0]
    assert "hello voice" in echo_text


@pytest.mark.asyncio
async def test_voice_with_no_api_key_warns(
    scenario: ScenarioHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bot_module.config, "openai_api_key", "")
    update = _make_voice_update(thread_id=42)
    await bot_module.voice_handler(update, scenario.context)

    update.message.reply_text.assert_awaited()
    reply_text = update.message.reply_text.await_args.args[0]
    assert "OpenAI API key" in reply_text


@pytest.mark.asyncio
async def test_voice_with_no_binding_replies_with_error(
    scenario: ScenarioHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bot_module.config, "openai_api_key", "sk-fake")
    update = _make_voice_update(thread_id=42)
    await bot_module.voice_handler(update, scenario.context)

    update.message.reply_text.assert_awaited()
    reply_text = update.message.reply_text.await_args.args[0]
    assert "No session bound" in reply_text
