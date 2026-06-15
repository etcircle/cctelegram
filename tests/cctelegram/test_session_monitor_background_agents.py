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
import os

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


# ── ISSUE-6: Workflow-tool launch bracket (Fix 2a wiring + 2c heartbeat) ──
#
# The Workflow tool's launch tool_result is shaped differently from the
# Agent/Task `agentId:` launch (Task ID mid-line, separate Run ID + Transcript
# dir). These tests pin the monitor's Workflow branch: a `wf-task:<id>` key in
# `.launched`, the matching `<task-notification>` close key, and the per-poll
# mtime-advance heartbeat into `.bracket_heartbeats` (Fix 2c — run-state is
# bounded by a DIR STAT only, never by parsing sidechain entries).

_WF_TASK = "wtask01abc"
_WF_RUN = "wf_run01abcd"


def _wf_launch_text(wf_dir) -> str:
    return (
        f"Workflow launched in background. Task ID: {_WF_TASK}\n"
        "Summary: background work\n"
        f"Transcript dir: {wf_dir}\n"
        f"Run ID: {_WF_RUN}\n"
    )


@pytest.mark.asyncio
async def test_parent_workflow_launch_recorded_as_wf_task_key(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, make_tool_result_block
):
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    wf_dir = sub_dir / "workflows" / _WF_RUN
    wf_dir.mkdir(parents=True, exist_ok=True)
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t9", "Workflow", {"script": "..."})],
                session_id=PARENT,
            ),
            make_jsonl_entry(
                "user",
                [make_tool_result_block("t9", _wf_launch_text(wf_dir))],
                session_id=PARENT,
            ),
        ],
    )
    await monitor.check_for_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    # The launch key is the EXACT prefixed string (namespace-isolated from the
    # Agent/Task agentId space), so it == the wf-task close key.
    assert f"wf-task:{_WF_TASK}" in activity[PARENT].launched


@pytest.mark.asyncio
async def test_workflow_task_notification_closes_open_bracket_with_wf_task_key(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, make_tool_result_block
):
    """Fix 2d: a <task-notification> whose Task ID matches an OPEN Workflow
    bracket emits the matching wf-task: close key (so the bracket tombstones).
    The realistic flow is launch → … → close (the launch opened the bracket)."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    wf_dir = sub_dir / "workflows" / _WF_RUN
    wf_dir.mkdir(parents=True, exist_ok=True)
    notif = (
        f"<task-notification>\n<task-id>{_WF_TASK}</task-id>\n"
        "<tool-use-id>toolu_x</tool-use-id>\n<status>completed</status>\n"
        "</task-notification>"
    )
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t9", "Workflow", {"script": "..."})],
                session_id=PARENT,
            ),
            make_jsonl_entry(
                "user",
                [make_tool_result_block("t9", _wf_launch_text(wf_dir))],
                session_id=PARENT,
            ),
            make_jsonl_entry("user", notif, session_id=PARENT),
        ],
    )
    await monitor.check_for_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    assert f"wf-task:{_WF_TASK}" in activity[PARENT].completed


@pytest.mark.asyncio
async def test_isolated_task_notification_without_bracket_emits_no_wf_task_key(
    monitor, tmp_path, make_jsonl_entry
):
    """Gate-on-bracket (Fix 2d): a <task-notification> with NO open bracket
    (the launch was never observed — restart / bot-down between launch and
    close) emits NO wf-task: close key. There is no route_runtime bg key to
    tombstone in that case, so the bare normalized close key suffices — the
    wf-task: close key is emitted ONLY when its launch bracket is open. This
    forbids guessing 'is this a Workflow id?' from the id's character set (a
    fragile external-format assumption); the OPEN BRACKET is the sole signal."""
    parent_jsonl, _ = _setup_parent(monitor, tmp_path)
    notif = (
        f"<task-notification>\n<task-id>{_WF_TASK}</task-id>\n"
        "<tool-use-id>toolu_x</tool-use-id>\n<status>completed</status>\n"
        "</task-notification>"
    )
    _append(parent_jsonl, [make_jsonl_entry("user", notif, session_id=PARENT)])
    await monitor.check_for_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    completed = activity[PARENT].completed if PARENT in activity else set()
    assert f"wf-task:{_WF_TASK}" not in completed


@pytest.mark.asyncio
async def test_workflow_bracket_heartbeats_only_on_mtime_advance(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, make_tool_result_block
):
    """Fix 2c: an OPEN bracket's wf_dir is stat'd each poll; a `wf-task:` activity
    refresh is emitted ONLY when the freshest `*.jsonl` mtime ADVANCED (real new
    sidechain writes). No advance → no heartbeat → the key ages out via TTL."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    wf_dir = sub_dir / "workflows" / _WF_RUN
    wf_dir.mkdir(parents=True, exist_ok=True)
    agent_file = wf_dir / "agent-aaa111.jsonl"
    agent_file.write_text("{}\n")
    t0 = agent_file.stat().st_mtime
    os.utime(agent_file, (t0, t0))

    # Open the bracket via the launch parse, then drain the launch signal.
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t9", "Workflow", {"script": "..."})],
                session_id=PARENT,
            ),
            make_jsonl_entry(
                "user",
                [make_tool_result_block("t9", _wf_launch_text(wf_dir))],
                session_id=PARENT,
            ),
        ],
    )
    await monitor.check_for_updates({PARENT})
    await monitor.check_sidechain_updates({PARENT})
    monitor.pop_sidechain_activity()  # drain launch + any baseline registration

    # Real new sidechain write → mtime advances → heartbeat for the wf-task key.
    os.utime(agent_file, (t0 + 30, t0 + 30))
    await monitor.check_sidechain_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    hb = activity[PARENT].bracket_heartbeats
    assert f"wf-task:{_WF_TASK}" in hb
    assert hb[f"wf-task:{_WF_TASK}"] >= t0 + 30

    # No further write → no heartbeat (the gate is advance-only).
    await monitor.check_sidechain_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    if PARENT in activity:
        assert f"wf-task:{_WF_TASK}" not in activity[PARENT].bracket_heartbeats


# ── Fix 5 PR-A characterization: pin the CURRENT top-level Agent/Task ──────
#   sidechain behavior of check_sidechain_updates BEFORE the helper
#   extraction (§4-A). This is the gate-landing safety net: the extraction
#   of _track_and_emit_sidechain_file(feed_run_state=True) for the top-level
#   loop must keep ALL of (a) tick population, (b) first-seen-at-EOF
#   registration, and (c) the _pending_tools tool_use/tool_result carry
#   across ticks byte-identical. Uses synthetic ids/content (no PII).


@pytest.mark.asyncio
async def test_characterize_toplevel_sidechain_ticks_eof_and_pending_carry(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, make_tool_result_block
):
    """Characterize the existing top-level (subagents/agent-*.jsonl) path.

    Pins three behaviors that the §2.1(a) extraction must preserve:
      (a) ``pop_sidechain_activity().ticks`` carries the normalized stem key
          with the correct ``max_event_ts`` and ``saw_end_of_turn``;
      (b) a newly-discovered file registers at EOF (first observation emits
          NOTHING and the tracker exists at the file's current size);
      (c) ``_pending_tools`` carries an unpaired ``tool_use`` across ticks and
          pairs it with the ``tool_result`` that lands the next tick.
    """
    _, sub_dir = _setup_parent(monitor, tmp_path)
    sc = sub_dir / "agent-char01.jsonl"

    # ── (b) first-seen registers at EOF ──────────────────────────────────
    # Pre-existing "historical" content the bot must NOT replay.
    _append(
        sc,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("old", "Bash", {"command": "echo hi"})],
                timestamp="2026-06-12T07:00:00.000Z",
            )
        ],
    )
    eof_size = sc.stat().st_size

    first_msgs = await monitor.check_sidechain_updates({PARENT})
    assert first_msgs == []  # started at EOF — no historical replay
    assert monitor.pop_sidechain_activity() == {}  # no activity on registration

    tracking_key = f"sub:{PARENT}:agent-char01"
    tracked = monitor.state.get_session(tracking_key)
    assert tracked is not None  # tracker exists after first observation
    assert tracked.parent_session_id == PARENT
    assert tracked.last_byte_offset == eof_size  # registered at EOF

    # ── (c) tick 1: an UNPAIRED tool_use → carried in _pending_tools ──────
    _append(
        sc,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("tc1", "Read", {"file_path": "syn.py"})],
                timestamp="2026-06-12T08:00:00.000Z",
            )
        ],
    )
    msgs_t1 = await monitor.check_sidechain_updates({PARENT})
    # The tool_use is forwarded (subagent-tagged) but its tool_result has not
    # arrived, so the parser carries it as a pending tool for next tick.
    assert [m.subagent_key for m in msgs_t1] == [tracking_key]
    assert msgs_t1[0].content_type == "tool_use"
    assert msgs_t1[0].tool_use_id == "tc1"
    assert "tc1" in monitor._pending_tools.get(tracking_key, {})

    # ── (a) tick 1 activity: normalized stem key + ts + no end-of-turn ────
    activity_t1 = monitor.pop_sidechain_activity()
    assert PARENT in activity_t1
    ticks = activity_t1[PARENT].ticks
    assert set(ticks) == {"char01"}  # normalized — "agent-" stripped
    assert ticks["char01"].max_event_ts == parse_iso_timestamp(
        "2026-06-12T08:00:00.000Z"
    )
    assert ticks["char01"].saw_end_of_turn is False

    # ── (c) tick 2: the tool_result pairs with the carried tool_use ──────
    # Plus an end-of-turn text block so (a) saw_end_of_turn flips True.
    _append(
        sc,
        [
            make_jsonl_entry(
                "user",
                [make_tool_result_block("tc1", "synthetic result")],
                timestamp="2026-06-12T08:05:00.000Z",
            ),
            make_jsonl_entry(
                "assistant",
                [{"type": "text", "text": "done"}],
                timestamp="2026-06-12T08:06:00.000Z",
            ),
        ],
    )
    # Mark the assistant turn's end so saw_end_of_turn flips.
    # (make_jsonl_entry has no stop_reason; set it on the last raw line.)
    raw = sc.read_text().splitlines()
    last = json.loads(raw[-1])
    last["message"]["stop_reason"] = "end_turn"
    raw[-1] = json.dumps(last)
    sc.write_text("\n".join(raw) + "\n")

    msgs_t2 = await monitor.check_sidechain_updates({PARENT})

    # The carried tool_use is now paired with its tool_result and cleared.
    assert "tc1" not in monitor._pending_tools.get(tracking_key, {})
    # The tool_result and the text both forward, subagent-tagged.
    kinds_t2 = [m.content_type for m in msgs_t2]
    assert "tool_result" in kinds_t2
    assert "text" in kinds_t2
    assert all(m.subagent_key == tracking_key for m in msgs_t2)

    # ── (a) tick 2 activity: end-of-turn now seen ────────────────────────
    activity_t2 = monitor.pop_sidechain_activity()
    assert PARENT in activity_t2
    ticks2 = activity_t2[PARENT].ticks
    assert "char01" in ticks2
    assert ticks2["char01"].max_event_ts == parse_iso_timestamp(
        "2026-06-12T08:06:00.000Z"
    )
    assert ticks2["char01"].saw_end_of_turn is True
