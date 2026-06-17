"""PR-1 Half B: the BUSY restart reconciler.

After a ``launchctl kickstart`` wipes the in-memory brackets/background_agents,
a still-running Workflow renders idle until a fresh parent turn. The startup
reconciler re-arms the busy lift from the FILESYSTEM: for each tracked parent
with no live bracket it stat-globs ``subagents/workflows/wf_*``, and for any
fresh-mtime dir it recovers the task_id + close-state from the parent JSONL
(bounded full-scan) and applies the THREE-state rule:

  STATE 1 (recovered + NO close)  → LIFT (reopen bracket + emit ``wf-task:`` launched)
  STATE 2 (close found)           → NO runtime lift (display-only closing bracket)
  STATE 3 (task_id unrecoverable) → DO NOT LIFT (fail-closed)

plus staleness / idempotency / cost-bound / no-reflood guards.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from cctelegram.route_runtime import BG_AGENT_TTL_SECONDS
from cctelegram.session_monitor import SessionInfo, SessionMonitor, TrackedSession

PARENT = "parent-sid"
RUNID = "wf_54f46aea-ba6"
TASK_ID = "w13z7jqx6"


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
    parent_jsonl.write_text("")
    (proj_dir / parent_sid / "subagents").mkdir(parents=True, exist_ok=True)
    monitor.state.update_session(
        TrackedSession(
            session_id=parent_sid,
            file_path=str(parent_jsonl),
            last_byte_offset=0,
        )
    )

    async def _scan():
        return [SessionInfo(session_id=parent_sid, file_path=parent_jsonl)]

    monitor.scan_projects = _scan  # type: ignore[method-assign]
    return parent_jsonl, proj_dir


def _wf_dir(proj_dir, parent_sid=PARENT, runid=RUNID):
    d = proj_dir / parent_sid / "subagents" / "workflows" / runid
    d.mkdir(parents=True, exist_ok=True)
    return d


def _make_fresh_agent_file(wf_dir, name="agent-aaa.jsonl", content="") -> None:
    (wf_dir / name).write_text(content)


def _launch_text(proj_dir, parent_sid=PARENT, runid=RUNID, task_id=TASK_ID) -> str:
    wf_dir = proj_dir / parent_sid / "subagents" / "workflows" / runid
    return (
        f"Workflow launched in background. Task ID: {task_id}\n"
        f"Run ID: {runid}\n"
        f"Transcript dir: {wf_dir}\n"
    )


def _append(path, entries):
    with open(path, "a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _launch_entry(text: str) -> dict:
    return {
        "type": "user",
        "message": {
            "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": text}]
        },
        "timestamp": "2026-06-17T08:00:00.000Z",
    }


def _close_entry(task_id: str = TASK_ID) -> dict:
    text = (
        "<task-notification>\n"
        f"<task-id>{task_id}</task-id>\n"
        "<status>completed</status>\n"
        "</task-notification>"
    )
    return {
        "type": "user",
        "message": {"content": [{"type": "text", "text": text}]},
        "timestamp": "2026-06-17T08:30:00.000Z",
    }


def _current_map(parent_sid: str = PARENT) -> dict[str, str]:
    return {"@1": parent_sid}


@pytest.mark.asyncio
async def test_state1_fresh_no_close_lifts(monitor, tmp_path):
    """STATE 1: fresh wf_dir, task_id recovered, NO close → reopen bracket + emit
    the ``wf-task:`` launched key (the fan-out seeds+lifts the parent route)."""
    parent_jsonl, proj_dir = _setup_parent(monitor, tmp_path)
    wf = _wf_dir(proj_dir)
    _make_fresh_agent_file(wf)
    _append(parent_jsonl, [_launch_entry(_launch_text(proj_dir))])

    await monitor._reconcile_workflow_brackets_on_startup(_current_map())

    brackets = monitor._open_workflow_brackets.get(PARENT, {})
    assert TASK_ID in brackets
    assert brackets[TASK_ID].closing is False
    assert brackets[TASK_ID].wf_dir == wf
    activity = monitor.pop_sidechain_activity()
    assert f"wf-task:{TASK_ID}" in activity[PARENT].launched


@pytest.mark.asyncio
async def test_state2_close_found_no_lift_display_only(monitor, tmp_path):
    """STATE 2: a matching ``<task-notification>`` close exists → NO launched key
    (no false re-light); a DISPLAY-ONLY closing bracket is opened for the final
    ↳ tail+collapse."""
    parent_jsonl, proj_dir = _setup_parent(monitor, tmp_path)
    wf = _wf_dir(proj_dir)
    _make_fresh_agent_file(wf)
    _append(
        parent_jsonl,
        [_launch_entry(_launch_text(proj_dir)), _close_entry()],
    )

    await monitor._reconcile_workflow_brackets_on_startup(_current_map())

    brackets = monitor._open_workflow_brackets.get(PARENT, {})
    assert TASK_ID in brackets
    assert brackets[TASK_ID].closing is True  # display-only
    assert monitor.pop_sidechain_activity() == {}  # NO lift


@pytest.mark.asyncio
async def test_state3_unrecoverable_task_id_no_lift(monitor, tmp_path):
    """STATE 3: a fresh wf_dir whose launch line is NOT in the parent JSONL (the
    launch scrolled out / unrecoverable) → DO NOT LIFT (fail-closed)."""
    parent_jsonl, proj_dir = _setup_parent(monitor, tmp_path)
    wf = _wf_dir(proj_dir)
    _make_fresh_agent_file(wf)
    # parent JSONL has NO Workflow launch line.

    await monitor._reconcile_workflow_brackets_on_startup(_current_map())

    assert monitor._open_workflow_brackets.get(PARENT, {}) == {}
    assert monitor.pop_sidechain_activity() == {}


@pytest.mark.asyncio
async def test_staleness_old_mtime_no_lift_and_no_scan(monitor, tmp_path):
    """A wf_dir whose freshest *.jsonl mtime is older than the TTL → no lift, and
    the JSONL is NEVER scanned (the staleness filter is stat-only, BEFORE scan)."""
    parent_jsonl, proj_dir = _setup_parent(monitor, tmp_path)
    wf = _wf_dir(proj_dir)
    _make_fresh_agent_file(wf)
    stale = time.time() - (BG_AGENT_TTL_SECONDS + 600)
    os.utime(wf / "agent-aaa.jsonl", (stale, stale))
    _append(parent_jsonl, [_launch_entry(_launch_text(proj_dir))])

    scanned = []
    orig = monitor._scan_workflow_launches_and_closes

    async def _spy(path):
        scanned.append(path)
        return await orig(path)

    monitor._scan_workflow_launches_and_closes = _spy  # type: ignore[method-assign]

    await monitor._reconcile_workflow_brackets_on_startup(_current_map())

    assert monitor._open_workflow_brackets.get(PARENT, {}) == {}
    assert monitor.pop_sidechain_activity() == {}
    assert scanned == []  # stale-only parent → no content read


@pytest.mark.asyncio
async def test_idempotency_live_bracket_skipped(monitor, tmp_path):
    """A parent that ALREADY has a live open bracket is skipped — no new lift."""
    from cctelegram.session_monitor import _WorkflowBracket

    parent_jsonl, proj_dir = _setup_parent(monitor, tmp_path)
    wf = _wf_dir(proj_dir)
    _make_fresh_agent_file(wf)
    _append(parent_jsonl, [_launch_entry(_launch_text(proj_dir))])
    monitor._open_workflow_brackets[PARENT] = {
        "existing": _WorkflowBracket(wf_dir=wf, last_seen_mtime=0.0, launch_wall=1.0)
    }

    await monitor._reconcile_workflow_brackets_on_startup(_current_map())

    # The pre-existing bracket is untouched; no fresh wf-task: lift emitted.
    assert set(monitor._open_workflow_brackets[PARENT]) == {"existing"}
    assert monitor.pop_sidechain_activity() == {}


@pytest.mark.asyncio
async def test_cost_bound_no_wf_dirs_no_scan(monitor, tmp_path):
    """A parent with ZERO wf_* dirs triggers NO JSONL scan (the cost-bound
    property: stat-only discovery, content read only when a fresh dir exists)."""
    parent_jsonl, proj_dir = _setup_parent(monitor, tmp_path)
    # No workflows dir at all.

    scanned = []
    orig = monitor._scan_workflow_launches_and_closes

    async def _spy(path):
        scanned.append(path)
        return await orig(path)

    monitor._scan_workflow_launches_and_closes = _spy  # type: ignore[method-assign]

    await monitor._reconcile_workflow_brackets_on_startup(_current_map())

    assert scanned == []
    assert monitor.pop_sidechain_activity() == {}


@pytest.mark.asyncio
async def test_no_reflood_reopened_bracket_resumes_at_eof(monitor, tmp_path):
    """A reconciler-reopened bracket's sub-files first-seen post-restart start at
    EOF — pre-restart blocks are NOT re-emitted as ↳ cards."""
    parent_jsonl, proj_dir = _setup_parent(monitor, tmp_path)
    wf = _wf_dir(proj_dir)
    # Agent file already has history (would be a reflood if replayed from 0).
    _make_fresh_agent_file(
        wf,
        content=json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "old block"}]},
                "timestamp": "2026-06-17T07:00:00.000Z",
            }
        )
        + "\n",
    )
    _append(parent_jsonl, [_launch_entry(_launch_text(proj_dir))])

    await monitor._reconcile_workflow_brackets_on_startup(_current_map())
    # First sidechain pass after the reopen: the agent file registers at EOF.
    msgs = await monitor.check_sidechain_updates({PARENT})
    assert all(m.text != "old block" for m in msgs)  # no history replay


# ── review hardening (codex P1 + P2-scope) ───────────────────────────────────


@pytest.mark.asyncio
async def test_corrupt_close_line_fails_closed_no_lift(monitor, tmp_path):
    """P1 (codex): a partial/corrupt `<task-notification>` line means a close
    might be MISSED — the scan is UNRELIABLE → fail-closed (NO lift), never the
    false relight of a completed Workflow the reconciler exists to prevent."""
    parent_jsonl, proj_dir = _setup_parent(monitor, tmp_path)
    wf = _wf_dir(proj_dir)
    _make_fresh_agent_file(wf)
    _append(parent_jsonl, [_launch_entry(_launch_text(proj_dir))])
    # A corrupt close line: carries the `task-notification` marker (byte
    # pre-filter hits) but is invalid JSON (truncated).
    with open(parent_jsonl, "a") as f:
        f.write(
            '{"type":"user","message":{"content":[{"text":"<task-notification' + "\n"
        )

    await monitor._reconcile_workflow_brackets_on_startup(_current_map())

    assert monitor._open_workflow_brackets.get(PARENT, {}) == {}
    assert monitor.pop_sidechain_activity() == {}


@pytest.mark.asyncio
async def test_launch_without_transcript_dir_not_recovered_no_lift(monitor, tmp_path):
    """P2 (codex): a `Task ID:` line WITHOUT a validated Workflow transcript dir
    (e.g. quoted/pasted prose) does NOT recover a launch key → no false lift; the
    launch is scoped to a genuine Workflow tool_result by its transcript dir."""
    parent_jsonl, proj_dir = _setup_parent(monitor, tmp_path)
    wf = _wf_dir(proj_dir)
    _make_fresh_agent_file(wf)
    # Launch-LOOKING text with Task ID + Run ID but NO `Transcript dir:` line.
    text = f"Workflow launched in background. Task ID: {TASK_ID}\nRun ID: {RUNID}\n"
    _append(parent_jsonl, [_launch_entry(text)])

    await monitor._reconcile_workflow_brackets_on_startup(_current_map())

    assert monitor._open_workflow_brackets.get(PARENT, {}) == {}
    assert monitor.pop_sidechain_activity() == {}


@pytest.mark.asyncio
async def test_fresh_dir_not_starved_by_many_stale_dirs(monitor, tmp_path):
    """P2 (codex): a parent with MANY stale wf_* dirs must NOT starve a later
    fresh dir out of the per-tick cap — the cap bounds FRESH candidates, the stale
    dirs are filtered out first."""
    parent_jsonl, proj_dir = _setup_parent(monitor, tmp_path)
    stale_ts = time.time() - (BG_AGENT_TTL_SECONDS + 600)
    for i in range(monitor._RECONCILE_MAX_WF_DIRS + 5):
        d = _wf_dir(proj_dir, runid=f"wf_stale{i:03d}")
        _make_fresh_agent_file(d)
        os.utime(d / "agent-aaa.jsonl", (stale_ts, stale_ts))
    # The one genuinely-fresh run.
    fresh = _wf_dir(proj_dir)  # RUNID
    _make_fresh_agent_file(fresh)
    _append(parent_jsonl, [_launch_entry(_launch_text(proj_dir))])

    await monitor._reconcile_workflow_brackets_on_startup(_current_map())

    assert TASK_ID in monitor._open_workflow_brackets.get(PARENT, {})
    assert f"wf-task:{TASK_ID}" in monitor.pop_sidechain_activity()[PARENT].launched


@pytest.mark.asyncio
async def test_pasted_launch_prose_in_text_block_not_recovered(monitor, tmp_path):
    """P2 (codex re-review): a FULL launch block (Task ID + Run ID + a real
    `Transcript dir: .../subagents/workflows/wf_*`) pasted into a user TEXT block
    must NOT recover a launch key — launch recovery is scoped to genuine Workflow
    `tool_result` blocks, never plain text."""
    parent_jsonl, proj_dir = _setup_parent(monitor, tmp_path)
    wf = _wf_dir(proj_dir)
    _make_fresh_agent_file(wf)
    # The full, valid launch prose — but in a user `text` block (as if pasted),
    # NOT a tool_result. The transcript dir basename even matches the fresh wf_dir.
    pasted = {
        "type": "user",
        "message": {"content": [{"type": "text", "text": _launch_text(proj_dir)}]},
        "timestamp": "2026-06-17T08:00:00.000Z",
    }
    _append(parent_jsonl, [pasted])

    await monitor._reconcile_workflow_brackets_on_startup(_current_map())

    assert monitor._open_workflow_brackets.get(PARENT, {}) == {}
    assert monitor.pop_sidechain_activity() == {}
