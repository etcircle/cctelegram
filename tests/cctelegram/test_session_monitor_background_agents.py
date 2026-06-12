"""GH #44 monitor tests — per-agent sidechain activity aggregation + the
parent-path launch / task-notification signal collection.

Pins the §4.1/§4.2 contract: ``check_sidechain_updates`` aggregates
per-(parent, agent_key) ``SidechainTick``s (max parsed-entry timestamp +
``saw_end_of_turn``, INCLUDING lifecycle-only end-turn markers), the parent
parse path records async-launch agentIds and task-notification completions,
and ``pop_sidechain_activity`` drains the combined structure consume-once.
"""

from __future__ import annotations

import json

import pytest

from cctelegram.session_monitor import SessionInfo, SessionMonitor, TrackedSession
from cctelegram.utils import parse_iso_timestamp

PARENT = "parent-sid"


@pytest.fixture
def monitor(tmp_path):
    return SessionMonitor(
        projects_path=tmp_path / "projects",
        state_file=tmp_path / "monitor_state.json",
    )


def _setup_parent(monitor, tmp_path, parent_sid: str = PARENT):
    proj_dir = tmp_path / "projects" / "-tmp-fake"
    proj_dir.mkdir(parents=True, exist_ok=True)
    parent_jsonl = proj_dir / f"{parent_sid}.jsonl"
    if not parent_jsonl.exists():
        parent_jsonl.write_text("")
    sub_dir = proj_dir / parent_sid / "subagents"
    sub_dir.mkdir(parents=True, exist_ok=True)
    monitor.state.update_session(
        TrackedSession(
            session_id=parent_sid,
            file_path=str(parent_jsonl),
            last_byte_offset=parent_jsonl.stat().st_size,
        )
    )

    # scan_projects shells out to tmux for active cwds — stub it like the
    # existing check_for_updates tests do.
    async def _scan():
        return [SessionInfo(session_id=parent_sid, file_path=parent_jsonl)]

    monitor.scan_projects = _scan  # type: ignore[method-assign]
    return parent_jsonl, sub_dir


def _append(path, entries):
    with open(path, "a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


# ── sidechain tick aggregation ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_per_agent_ticks_with_max_timestamp(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    _, sub_dir = _setup_parent(monitor, tmp_path)
    sc = sub_dir / "agent-abc.jsonl"
    sc.write_text("")
    await monitor.check_sidechain_updates({PARENT})  # register at EOF
    assert monitor.pop_sidechain_activity() == {}

    _append(
        sc,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t1", "Bash", {"command": "ls"})],
                timestamp="2026-06-12T08:00:00.000Z",
            ),
            make_jsonl_entry(
                "assistant",
                [{"type": "text", "text": "working"}],
                timestamp="2026-06-12T08:05:00.000Z",
            ),
        ],
    )
    await monitor.check_sidechain_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    assert PARENT in activity
    ticks = activity[PARENT].ticks
    assert set(ticks) == {"abc"}  # normalized key — no agent- prefix
    assert ticks["abc"].max_event_ts == parse_iso_timestamp("2026-06-12T08:05:00.000Z")
    assert ticks["abc"].saw_end_of_turn is False
    # Consume-once.
    assert monitor.pop_sidechain_activity() == {}


@pytest.mark.asyncio
async def test_sibling_agents_each_get_their_own_tick(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    _, sub_dir = _setup_parent(monitor, tmp_path)
    sc1 = sub_dir / "agent-abc.jsonl"
    sc2 = sub_dir / "agent-def.jsonl"
    sc1.write_text("")
    sc2.write_text("")
    await monitor.check_sidechain_updates({PARENT})

    entry = make_jsonl_entry(
        "assistant",
        [make_tool_use_block("t1", "Bash", {"command": "ls"})],
        timestamp="2026-06-12T08:00:00.000Z",
    )
    _append(sc1, [entry])
    _append(sc2, [entry])
    await monitor.check_sidechain_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    assert set(activity[PARENT].ticks) == {"abc", "def"}


@pytest.mark.asyncio
async def test_all_none_timestamp_batch_reports_none_ts(
    monitor, tmp_path, make_tool_use_block
):
    _, sub_dir = _setup_parent(monitor, tmp_path)
    sc = sub_dir / "agent-abc.jsonl"
    sc.write_text("")
    await monitor.check_sidechain_updates({PARENT})

    entry = {
        "type": "assistant",
        "message": {"content": [make_tool_use_block("t1", "Bash", {})]},
        "sessionId": "x",
        # no timestamp key at all
    }
    _append(sc, [entry])
    await monitor.check_sidechain_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    assert activity[PARENT].ticks["abc"].max_event_ts is None


@pytest.mark.asyncio
async def test_end_of_turn_detected_including_lifecycle_only(
    monitor, tmp_path, make_jsonl_entry
):
    """codex r2 P2-2 / hermes r2 P2-3: an end-turn entry with NO visible text
    (lifecycle-only) must still flip saw_end_of_turn."""
    _, sub_dir = _setup_parent(monitor, tmp_path)
    sc = sub_dir / "agent-abc.jsonl"
    sc.write_text("")
    await monitor.check_sidechain_updates({PARENT})

    entry = make_jsonl_entry(
        "assistant",
        [],  # empty content — parses to a lifecycle-only end-turn marker
        timestamp="2026-06-12T09:00:00.000Z",
    )
    entry["message"]["stop_reason"] = "end_turn"
    _append(sc, [entry])
    await monitor.check_sidechain_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    assert activity[PARENT].ticks["abc"].saw_end_of_turn is True


# ── parent-path signals: async launch + task-notification ────────────────


@pytest.mark.asyncio
async def test_parent_async_launch_recorded(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, make_tool_result_block
):
    parent_jsonl, _ = _setup_parent(monitor, tmp_path)
    launch_text = (
        "Async agent launched successfully.\n"
        "agentId: abc123def456 (internal ID - do not mention to user.)\n"
        "The agent is working in the background."
    )
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t9", "Agent", {"prompt": "go"})],
                session_id=PARENT,
            ),
            make_jsonl_entry(
                "user",
                [make_tool_result_block("t9", launch_text)],
                session_id=PARENT,
            ),
        ],
    )
    await monitor.check_for_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    assert activity[PARENT].launched == {"abc123def456"}


@pytest.mark.asyncio
async def test_parent_task_notification_recorded_as_completion(
    monitor, tmp_path, make_jsonl_entry
):
    parent_jsonl, _ = _setup_parent(monitor, tmp_path)
    notif = (
        "<task-notification>\n<task-id>abc123def456</task-id>\n"
        "<tool-use-id>toolu_x</tool-use-id>\n<status>completed</status>\n"
        "</task-notification>"
    )
    _append(
        parent_jsonl,
        [make_jsonl_entry("user", notif, session_id=PARENT)],
    )
    await monitor.check_for_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    assert activity[PARENT].completed == {"abc123def456"}


@pytest.mark.asyncio
async def test_ordinary_tool_results_and_user_text_record_nothing(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, make_tool_result_block
):
    parent_jsonl, _ = _setup_parent(monitor, tmp_path)
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t1", "Bash", {"command": "ls"})],
                session_id=PARENT,
            ),
            make_jsonl_entry(
                "user",
                [make_tool_result_block("t1", "file1\nfile2")],
                session_id=PARENT,
            ),
            make_jsonl_entry("user", "just a normal prompt", session_id=PARENT),
        ],
    )
    await monitor.check_for_updates({PARENT})
    assert monitor.pop_sidechain_activity() == {}
