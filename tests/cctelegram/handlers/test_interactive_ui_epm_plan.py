"""ExitPlanMode plan-before-card: _read_epm_plan_file + _maybe_post_epm_plan.

The plan body is the ExitPlanMode tool's input.plan, buffered in JSONL until
approval — so the user used to approve blind and get the plan AFTER. The fix
posts it before the card: for a LIVE pane card (tool_input None) it reads the
~/.claude/plans/<slug>.md file named in the pane footer; idempotent via an
md_capture norm_hash marker; the post-resolution JSONL copy is deduped.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from cctelegram import md_capture
from cctelegram.handlers import interactive_ui

_SID = "feedface-0000-1111-2222-333344445555"
_PLAN = (
    "# Plan: add a docs/README.md index\n\n## Context\n\nThe docs dir lacks an index.\n"
)


@pytest.fixture
def cc_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
    md_capture.msg_display_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture(autouse=True)
def _clean_state():
    interactive_ui._interactive_msgs.clear()
    yield
    interactive_ui._interactive_msgs.clear()


@pytest.fixture
def captured_posts(monkeypatch):
    posts: list[str] = []
    sent_msg = AsyncMock()
    sent_msg.message_id = 555

    async def fake_topic_send(bot, **kwargs):
        posts.append(kwargs["text"])
        return sent_msg, None

    monkeypatch.setattr(interactive_ui, "topic_send", fake_topic_send)
    monkeypatch.setattr(interactive_ui, "session_id_for_window", lambda _wid: _SID)
    return posts


def _home_with_plan(monkeypatch, tmp_path, slug="the-plan.md", body=_PLAN) -> str:
    """Point HOME at tmp, write ~/.claude/plans/<slug>, return the ~ path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    plans = tmp_path / ".claude" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / slug).write_text(body)
    return f"~/.claude/plans/{slug}"


# ── _read_epm_plan_file ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_plan_file_exists(tmp_path, monkeypatch):
    path = _home_with_plan(monkeypatch, tmp_path)
    assert await interactive_ui._read_epm_plan_file(path) == _PLAN


@pytest.mark.asyncio
async def test_read_plan_file_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude" / "plans").mkdir(parents=True, exist_ok=True)
    assert await interactive_ui._read_epm_plan_file("~/.claude/plans/gone.md") is None


@pytest.mark.asyncio
async def test_read_plan_file_path_traversal_blocked(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude" / "plans").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".claude" / "secret.md").write_text("SECRET")
    # A path that escapes ~/.claude/plans/ must be refused.
    assert (
        await interactive_ui._read_epm_plan_file("~/.claude/plans/../secret.md") is None
    )


@pytest.mark.asyncio
async def test_read_plan_file_none_and_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    plans = tmp_path / ".claude" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / "blank.md").write_text("   \n\n")
    assert await interactive_ui._read_epm_plan_file(None) is None
    assert await interactive_ui._read_epm_plan_file("~/.claude/plans/blank.md") is None


# ── _maybe_post_epm_plan ─────────────────────────────────────────────────────


async def _post(pane_text="", tool_input=None):
    await interactive_ui._maybe_post_epm_plan(
        AsyncMock(),
        user_id=1,
        thread_id=100,
        chat_id=42,
        window_id="@0",
        pane_text=pane_text,
        tool_input=tool_input,
    )


@pytest.mark.asyncio
async def test_posts_plan_once_via_tool_input(cc_dir, captured_posts):
    # Replay path (tool_input.plan present): posts once, records the marker;
    # a second call is suppressed by the marker (idempotent re-render/restart).
    await _post(tool_input={"plan": _PLAN})
    assert len(captured_posts) == 1
    assert "add a docs/README.md index" in captured_posts[0]
    nh = md_capture.prose_norm_hash(_PLAN)
    assert md_capture.was_epm_plan_shown_live(_SID, nh) is True
    await _post(tool_input={"plan": _PLAN})
    assert len(captured_posts) == 1  # not re-posted


@pytest.mark.asyncio
async def test_posts_plan_from_footer_file_when_tool_input_none(
    cc_dir, captured_posts, tmp_path, monkeypatch
):
    # LIVE pane path: tool_input None → read the file named in the footer.
    path = _home_with_plan(monkeypatch, tmp_path)
    pane = f" ctrl+g to edit in  Vim  · {path}\n"
    await _post(pane_text=pane, tool_input=None)
    assert len(captured_posts) == 1
    assert "add a docs/README.md index" in captured_posts[0]


@pytest.mark.asyncio
async def test_card_exists_skips(cc_dir, captured_posts):
    interactive_ui._interactive_msgs[(1, 100)] = 999  # card already up
    await _post(tool_input={"plan": _PLAN})
    assert captured_posts == []


@pytest.mark.asyncio
async def test_no_plan_no_file_degrades_silently(
    cc_dir, captured_posts, tmp_path, monkeypatch
):
    # No tool_input + a footer pointing at a missing file → no post, no crash,
    # no marker (the JSONL copy delivers post-resolution).
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude" / "plans").mkdir(parents=True, exist_ok=True)
    pane = " ctrl+g to edit in  Vim  · ~/.claude/plans/gone.md\n"
    await _post(pane_text=pane, tool_input=None)
    assert captured_posts == []
    assert md_capture.read_epm_plan_shown_live_markers(_SID) == []


@pytest.mark.asyncio
async def test_send_failure_records_no_marker(cc_dir, monkeypatch):
    # topic_send returns (None, ...) → no marker, so the next render retries and
    # the JSONL copy is NOT suppressed (no silent loss).
    monkeypatch.setattr(interactive_ui, "session_id_for_window", lambda _wid: _SID)

    async def failing_send(bot, **kwargs):
        return None, None

    monkeypatch.setattr(interactive_ui, "topic_send", failing_send)
    await _post(tool_input={"plan": _PLAN})
    nh = md_capture.prose_norm_hash(_PLAN)
    assert md_capture.was_epm_plan_shown_live(_SID, nh) is False
