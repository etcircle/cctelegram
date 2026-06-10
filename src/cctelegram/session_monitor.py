"""Session monitoring service — watches JSONL files for new messages.

Runs an async polling loop that:
  1. Loads the current session_map to know which sessions to watch.
  2. Detects session_map changes (new/changed/deleted windows) and cleans up.
  3. Reads new JSONL lines from each session file using byte-offset tracking.
  4. Parses entries via TranscriptParser and emits NewMessage objects to a callback.
  5. Tails sub-agent (sidechain) JSONLs unconditionally — display emission is
     gated by show_tool_calls, but per-tick parent activity is always reported
     (pop_sidechain_active_parents → route_runtime keep-alive).

Optimizations: mtime cache skips unchanged files; byte offset avoids re-reading.

Key classes: SessionMonitor, NewMessage, SessionInfo.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Awaitable, Literal

import aiofiles

from . import md_capture
from .config import config
from .monitor_state import MonitorState, TrackedSession
from .tmux_manager import tmux_manager
from .transcript_parser import TranscriptParser
from .utils import read_cwd_from_jsonl

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
        # Wave A: parent session_ids whose sidechain files produced new parsed
        # entries this tick. Populated by ``check_sidechain_updates``
        # unconditionally (even with show_tool_calls disabled) and drained via
        # ``pop_sidechain_active_parents`` — the run-state keep-alive signal.
        self._sidechain_active_parents: set[str] = set()
        # Per-tick fan-out for sidechain activity (wired from bot.post_init,
        # like ``_message_callback`` / ``_event_callback``).
        self._subagent_activity_callback: (
            Callable[[set[str]], Awaitable[None]] | None
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
        self, callback: Callable[[set[str]], Awaitable[None]]
    ) -> None:
        """Wire the per-tick sidechain-activity fan-out (Wave A).

        Called from ``_monitor_loop`` with the set of parent session_ids
        whose sidechains produced new parsed entries this tick.
        """
        self._subagent_activity_callback = callback

    def pop_sidechain_active_parents(self) -> set[str]:
        """Drain (consume-once) the parents with sidechain activity this tick."""
        parents = self._sidechain_active_parents
        self._sidechain_active_parents = set()
        return parents

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

                    # Lifecycle-only entries exist purely to drive the
                    # busy indicator; they have no visible content and must
                    # not fan out to Telegram.
                    if entry.lifecycle_only:
                        continue

                    if not entry.text and not entry.image_data:
                        continue
                    # Skip user messages unless show_user_messages is enabled
                    if entry.role == "user" and not config.show_user_messages:
                        continue
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
        recorded for ``pop_sidechain_active_parents`` (the Wave A run-state
        keep-alive) regardless of display settings. ``config.show_tool_calls``
        gates only the ``NewMessage`` EMISSION below: when False, no sidechain
        messages are emitted (complete display suppression at this point — not
        bot.py-side filtering).
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
            sub_dir = parent_jsonl.parent / parent_session_id / "subagents"
            try:
                if not sub_dir.is_dir():
                    continue
                sidechain_files = list(sub_dir.glob("agent-*.jsonl"))
            except OSError:
                continue

            for sc_file in sidechain_files:
                # sc_file.stem looks like "agent-a05666f9d196136af"
                tracking_key = f"sub:{parent_session_id}:{sc_file.stem}"

                tracked = self.state.get_session(tracking_key)
                if tracked is None:
                    # New sidechain file — start at EOF to skip history.
                    # On startup this avoids replaying long-finished
                    # sub-agent runs; mid-session it means we miss a few
                    # lines that landed before discovery, which is fine.
                    try:
                        st = sc_file.stat()
                    except OSError:
                        continue
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
                    continue

                try:
                    st = sc_file.stat()
                    current_mtime = st.st_mtime
                    current_size = st.st_size
                except OSError:
                    continue

                last_mtime = self._file_mtimes.get(tracking_key, 0.0)
                if (
                    current_mtime <= last_mtime
                    and current_size <= tracked.last_byte_offset
                ):
                    continue

                new_entries = await self._read_new_lines(tracked, sc_file)
                self._file_mtimes[tracking_key] = current_mtime

                if not new_entries:
                    self.state.update_session(tracked)
                    continue

                carry = self._pending_tools.get(tracking_key, {})
                parsed_entries, remaining = TranscriptParser.parse_entries(
                    new_entries,
                    pending_tools=carry,
                )
                if remaining:
                    self._pending_tools[tracking_key] = remaining
                else:
                    self._pending_tools.pop(tracking_key, None)

                # Wave A: new parsed entries = sidechain activity for the
                # parent's route, regardless of whether anything is displayed.
                if parsed_entries:
                    self._sidechain_active_parents.add(parent_session_id)

                if not config.show_tool_calls:
                    # Display suppressed — activity already recorded above.
                    self.state.update_session(tracked)
                    continue

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

        self.state.save_if_dirty()
        return new_messages

    def _remove_sidechains_for_parent(self, parent_session_id: str) -> None:
        """Drop sidechain trackers belonging to a parent that's been cleaned up."""
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

            for user_id, thread_id, wid in session_manager.iter_thread_bindings():
                if wid in changed_window_ids:
                    await route_runtime.mark_session_reset(
                        (user_id, thread_id or 0, wid)
                    )
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

            for wid in changed_window_ids:
                old_sid = changed_old_sessions.get(wid, "")
                if old_sid:
                    unlink_for_session(old_sid)
                    # Bug 2: tear down the OLD session's live-prose capture on
                    # /clear (the session_id was swapped, so forget_ask_tool_input
                    # below would only see the NEW session — same parity reason
                    # as unlink_for_session above).
                    md_capture.teardown_session(old_sid)
                forget_ask_tool_input(wid)

        # Codex P2 (chunk 5): unlink side files for deleted windows
        # too. Outside the ``if changed_window_ids`` block because a
        # cycle with only deletes (no changes) still needs cleanup.
        if deleted_session_ids:
            from .handlers.auq_source import unlink_for_session

            for sid in deleted_session_ids:
                unlink_for_session(sid)
                md_capture.teardown_session(sid)

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

                # Wave A: fan sidechain activity out to route_runtime (the
                # keep-alive heartbeat) — pull-only, once per tick. Never let
                # it break the dispatch loop.
                sidechain_parents = self.pop_sidechain_active_parents()
                if sidechain_parents and self._subagent_activity_callback:
                    try:
                        await self._subagent_activity_callback(sidechain_parents)
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
