"""Session monitoring service — watches JSONL files for new messages.

Runs an async polling loop that:
  1. Loads the current session_map to know which sessions to watch.
  2. Detects session_map changes (new/changed/deleted windows) and cleans up.
  3. Reads new JSONL lines from each session file using byte-offset tracking.
  4. Parses entries via TranscriptParser and emits NewMessage objects to a callback.
  5. Tails sub-agent (sidechain) JSONLs unconditionally — display emission is
     gated by show_tool_calls, but per-tick per-agent activity (max event ts,
     end-of-turn) plus parent-transcript async-launch / task-notification
     signals are always reported (pop_sidechain_activity → the GH #44
     route_runtime background-agent marks).

Optimizations: mtime cache skips unchanged files; byte offset avoids re-reading.

Key classes: SessionMonitor, NewMessage, SessionInfo.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable, Literal

import aiofiles

from . import md_capture
from .config import config
from .monitor_state import MonitorState, TrackedSession
from .tmux_manager import tmux_manager
from .transcript_parser import TranscriptParser
from .utils import (
    normalize_background_agent_key,
    parse_iso_timestamp,
    read_cwd_from_jsonl,
)

logger = logging.getLogger(__name__)

# Fix 11: minimum number of _read_new_lines cycles an unparseable non-empty
# line must survive (while the file has grown PAST its end) before it is
# discarded. A genuine partial write parses on the next cycle or two; a line
# that never parses after the file grew beyond it is a crash-torn fusion that
# would otherwise wedge the session's byte offset forever.
_STALL_SKIP_MIN_CYCLES = 3

# Tool names whose buffered turn carries the Bug 2 live-prose surface. Mirrors
# route_runtime.INTERACTIVE_TOOL_NAMES; duplicated to keep session_monitor free
# of a route_runtime import (and the dedup is content-only, not run-state).
_INTERACTIVE_TOOL_NAMES = frozenset({"AskUserQuestion", "ExitPlanMode"})

# GH #44: stop_reasons that end an assistant turn. Mirrors
# ``route_runtime.TURN_END_REASONS``; duplicated to keep session_monitor free
# of a route_runtime import (same pattern as _INTERACTIVE_TOOL_NAMES above) —
# the fixture-gate test pins the two definitions equal.
_TURN_END_REASONS = frozenset({"end_turn", "stop_sequence"})


@dataclass
class SidechainTick:
    """One tick's parsed activity for one sidechain agent (GH #44).

    ``max_event_ts`` — max JSONL timestamp over the batch's parsed entries
    (epoch via the shared ``utils.parse_iso_timestamp``; ``None`` when no
    entry carried a parseable stamp). ``saw_end_of_turn`` — any entry
    (INCLUDING lifecycle-only markers with no visible text) ended the
    agent's turn.
    """

    max_event_ts: float | None = None
    saw_end_of_turn: bool = False


@dataclass
class ParentSidechainActivity:
    """Per-parent, per-tick background-agent signals (GH #44).

    ``ticks`` — normalized agent_key → SidechainTick from the sidechain
    tail. ``launched`` — agentIds extracted from async-launch Agent
    tool_results in the PARENT transcript this tick (plus the raw
    ``wf-task:<id>`` keys from Workflow launches — Fix 2a). ``completed`` —
    task-ids extracted from parent ``<task-notification>`` user entries (plus
    the matching ``wf-task:<id>`` close keys — Fix 2d). ``bracket_heartbeats``
    — ``wf-task:<id>`` → freshest sidechain ``*.jsonl`` mtime, emitted ONLY on
    a per-bracket mtime ADVANCE (Fix 2c, DESIGN B — a SEPARATE channel from
    ``ticks`` so run-state never consumes sidechain entries for a Workflow;
    the bot fan-out turns it into ``mark_background_agent_activity``). Drained
    consume-once via ``pop_sidechain_activity`` and applied by the bot fan-out
    AFTER the tick's lifecycle dispatch (the §4.2 ordering: lifecycle → launch
    → activity → done).
    """

    launched: set[str] = field(default_factory=set)
    completed: set[str] = field(default_factory=set)
    ticks: dict[str, SidechainTick] = field(default_factory=dict)
    bracket_heartbeats: dict[str, float] = field(default_factory=dict)


@dataclass
class _WorkflowBracket:
    """One open Workflow-tool bracket (ISSUE-6 / Fix 2c).

    Persists across poll ticks (NOT per-tick like ``ParentSidechainActivity``):
    opened at the Workflow launch parse, closed at the ``<task-notification>``
    or any sidechain teardown. ``wf_dir`` is the validated
    ``subagents/workflows/wf_<runid>`` Path whose freshest ``*.jsonl`` mtime is
    stat'd each poll (the heartbeat gate); ``None`` when the launch carried no
    Run ID / Transcript dir → the bracket NEVER heartbeats and its key ages
    out one ``BG_AGENT_TTL_SECONDS`` from ``launch_wall``. ``last_seen_mtime``
    is the advance-only gate; ``launch_wall`` is the no-dir TTL basis.
    """

    wf_dir: Path | None
    last_seen_mtime: float
    launch_wall: float
    # Fix 5 PR-B: set at the ``<task-notification>`` close INSTEAD of popping the
    # bracket, so ``check_sidechain_updates`` tails the ``wf_dir`` ONE final time
    # (final display tail) and emits the route-FIFO collapse signal BEFORE the
    # bracket is removed. ``_emit_workflow_bracket_heartbeats`` skips closing
    # brackets (the run-state done already fired via ``rec.completed``).
    closing: bool = False


def _is_window_id(key: str) -> bool:
    """Check if a session_map suffix looks like a tmux window ID (e.g. '@0', '@12').

    Mirrors ``SessionManager._is_window_id``; kept local so session_monitor
    has no import-time dependency on ``session``.
    """
    return key.startswith("@") and len(key) > 1 and key[1:].isdigit()


@dataclass
class SessionInfo:
    """Information about a Claude Code session."""

    session_id: str
    file_path: Path


@dataclass
class TranscriptEvent:
    """A lifecycle event derived from one parsed JSONL block.

    Lower-level than NewMessage — preserves raw JSONL provenance
    (stop_reason, timestamp, tool_use_id) so consumers like the
    BusyIndicator can drive run-state transitions without re-deriving
    them from heuristics on the rendered text.
    """

    session_id: str
    role: Literal["user", "assistant"]
    block_type: Literal["text", "thinking", "tool_use", "tool_result"]
    tool_use_id: str | None
    tool_name: str | None
    stop_reason: str | None
    timestamp: str | None
    text: str
    image_data: list[tuple[str, bytes]] | None
    tool_input: dict[str, Any] | None = None
    transcript_uuid: str | None = None
    # JSONL ``message.id`` (per-message, shared across a turn's blocks) and the
    # synthetic-text origin marker, propagated from ParsedEntry. Bug 2's
    # live-prose dedup groups a prose block with its sibling interactive
    # tool_use by ``(session_id, message_id)`` and excludes synthetic plan text.
    message_id: str | None = None
    block_origin: str | None = None


@dataclass
class NewMessage:
    """A new message detected by the monitor."""

    session_id: str
    text: str
    content_type: str = "text"  # "text" or "thinking"
    tool_use_id: str | None = None
    role: str = "assistant"  # "user" or "assistant"
    tool_name: str | None = None  # For tool_use messages, the tool name
    image_data: list[tuple[str, bytes]] | None = None  # From tool_result images
    # Raw tool_use input dict for Edit/Write/Agent/Task — used by §2.7
    # Agent prominence to pull description / subagent_type / prompt out of
    # the JSONL block without a second pass over the file.
    tool_input: dict[str, Any] | None = None
    transcript_uuid: str | None = None
    # When non-None, this message represents a block from a sub-agent's
    # sidechain JSONL. The handler routes it to the per-sub-agent digest
    # so a multi-step run renders as one editable message instead of one
    # bubble per block.
    subagent_key: str | None = None
    # Fix 5 PR-B: a display-control marker (mutually exclusive with content
    # fields) emitted on the ``new_messages`` lane AFTER a closed Workflow run's
    # final display cards. ``bot.handle_new_message`` routes it to
    # ``message_queue.enqueue_subagent_collapse(route, prefix)`` — a route-FIFO
    # ``subagent_collapse`` control task that collapses every live ``↳`` card
    # whose ``subagent_key`` starts with this prefix (``sub:<parent>:<runid>:``).
    # DISPLAY ONLY; never a run-state input.
    subagent_collapse_prefix: str | None = None
    # JSONL ``message.stop_reason``, propagated from ParsedEntry. Used by
    # the bot adapter to identify end-of-turn assistant text bubbles, where
    # the per-message context-window footer is appended.
    stop_reason: str | None = None
    # JSONL ``message.id`` (per-message, shared across a turn's blocks) and the
    # synthetic-text origin marker, propagated from ParsedEntry. Bug 2's
    # live-prose dedup groups a prose block with its sibling interactive
    # tool_use by ``(session_id, message_id)`` and excludes synthetic plan text
    # (``block_origin``) so it never suppresses real prose.
    message_id: str | None = None
    block_origin: str | None = None


def filter_live_prose_duplicates(messages: list[NewMessage]) -> list[NewMessage]:
    """Bug 2 dedup: suppress the post-resolution JSONL copy of prose that was
    already delivered LIVE before a picker card.

    Operates at the batch/group level (NOT per-message) because the prose text
    block and its sibling interactive ``tool_use`` arrive as separate
    ``NewMessage``s of the SAME ``message_id``, and prose precedes the tool_use
    — only the whole batch sees the pairing. For each ``(session_id,
    message_id)`` group that contains an AskUserQuestion / ExitPlanMode
    ``tool_use``, the REAL text blocks are aggregated (synthetic ExitPlanMode
    plan text, ``block_origin`` set, is excluded), normalized via the SINGLE
    shared ``md_capture.prose_norm_hash``, and matched against an unconsumed
    shown-live marker for the session. A match → suppress that group's prose
    ``NewMessage``s and consume the marker (consume-once, restart-safe).

    EPM ambiguity safety: if MORE THAN ONE group in the batch matches the same
    ``(session_id, norm_hash)``, suppress NONE (no first-match-wins). Multi-block
    parity: aggregation joins the parser-stripped blocks with ``\\n``; this is
    exact for single-block prose (Bug 2's observed shape) and adjacent
    multi-block, and degrades to a benign double-post only for the rare
    multi-block message with a blank line BETWEEN blocks (see
    ``md_capture.normalize_prose``).

    Returns the batch with suppressed prose removed (order preserved). A batch
    with no interactive group is returned unchanged with no marker I/O.
    """
    groups: dict[tuple[str, str], list[NewMessage]] = {}
    for m in messages:
        if m.role == "assistant" and m.session_id and m.message_id:
            groups.setdefault((m.session_id, m.message_id), []).append(m)
    if not groups:
        return messages

    # (session_id, norm_hash) -> list of each matching group's real-prose msgs.
    by_key: dict[tuple[str, str], list[list[NewMessage]]] = {}
    for (sid, _mid), grp in groups.items():
        has_interactive = any(
            g.content_type == "tool_use" and g.tool_name in _INTERACTIVE_TOOL_NAMES
            for g in grp
        )
        if not has_interactive:
            continue
        real = [
            g
            for g in grp
            if g.content_type == "text" and g.block_origin is None and g.text
        ]
        if not real:
            continue
        norm_hash = md_capture.prose_norm_hash("\n".join(g.text for g in real))
        by_key.setdefault((sid, norm_hash), []).append(real)
    if not by_key:
        return messages

    markers_cache: dict[str, list[md_capture.ShownLiveMarker]] = {}
    suppress: set[int] = set()
    for (sid, norm_hash), real_lists in by_key.items():
        markers = markers_cache.get(sid)
        if markers is None:
            markers = md_capture.read_shown_live_markers(sid)
            markers_cache[sid] = markers
        match = next((mk for mk in markers if mk.norm_hash == norm_hash), None)
        if match is None:
            continue
        if len(real_lists) > 1:
            # >1 unresolved candidate group for one marker — ambiguous; the
            # plan's contract is to suppress NONE and consume no marker.
            logger.warning(
                "live-prose dedup ambiguous for session %s (%d groups share "
                "one marker); suppressing none",
                sid[:8],
                len(real_lists),
            )
            continue
        for g in real_lists[0]:
            suppress.add(id(g))
        md_capture.consume_shown_live(sid, match.md_message_id)
        logger.info(
            "Bug2 live-prose deduped post-resolution copy: session=%s md_msg=%s",
            sid[:8],
            match.md_message_id[:8],
        )
    if not suppress:
        return messages
    return [m for m in messages if id(m) not in suppress]


class SessionMonitor:
    """Monitors Claude Code sessions for new assistant messages.

    Uses simple async polling with aiofiles for non-blocking I/O.
    Emits both intermediate and complete assistant messages.
    """

    def __init__(
        self,
        projects_path: Path | None = None,
        poll_interval: float | None = None,
        state_file: Path | None = None,
    ):
        self.projects_path = (
            projects_path if projects_path is not None else config.claude_projects_path
        )
        self.poll_interval = (
            poll_interval if poll_interval is not None else config.monitor_poll_interval
        )

        self.state = MonitorState(state_file=state_file or config.monitor_state_file)
        self.state.load()

        self._running = False
        self._task: asyncio.Task | None = None
        self._message_callback: Callable[[NewMessage], Awaitable[None]] | None = None
        self._event_callback: Callable[[TranscriptEvent], Awaitable[None]] | None = None
        # Optional bot reference, set via ``set_bot`` from ``bot.post_init``.
        # Used by ``_hydrate_ask_tool_input_cache`` to trigger
        # ``maybe_upgrade_auq_context_message`` when a buffered AUQ is
        # discovered post-restart and a form-source context message was
        # persisted pre-restart — without this hook the descriptions
        # never get edited in after the bot comes back up.
        self._bot: Any = None
        # Per-session pending tool_use state carried across poll cycles
        self._pending_tools: dict[str, dict[str, Any]] = {}  # session_id -> pending
        # Track last known session_map for detecting changes.
        # Only window_id (@12) keys are accepted; pre-2026-02-11 window_name
        # keys are filtered out (with a one-shot warning) in
        # _load_current_session_map.
        self._last_session_map: dict[str, str] = {}  # window_id -> session_id
        # One-shot guard so the legacy-key warning fires at most once per run.
        self._warned_legacy_session_map_keys = False
        # In-memory mtime cache for quick file change detection (not persisted)
        self._file_mtimes: dict[str, float] = {}  # session_id -> last_seen_mtime
        # Fix 11: per-session tracking of a non-empty line that repeatedly
        # fails to parse, so a never-parseable line (crash-torn fusion) can
        # be skipped instead of wedging the offset forever.
        # session_id -> (line_start_offset, first_seen_ts, read_count).
        # At most one entry per session (keyed by session_id; the offset is
        # stored in the value and resets the entry when it moves). Popped on
        # skip, on offset progress, and at every session-cleanup site.
        self._unparseable_stalls: dict[str, tuple[int, float, int]] = {}
        # GH #44 (successor of Wave A's parent-set): per-parent, per-tick
        # background-agent signals — sidechain ticks (key + max event ts +
        # saw_end_of_turn) populated by ``check_sidechain_updates``
        # unconditionally (even with show_tool_calls disabled), plus
        # async-launch agentIds / task-notification completions collected in
        # the PARENT parse path by ``check_for_updates``. Drained consume-once
        # via ``pop_sidechain_activity`` — the run-state keep-alive +
        # projection-input signal.
        self._sidechain_activity: dict[str, ParentSidechainActivity] = {}
        # ISSUE-6 / Fix 2c: persistent per-parent open Workflow brackets
        # (parent_sid -> {task_id -> _WorkflowBracket}). Opened at a Workflow
        # launch parse, stat'd each poll for an mtime-advance heartbeat, and
        # removed at the matching <task-notification> close OR any sidechain
        # teardown. NOT per-tick — survives ticks until close.
        self._open_workflow_brackets: dict[str, dict[str, _WorkflowBracket]] = {}
        # Per-tick fan-out for sidechain activity (wired from bot.post_init,
        # like ``_message_callback`` / ``_event_callback``).
        self._subagent_activity_callback: (
            Callable[[dict[str, ParentSidechainActivity]], Awaitable[None]] | None
        ) = None

    def set_message_callback(
        self, callback: Callable[[NewMessage], Awaitable[None]]
    ) -> None:
        self._message_callback = callback

    def set_bot(self, bot: Any) -> None:
        """Store a bot reference for hydrate-time AUQ context upgrades."""
        self._bot = bot

    def set_event_callback(
        self, callback: Callable[[TranscriptEvent], Awaitable[None]]
    ) -> None:
        self._event_callback = callback

    def set_subagent_activity_callback(
        self,
        callback: Callable[[dict[str, ParentSidechainActivity]], Awaitable[None]],
    ) -> None:
        """Wire the per-tick sidechain-activity fan-out (GH #44, ex-Wave A).

        Called from ``_monitor_loop`` with the per-parent
        ``ParentSidechainActivity`` map for this tick — AFTER the tick's
        parent lifecycle events have already been dispatched (the §4.2
        ordering guarantee the tombstone-reset logic relies on).
        """
        self._subagent_activity_callback = callback

    def pop_sidechain_activity(self) -> dict[str, ParentSidechainActivity]:
        """Drain (consume-once) this tick's per-parent background-agent
        signals (sidechain ticks + parent launch/completion extractions)."""
        activity = self._sidechain_activity
        self._sidechain_activity = {}
        return activity

    def _parent_activity(self, parent_session_id: str) -> ParentSidechainActivity:
        """Get/create the per-tick activity record for a parent session."""
        rec = self._sidechain_activity.get(parent_session_id)
        if rec is None:
            rec = ParentSidechainActivity()
            self._sidechain_activity[parent_session_id] = rec
        return rec

    def _open_workflow_bracket(self, parent_session_id: str, info: Any) -> None:
        """Open a persistent Workflow bracket (ISSUE-6 / Fix 2c).

        ``info`` is a ``response_builder.WorkflowLaunchInfo``. The bracket is
        keyed by the bare ``task_id`` (the ``wf-task:`` prefix is added on the
        key seams); ``wf_dir`` (the validated ``subagents/workflows/wf_…``
        path) feeds the mtime heartbeat. A re-launch of the same task-id
        refreshes the bracket. Defensive against a tombstoned re-launch is the
        route_runtime done-guard, not here.
        """
        tdir = info.transcript_dir
        wf_dir = Path(tdir) if tdir else None
        brackets = self._open_workflow_brackets.setdefault(parent_session_id, {})
        brackets[info.task_id] = _WorkflowBracket(
            wf_dir=wf_dir,
            last_seen_mtime=0.0,
            launch_wall=time.time(),
        )

    def _has_open_workflow_bracket(self, parent_session_id: str, task_id: str) -> bool:
        """True IFF a live OPEN Workflow bracket exists for ``task_id``
        (ISSUE-6 / Fix 2d).

        Gate-on-bracket-only: the open bracket is the SOLE signal that a
        ``<task-notification>`` close belongs to a Workflow launch. An isolated
        close with no open bracket has no route_runtime bg key to tombstone, so
        the bare normalized close key suffices and NO ``wf-task:`` close key is
        emitted — we never guess "is this a Workflow id?" from the id's
        character set (a fragile external-format assumption).
        """
        brackets = self._open_workflow_brackets.get(parent_session_id)
        return bool(brackets) and task_id in brackets

    def _close_workflow_bracket(self, parent_session_id: str, task_id: str) -> None:
        """Remove an open Workflow bracket on its ``<task-notification>`` close
        (ISSUE-6 / Fix 2d). No-op for an unknown task-id."""
        brackets = self._open_workflow_brackets.get(parent_session_id)
        if not brackets:
            return
        brackets.pop(task_id, None)
        if not brackets:
            self._open_workflow_brackets.pop(parent_session_id, None)

    def _emit_workflow_bracket_heartbeats(self, parent_session_id: str) -> None:
        """Stat each open Workflow bracket's ``wf_dir`` and emit a
        ``wf-task:<id>`` heartbeat ONLY when the freshest ``*.jsonl`` mtime
        ADVANCED (ISSUE-6 / Fix 2c, DESIGN B).

        A bracket with ``wf_dir is None`` never heartbeats (it ages out one
        TTL from ``launch_wall``). The heartbeat lands in
        ``ParentSidechainActivity.bracket_heartbeats`` — a SEPARATE channel
        from ``ticks`` so run-state never consumes a Workflow's sidechain
        entries; only the dir-stat drives the lift.
        """
        brackets = self._open_workflow_brackets.get(parent_session_id)
        if not brackets:
            return
        for task_id, bracket in brackets.items():
            # Fix 5 PR-B: a closing bracket does NOT heartbeat — the run-state
            # done already fired via rec.completed at the <task-notification>,
            # and the bot fan-out processes completed AFTER bracket_heartbeats,
            # so a stray heartbeat would lose to the done anyway; skipping is
            # cleaner and avoids the activity→done churn.
            if bracket.wf_dir is None or bracket.closing:
                continue
            try:
                latest = max(
                    (f.stat().st_mtime for f in bracket.wf_dir.glob("*.jsonl")),
                    default=0.0,
                )
            except OSError:
                continue
            if latest > bracket.last_seen_mtime:
                bracket.last_seen_mtime = latest
                self._parent_activity(parent_session_id).bracket_heartbeats[
                    f"wf-task:{task_id}"
                ] = latest

    # PR-1 Half B (BUSY restart reconciler). The fresh-mtime window for a wf_*
    # dir to be considered a still-running Workflow — mirrors
    # ``route_runtime.BG_AGENT_TTL_SECONDS`` (1800s) WITHOUT importing it (this
    # module deliberately carries no route_runtime import). PER-PARENT cap on the
    # FRESH wf_* candidates (newest-first), so a pathological many-run parent can't
    # blow the first sweep AND a parent's stale dirs never starve a fresh one.
    _RECONCILE_FRESH_WINDOW_S = 1800.0
    _RECONCILE_MAX_WF_DIRS = 16

    async def _scan_workflow_launches_and_closes(
        self, jsonl_path: Path
    ) -> tuple[dict[str, str], set[str], bool]:
        """Bounded full-scan of a parent JSONL for Workflow launches + closes
        (PR-1 Half B). Returns ``(launches, closes, reliable)`` where ``launches``
        maps a ``wf_<runid>`` dir-name → its Task ID (via the launch tool_result's
        Run ID / Transcript dir), ``closes`` is the set of ``<task-notification>``
        task-ids, and ``reliable`` is False iff a prefiltered (potential launch or
        close) line could NOT be parsed.

        Mirrors ``_auq_tool_result_present``'s cheap byte pre-filter + whole-file
        stream (the launch / close can scroll far past any tail on a long
        session): only a line containing ``Task ID`` or ``task-notification`` is
        JSON-parsed. **Fail-CLOSED on a malformed prefiltered line (codex P1):** a
        corrupt/partial ``<task-notification>`` line would otherwise leave its
        close OUT of ``closes`` and false-relight a COMPLETED Workflow — so any
        such parse failure flips ``reliable`` False and the caller does NOT lift
        for that parent. A read error returns ``({}, set(), False)`` likewise.

        Launch recovery is SCOPED to a genuine Workflow launch by reading ONLY
        ``tool_result`` block text (codex P2 — pasted launch prose lands in a user
        ``text`` block, never a ``tool_result``) AND requiring the VALIDATED
        Workflow ``transcript_dir`` (under ``subagents/workflows/wf_…``). Close
        detection reads BOTH lanes (a ``<task-notification>`` close IS a user
        ``text`` block, and a missed close must fail closed)."""
        launches: dict[str, str] = {}
        closes: set[str] = set()
        reliable = True
        from .handlers.response_builder import (
            extract_task_notification_task_id,
            extract_workflow_launch_info,
        )

        def _block_texts(entry: dict) -> tuple[list[str], list[str]]:
            """Return ``(tool_result_texts, plain_texts)`` for an entry. Launch
            recovery reads ONLY ``tool_result`` block text (a genuine Workflow
            launch RESULT — codex P2: pasted launch prose lands in a user ``text``
            block, never a ``tool_result``, so it can't recover a key); close
            detection reads BOTH (a ``<task-notification>`` close is a user ``text``
            block, and a missed close must fail closed)."""
            msg = entry.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            tr: list[str] = []
            tx: list[str] = []
            # A string ``message.content`` is plain assistant text, never a
            # tool_result wrapper → plain lane only.
            if isinstance(content, str):
                tx.append(content)
            elif isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    ctype = c.get("type")
                    if ctype == "tool_result":
                        tc = c.get("content")
                        if isinstance(tc, str):
                            tr.append(tc)
                        elif isinstance(tc, list):
                            for b in tc:
                                if isinstance(b, dict) and isinstance(
                                    b.get("text"), str
                                ):
                                    tr.append(b["text"])
                    elif ctype == "text" and isinstance(c.get("text"), str):
                        tx.append(c["text"])
            return tr, tx

        try:
            async with aiofiles.open(jsonl_path, "rb") as f:
                async for raw in f:
                    has_task = b"Task ID" in raw
                    has_notif = b"task-notification" in raw
                    if not (has_task or has_notif):
                        continue
                    try:
                        entry = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        # A prefiltered (potential launch/close) line we could not
                        # read. A MISSED close would false-relight a completed
                        # Workflow → mark the scan unreliable; the caller fails
                        # closed (no lift for this parent).
                        reliable = False
                        continue
                    if not isinstance(entry, dict):
                        reliable = False
                        continue
                    tr_texts, tx_texts = _block_texts(entry)
                    if has_task:
                        # Launches ONLY from tool_result blocks (codex P2).
                        for text in tr_texts:
                            info = extract_workflow_launch_info(text)
                            # Require the VALIDATED Workflow transcript dir so a
                            # quoted/pasted ``Task ID:`` line can't recover a key.
                            if info and info.task_id and info.transcript_dir:
                                for name in (
                                    info.run_id,
                                    Path(info.transcript_dir).name,
                                ):
                                    if name:
                                        launches[name] = info.task_id
                    if has_notif:
                        # Closes from EITHER lane (a missed close must fail closed).
                        for text in tr_texts + tx_texts:
                            tid = extract_task_notification_task_id(text)
                            if tid:
                                closes.add(tid)
        except OSError:
            return {}, set(), False
        return launches, closes, reliable

    async def _reconcile_workflow_brackets_on_startup(
        self, current_map: dict[str, str]
    ) -> None:
        """Re-arm a still-running Workflow's busy lift (typing + 🟡 + ↳) from the
        FILESYSTEM after a ``launchctl kickstart`` wiped the in-memory brackets /
        background_agents (PR-1 Half B). The owner's highest-frequency symptom.

        For each tracked parent with NO live open bracket, STAT-glob
        ``<project>/<parent_sid>/subagents/workflows/wf_*`` (anchored, never
        ``rglob``) and, for any ``wf_*`` dir whose freshest ``*.jsonl`` mtime is
        within ``_RECONCILE_FRESH_WINDOW_S``, recover its Task ID + close-state
        from ONE bounded parent-JSONL scan and apply the THREE-state rule:

          1. task_id recovered + NO close → LIFT: reopen a ``_WorkflowBracket``
             (the steady-state heartbeat + Fix-5 ↳ display take over) AND emit the
             raw ``wf-task:<id>`` launched key — the bot fan-out
             (``apply_sidechain_activity`` → ``seed_idle_and_mark_background_agent_
             launched``) seeds the parent route IDLE and lifts it to projected
             RUNNING.
          2. close FOUND → NO runtime lift (a Workflow that finished just before
             the deploy must NOT false-relight): open a DISPLAY-ONLY ``closing``
             bracket so ``check_sidechain_updates`` tails the wf_dir ONE final time
             + fires the deterministic route-FIFO collapse, then drops it.
          3. task_id UNRECOVERABLE (launch scrolled past / scan failed) → DO NOT
             LIFT (fail-closed; prefer dark-until-next-turn over a false 🟡).

        STAT-ONLY discovery (no ``agent-*.jsonl`` content read during discovery);
        the parent JSONL is scanned ONLY when a fresh wf_* dir exists; a PER-PARENT
        cap on the FRESH candidates (newest-first) bounds a pathological many-run
        parent without a stale dir starving a fresh one (codex P2); the whole pass
        is wrapped so it can never extend or break the dispatch loop. Pull-only;
        no observer."""
        try:
            now = time.time()
            relit = 0
            for session_id in current_map.values():
                if session_id.startswith("sub:"):
                    continue
                if self._open_workflow_brackets.get(session_id):
                    continue  # idempotency: a live bracket already drives the lift
                tracked = self.state.get_session(session_id)
                if tracked is None or not tracked.file_path:
                    continue
                jsonl_path = Path(tracked.file_path)
                if not jsonl_path.exists():
                    continue
                wf_root = jsonl_path.parent / session_id / "subagents" / "workflows"
                try:
                    wf_dirs = [d for d in wf_root.glob("wf_*") if d.is_dir()]
                except OSError:
                    continue
                # STAT-ONLY freshness filter FIRST; only read the JSONL when at
                # least one fresh dir exists (the cost-bound property). The cap is
                # applied to the FRESH candidates (newest-first), so a parent's
                # stale dirs never starve a genuinely-live one (codex P2).
                fresh_with_ts: list[tuple[Path, float]] = []
                for wf_dir in wf_dirs:
                    try:
                        latest = max(
                            (f.stat().st_mtime for f in wf_dir.glob("*.jsonl")),
                            default=0.0,
                        )
                    except OSError:
                        continue
                    if latest <= 0.0 or (now - latest) > self._RECONCILE_FRESH_WINDOW_S:
                        continue
                    fresh_with_ts.append((wf_dir, latest))
                if not fresh_with_ts:
                    continue
                fresh_with_ts.sort(key=lambda t: t[1], reverse=True)  # newest first
                fresh = [d for d, _ in fresh_with_ts[: self._RECONCILE_MAX_WF_DIRS]]
                (
                    launches,
                    closes,
                    reliable,
                ) = await self._scan_workflow_launches_and_closes(jsonl_path)
                if not reliable:
                    # A malformed prefiltered (launch/close) line means a close may
                    # be MISSED → fail closed for this parent rather than risk a
                    # false relight of a completed Workflow (codex P1).
                    continue
                for wf_dir in fresh:
                    task_id = launches.get(wf_dir.name)
                    if task_id is None:
                        continue  # STATE 3: unrecoverable → fail-closed, no lift
                    brackets = self._open_workflow_brackets.setdefault(session_id, {})
                    if task_id in closes:
                        # STATE 2: finished pre-restart → display-only catch-up.
                        brackets[task_id] = _WorkflowBracket(
                            wf_dir=wf_dir,
                            last_seen_mtime=0.0,
                            launch_wall=now,
                            closing=True,
                        )
                        continue
                    # STATE 1: live → reopen bracket + emit the launched key.
                    brackets[task_id] = _WorkflowBracket(
                        wf_dir=wf_dir, last_seen_mtime=0.0, launch_wall=now
                    )
                    self._parent_activity(session_id).launched.add(f"wf-task:{task_id}")
                    relit += 1
            if relit:
                logger.info("relit %d workflow brackets from filesystem", relit)
        except Exception as e:  # never break startup
            logger.warning("workflow-bracket reconcile failed: %s", e)

    def register_session(
        self, session_id: str, file_path: Path, offset: int = 0
    ) -> bool:
        """Pre-register a freshly created session at a known byte offset.

        ``check_for_updates`` initializes a previously-unseen session at
        end-of-file to avoid replaying historical conversations. That default
        drops the first user/assistant exchange of a session created by the
        bot itself: the JSONL has already been appended with the seed
        message and Claude's reply by the time the monitor first observes
        the file. Pre-registering at offset 0 forces the next poll to read
        the whole file from the start.

        No-op if the session is already tracked (preserves the live offset
        across resume / bot-restart paths).
        """
        if self.state.get_session(session_id) is not None:
            return False
        self.state.update_session(
            TrackedSession(
                session_id=session_id,
                file_path=str(file_path),
                last_byte_offset=offset,
            )
        )
        self.state.save_if_dirty()
        logger.info(
            "Pre-registered session %s at offset %d (file=%s)",
            session_id,
            offset,
            file_path,
        )
        return True

    async def _get_active_cwds(self) -> set[str]:
        """Get normalized cwds of all active tmux windows."""
        cwds = set()
        windows = await tmux_manager.list_windows()
        for w in windows:
            try:
                cwds.add(str(Path(w.cwd).resolve()))
            except (OSError, ValueError):
                cwds.add(w.cwd)
        return cwds

    async def scan_projects(self) -> list[SessionInfo]:
        """Scan projects that have active tmux windows."""
        active_cwds = await self._get_active_cwds()
        if not active_cwds:
            return []

        sessions = []

        if not self.projects_path.exists():
            return sessions

        for project_dir in self.projects_path.iterdir():
            if not project_dir.is_dir():
                continue

            index_file = project_dir / "sessions-index.json"
            original_path = ""
            indexed_ids: set[str] = set()

            if index_file.exists():
                try:
                    async with aiofiles.open(index_file, "r") as f:
                        content = await f.read()
                    index_data = json.loads(content)
                    entries = index_data.get("entries", [])
                    original_path = index_data.get("originalPath", "")

                    for entry in entries:
                        session_id = entry.get("sessionId", "")
                        full_path = entry.get("fullPath", "")
                        project_path = entry.get("projectPath", original_path)

                        if not session_id or not full_path:
                            continue

                        try:
                            norm_pp = str(Path(project_path).resolve())
                        except (OSError, ValueError):
                            norm_pp = project_path
                        if norm_pp not in active_cwds:
                            continue

                        indexed_ids.add(session_id)
                        file_path = Path(full_path)
                        if file_path.exists():
                            sessions.append(
                                SessionInfo(
                                    session_id=session_id,
                                    file_path=file_path,
                                )
                            )

                except (json.JSONDecodeError, OSError) as e:
                    logger.debug(f"Error reading index {index_file}: {e}")

            # Pick up un-indexed .jsonl files
            try:
                for jsonl_file in project_dir.glob("*.jsonl"):
                    session_id = jsonl_file.stem
                    if session_id in indexed_ids:
                        continue

                    # Determine project_path for this file
                    file_project_path = original_path
                    if not file_project_path:
                        file_project_path = await asyncio.to_thread(
                            read_cwd_from_jsonl, jsonl_file
                        )
                    if not file_project_path:
                        dir_name = project_dir.name
                        if dir_name.startswith("-"):
                            file_project_path = dir_name.replace("-", "/")

                    try:
                        norm_fp = str(Path(file_project_path).resolve())
                    except (OSError, ValueError):
                        norm_fp = file_project_path

                    if norm_fp not in active_cwds:
                        continue

                    sessions.append(
                        SessionInfo(
                            session_id=session_id,
                            file_path=jsonl_file,
                        )
                    )
            except OSError as e:
                logger.debug(f"Error scanning jsonl files in {project_dir}: {e}")

        return sessions

    async def _read_new_lines(
        self, session: TrackedSession, file_path: Path
    ) -> list[dict]:
        """Read new lines from a session file using byte offset for efficiency.

        Detects file truncation (e.g. after /clear) and resets offset.
        Recovers from corrupted offsets (mid-line) by scanning to next line.
        """
        new_entries = []
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                # Get file size to detect truncation
                await f.seek(0, 2)  # Seek to end
                file_size = await f.tell()

                # Detect file truncation: if offset is beyond file size, reset
                if session.last_byte_offset > file_size:
                    logger.info(
                        "File truncated for session %s "
                        "(offset %d > size %d). Resetting.",
                        session.session_id,
                        session.last_byte_offset,
                        file_size,
                    )
                    session.last_byte_offset = 0

                # Seek to last read position for incremental reading
                await f.seek(session.last_byte_offset)

                # Detect corrupted offset: if we're mid-line (not at '{'),
                # scan forward to the next line start. This can happen if
                # the state file was manually edited or corrupted.
                if session.last_byte_offset > 0:
                    first_char = await f.read(1)
                    if first_char and first_char != "{":
                        logger.warning(
                            "Corrupted offset %d in session %s (mid-line), "
                            "scanning to next line",
                            session.last_byte_offset,
                            session.session_id,
                        )
                        await f.readline()  # Skip rest of partial line
                        session.last_byte_offset = await f.tell()
                        return []
                    await f.seek(session.last_byte_offset)  # Reset for normal read

                # Read only new lines from the offset.
                # Track safe_offset: only advance past lines that parsed
                # successfully. A non-empty line that fails JSON parsing is
                # likely a partial write; stop and retry next cycle.
                safe_offset = session.last_byte_offset
                async for line in f:
                    data = TranscriptParser.parse_line(line)
                    if data:
                        new_entries.append(data)
                        safe_offset = await f.tell()
                    elif line.strip():
                        # Non-empty line that failed to parse. Usually a
                        # partial trailing write — don't advance past it, retry
                        # next cycle. But a NEVER-parseable line (a crash-torn
                        # final line fused with the next append) would wedge
                        # the session forever: track (offset, first_seen,
                        # count) and skip the line once the file has grown
                        # PAST its end (proof it isn't a trailing partial)
                        # AND it survived >= _STALL_SKIP_MIN_CYCLES re-reads.
                        line_start = safe_offset
                        line_end = await f.tell()
                        sid = session.session_id
                        prev = self._unparseable_stalls.get(sid)
                        if prev is None or prev[0] != line_start:
                            entry = (line_start, time.time(), 1)
                        else:
                            entry = (prev[0], prev[1], prev[2] + 1)
                        self._unparseable_stalls[sid] = entry
                        # file_size > line_end ⟺ the line ended with '\n' and
                        # bytes exist beyond it — NEVER skip a line still at
                        # EOF (a genuine partial write mid-append).
                        if entry[2] >= _STALL_SKIP_MIN_CYCLES and file_size > line_end:
                            logger.warning(
                                "Skipping unparseable JSONL bytes %d-%d in "
                                "session %s after %d read cycles "
                                "(stuck %.1fs); discarding the line",
                                line_start,
                                line_end,
                                sid,
                                entry[2],
                                time.time() - entry[1],
                            )
                            safe_offset = line_end
                            self._unparseable_stalls.pop(sid, None)
                            continue
                        logger.warning(
                            "Partial JSONL line in session %s, will retry next cycle",
                            session.session_id,
                        )
                        break
                    else:
                        # Empty line — safe to skip
                        safe_offset = await f.tell()

                session.last_byte_offset = safe_offset

                # Fix 11 hygiene: a previously-stuck line that eventually
                # parsed (or any progress past the tracked offset) clears
                # its stall entry so the dict stays bounded and accurate.
                stall = self._unparseable_stalls.get(session.session_id)
                if stall is not None and stall[0] < safe_offset:
                    self._unparseable_stalls.pop(session.session_id, None)

        except OSError as e:
            logger.error("Error reading session file %s: %s", file_path, e)
        return new_entries

    async def check_for_updates(self, active_session_ids: set[str]) -> list[NewMessage]:
        """Check all sessions for new assistant messages.

        Reads from last byte offset. Emits both intermediate
        (stop_reason=null) and complete messages.

        Args:
            active_session_ids: Set of session IDs currently in session_map
        """
        new_messages = []

        # Scan projects to get available session files
        sessions = await self.scan_projects()

        # Only process sessions that are in session_map
        for session_info in sessions:
            if session_info.session_id not in active_session_ids:
                continue
            try:
                tracked = self.state.get_session(session_info.session_id)

                if tracked is None:
                    # For new sessions, initialize offset to end of file
                    # to avoid re-processing old messages
                    try:
                        file_size = session_info.file_path.stat().st_size
                        current_mtime = session_info.file_path.stat().st_mtime
                    except OSError:
                        file_size = 0
                        current_mtime = 0.0
                    tracked = TrackedSession(
                        session_id=session_info.session_id,
                        file_path=str(session_info.file_path),
                        last_byte_offset=file_size,
                    )
                    self.state.update_session(tracked)
                    self._file_mtimes[session_info.session_id] = current_mtime
                    logger.info(f"Started tracking session: {session_info.session_id}")
                    continue

                # Check mtime + file size to see if file has changed
                try:
                    st = session_info.file_path.stat()
                    current_mtime = st.st_mtime
                    current_size = st.st_size
                except OSError:
                    continue

                last_mtime = self._file_mtimes.get(session_info.session_id, 0.0)
                if (
                    current_mtime <= last_mtime
                    and current_size <= tracked.last_byte_offset
                ):
                    # File hasn't changed, skip reading
                    continue

                # File changed, read new content from last offset
                new_entries = await self._read_new_lines(
                    tracked, session_info.file_path
                )
                self._file_mtimes[session_info.session_id] = current_mtime

                if new_entries:
                    logger.debug(
                        f"Read {len(new_entries)} new entries for "
                        f"session {session_info.session_id}"
                    )

                # Parse new entries using the shared logic, carrying over pending tools
                carry = self._pending_tools.get(session_info.session_id, {})
                parsed_entries, remaining = TranscriptParser.parse_entries(
                    new_entries,
                    pending_tools=carry,
                )
                if remaining:
                    self._pending_tools[session_info.session_id] = remaining
                else:
                    self._pending_tools.pop(session_info.session_id, None)

                if new_entries:
                    logger.info(
                        "parse_diag session=%s raw_entries=%d parsed=%d types=%s",
                        session_info.session_id[:8],
                        len(new_entries),
                        len(parsed_entries),
                        [(p.role, p.content_type, p.text[:40]) for p in parsed_entries],
                    )

                for entry in parsed_entries:
                    # Dispatch the lower-level event BEFORE display
                    # filtering. Run-state transitions (BusyIndicator) are
                    # lifecycle events, not display events: a quiet
                    # tool_result, an empty-text end_turn, or a hidden user
                    # message must still reach the indicator so it can close
                    # an open tool slot or step the route to IDLE_RECENT.
                    # Coupling lifecycle dispatch to "has visible content"
                    # is exactly what leaves the typing indicator stuck on.
                    if self._event_callback is not None and entry.role in (
                        "user",
                        "assistant",
                    ):
                        if entry.content_type in (
                            "text",
                            "thinking",
                            "tool_use",
                            "tool_result",
                        ):
                            assert entry.role in ("user", "assistant")
                            event = TranscriptEvent(
                                session_id=session_info.session_id,
                                role=entry.role,
                                block_type=entry.content_type,
                                tool_use_id=entry.tool_use_id,
                                tool_name=entry.tool_name,
                                stop_reason=entry.stop_reason,
                                timestamp=entry.timestamp,
                                text=entry.text,
                                image_data=entry.image_data,
                                tool_input=entry.tool_input,
                                transcript_uuid=entry.uuid,
                                message_id=entry.message_id,
                                block_origin=entry.block_origin,
                            )
                            try:
                                await self._event_callback(event)
                            except Exception as e:
                                logger.error(f"Event callback error: {e}")

                    # GH #44: collect background-agent signals from the
                    # PARENT transcript — applied by the bot fan-out AFTER
                    # this tick's lifecycle dispatch (which already happened
                    # above), preserving the §4.2 ordering. Deferred import:
                    # the envelope/agentId extractors live in
                    # handlers.response_builder (single regex owner).
                    if entry.content_type == "tool_result" and entry.tool_name in (
                        "Agent",
                        "Task",
                    ):
                        from .handlers.response_builder import (
                            extract_async_agent_launch_id,
                        )

                        launch_id = extract_async_agent_launch_id(entry.text)
                        if launch_id:
                            self._parent_activity(session_info.session_id).launched.add(
                                normalize_background_agent_key(launch_id)
                            )
                    elif (
                        entry.content_type == "tool_result"
                        and entry.tool_name == "Workflow"
                    ):
                        # ISSUE-6 / Fix 2a: the Workflow launch tool_result has
                        # a DIFFERENT shape (Task ID mid-line + separate Run ID
                        # / Transcript dir). Record the RAW prefixed key (no
                        # normalize — the wf-task: namespace is isolated from
                        # the Agent/Task agentId space) and open a persistent
                        # bracket for the Fix 2c mtime heartbeat.
                        from .handlers.response_builder import (
                            extract_workflow_launch_info,
                        )

                        info = extract_workflow_launch_info(entry.text)
                        if info and info.task_id:
                            key = f"wf-task:{info.task_id}"
                            self._parent_activity(session_info.session_id).launched.add(
                                key
                            )
                            self._open_workflow_bracket(session_info.session_id, info)
                    elif (
                        entry.role == "user"
                        and entry.content_type == "text"
                        and entry.text
                        and entry.text.lstrip().startswith("<task-notification>")
                    ):
                        from .handlers.response_builder import (
                            extract_task_notification_task_id,
                        )

                        task_id = extract_task_notification_task_id(entry.text)
                        if task_id:
                            rec = self._parent_activity(session_info.session_id)
                            rec.completed.add(normalize_background_agent_key(task_id))
                            # ISSUE-6 / Fix 2d: for a WORKFLOW task-id, ALSO emit
                            # the wf-task: close key (== the wf-task launch key)
                            # so the bracket tombstones, and drop the open
                            # bracket. A Workflow task-id is identified ONLY by a
                            # live OPEN bracket (gate-on-bracket): an isolated
                            # close with no bracket has no route_runtime bg key
                            # to tombstone, so the bare key above suffices. This
                            # keeps an Agent close from spuriously emitting a
                            # wf-task: done key without guessing a Workflow id
                            # from its character set.
                            if self._has_open_workflow_bracket(
                                session_info.session_id, task_id
                            ):
                                rec.completed.add(f"wf-task:{task_id}")
                                # Fix 5 PR-B: do NOT pop yet — let
                                # check_sidechain_updates tail the wf_dir ONE
                                # final time (capture the last display blocks)
                                # and emit the route-FIFO collapse signal,
                                # THEN remove the bracket. Popping here would
                                # make the bracket-gated discovery skip the
                                # final tail (Hermes-delta P1-2). The run-state
                                # done already fired via rec.completed above.
                                self._open_workflow_brackets[session_info.session_id][
                                    task_id
                                ].closing = True

                    # Lifecycle-only entries exist purely to drive the
                    # busy indicator; they have no visible content and must
                    # not fan out to Telegram.
                    if entry.lifecycle_only:
                        continue

                    if not entry.text and not entry.image_data:
                        continue
                    # User entries are always emitted; the 👤 echo gate is
                    # PER-RECIPIENT in bot.handle_new_message (plan v4 §4 —
                    # output_prefs.user_echo, env CC_TELEGRAM_SHOW_USER_MESSAGES
                    # is its default layer). A monitor-level drop here would
                    # make a stored user override unable to re-enable echoes.
                    # Suppress user-message echoes for text we just typed
                    # into the pane via send_to_window — the user already
                    # saw their own bubble in Telegram. Direct typing into
                    # tmux (which never goes through send_to_window) still
                    # falls through and gets surfaced.
                    if entry.role == "user" and entry.text:
                        from .session import consume_bot_sent_text

                        if consume_bot_sent_text(session_info.session_id, entry.text):
                            continue

                    new_messages.append(
                        NewMessage(
                            session_id=session_info.session_id,
                            text=entry.text,
                            content_type=entry.content_type,
                            tool_use_id=entry.tool_use_id,
                            role=entry.role,
                            tool_name=entry.tool_name,
                            image_data=entry.image_data,
                            tool_input=entry.tool_input,
                            transcript_uuid=entry.uuid,
                            stop_reason=entry.stop_reason,
                            message_id=entry.message_id,
                            block_origin=entry.block_origin,
                        )
                    )
                    logger.info(
                        "emit_diag session=%s role=%s type=%s text=%r",
                        session_info.session_id[:8],
                        entry.role,
                        entry.content_type,
                        entry.text[:60],
                    )

                self.state.update_session(tracked)

            except OSError as e:
                logger.debug(f"Error processing session {session_info.session_id}: {e}")

        self.state.save_if_dirty()
        return new_messages

    async def check_sidechain_updates(
        self, active_session_ids: set[str]
    ) -> list[NewMessage]:
        """Tail sub-agent JSONL files for active parent sessions.

        Sub-agent (sidechain) transcripts live at
        ``<project_dir>/<parent_session_id>/subagents/agent-*.jsonl``.
        For each new file we track byte offsets the same way we do for
        regular sessions and emit assistant ``tool_use``, ``text``, and
        ``thinking`` blocks routed back to the parent's session_id so
        they land in the parent's topic. Tool calls render as ``↳ ``
        headers; prose and thinking render as ``↳ `` followed by an
        expandable blockquote so the sub-agent's plan / narrative is
        available to peek at without dominating the topic. Tool results
        and the prompts the parent sends to the sub-agent are dropped.

        Files first seen on bot startup begin at EOF (skip historical
        runs). Mid-session discovery is best-effort — a sub-agent whose
        first lines land between two poll ticks will lose those lines.

        Tracking, parsing, and activity reporting run UNCONDITIONALLY —
        a parent whose sidechains produced new parsed entries this tick is
        recorded per agent key for ``pop_sidechain_activity`` (the GH #44
        run-state keep-alive + projection input) regardless of display
        settings. Sidechain ``NewMessage`` emission is likewise UNCONDITIONAL
        at the monitor; per-recipient display suppression is entirely
        downstream via ``output_prefs.subagent_cards`` in the message_queue
        digest path (a monitor-level drop would make a stored user override
        unable to re-enable the cards).

        The shared per-file body lives in ``_track_and_emit_sidechain_file``;
        the top-level Agent/Task loop drives it with ``feed_run_state=True``
        (Fix 5 PR-A extraction).

        Fix 5 PR-B adds a SECOND, nested enumeration (DISPLAY ONLY): a
        Workflow's sub-agents live one level deeper at
        ``subagents/workflows/wf_<runid>/agent-*.jsonl``. They are discovered via
        THIS parent's OPEN brackets' ``wf_dir`` — the SAME dir
        ``_emit_workflow_bracket_heartbeats`` stats — with an anchored
        ``glob("agent-*.jsonl")`` and a run-id-qualified
        ``sub:<parent>:<runid>:<stem>`` key, driven through the shared helper
        with ``feed_run_state=False`` so Workflow sidechain ENTRIES NEVER feed
        run-state (the ``wf-task:`` bracket + its mtime heartbeat stay the SOLE
        Workflow run-state input). A bracket marked ``closing`` (its
        ``<task-notification>`` landed this tick) is tailed ONE final time here,
        then a ``NewMessage(subagent_collapse_prefix=...)`` is appended to the
        display lane AFTER its cards and the bracket is popped — the
        deterministic route-FIFO close collapse.
        """
        new_messages: list[NewMessage] = []

        # Build parent_session_id -> parent_jsonl_path lookup from currently
        # tracked parent sessions. Skip any tracking_key that's itself a
        # sidechain (parent_session_id is set).
        parent_files: dict[str, Path] = {}
        for sid in active_session_ids:
            tracked = self.state.get_session(sid)
            if tracked is None or tracked.parent_session_id is not None:
                continue
            if not tracked.file_path:
                continue
            parent_files[sid] = Path(tracked.file_path)

        for parent_session_id, parent_jsonl in parent_files.items():
            # ISSUE-6 / Fix 2c: emit the per-bracket mtime-advance heartbeat
            # FIRST, before the agent-*.jsonl glob — a Workflow's sidechains
            # live one level deeper (subagents/workflows/wf_*/), so the
            # bracket stat must not depend on the top-level subagents glob (or
            # the run-state lift would die whenever a parent has no direct
            # Agent/Task sidechain).
            self._emit_workflow_bracket_heartbeats(parent_session_id)

            # hermes P2: a missing/unreadable top-level ``subagents`` dir means
            # NO top-level Agent/Task sidechains THIS tick — default to ``[]``
            # WITHOUT ``continue``. A bare ``continue`` here would also skip the
            # Workflow ``wf_dir`` enumeration AND the ``closing``-bracket
            # collapse/pop below (same per-parent loop body), stranding a
            # ``wf_dir=None`` closing bracket in ``_open_workflow_brackets``.
            sub_dir = parent_jsonl.parent / parent_session_id / "subagents"
            sidechain_files: list[Path] = []
            try:
                if sub_dir.is_dir():
                    sidechain_files = list(sub_dir.glob("agent-*.jsonl"))
            except OSError:
                sidechain_files = []

            for sc_file in sidechain_files:
                # sc_file.stem looks like "agent-a05666f9d196136af"
                tracking_key = f"sub:{parent_session_id}:{sc_file.stem}"
                await self._track_and_emit_sidechain_file(
                    parent_session_id=parent_session_id,
                    sc_file=sc_file,
                    tracking_key=tracking_key,
                    new_messages=new_messages,
                    feed_run_state=True,
                )

            # ISSUE-6 / Fix 5 PR-B (DISPLAY ONLY): a Workflow's sub-agents live
            # one level deeper at subagents/workflows/wf_<runid>/agent-*.jsonl.
            # Enumerate them via THIS parent's OPEN brackets' wf_dir — the SAME
            # dir _emit_workflow_bracket_heartbeats stats above — so display +
            # run-state share one discovery. Emit the cards WITHOUT feeding
            # run-state (feed_run_state=False): the wf-task: bracket + its mtime
            # heartbeat stay the SOLE run-state input for Workflow. INSIDE the
            # per-parent loop, so parent_session_id is unambiguous and an empty
            # parent_files never references it (v2 P1 placement).
            for task_id, bracket in self._open_workflow_brackets.get(
                parent_session_id, {}
            ).items():
                if bracket.wf_dir is None:
                    continue
                try:
                    # ANCHORED to the validated wf_<runid> dir — never rglob.
                    wf_files = list(bracket.wf_dir.glob("agent-*.jsonl"))
                except OSError:
                    continue
                # Run-id-qualified key (v2 §3.5): wf_dir.name == "wf_<runid>", so
                # two concurrent runs under one parent never collide on a
                # same-stem agent file. Keeps the sub:<parent>: teardown prefix.
                run_id = bracket.wf_dir.name
                for sc_file in wf_files:
                    tracking_key = f"sub:{parent_session_id}:{run_id}:{sc_file.stem}"
                    await self._track_and_emit_sidechain_file(
                        parent_session_id=parent_session_id,
                        sc_file=sc_file,
                        tracking_key=tracking_key,
                        new_messages=new_messages,
                        feed_run_state=False,
                    )

            # Fix 5 PR-B: a bracket marked ``closing`` (its <task-notification>
            # landed this tick) was just tailed ONE final time above. Now append
            # the route-FIFO collapse signal AFTER its display cards and pop the
            # bracket — the deterministic close collapse. ``list(...)`` so the
            # pop inside the loop doesn't mutate the dict under iteration.
            for task_id, bracket in list(
                self._open_workflow_brackets.get(parent_session_id, {}).items()
            ):
                if not bracket.closing:
                    continue
                if bracket.wf_dir is not None:
                    new_messages.append(
                        NewMessage(
                            session_id=parent_session_id,
                            text="",
                            subagent_collapse_prefix=(
                                f"sub:{parent_session_id}:{bracket.wf_dir.name}:"
                            ),
                        )
                    )
                self._close_workflow_bracket(parent_session_id, task_id)

        self.state.save_if_dirty()
        return new_messages

    async def _track_and_emit_sidechain_file(
        self,
        *,
        parent_session_id: str,
        sc_file: Path,
        tracking_key: str,
        new_messages: list[NewMessage],
        feed_run_state: bool,
    ) -> None:
        """Track byte offsets for ONE sidechain JSONL and emit its blocks.

        The shared per-file body lifted out of ``check_sidechain_updates``'s
        top-level ``for sc_file in sidechain_files`` loop (Fix 5 PR-A). It
        owns the first-seen-at-EOF registration, the mtime/size short-circuit,
        the ``_read_new_lines`` + ``TranscriptParser.parse_entries`` +
        ``_pending_tools`` carry, the per-block subagent-tagged ``NewMessage``
        emission, and the trailing ``update_session`` — byte-identical to the
        pre-extraction top-level path.

        ``feed_run_state`` gates ONLY the GH #44 run-state tick block: when
        True (the top-level Agent/Task caller) new parsed entries populate the
        parent's ``ParentSidechainActivity.ticks`` for ``pop_sidechain_activity``
        (the run-state keep-alive / projection input). When False (the Fix 5
        Workflow ``wf_dir`` caller in PR-B) the tick block is SKIPPED — the
        ``wf-task:`` bracket + its mtime heartbeat stay the SOLE run-state input
        for Workflow sidechains; their entries feed display ONLY. Everything
        else — tracking, parsing, ``_pending_tools`` carry, and the
        ``subagent_key``-tagged emission — runs unconditionally for both callers.
        """
        tracked = self.state.get_session(tracking_key)
        if tracked is None:
            # New sidechain file — start at EOF to skip history.
            # On startup this avoids replaying long-finished
            # sub-agent runs; mid-session it means we miss a few
            # lines that landed before discovery, which is fine.
            try:
                st = sc_file.stat()
            except OSError:
                return
            tracked = TrackedSession(
                session_id=tracking_key,
                file_path=str(sc_file),
                last_byte_offset=st.st_size,
                parent_session_id=parent_session_id,
            )
            self.state.update_session(tracked)
            self._file_mtimes[tracking_key] = st.st_mtime
            logger.info(
                "Started tracking sidechain %s (parent=%s, size=%d)",
                sc_file.name,
                parent_session_id[:8],
                st.st_size,
            )
            return

        try:
            st = sc_file.stat()
            current_mtime = st.st_mtime
            current_size = st.st_size
        except OSError:
            return

        last_mtime = self._file_mtimes.get(tracking_key, 0.0)
        if current_mtime <= last_mtime and current_size <= tracked.last_byte_offset:
            return

        new_entries = await self._read_new_lines(tracked, sc_file)
        self._file_mtimes[tracking_key] = current_mtime

        if not new_entries:
            self.state.update_session(tracked)
            return

        carry = self._pending_tools.get(tracking_key, {})
        parsed_entries, remaining = TranscriptParser.parse_entries(
            new_entries,
            pending_tools=carry,
        )
        if remaining:
            self._pending_tools[tracking_key] = remaining
        else:
            self._pending_tools.pop(tracking_key, None)

        # GH #44 (ex-Wave A): new parsed entries = sidechain activity
        # for the parent's route, regardless of whether anything is
        # displayed. Aggregate per normalized agent key: max parsed
        # JSONL timestamp + end-of-turn detection — INCLUDING
        # lifecycle-only entries with no visible text (a quiet final
        # turn must still clear the projection; codex r2 P2-2).
        #
        # Fix 5: gated on ``feed_run_state`` — the Workflow ``wf_dir`` caller
        # (PR-B) passes False so Workflow sidechain ENTRIES never feed
        # run-state (the ``wf-task:`` bracket + mtime heartbeat are the SOLE
        # Workflow run-state input). The top-level Agent/Task caller passes
        # True, preserving today's behavior.
        if feed_run_state and parsed_entries:
            agent_key = normalize_background_agent_key(sc_file.stem)
            tick = self._parent_activity(parent_session_id).ticks.setdefault(
                agent_key, SidechainTick()
            )
            for entry in parsed_entries:
                ts = parse_iso_timestamp(entry.timestamp)
                if ts is not None and (
                    tick.max_event_ts is None or ts > tick.max_event_ts
                ):
                    tick.max_event_ts = ts
                if entry.stop_reason in _TURN_END_REASONS:
                    tick.saw_end_of_turn = True

        # Sidechain blocks are always emitted (keep-alive above is
        # already unconditional); display gating is PER-RECIPIENT via
        # output_prefs (subagent_cards / tool_activity) in the
        # message_queue digest path — a monitor-level drop here would
        # make a stored user override unable to re-enable the cards
        # (plan v4 §4).

        for entry in parsed_entries:
            # Each block (text / thinking / tool_use / tool_result)
            # becomes one event for the per-sub-agent digest. The
            # message_queue collapses these into a single editable
            # message keyed by ``subagent_key=tracking_key`` so a
            # multi-step run renders as one bubble in the parent
            # topic, not N. Routing through ``subagent_key`` also
            # bypasses Agent prominence / parent activity digest /
            # interactive UI dispatch — those apply only to the
            # parent's own blocks.
            if entry.role != "assistant":
                continue
            if entry.content_type not in (
                "text",
                "thinking",
                "tool_use",
                "tool_result",
            ):
                continue
            if not entry.text and not entry.tool_use_id:
                continue
            new_messages.append(
                NewMessage(
                    session_id=parent_session_id,
                    text=entry.text,
                    content_type=entry.content_type,
                    tool_use_id=entry.tool_use_id,
                    role="assistant",
                    tool_name=entry.tool_name,
                    image_data=None,
                    tool_input=entry.tool_input,
                    transcript_uuid=entry.uuid,
                    subagent_key=tracking_key,
                    stop_reason=entry.stop_reason,
                    message_id=entry.message_id,
                    block_origin=entry.block_origin,
                )
            )

        self.state.update_session(tracked)

    def _remove_sidechains_for_parent(self, parent_session_id: str) -> None:
        """Drop sidechain trackers belonging to a parent that's been cleaned up."""
        # ISSUE-6 / Fix 2c: a parent's open Workflow brackets die with it (the
        # central per-parent sidechain-teardown seam — reached by the runtime
        # session-change cleanup, the deleted-window cleanup, and both startup
        # sweeps).
        self._open_workflow_brackets.pop(parent_session_id, None)
        prefix = f"sub:{parent_session_id}:"
        stale = [k for k in self.state.tracked_sessions if k.startswith(prefix)]
        for k in stale:
            self.state.remove_session(k)
            self._file_mtimes.pop(k, None)
            self._pending_tools.pop(k, None)
            self._unparseable_stalls.pop(k, None)
        if stale:
            logger.info(
                "Removed %d sidechain tracker(s) for parent %s",
                len(stale),
                parent_session_id[:8],
            )

    async def _load_current_session_map(self) -> dict[str, str]:
        """Load current session_map and return window_id -> session_id mapping.

        Keys in session_map are formatted as "tmux_session:window_id"
        (e.g. "cc-telegram:@12"). Only entries matching our tmux_session_name
        AND carrying an ``@N`` window_id suffix are processed. Pre-2026-02-11
        window_name-keyed entries (e.g. "cc-telegram:myproject") are dropped:
        the live SessionStart hook only ever emits ``@N`` keys, so a non-``@``
        suffix can only come from a stale on-disk file. A one-shot warning
        naming the dropped keys fires before they are filtered out, so they do
        not silently stop being monitored.
        """
        window_to_session: dict[str, str] = {}
        if config.session_map_file.exists():
            try:
                async with aiofiles.open(config.session_map_file, "r") as f:
                    content = await f.read()
                session_map = json.loads(content)
                prefix = f"{config.tmux_session_name}:"
                legacy_keys: list[str] = []
                for key, info in session_map.items():
                    # Only process entries for our tmux session
                    if not key.startswith(prefix):
                        continue
                    window_key = key[len(prefix) :]
                    if not _is_window_id(window_key):
                        # Pre-2026-02-11 window_name-keyed legacy entry — dropped.
                        legacy_keys.append(key)
                        continue
                    session_id = info.get("session_id", "")
                    if session_id:
                        window_to_session[window_key] = session_id
                if legacy_keys and not self._warned_legacy_session_map_keys:
                    logger.warning(
                        "dropping legacy window_name-keyed session_map entries "
                        "(no longer monitored): %s",
                        sorted(legacy_keys),
                    )
                    self._warned_legacy_session_map_keys = True
            except (json.JSONDecodeError, OSError):
                pass
        return window_to_session

    async def _cleanup_all_stale_sessions(self) -> None:
        """Clean up all tracked sessions not in current session_map (used on startup)."""
        current_map = await self._load_current_session_map()
        active_session_ids = set(current_map.values())

        stale_sessions = []
        for session_id, tracked in self.state.tracked_sessions.items():
            # Skip sidechain trackers — they're cleaned up via
            # _remove_sidechains_for_parent when their parent goes stale.
            if tracked.parent_session_id is not None:
                continue
            if session_id not in active_session_ids:
                stale_sessions.append(session_id)

        if stale_sessions:
            logger.info(
                f"[Startup cleanup] Removing {len(stale_sessions)} stale sessions"
            )
            for session_id in stale_sessions:
                self.state.remove_session(session_id)
                self._file_mtimes.pop(session_id, None)
                # Fix 18: mirror the sidechain paths — the parent's pending
                # tool_use carry + stall tracking must not outlive it.
                self._pending_tools.pop(session_id, None)
                self._unparseable_stalls.pop(session_id, None)
                self._remove_sidechains_for_parent(session_id)

        # Defensive sweep: drop any sidechain trackers whose parent isn't
        # currently tracked. Reaches orphans left behind if a previous run
        # crashed between removing a parent and removing its sidechains,
        # or if a parent was removed by a code path that didn't call
        # ``_remove_sidechains_for_parent``.
        live_parents = {
            sid
            for sid, t in self.state.tracked_sessions.items()
            if t.parent_session_id is None
        }
        orphan_sidechains = [
            sid
            for sid, t in self.state.tracked_sessions.items()
            if t.parent_session_id is not None
            and t.parent_session_id not in live_parents
        ]
        if orphan_sidechains:
            logger.info(
                "[Startup cleanup] Removing %d orphan sidechain tracker(s)",
                len(orphan_sidechains),
            )
            for sid in orphan_sidechains:
                self.state.remove_session(sid)
                self._file_mtimes.pop(sid, None)
                self._pending_tools.pop(sid, None)
                self._unparseable_stalls.pop(sid, None)

        self.state.save_if_dirty()

    async def _detect_and_cleanup_changes(self) -> dict[str, str]:
        """Detect session_map changes and cleanup replaced/removed sessions.

        Returns current session_map for further processing.
        """
        current_map = await self._load_current_session_map()

        sessions_to_remove: set[str] = set()
        # Windows whose session_id flipped (e.g. /clear): the route's
        # route_runtime state carries pre-/clear ``open_tools`` IDs that the
        # new session will never close, pinning the route to RUNNING forever.
        # Tracked here so we can hand them to ``route_runtime.mark_session_reset``
        # once routes are resolved below.
        changed_window_ids: set[str] = set()
        # window_id → OLD session_id for windows whose session_id flipped
        # since the last poll. Used by the AUQ PreToolUse cleanup path
        # below — ``forget_ask_tool_input`` resolves the side file via
        # the CURRENT session (now the new one), so we must capture the
        # old session_id here while it's still reachable.
        changed_old_sessions: dict[str, str] = {}

        # Check for window session changes (window exists in both, but session_id changed)
        for window_id, old_session_id in self._last_session_map.items():
            new_session_id = current_map.get(window_id)
            if new_session_id and new_session_id != old_session_id:
                logger.info(
                    "Window '%s' session changed: %s -> %s",
                    window_id,
                    old_session_id,
                    new_session_id,
                )
                sessions_to_remove.add(old_session_id)
                changed_window_ids.add(window_id)
                changed_old_sessions[window_id] = old_session_id

        # Check for deleted windows (window in old map but not in current)
        old_windows = set(self._last_session_map.keys())
        current_windows = set(current_map.keys())
        deleted_windows = old_windows - current_windows

        # Deleted windows: track their session_ids for AUQ side-file
        # cleanup alongside sessions_to_remove. Codex P2 (chunk 5): even
        # though the reader can't serve these (no window binding), the
        # side files carry AUQ tool_input text and shouldn't linger
        # until the next bot-startup GC.
        deleted_session_ids: list[str] = []
        for window_id in deleted_windows:
            old_session_id = self._last_session_map[window_id]
            logger.info(
                "Window '%s' deleted, removing session %s",
                window_id,
                old_session_id,
            )
            sessions_to_remove.add(old_session_id)
            deleted_session_ids.append(old_session_id)

        # Perform cleanup
        if sessions_to_remove:
            for session_id in sessions_to_remove:
                self.state.remove_session(session_id)
                self._file_mtimes.pop(session_id, None)
                # Fix 18: mirror the sidechain paths — the parent's pending
                # tool_use carry + stall tracking must not outlive it.
                self._pending_tools.pop(session_id, None)
                self._unparseable_stalls.pop(session_id, None)
                self._remove_sidechains_for_parent(session_id)
            self.state.save_if_dirty()

        # Reset route_runtime state for routes bound to windows whose session
        # changed. Without this, ``open_tools`` keeps the tool_use_ids from the
        # pre-/clear session and the run-state never returns to IDLE, so the
        # typing indicator and "🟡 Busy" card stay stuck forever even though
        # the new session is genuinely idle. ``mark_session_reset`` transitions
        # the snapshot visibly (IDLE_CLEARED with ``status_card_msg_id``
        # preserved) so the next ``snapshot(route)`` read reflects the reset
        # instead of silently dropping route state; it also drops the
        # context_usage cache, so the 1M latch can't survive into the new
        # session's footer.
        # Deferred imports for the same reason as ``_monitor_loop`` — these
        # modules transitively pull in this one.
        if changed_window_ids:
            from .session import session_manager
            from .handlers.interactive_ui import forget_ask_tool_input
            from . import route_runtime
            from .handlers import pane_signals

            for user_id, thread_id, wid in session_manager.iter_thread_bindings():
                if wid in changed_window_ids:
                    await route_runtime.mark_session_reset(
                        (user_id, thread_id or 0, wid)
                    )
                    pane_signals.clear_route((user_id, thread_id or 0, wid))
                    logger.info(
                        "Reset route_runtime route after session change: "
                        "user=%d thread=%s window=%s",
                        user_id,
                        thread_id,
                        wid,
                    )
            # The AUQ tool_input cache is keyed only by window_id (not by
            # session_id), so a /clear that rebinds the same window to a new
            # session would leave the old session's cached tool_input in
            # place. Render path would then overlay options from the dead
            # AUQ onto the new session's pane — a wrong-action class shape.
            # Drop the cache for any window whose session_id changed.
            #
            # Codex R2 fix: also unlink the OLD session's PreToolUse side
            # file BEFORE forget_ask_tool_input runs. By the time
            # forget_ask_tool_input executes, ``peek_session_id_for_window``
            # returns the NEW session_id, so a delete keyed by the current
            # session would miss the old session's file entirely. We
            # captured the old session_id in ``changed_old_sessions`` above
            # for exactly this purpose.
            from .handlers.auq_source import unlink_for_session
            from .handlers.notify_source import (
                unlink_for_session as notify_unlink_for_session,
            )

            for wid in changed_window_ids:
                old_sid = changed_old_sessions.get(wid, "")
                if old_sid:
                    unlink_for_session(old_sid)
                    # Bug 2: tear down the OLD session's live-prose capture on
                    # /clear (the session_id was swapped, so forget_ask_tool_input
                    # below would only see the NEW session — same parity reason
                    # as unlink_for_session above).
                    md_capture.teardown_session(old_sid)
                    # Wave B: the OLD session's notification marker dies with
                    # it (same old-session-id parity as above).
                    notify_unlink_for_session(old_sid)
                forget_ask_tool_input(wid)

        # Codex P2 (chunk 5): unlink side files for deleted windows
        # too. Outside the ``if changed_window_ids`` block because a
        # cycle with only deletes (no changes) still needs cleanup.
        if deleted_session_ids:
            from .handlers.auq_source import unlink_for_session
            from .handlers.notify_source import (
                unlink_for_session as notify_unlink_for_session,
            )

            for sid in deleted_session_ids:
                unlink_for_session(sid)
                md_capture.teardown_session(sid)
                # Wave B: deleted window → its notification marker is dead.
                notify_unlink_for_session(sid)

        # Update last known map
        self._last_session_map = current_map

        return current_map

    # Bounds for the startup AUQ-cache hydration JSONL tail read. Most live
    # sessions are well under 1MB at the relevant tail; we read 1MB first and
    # fall back to the whole file when smaller. The hard cap stops a
    # pathologically long single-session JSONL from blocking startup. Class
    # attributes so tests can monkey-patch them without reaching into method
    # locals.
    _AUQ_HYDRATE_TAIL_BYTES = 1 * 1024 * 1024
    _AUQ_HYDRATE_HARD_CAP = 16 * 1024 * 1024

    async def _hydrate_ask_tool_input_cache(self, current_map: dict[str, str]) -> None:
        """Pre-populate the AskUserQuestion ``tool_input`` cache for windows
        whose Claude session still has a pending AUQ on startup.

        Bot restart wipes ``handlers.interactive_ui._last_completed_ask_tool_input``
        and ``check_for_updates`` resumes from the persisted byte offset, so
        a tool_use line consumed pre-restart never re-emits. Without this
        hydration the poller's render path falls through to JSONL-missing →
        pane-only, which renders a partial card when long option
        descriptions push options 1-N off the visible pane region.

        For each (window_id, session_id) in ``current_map``, locate the
        session's JSONL via ``scan_projects`` and walk the tail looking for
        the most recent AskUserQuestion tool_use whose tool_use_id has no
        matching tool_result. Hydrate the cache for that window if found.
        """
        # Deferred import to avoid the circular: interactive_ui imports
        # terminal_parser which imports session_monitor indirectly.
        from .handlers.interactive_ui import (
            maybe_upgrade_auq_context_message,
            remember_ask_tool_input,
        )

        if not current_map:
            return

        # Build session_id → file_path from the active project scan rather
        # than monitor_state alone — a corrupt or missing monitor_state would
        # otherwise silently skip hydration even though the JSONL is
        # discoverable on disk.
        try:
            sessions = await self.scan_projects()
        except Exception as e:
            logger.warning("AUQ hydrate: scan_projects failed: %s", e)
            return
        paths = {s.session_id: s.file_path for s in sessions}

        for window_id, session_id in current_map.items():
            # ``session_map.json`` is hook-written and only contains parent
            # sessions, but defend explicitly against ``sub:<parent>:agent-*``
            # keys: parent panes can't render subagent AUQs, and a wrong
            # tool_input hydrated under a parent window would mis-label the
            # pick buttons.
            if session_id.startswith("sub:"):
                continue

            jsonl_path = paths.get(session_id)
            if jsonl_path is None or not jsonl_path.exists():
                continue

            try:
                candidate = await self._find_latest_pending_auq(jsonl_path)
            except Exception as e:
                logger.warning(
                    "AUQ hydrate: scan failed for window %s session %s: %s",
                    window_id,
                    session_id[:8],
                    e,
                )
                continue

            if candidate is not None:
                tool_input = candidate.get("input")
                tool_use_id = candidate.get("id")
                if isinstance(tool_input, dict):
                    remember_ask_tool_input(
                        window_id,
                        tool_input,
                        tool_use_id if isinstance(tool_use_id, str) else None,
                    )
                    logger.info(
                        "AUQ cache hydrated for window %s session %s — "
                        "%d question(s) from %s",
                        window_id,
                        session_id[:8],
                        len(tool_input.get("questions", []))
                        if isinstance(tool_input.get("questions"), list)
                        else 0,
                        jsonl_path.name,
                    )
                    # Codex P2 round 3 #3 (2026-05-25): a form-source
                    # context message persisted pre-restart only gets
                    # its descriptions edited in if maybe_upgrade
                    # fires for this window. The normal bot.handle_new_message
                    # hook (bot.py:861) won't fire — that path runs
                    # on NEW JSONL lines emitted by the polling loop;
                    # the buffered AUQ is already past the offset.
                    # Trigger the upgrade explicitly from the hydrate
                    # path. ``_bot`` is set by bot.post_init via
                    # ``set_bot``; skip silently if absent (e.g. tests
                    # that exercise the hydrate path without a bot).
                    if self._bot is not None:
                        try:
                            await maybe_upgrade_auq_context_message(
                                self._bot, window_id
                            )
                        except Exception as upgrade_exc:  # pragma: no cover
                            logger.warning(
                                "AUQ hydrate-time upgrade raised (window=%s): %s",
                                window_id,
                                upgrade_exc,
                            )
            else:
                # No pending AUQ in this session's JSONL. That alone is NOT
                # proof the side file's AUQ resolved: Claude BUFFERS the
                # AskUserQuestion tool_use in JSONL until the prompt resolves,
                # so a genuinely-LIVE AUQ also shows no pending tool_use (and no
                # tool_result). Unlinking on "no pending AUQ" alone would delete
                # a LIVE card's liveness authority on startup.
                #
                # POSITIVE-proof reconcile: unlink the side file ONLY when its
                # captured ``tool_use_id`` has a matching AUQ ``tool_result`` in
                # the JSONL — the unambiguous "this AUQ was answered" signal. The
                # remaining ORPHAN class this still covers: the bot was down (or
                # the message callback errored) while ``check_for_updates`` had
                # already advanced the byte offset past the tool_result, so its
                # forget_ask_tool_input unlink never ran; the tool_result is in
                # the JSONL but the side file lingers. Without this the orphan
                # would make ``side_file_live_for_window`` return True forever and
                # strand a DEAD card at status_polling's clear gate. A
                # still-BUFFERED tool_use (no tool_result) or an empty captured
                # tool_use_id (can't be matched) → PRESERVE.
                #
                # SESSION-KEYED on purpose: peek the tool_use_id and unlink the
                # SAME ``session_id`` (from ``current_map``). The window-keyed
                # ``side_file_live_for_window`` would re-resolve the session via
                # ``peek_session_id_for_window`` against ``window_states``, which
                # at this point (hydration runs before the loop's first
                # ``load_session_map``) can disagree with ``current_map`` — and
                # checking one source while unlinking another is exactly the
                # mint/validate parity trap (Hermes round-2 P2).
                from .handlers.auq_source import (
                    peek_side_file_tool_use_id,
                    unlink_for_session,
                )

                side_tuid = peek_side_file_tool_use_id(session_id)
                if side_tuid is None:
                    # No valid/live side file → nothing to reconcile.
                    continue
                if not side_tuid:
                    # P3 (Codex R3): an empty captured tool_use_id cannot be
                    # matched to a tool_result, so "no pending AUQ" is NOT proof
                    # THIS AUQ resolved. Leave it for session-replacement /
                    # /clear / topic-close. NOTE (review finding 26): the 1h
                    # startup GC is NOT a backstop for a TRACKED session —
                    # gc_stale's injected liveness predicate skips any tracked
                    # session — so an empty-id orphan on a long-lived tracked
                    # session is UNBOUNDED. Documented residual, bundled with
                    # finding 25 for the next architecture wave.
                    continue
                if await self._auq_tool_result_present(jsonl_path, side_tuid):
                    # TOCTOU re-peek (review finding 12): the proof scan above
                    # is a whole-file JSONL read that yields the loop,
                    # potentially for seconds. A fresh PreToolUse(AUQ) firing
                    # during that await atomically REPLACES the side file; a
                    # blind unlink here would delete the NEW live AUQ's record
                    # (the card-liveness authority). Re-peek the SAME
                    # current_map session (session-keyed discipline, as above)
                    # and unlink ONLY if the id is unchanged — mirrors the
                    # re-stat-before-unlink guard gc_stale got in PR-B.
                    recheck_tuid = peek_side_file_tool_use_id(session_id)
                    if recheck_tuid != side_tuid:
                        logger.info(
                            "AUQ reconcile: side file for window %s session %s "
                            "changed during the tool_result scan (peeked %r, "
                            "now %r) — a fresh PreToolUse replaced it; "
                            "skipping unlink",
                            window_id,
                            session_id[:8],
                            side_tuid,
                            recheck_tuid,
                        )
                        continue
                    unlink_for_session(session_id)
                    logger.info(
                        "AUQ reconcile: unlinked RESOLVED side file for "
                        "window %s session %s (tool_result present)",
                        window_id,
                        session_id[:8],
                    )
                    # Wave 2 (Hermes R1 P2-1): the SAME positive proof that
                    # gates the unlink also releases the resolved window's
                    # action-ledger rows — the crash window where the bot was
                    # down between the tool_result and the live release seam
                    # (bot.handle_new_message's explicit AUQ tool_result
                    # branch, which never ran; per Hermes R2 P1-1 the generic
                    # forget_ask_tool_input teardown deliberately does NOT
                    # release). Without
                    # this a stale `dispatched` row blocks a same-day
                    # identical AUQ ("Action already received") until the 24h
                    # retention. Fires ONLY inside this re-peek-guarded
                    # proven-resolved block — no new reap authority (the
                    # finding-25/26 constraint); the TOCTOU-swap path above
                    # `continue`s before reaching here. Deferred import: the
                    # ledger is a handlers leaf (utils-only imports).
                    from .handlers.auq_ledger import release_window

                    release_window(window_id)
                # else: live BUFFERED tool_use (no tool_result) → PRESERVE.

    async def _find_latest_pending_auq(self, jsonl_path: Path) -> dict | None:
        """Return ``{"id": tool_use_id, "input": tool_input}`` for the most
        recent AskUserQuestion in ``jsonl_path`` that has no matching
        ``tool_result``, or ``None``.

        Reads the JSONL tail (up to ``_AUQ_HYDRATE_TAIL_BYTES``; capped at
        ``_AUQ_HYDRATE_HARD_CAP`` from the end). When mid-line at the read
        start, the first partial line is dropped. Invalid JSON lines are
        skipped silently — a partial trailing write isn't worth aborting the
        whole scan.

        Pairing semantics match ``transcript_parser``'s tool_use_id model:
        an AUQ is "pending" iff its tool_use.id never appears as a sibling
        ``tool_result.tool_use_id`` anywhere in the scanned region.
        """
        buf = await self._read_jsonl_tail(jsonl_path)
        if buf is None:
            return None

        answered_ids: set[str] = set()
        candidates: list[dict] = []

        for raw_line in buf.split(b"\n"):
            if not raw_line.strip():
                continue
            try:
                entry = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(entry, dict):
                continue
            msg = entry.get("message")
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for c in content:
                if not isinstance(c, dict):
                    continue
                ctype = c.get("type")
                if ctype == "tool_use" and c.get("name") == "AskUserQuestion":
                    tid = c.get("id")
                    inp = c.get("input")
                    if isinstance(tid, str) and isinstance(inp, dict):
                        candidates.append({"id": tid, "input": inp})
                elif ctype == "tool_result":
                    rid = c.get("tool_use_id")
                    if isinstance(rid, str):
                        answered_ids.add(rid)

        # Most recent unanswered AUQ wins. Returns the full candidate
        # dict (``id`` + ``input``) so the AUQ context-message gate in
        # ``handlers.interactive_ui`` can dedup per ``tool_use.id``.
        for cand in reversed(candidates):
            if cand["id"] not in answered_ids:
                return cand
        return None

    async def _read_jsonl_tail(self, jsonl_path: Path) -> bytes | None:
        """Read the JSONL tail bytes for ``jsonl_path``, resuming at a full line.

        Reads up to ``_AUQ_HYDRATE_TAIL_BYTES`` (hard-capped at
        ``_AUQ_HYDRATE_HARD_CAP``) from the end of the file. When the read
        window lands mid-line, the leading partial line is dropped so the caller
        always sees whole lines. Returns ``None`` on missing/empty file or a read
        error (the partial-trailing-write case is the caller's split-and-skip).
        Used by :func:`_find_latest_pending_auq` (the tail is correct there — a
        recent pending AUQ or a buffered tool_use is near the end of the file).
        """
        try:
            stat = jsonl_path.stat()
        except OSError:
            return None
        size = stat.st_size
        if size == 0:
            return None

        prefer_start = max(0, size - min(size, self._AUQ_HYDRATE_TAIL_BYTES))
        # Defensive hard cap: don't read more than HARD_CAP bytes even if the
        # file is huge and the tail bytes constant is misconfigured.
        prefer_start = max(prefer_start, size - self._AUQ_HYDRATE_HARD_CAP)

        # Read one byte earlier so we can disambiguate "started mid-line" from
        # "started exactly on a line boundary." Without this peek a tail window
        # that lands on a newline would discard the first full line — silently
        # missing the only pending AUQ if it happened to align with the cap.
        read_start = max(0, prefer_start - 1) if prefer_start > 0 else 0

        try:
            async with aiofiles.open(jsonl_path, "rb") as f:
                if read_start > 0:
                    await f.seek(read_start)
                buf = await f.read()
        except OSError as e:
            logger.debug("AUQ hydrate: read %s failed: %s", jsonl_path, e)
            return None

        if read_start > 0:
            # buf[0] is the byte at (prefer_start - 1) on disk.
            if buf[:1] == b"\n":
                # prefer_start is a line boundary — drop only the terminator,
                # keep the line that starts at prefer_start.
                buf = buf[1:]
            else:
                # prefer_start landed mid-line — drop everything up to and
                # including the next newline so we resume at the next full
                # line.
                nl = buf.find(b"\n")
                if nl < 0:
                    return None
                buf = buf[nl + 1 :]
        return buf

    async def _auq_tool_result_present(
        self, jsonl_path: Path, tool_use_id: str
    ) -> bool:
        """True iff the JSONL carries a ``tool_result`` for ``tool_use_id``.

        The POSITIVE-resolution proof for the startup reconciler: a side file is
        unlinked only when its captured ``tool_use_id`` has a matching
        ``tool_result`` in the JSONL — never merely because "no pending AUQ"
        (which is ALSO true for a LIVE AUQ whose tool_use is buffered until the
        prompt resolves).

        Scans the WHOLE file, NOT just the hydrate tail (Hermes PR-B P2): the
        ``tool_result`` is written at resolution time, so on a long-running
        session it can scroll arbitrarily far past the 1 MB tail while the AUQ
        was genuinely answered. A tail-only check would then miss the proof and
        — combined with the now live-safe ``gc_stale`` skipping the tracked
        session — strand the orphan's side file unboundedly (the dead-card class
        PR-B fights). A full scan keeps the live/orphan distinction exact: an
        orphan's ``tool_result`` exists SOMEWHERE; a live-buffered AUQ's exists
        NOWHERE. Streams line-by-line (memory-bounded) with a cheap bytes
        pre-filter + short-circuit, so the orphan case returns as soon as the
        result line is reached and only the rare live-AUQ case reads to EOF.
        Runs once per orphan-candidate window at startup. Robust to a missing
        file / partial lines / read errors → ``False`` (conservative preserve).
        """
        if not tool_use_id:
            return False
        # Cheap byte pre-filter: only JSON-parse a line that could possibly carry
        # this tool_result (avoids parsing every line of a multi-MB session).
        id_bytes = tool_use_id.encode("utf-8")
        needle = b'"tool_result"'
        try:
            async with aiofiles.open(jsonl_path, "rb") as f:
                async for raw_line in f:
                    if needle not in raw_line or id_bytes not in raw_line:
                        continue
                    try:
                        entry = json.loads(raw_line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if not isinstance(entry, dict):
                        continue
                    msg = entry.get("message")
                    if not isinstance(msg, dict):
                        continue
                    content = msg.get("content")
                    if not isinstance(content, list):
                        continue
                    for c in content:
                        if not isinstance(c, dict):
                            continue
                        if (
                            c.get("type") == "tool_result"
                            and c.get("tool_use_id") == tool_use_id
                        ):
                            return True
        except OSError:
            return False
        return False

    async def _monitor_loop(self) -> None:
        """Background loop for checking session updates.

        Uses simple async polling with aiofiles for non-blocking I/O.
        """
        logger.info("Session monitor started, polling every %ss", self.poll_interval)

        # Deferred import to avoid circular dependency (cached once)
        from .session import session_manager

        # Clean up all stale sessions on startup
        await self._cleanup_all_stale_sessions()
        # Initialize last known session_map
        self._last_session_map = await self._load_current_session_map()

        # Hydrate the AskUserQuestion tool_input cache from the JSONL tail of
        # each currently-bound (window_id, session_id). Bot restart wipes the
        # in-memory cache in handlers.interactive_ui, and ``check_for_updates``
        # resumes from the persisted byte offset — so an AUQ tool_use line
        # already consumed pre-restart never re-emits, and a still-pending
        # AUQ on the pane renders as a partial pane-only card. Run after
        # cleanup + map load so we don't hydrate from stale bindings, and
        # before the polling loop so the first tick has a populated cache.
        await self._hydrate_ask_tool_input_cache(self._last_session_map)

        # PR-1 Half B: re-arm any still-running Workflow's busy lift (typing + 🟡
        # + ↳) from the filesystem after a ``launchctl kickstart`` wiped the
        # in-memory brackets / background_agents. Beside the AUQ hydrate (same
        # rationale: the first tick must already reflect pre-restart state) and
        # before the loop so the first ``pop_sidechain_activity`` drains the relit
        # ``wf-task:`` launched keys. Self-contained + try/except-guarded.
        await self._reconcile_workflow_brackets_on_startup(self._last_session_map)

        while self._running:
            try:
                # Load hook-based session map updates
                await session_manager.load_session_map()

                # Detect session_map changes and cleanup replaced/removed sessions
                current_map = await self._detect_and_cleanup_changes()
                active_session_ids = set(current_map.values())

                # Check for new messages (all I/O is async)
                new_messages = await self.check_for_updates(active_session_ids)

                # Tail any sub-agent (sidechain) JSONL files for active
                # parent sessions and append their filtered tool_use
                # headers. Routed back to the parent session_id so they
                # surface in the parent topic with a "↳ " prefix.
                sidechain_messages = await self.check_sidechain_updates(
                    active_session_ids
                )
                if sidechain_messages:
                    new_messages.extend(sidechain_messages)

                # GH #44 (ex-Wave A): fan the tick's background-agent
                # signals out to route_runtime (keyed keep-alive + launch /
                # done marks) — pull-only, once per tick, AFTER the parent
                # lifecycle dispatch above (§4.2 ordering). Never let it
                # break the dispatch loop.
                sidechain_activity = self.pop_sidechain_activity()
                if sidechain_activity and self._subagent_activity_callback:
                    try:
                        await self._subagent_activity_callback(sidechain_activity)
                    except Exception as e:
                        logger.error(f"Subagent activity callback error: {e}")

                # Bug 2: drop the post-resolution copy of any prose already
                # delivered live before the picker (batch/group dedup against
                # the shown-live markers). Never let it break the dispatch loop.
                try:
                    new_messages = filter_live_prose_duplicates(new_messages)
                except Exception as e:
                    logger.error(f"live-prose dedup error: {e}")

                for msg in new_messages:
                    preview = msg.text[:80] + ("..." if len(msg.text) > 80 else "")
                    logger.info("session=%s: %s", msg.session_id, preview)
                    if self._message_callback:
                        try:
                            await self._message_callback(msg)
                        except Exception as e:
                            logger.error(f"Message callback error: {e}")

            except Exception as e:
                logger.error(f"Monitor loop error: {e}")

            await asyncio.sleep(self.poll_interval)

        logger.info("Session monitor stopped")

    def start(self) -> None:
        if self._running:
            logger.warning("Monitor already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        self.state.save()
        logger.info("Session monitor stopped and state saved")
