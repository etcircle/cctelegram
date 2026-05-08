"""Session monitoring service — watches JSONL files for new messages.

Runs an async polling loop that:
  1. Loads the current session_map to know which sessions to watch.
  2. Detects session_map changes (new/changed/deleted windows) and cleans up.
  3. Reads new JSONL lines from each session file using byte-offset tracking.
  4. Parses entries via TranscriptParser and emits NewMessage objects to a callback.

Optimizations: mtime cache skips unchanged files; byte offset avoids re-reading.

Key classes: SessionMonitor, NewMessage, SessionInfo.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Awaitable, Literal

import aiofiles

from .config import config
from .monitor_state import MonitorState, TrackedSession
from .tmux_manager import tmux_manager
from .transcript_parser import TranscriptParser
from .utils import read_cwd_from_jsonl

logger = logging.getLogger(__name__)


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
        # Per-session pending tool_use state carried across poll cycles
        self._pending_tools: dict[str, dict[str, Any]] = {}  # session_id -> pending
        # Track last known session_map for detecting changes
        # Keys may be window_id (@12) or window_name (old format) during transition
        self._last_session_map: dict[str, str] = {}  # window_key -> session_id
        # In-memory mtime cache for quick file change detection (not persisted)
        self._file_mtimes: dict[str, float] = {}  # session_id -> last_seen_mtime

    def set_message_callback(
        self, callback: Callable[[NewMessage], Awaitable[None]]
    ) -> None:
        self._message_callback = callback

    def set_event_callback(
        self, callback: Callable[[TranscriptEvent], Awaitable[None]]
    ) -> None:
        self._event_callback = callback

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
                        # Partial JSONL line — don't advance offset past it
                        logger.warning(
                            "Partial JSONL line in session %s, will retry next cycle",
                            session.session_id,
                        )
                        break
                    else:
                        # Empty line — safe to skip
                        safe_offset = await f.tell()

                session.last_byte_offset = safe_offset

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
        """
        new_messages: list[NewMessage] = []

        # Without show_tool_calls there's nothing useful to surface for
        # sub-agents under option (a) + (ii) — bail early to keep this
        # consistent with how the parent stream is filtered in bot.py.
        if not config.show_tool_calls:
            return new_messages

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
        if stale:
            logger.info(
                "Removed %d sidechain tracker(s) for parent %s",
                len(stale),
                parent_session_id[:8],
            )

    async def _load_current_session_map(self) -> dict[str, str]:
        """Load current session_map and return window_key -> session_id mapping.

        Keys in session_map are formatted as "tmux_session:window_id"
        (e.g. "cctelegram:@12"). Old-format keys ("cctelegram:window_name") are also
        accepted so that sessions running before a code upgrade continue
        to be monitored until the hook re-fires with new format.
        Only entries matching our tmux_session_name are processed.
        """
        window_to_session: dict[str, str] = {}
        if config.session_map_file.exists():
            try:
                async with aiofiles.open(config.session_map_file, "r") as f:
                    content = await f.read()
                session_map = json.loads(content)
                prefix = f"{config.tmux_session_name}:"
                for key, info in session_map.items():
                    # Only process entries for our tmux session
                    if not key.startswith(prefix):
                        continue
                    window_key = key[len(prefix) :]
                    session_id = info.get("session_id", "")
                    if session_id:
                        window_to_session[window_key] = session_id
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

        self.state.save_if_dirty()

    async def _detect_and_cleanup_changes(self) -> dict[str, str]:
        """Detect session_map changes and cleanup replaced/removed sessions.

        Returns current session_map for further processing.
        """
        current_map = await self._load_current_session_map()

        sessions_to_remove: set[str] = set()
        # Windows whose session_id flipped (e.g. /clear): the route's
        # busy_indicator carries pre-/clear ``open_tools`` IDs that the new
        # session will never close, pinning the route to RUNNING forever.
        # Tracked here so we can hand them to ``busy_indicator.clear_route``
        # once routes are resolved below.
        changed_window_ids: set[str] = set()

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

        # Check for deleted windows (window in old map but not in current)
        old_windows = set(self._last_session_map.keys())
        current_windows = set(current_map.keys())
        deleted_windows = old_windows - current_windows

        for window_id in deleted_windows:
            old_session_id = self._last_session_map[window_id]
            logger.info(
                "Window '%s' deleted, removing session %s",
                window_id,
                old_session_id,
            )
            sessions_to_remove.add(old_session_id)

        # Perform cleanup
        if sessions_to_remove:
            for session_id in sessions_to_remove:
                self.state.remove_session(session_id)
                self._file_mtimes.pop(session_id, None)
                self._remove_sidechains_for_parent(session_id)
            self.state.save_if_dirty()

        # Reset busy_indicator state for routes bound to windows whose
        # session changed. Without this, ``_open_tools`` keeps the
        # tool_use_ids from the pre-/clear session and ``_state_from_open_tools``
        # never returns to IDLE, so the typing indicator and "🟡 Busy" card
        # stay stuck forever even though the new session is genuinely idle.
        # Deferred imports for the same reason as ``_monitor_loop`` — these
        # modules transitively pull in this one.
        if changed_window_ids:
            from .session import session_manager
            from .handlers import busy_indicator

            for user_id, thread_id, wid in session_manager.iter_thread_bindings():
                if wid in changed_window_ids:
                    busy_indicator.clear_route((user_id, thread_id or 0, wid))
                    logger.info(
                        "Cleared busy_indicator route after session change: "
                        "user=%d thread=%s window=%s",
                        user_id,
                        thread_id,
                        wid,
                    )

        # Update last known map
        self._last_session_map = current_map

        return current_map

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
