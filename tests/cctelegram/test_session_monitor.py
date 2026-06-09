"""Unit tests for SessionMonitor JSONL reading and offset handling."""

import json
import time

import pytest

from cctelegram.monitor_state import TrackedSession
from cctelegram.session_monitor import (
    NewMessage,
    SessionInfo,
    SessionMonitor,
    TranscriptEvent,
)


class TestReadNewLinesOffsetRecovery:
    """Tests for _read_new_lines offset corruption recovery."""

    @pytest.fixture
    def monitor(self, tmp_path):
        """Create a SessionMonitor with temp state file."""
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    @pytest.mark.asyncio
    async def test_mid_line_offset_recovery(self, monitor, tmp_path, make_jsonl_entry):
        """Recover from corrupted offset pointing mid-line."""
        # Create JSONL file with two valid lines
        jsonl_file = tmp_path / "session.jsonl"
        entry1 = make_jsonl_entry(msg_type="assistant", content="first message")
        entry2 = make_jsonl_entry(msg_type="assistant", content="second message")
        jsonl_file.write_text(
            json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n",
            encoding="utf-8",
        )

        # Calculate offset pointing into the middle of line 1
        line1_bytes = len(json.dumps(entry1).encode("utf-8")) // 2
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=line1_bytes,  # Mid-line (corrupted)
        )

        # Read should recover and return empty (offset moved to next line)
        result = await monitor._read_new_lines(session, jsonl_file)

        # Should return empty list (recovery skips to next line, no new content yet)
        assert result == []

        # Offset should now point to start of line 2
        line1_full = len(json.dumps(entry1).encode("utf-8")) + 1  # +1 for newline
        assert session.last_byte_offset == line1_full

    @pytest.mark.asyncio
    async def test_valid_offset_reads_normally(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        """Normal reading when offset points to line start."""
        jsonl_file = tmp_path / "session.jsonl"
        entry1 = make_jsonl_entry(msg_type="assistant", content="first")
        entry2 = make_jsonl_entry(msg_type="assistant", content="second")
        jsonl_file.write_text(
            json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n",
            encoding="utf-8",
        )

        # Offset at 0 should read both lines
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        assert len(result) == 2
        assert session.last_byte_offset == jsonl_file.stat().st_size

    @pytest.mark.asyncio
    async def test_truncation_detection(self, monitor, tmp_path, make_jsonl_entry):
        """Detect file truncation and reset offset."""
        jsonl_file = tmp_path / "session.jsonl"
        entry = make_jsonl_entry(msg_type="assistant", content="content")
        jsonl_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        # Set offset beyond file size (simulates truncation)
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=9999,  # Beyond file size
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        # Should reset offset to 0 and read the line
        assert session.last_byte_offset == jsonl_file.stat().st_size
        assert len(result) == 1


class TestRegisterSession:
    """Tests for SessionMonitor.register_session pre-registration."""

    @pytest.fixture
    def monitor(self, tmp_path):
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    def test_registers_unknown_session_at_offset_zero(self, monitor, tmp_path):
        jsonl_file = tmp_path / "session.jsonl"

        registered = monitor.register_session("sid-new", jsonl_file, offset=0)

        assert registered is True
        tracked = monitor.state.get_session("sid-new")
        assert tracked is not None
        assert tracked.file_path == str(jsonl_file)
        assert tracked.last_byte_offset == 0

    def test_noop_when_session_already_tracked(self, monitor, tmp_path):
        jsonl_file = tmp_path / "session.jsonl"
        monitor.state.update_session(
            TrackedSession(
                session_id="sid-existing",
                file_path=str(jsonl_file),
                last_byte_offset=42,
            )
        )

        registered = monitor.register_session("sid-existing", jsonl_file, offset=0)

        assert registered is False
        # Existing offset preserved.
        assert monitor.state.get_session("sid-existing").last_byte_offset == 42

    @pytest.mark.asyncio
    async def test_pre_registered_offset_zero_picks_up_first_exchange(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        """Regression: a freshly bound session must read from offset 0.

        Without pre-registration, ``check_for_updates`` initializes new
        sessions at end-of-file, dropping the seed user message and the
        first assistant reply that were already written between hook fire
        and the first poll cycle.
        """
        jsonl_file = tmp_path / "session.jsonl"

        # Pre-register before any content exists (mirrors the bot flow:
        # hook fires → register → send pending text → Claude appends).
        monitor.register_session("sid-fresh", jsonl_file, offset=0)

        # Now simulate Claude appending the seed exchange.
        user_entry = make_jsonl_entry(msg_type="user", content="Hi")
        assistant_entry = make_jsonl_entry(
            msg_type="assistant", content="Hi! What can I help you with?"
        )
        jsonl_file.write_text(
            json.dumps(user_entry) + "\n" + json.dumps(assistant_entry) + "\n",
            encoding="utf-8",
        )

        tracked = monitor.state.get_session("sid-fresh")
        result = await monitor._read_new_lines(tracked, jsonl_file)

        assert len(result) == 2
        assert tracked.last_byte_offset == jsonl_file.stat().st_size


class TestEventCallback:
    """TranscriptEvent dispatch and legacy NewMessage co-emission."""

    @pytest.fixture
    def monitor(self, tmp_path):
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    def _write_jsonl(self, path, lines: list[dict]) -> None:
        path.write_text(
            "\n".join(json.dumps(line) for line in lines) + "\n",
            encoding="utf-8",
        )

    def _patch_scan(self, monitor, session_id: str, jsonl_file):
        async def _scan():
            return [SessionInfo(session_id=session_id, file_path=jsonl_file)]

        monitor.scan_projects = _scan  # type: ignore[method-assign]

    @pytest.mark.asyncio
    async def test_event_callback_fires_with_assistant_text(
        self, monitor, tmp_path, make_jsonl_entry, make_text_block
    ):
        jsonl_file = tmp_path / "session.jsonl"
        entry = make_jsonl_entry(
            "assistant", [make_text_block("hello world")], session_id="sid"
        )
        entry["message"]["stop_reason"] = "end_turn"
        entry["uuid"] = "evt-uuid-1"
        self._write_jsonl(jsonl_file, [entry])

        monitor.register_session("sid", jsonl_file, offset=0)
        self._patch_scan(monitor, "sid", jsonl_file)

        events: list[TranscriptEvent] = []

        async def on_event(ev: TranscriptEvent) -> None:
            events.append(ev)

        monitor.set_event_callback(on_event)

        msgs = await monitor.check_for_updates({"sid"})

        assert len(events) == 1
        ev = events[0]
        assert ev.session_id == "sid"
        assert ev.role == "assistant"
        assert ev.block_type == "text"
        assert ev.stop_reason == "end_turn"
        assert ev.timestamp is not None
        assert ev.text == "hello world"
        assert ev.transcript_uuid == "evt-uuid-1"
        # Legacy NewMessage callback path still emits the message.
        assert len(msgs) == 1
        assert isinstance(msgs[0], NewMessage)
        assert msgs[0].text == "hello world"
        assert msgs[0].transcript_uuid == "evt-uuid-1"

    @pytest.mark.asyncio
    async def test_event_callback_carries_tool_use_metadata(
        self,
        monitor,
        tmp_path,
        make_jsonl_entry,
        make_tool_use_block,
        make_tool_result_block,
    ):
        jsonl_file = tmp_path / "session.jsonl"
        assistant_entry = make_jsonl_entry(
            "assistant",
            [make_tool_use_block("t1", "Read", {"file_path": "a.py"})],
            session_id="sid",
        )
        assistant_entry["message"]["stop_reason"] = "tool_use"
        user_entry = make_jsonl_entry(
            "user",
            [make_tool_result_block("t1", "ok")],
            session_id="sid",
        )
        self._write_jsonl(jsonl_file, [assistant_entry, user_entry])

        monitor.register_session("sid", jsonl_file, offset=0)
        self._patch_scan(monitor, "sid", jsonl_file)

        events: list[TranscriptEvent] = []
        msgs_seen: list[NewMessage] = []

        async def on_event(ev: TranscriptEvent) -> None:
            events.append(ev)

        monitor.set_event_callback(on_event)
        msgs_seen = await monitor.check_for_updates({"sid"})

        tool_use_events = [e for e in events if e.block_type == "tool_use"]
        tool_result_events = [e for e in events if e.block_type == "tool_result"]
        assert len(tool_use_events) == 1
        assert tool_use_events[0].tool_use_id == "t1"
        assert tool_use_events[0].tool_name == "Read"
        assert tool_use_events[0].stop_reason == "tool_use"
        assert len(tool_result_events) == 1
        assert tool_result_events[0].tool_use_id == "t1"
        # User-role message → no stop_reason on the resulting event.
        assert tool_result_events[0].stop_reason is None
        # Regression: NewMessage co-emission is preserved for both blocks.
        assert len(msgs_seen) == 2

    @pytest.mark.asyncio
    async def test_no_event_callback_still_emits_messages(
        self, monitor, tmp_path, make_jsonl_entry, make_text_block
    ):
        jsonl_file = tmp_path / "session.jsonl"
        entry = make_jsonl_entry("assistant", [make_text_block("hi")], session_id="sid")
        self._write_jsonl(jsonl_file, [entry])

        monitor.register_session("sid", jsonl_file, offset=0)
        self._patch_scan(monitor, "sid", jsonl_file)

        # No event callback set.
        msgs = await monitor.check_for_updates({"sid"})

        assert len(msgs) == 1
        assert msgs[0].text == "hi"

    @pytest.mark.asyncio
    async def test_event_callback_raises_does_not_block_messages(
        self, monitor, tmp_path, make_jsonl_entry, make_text_block, caplog
    ):
        """A raising event callback must not crash the loop nor suppress NewMessage."""
        import logging

        jsonl_file = tmp_path / "session.jsonl"
        entry = make_jsonl_entry(
            "assistant", [make_text_block("hello")], session_id="sid"
        )
        self._write_jsonl(jsonl_file, [entry])

        monitor.register_session("sid", jsonl_file, offset=0)
        self._patch_scan(monitor, "sid", jsonl_file)

        async def on_event(ev: TranscriptEvent) -> None:
            raise RuntimeError("boom")

        monitor.set_event_callback(on_event)

        with caplog.at_level(logging.ERROR, logger="cctelegram.session_monitor"):
            # (i) does not crash
            msgs = await monitor.check_for_updates({"sid"})

        # (ii) NewMessage still emitted
        assert len(msgs) == 1
        assert isinstance(msgs[0], NewMessage)
        assert msgs[0].text == "hello"

        # (iii) error logged
        assert any(
            "Event callback error" in record.getMessage()
            and record.levelno == logging.ERROR
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_per_cycle_order_events_before_messages(
        self, monitor, tmp_path, make_jsonl_entry, make_text_block
    ):
        """All TranscriptEvents for a cycle must complete before any NewMessage."""
        jsonl_file = tmp_path / "session.jsonl"
        e1 = make_jsonl_entry("assistant", [make_text_block("one")], session_id="sid")
        e2 = make_jsonl_entry("assistant", [make_text_block("two")], session_id="sid")
        e3 = make_jsonl_entry("assistant", [make_text_block("three")], session_id="sid")
        self._write_jsonl(jsonl_file, [e1, e2, e3])

        monitor.register_session("sid", jsonl_file, offset=0)
        self._patch_scan(monitor, "sid", jsonl_file)

        order: list[tuple[str, str, str]] = []

        async def on_event(ev: TranscriptEvent) -> None:
            order.append(("event", ev.session_id, ev.text))

        async def on_message(msg: NewMessage) -> None:
            order.append(("message", msg.session_id, msg.text))

        monitor.set_event_callback(on_event)
        monitor.set_message_callback(on_message)

        # Drive the same control flow as _monitor_loop: check_for_updates
        # awaits all events for the cycle inline, then the loop dispatches
        # messages.
        msgs = await monitor.check_for_updates({"sid"})
        for msg in msgs:
            await on_message(msg)

        assert len(order) == 6
        kinds = [k for (k, _, _) in order]
        # All three events fire before any of the three messages.
        assert kinds == ["event", "event", "event", "message", "message", "message"]
        # And payloads come through in source order on each side.
        event_texts = [t for (k, _, t) in order if k == "event"]
        message_texts = [t for (k, _, t) in order if k == "message"]
        assert event_texts == ["one", "two", "three"]
        assert message_texts == ["one", "two", "three"]

    @pytest.mark.asyncio
    async def test_multi_block_message_emits_event_per_block(
        self,
        monitor,
        tmp_path,
        make_jsonl_entry,
        make_thinking_block,
        make_tool_use_block,
    ):
        """One assistant message with thinking + tool_use → two events sharing
        stop_reason and timestamp."""
        jsonl_file = tmp_path / "session.jsonl"
        entry = make_jsonl_entry(
            "assistant",
            [
                make_thinking_block("planning the call"),
                make_tool_use_block("t1", "Read", {"file_path": "a.py"}),
            ],
            session_id="sid",
            timestamp="2026-05-02T12:00:00.000Z",
        )
        entry["message"]["stop_reason"] = "tool_use"
        self._write_jsonl(jsonl_file, [entry])

        monitor.register_session("sid", jsonl_file, offset=0)
        self._patch_scan(monitor, "sid", jsonl_file)

        events: list[TranscriptEvent] = []

        async def on_event(ev: TranscriptEvent) -> None:
            events.append(ev)

        monitor.set_event_callback(on_event)

        await monitor.check_for_updates({"sid"})

        block_types = [e.block_type for e in events]
        assert "thinking" in block_types
        assert "tool_use" in block_types
        assert len(events) == 2

        # Both events carry the SAME stop_reason and timestamp from the
        # parent JSONL message.
        assert all(e.stop_reason == "tool_use" for e in events)
        assert all(e.timestamp == "2026-05-02T12:00:00.000Z" for e in events)


class TestSidechainTailing:
    """check_sidechain_updates: tail sub-agent JSONLs and tag with subagent_key."""

    @pytest.fixture
    def monitor(self, tmp_path):
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    def _write_jsonl(self, path, lines: list[dict]) -> None:
        path.write_text(
            "\n".join(json.dumps(line) for line in lines) + "\n",
            encoding="utf-8",
        )

    def _append_jsonl(self, path, lines: list[dict]) -> None:
        with path.open("a", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")

    def _setup_parent(self, monitor, tmp_path, parent_sid: str = "parent-sid") -> tuple:
        """Build a fake project with a parent JSONL and an empty sidechain dir."""
        proj_dir = tmp_path / "projects" / "-tmp-fake"
        proj_dir.mkdir(parents=True)
        parent_jsonl = proj_dir / f"{parent_sid}.jsonl"
        parent_jsonl.write_text("")  # parent file just needs to exist
        sub_dir = proj_dir / parent_sid / "subagents"
        sub_dir.mkdir(parents=True)
        # Pre-register the parent in tracked_sessions so check_sidechain_updates
        # can resolve its file_path.
        monitor.state.update_session(
            TrackedSession(
                session_id=parent_sid,
                file_path=str(parent_jsonl),
                last_byte_offset=0,
            )
        )
        return parent_jsonl, sub_dir

    @pytest.mark.asyncio
    async def test_first_seen_registers_at_eof_no_emit(
        self, monitor, tmp_path, make_jsonl_entry, make_tool_use_block
    ):
        """A sidechain file present on first observation is registered at EOF."""
        parent_sid = "parent-sid"
        _, sub_dir = self._setup_parent(monitor, tmp_path, parent_sid)

        # Pre-existing sidechain content (a "historical run" we should NOT replay).
        sc_file = sub_dir / "agent-abc.jsonl"
        old_entry = make_jsonl_entry(
            "assistant",
            [make_tool_use_block("t1", "Bash", {"command": "ls"})],
            session_id="ignored",
        )
        self._write_jsonl(sc_file, [old_entry])

        msgs = await monitor.check_sidechain_updates({parent_sid})

        # Nothing emitted (started at EOF), but the tracker is registered.
        assert msgs == []
        tracking_key = f"sub:{parent_sid}:agent-abc"
        tracked = monitor.state.get_session(tracking_key)
        assert tracked is not None
        assert tracked.parent_session_id == parent_sid
        assert tracked.last_byte_offset == sc_file.stat().st_size

    @pytest.mark.asyncio
    async def test_appended_tool_use_emits_subagent_tagged_event(
        self, monitor, tmp_path, make_jsonl_entry, make_tool_use_block
    ):
        """New tool_use lines after registration emit subagent-tagged NewMessages."""
        parent_sid = "parent-sid"
        _, sub_dir = self._setup_parent(monitor, tmp_path, parent_sid)
        sc_file = sub_dir / "agent-abc.jsonl"
        sc_file.write_text("")  # start empty

        # First call registers the empty file at offset 0.
        await monitor.check_sidechain_updates({parent_sid})

        # Sub-agent now emits a tool_use.
        new_entry = make_jsonl_entry(
            "assistant",
            [make_tool_use_block("t1", "Bash", {"command": "pnpm test"})],
            session_id="ignored",
        )
        self._append_jsonl(sc_file, [new_entry])

        msgs = await monitor.check_sidechain_updates({parent_sid})

        assert len(msgs) == 1
        m = msgs[0]
        assert m.session_id == parent_sid  # routed to parent's topic
        assert "Bash" in m.text
        assert "pnpm test" in m.text
        # Underlying block type is preserved so the digest can pair tool_use
        # with its tool_result; the queue routes via subagent_key first.
        assert m.content_type == "tool_use"
        assert m.tool_use_id == "t1"
        assert m.tool_name == "Bash"
        assert m.role == "assistant"
        assert m.subagent_key == f"sub:{parent_sid}:agent-abc"

    @pytest.mark.asyncio
    async def test_text_thinking_tool_use_and_tool_result_all_forwarded(
        self,
        monitor,
        tmp_path,
        make_jsonl_entry,
        make_text_block,
        make_thinking_block,
        make_tool_use_block,
        make_tool_result_block,
    ):
        """All four block types forward as subagent-tagged events for the digest."""
        parent_sid = "parent-sid"
        _, sub_dir = self._setup_parent(monitor, tmp_path, parent_sid)
        sc_file = sub_dir / "agent-abc.jsonl"
        sc_file.write_text("")

        await monitor.check_sidechain_updates({parent_sid})

        text_entry = make_jsonl_entry(
            "assistant", [make_text_block("agent's plan")], session_id="x"
        )
        thinking_entry = make_jsonl_entry(
            "assistant", [make_thinking_block("agent thinking")], session_id="x"
        )
        tool_use_entry = make_jsonl_entry(
            "assistant",
            [make_tool_use_block("t1", "Read", {"file_path": "a.py"})],
            session_id="x",
        )
        tool_result_entry = make_jsonl_entry(
            "user",
            [make_tool_result_block("t1", "file contents...")],
            session_id="x",
        )
        self._append_jsonl(
            sc_file, [text_entry, thinking_entry, tool_use_entry, tool_result_entry]
        )

        msgs = await monitor.check_sidechain_updates({parent_sid})

        expected_key = f"sub:{parent_sid}:agent-abc"
        for m in msgs:
            assert m.session_id == parent_sid
            assert m.subagent_key == expected_key

        # Text, thinking, tool_use, and tool_result all flow through the
        # digest path now — the per-sub-agent card handles pairing.
        kinds = [m.content_type for m in msgs]
        assert "text" in kinds
        assert "thinking" in kinds
        assert "tool_use" in kinds
        assert "tool_result" in kinds

    @pytest.mark.asyncio
    async def test_show_tool_calls_false_short_circuits(
        self, monitor, tmp_path, monkeypatch, make_jsonl_entry, make_tool_use_block
    ):
        """When show_tool_calls is disabled, no sidechain messages are emitted."""
        from cctelegram.session_monitor import config as monitor_config

        monkeypatch.setattr(monitor_config, "show_tool_calls", False)

        parent_sid = "parent-sid"
        _, sub_dir = self._setup_parent(monitor, tmp_path, parent_sid)
        sc_file = sub_dir / "agent-abc.jsonl"
        entry = make_jsonl_entry(
            "assistant",
            [make_tool_use_block("t1", "Bash", {"command": "ls"})],
            session_id="x",
        )
        self._write_jsonl(sc_file, [entry])

        msgs = await monitor.check_sidechain_updates({parent_sid})

        assert msgs == []
        # Also: no tracker registered, since we bailed before the scan.
        assert monitor.state.get_session(f"sub:{parent_sid}:agent-abc") is None

    def test_remove_sidechains_for_parent_drops_all(self, monitor, tmp_path):
        """_remove_sidechains_for_parent clears trackers + caches for one parent only."""
        parent_a = "parent-a"
        parent_b = "parent-b"

        # Two trackers for parent_a, one for parent_b, plus a non-sidechain.
        monitor.state.update_session(
            TrackedSession(
                session_id=f"sub:{parent_a}:agent-1",
                file_path="/tmp/a1.jsonl",
                parent_session_id=parent_a,
            )
        )
        monitor.state.update_session(
            TrackedSession(
                session_id=f"sub:{parent_a}:agent-2",
                file_path="/tmp/a2.jsonl",
                parent_session_id=parent_a,
            )
        )
        monitor.state.update_session(
            TrackedSession(
                session_id=f"sub:{parent_b}:agent-1",
                file_path="/tmp/b1.jsonl",
                parent_session_id=parent_b,
            )
        )
        monitor.state.update_session(
            TrackedSession(
                session_id=parent_a,
                file_path="/tmp/parent_a.jsonl",
            )
        )
        monitor._file_mtimes[f"sub:{parent_a}:agent-1"] = 1.0
        monitor._pending_tools[f"sub:{parent_a}:agent-2"] = {"x": object()}

        monitor._remove_sidechains_for_parent(parent_a)

        # Parent A's sidechains gone; parent A itself + parent B's child remain.
        assert monitor.state.get_session(f"sub:{parent_a}:agent-1") is None
        assert monitor.state.get_session(f"sub:{parent_a}:agent-2") is None
        assert monitor.state.get_session(f"sub:{parent_b}:agent-1") is not None
        assert monitor.state.get_session(parent_a) is not None
        # Caches scrubbed for the removed keys only.
        assert f"sub:{parent_a}:agent-1" not in monitor._file_mtimes
        assert f"sub:{parent_a}:agent-2" not in monitor._pending_tools


def _auq_tool_use_entry(tool_use_id: str, n_questions: int = 1) -> dict:
    """Build an assistant message with an AskUserQuestion tool_use block."""
    questions = [
        {
            "question": f"Question {i + 1}?",
            "header": f"Q{i + 1}",
            "options": [{"label": f"opt-{i}-a"}, {"label": f"opt-{i}-b"}],
        }
        for i in range(n_questions)
    ]
    return {
        "type": "assistant",
        "timestamp": "2026-05-16T13:00:00.000Z",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": "AskUserQuestion",
                    "input": {"questions": questions},
                }
            ],
        },
    }


def _tool_result_entry(tool_use_id: str) -> dict:
    """Build a user message carrying a tool_result for the given tool_use_id."""
    return {
        "type": "user",
        "timestamp": "2026-05-16T13:01:00.000Z",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": [{"type": "text", "text": "answered"}],
                }
            ],
        },
    }


def _write_jsonl(path, entries):
    """Write an iterable of dict entries as a JSONL file."""
    path.write_text(
        "".join(json.dumps(e) + "\n" for e in entries),
        encoding="utf-8",
    )


class TestFindLatestPendingAuq:
    """Tests for SessionMonitor._find_latest_pending_auq (tail scan core)."""

    @pytest.fixture
    def monitor(self, tmp_path):
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    @pytest.mark.asyncio
    async def test_returns_none_for_missing_file(self, monitor, tmp_path):
        result = await monitor._find_latest_pending_auq(tmp_path / "nope.jsonl")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_file(self, monitor, tmp_path):
        f = tmp_path / "empty.jsonl"
        f.write_text("", encoding="utf-8")
        result = await monitor._find_latest_pending_auq(f)
        assert result is None

    @pytest.mark.asyncio
    async def test_unanswered_auq_is_hydrated(self, monitor, tmp_path):
        # Single pending AUQ at the end of the JSONL — the hydration path.
        f = tmp_path / "session.jsonl"
        _write_jsonl(
            f,
            [
                {"type": "user", "message": {"role": "user", "content": "hi"}},
                _auq_tool_use_entry("auq_1", n_questions=2),
            ],
        )
        result = await monitor._find_latest_pending_auq(f)
        assert result is not None
        assert result["id"] == "auq_1"
        assert len(result["input"]["questions"]) == 2
        assert result["input"]["questions"][0]["header"] == "Q1"

    @pytest.mark.asyncio
    async def test_answered_auq_is_not_hydrated(self, monitor, tmp_path):
        # AUQ followed by a matching tool_result: nothing pending → None.
        f = tmp_path / "session.jsonl"
        _write_jsonl(
            f,
            [
                _auq_tool_use_entry("auq_1"),
                _tool_result_entry("auq_1"),
            ],
        )
        result = await monitor._find_latest_pending_auq(f)
        assert result is None

    @pytest.mark.asyncio
    async def test_latest_pending_wins_over_older_answered(self, monitor, tmp_path):
        # Two AUQs: older one is answered, newer one is still pending.
        # Hydration must surface the newer pending input, not the older one.
        f = tmp_path / "session.jsonl"
        _write_jsonl(
            f,
            [
                _auq_tool_use_entry("auq_old", n_questions=1),
                _tool_result_entry("auq_old"),
                _auq_tool_use_entry("auq_new", n_questions=3),
            ],
        )
        result = await monitor._find_latest_pending_auq(f)
        assert result is not None
        assert result["id"] == "auq_new"
        assert len(result["input"]["questions"]) == 3

    @pytest.mark.asyncio
    async def test_invalid_json_lines_are_skipped(self, monitor, tmp_path):
        # A partial trailing write shouldn't abort the whole scan.
        f = tmp_path / "session.jsonl"
        entries_text = (
            json.dumps(_auq_tool_use_entry("auq_1")) + "\n{not-valid-json\n\n    \n"
        )
        f.write_text(entries_text, encoding="utf-8")
        result = await monitor._find_latest_pending_auq(f)
        assert result is not None
        assert result["id"] == "auq_1"

    @pytest.mark.asyncio
    async def test_preserves_first_full_line_when_tail_lands_on_line_boundary(
        self, monitor, tmp_path
    ):
        # Hermes review P2 on PR #24: if prefer_start lands exactly on a line
        # boundary (the byte after a previous line's '\n'), the partial-line
        # drop must NOT discard the first line — it's a complete line and
        # may be the only pending AUQ. The fix peeks one byte earlier and
        # only drops when that byte is not '\n'.
        f = tmp_path / "boundary.jsonl"
        first_entry = _auq_tool_use_entry("auq_before", n_questions=1)
        first_entry_with_result = [
            first_entry,
            _tool_result_entry("auq_before"),
        ]
        second_entry = _auq_tool_use_entry("auq_after_boundary", n_questions=3)
        _write_jsonl(f, first_entry_with_result + [second_entry])

        # Compute the byte offset that lands exactly at the start of the
        # final entry (the only pending AUQ).
        prefix_text = "".join(json.dumps(e) + "\n" for e in first_entry_with_result)
        boundary_offset = len(prefix_text.encode("utf-8"))
        size = f.stat().st_size
        # Tail-bytes value that produces prefer_start == boundary_offset.
        monkey_tail = size - boundary_offset
        original_tail = monitor._AUQ_HYDRATE_TAIL_BYTES
        monitor.__class__._AUQ_HYDRATE_TAIL_BYTES = monkey_tail
        try:
            result = await monitor._find_latest_pending_auq(f)
        finally:
            monitor.__class__._AUQ_HYDRATE_TAIL_BYTES = original_tail

        # The pending AUQ is auq_after_boundary (3 questions). Without the
        # peek-byte fix this would return None because the only pending AUQ
        # line would have been dropped.
        assert result is not None
        assert result["id"] == "auq_after_boundary"
        assert len(result["input"]["questions"]) == 3

    @pytest.mark.asyncio
    async def test_drops_partial_first_line_when_seeking_past_zero(
        self, monitor, tmp_path
    ):
        # When the tail-bytes window starts mid-line of an earlier entry,
        # the first partial line must be discarded (it would fail json.loads
        # anyway) without preventing the trailing valid AUQ from being
        # found. Tail-bytes is sized to clip into the first entry but leave
        # the second one intact so we have a concrete pending AUQ to find.
        f = tmp_path / "big.jsonl"
        first_entry = _auq_tool_use_entry("auq_first", n_questions=1)
        second_entry = _auq_tool_use_entry("auq_tail", n_questions=2)
        _write_jsonl(f, [first_entry, second_entry])
        # Place the tail-bytes cap such that the read starts inside the
        # first entry's JSON but the entire second entry is included.
        first_bytes = len(json.dumps(first_entry).encode("utf-8")) + 1  # +"\n"
        second_bytes = len(json.dumps(second_entry).encode("utf-8")) + 1
        # Want prefer_start to fall in the middle of the first entry.
        monkey_tail = second_bytes + (first_bytes // 2)
        original_tail = monitor._AUQ_HYDRATE_TAIL_BYTES
        monitor.__class__._AUQ_HYDRATE_TAIL_BYTES = monkey_tail
        try:
            result = await monitor._find_latest_pending_auq(f)
        finally:
            monitor.__class__._AUQ_HYDRATE_TAIL_BYTES = original_tail
        # auq_first's JSON was clipped; partial-line drop discarded the
        # clipped chunk; auq_tail was wholly visible → that's what we get.
        assert result is not None
        assert result["id"] == "auq_tail"
        assert len(result["input"]["questions"]) == 2


class TestAuqCacheHydration:
    """Tests for SessionMonitor._hydrate_ask_tool_input_cache (orchestrator)."""

    @pytest.fixture
    def monitor(self, tmp_path):
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    @pytest.mark.asyncio
    async def test_hydrates_cache_for_bound_window_with_pending_auq(
        self, monitor, tmp_path, monkeypatch
    ):
        # End-to-end: a bound window's session has a pending AUQ; after
        # hydration, the public resolve_ask_tool_input returns its input.
        from cctelegram.handlers import interactive_ui

        jsonl = tmp_path / "session.jsonl"
        _write_jsonl(jsonl, [_auq_tool_use_entry("auq_hydrate")])
        sessions = [SessionInfo(session_id="sid-1", file_path=jsonl)]

        async def fake_scan_projects():
            return sessions

        monkeypatch.setattr(monitor, "scan_projects", fake_scan_projects)
        # Start from a clean cache to avoid cross-test bleed.
        interactive_ui._last_completed_ask_tool_input.pop("@7", None)

        await monitor._hydrate_ask_tool_input_cache({"@7": "sid-1"})

        cached = interactive_ui.resolve_ask_tool_input("@7")
        assert cached is not None
        assert isinstance(cached.get("questions"), list)
        # Clean up for downstream tests.
        interactive_ui._last_completed_ask_tool_input.pop("@7", None)

    @pytest.mark.asyncio
    async def test_skips_subagent_session_keys(self, monitor, tmp_path, monkeypatch):
        # Defense in depth: ``sub:<parent>:agent-…`` keys in current_map are
        # not supposed to exist (session_map only stores parent sessions),
        # but if one ever slipped in, hydrating it under a parent window
        # would mis-label pick buttons. Must be skipped silently.
        from cctelegram.handlers import interactive_ui

        jsonl = tmp_path / "subagent.jsonl"
        _write_jsonl(jsonl, [_auq_tool_use_entry("auq_sub")])

        async def fake_scan_projects():
            return [SessionInfo(session_id="sub:parent-abc:agent-xyz", file_path=jsonl)]

        monkeypatch.setattr(monitor, "scan_projects", fake_scan_projects)
        interactive_ui._last_completed_ask_tool_input.pop("@8", None)

        await monitor._hydrate_ask_tool_input_cache({"@8": "sub:parent-abc:agent-xyz"})

        # Cache untouched.
        assert interactive_ui.resolve_ask_tool_input("@8") is None

    @pytest.mark.asyncio
    async def test_empty_map_is_a_noop(self, monitor):
        # Bot started before any window is bound: no-op, no exception.
        await monitor._hydrate_ask_tool_input_cache({})

    @pytest.mark.asyncio
    async def test_no_path_for_session_is_skipped(self, monitor, tmp_path, monkeypatch):
        # scan_projects returns nothing for the bound session_id (e.g.
        # JSONL hasn't been written yet, or the cwd isn't active right
        # now). Must not raise, must not hydrate.
        from cctelegram.handlers import interactive_ui

        async def fake_scan_projects():
            return []

        monkeypatch.setattr(monitor, "scan_projects", fake_scan_projects)
        interactive_ui._last_completed_ask_tool_input.pop("@9", None)

        await monitor._hydrate_ask_tool_input_cache({"@9": "sid-unknown"})
        assert interactive_ui.resolve_ask_tool_input("@9") is None


class TestAuqCacheClearOnSessionChange:
    """Tests that _detect_and_cleanup_changes clears the AUQ cache for windows
    whose session_id flipped (e.g. /clear in tmux). Without this, the cache
    keyed only by window_id would survive across the session swap and the
    next render would overlay dead-AUQ labels onto the new session's pane."""

    @pytest.fixture
    def monitor(self, tmp_path):
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    @pytest.mark.asyncio
    async def test_session_flip_clears_auq_cache_for_changed_window(
        self, monitor, monkeypatch
    ):
        from cctelegram.handlers import interactive_ui

        # Seed the AUQ cache as if a render had captured an input for @11.
        interactive_ui.remember_ask_tool_input(
            "@11", {"questions": [{"options": [{"label": "x"}]}]}
        )
        assert interactive_ui.resolve_ask_tool_input("@11") is not None

        monitor._last_session_map = {"@11": "session-old"}

        async def fake_load_current_map():
            return {"@11": "session-new"}

        monkeypatch.setattr(monitor, "_load_current_session_map", fake_load_current_map)

        await monitor._detect_and_cleanup_changes()

        # /clear flipped the session under window @11 → cached AUQ tool_input
        # belongs to the dead session and must be dropped.
        assert interactive_ui.resolve_ask_tool_input("@11") is None

    @pytest.mark.asyncio
    async def test_session_flip_does_not_release_ledger_rows(
        self, monitor, tmp_path, monkeypatch
    ):
        """Wave 2 P1-1 regression — session replacement (`/clear`,
        restart/session-map flip) runs `forget_ask_tool_input(wid)` as
        GENERIC cleanup with NO AUQ tool_result observed. That is not
        resolution proof: a `dispatched` ledger row for the window must stay
        `dispatched`, NOT be released — releasing would remove the durable
        single-use brake and let a stale same-fingerprint tap re-dispatch.
        (Release fires only at the positive-proof seams: bot.handle_new_message's
        AUQ tool_result branch + the startup reconciler.)"""
        from cctelegram.handlers import auq_ledger

        key = auq_ledger.make_ledger_key("deadbeef", "cafebabe", 2)
        auq_ledger.reset_for_tests(path=tmp_path / "auq_action_ledger.jsonl")
        auq_ledger.record(
            key,
            state="accepted",
            user_id=42,
            window_id="@11",
            full_fingerprint="ff" * 20,
            option_number=2,
            option_label="alpha",
        )
        auq_ledger.record(key, state="dispatched")

        monitor._last_session_map = {"@11": "session-old"}

        async def fake_load_current_map():
            return {"@11": "session-new"}

        monkeypatch.setattr(monitor, "_load_current_session_map", fake_load_current_map)

        try:
            await monitor._detect_and_cleanup_changes()

            row = auq_ledger.lookup(key)
            assert row is not None and row.state == "dispatched", (
                "session replacement with NO AUQ tool_result must NOT release "
                "the window's ledger rows — the dispatched-but-UNRESOLVED "
                "instance keeps its durable single-use brake"
            )
        finally:
            auq_ledger.reset_for_tests()

    @staticmethod
    def _write_side_file(tmp_path, session_id: str, tool_use_id: str) -> "object":
        pending = tmp_path / "auq_pending"
        pending.mkdir(mode=0o700, exist_ok=True)
        path = pending / f"{session_id}.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "session_id": session_id,
                    "tool_use_id": tool_use_id,
                    "written_at": time.time(),
                    "tool_input": {
                        "questions": [
                            {
                                "question": "Q?",
                                "header": "Q1",
                                "options": [{"label": "a"}, {"label": "b"}],
                            }
                        ]
                    },
                }
            )
        )
        return path

    @pytest.mark.asyncio
    async def test_reconciles_orphaned_side_file_when_no_pending_auq(
        self, monitor, tmp_path, monkeypatch
    ):
        """Codex+Hermes dual-FAIL fix (2026-05-31): an answered AUQ whose side
        file was never unlinked (bot down / callback errored while the monitor
        offset had already passed the tool_result) must be reconciled on
        startup hydration — otherwise side_file_live_for_window keeps a DEAD
        card alive forever.
        """
        from cctelegram.handlers import auq_source
        from cctelegram.session import WindowState, session_manager

        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        auq_source.reset_for_tests()

        # session_id must be a canonical UUID — the side-file reader rejects
        # non-UUID names (path-traversal defense).
        sid = "11111111-1111-4111-8111-111111111111"
        # JSONL shows the AUQ was ANSWERED (tool_use + matching tool_result)
        # → _find_latest_pending_auq returns None.
        jsonl = tmp_path / "session.jsonl"
        _write_jsonl(
            jsonl,
            [_auq_tool_use_entry("auq_done"), _tool_result_entry("auq_done")],
        )

        async def fake_scan_projects():
            return [SessionInfo(session_id=sid, file_path=jsonl)]

        monkeypatch.setattr(monitor, "scan_projects", fake_scan_projects)

        side = self._write_side_file(tmp_path, sid, "auq_done")
        session_manager.window_states["@7"] = WindowState(cwd="/r", session_id=sid)
        try:
            assert auq_source.side_file_live_for_window("@7") is True

            await monitor._hydrate_ask_tool_input_cache({"@7": sid})

            assert not side.exists(), "orphaned side file must be reconciled away"
            assert auq_source.side_file_live_for_window("@7") is False
        finally:
            session_manager.window_states.pop("@7", None)
            auq_source.reset_for_tests()

    @pytest.mark.asyncio
    async def test_keeps_side_file_when_auq_still_pending(
        self, monitor, tmp_path, monkeypatch
    ):
        """Inverse safety: a genuinely PENDING AUQ (tool_use, no tool_result)
        must NOT have its side file reconciled away — that would resurrect the
        disappearing-card bug.
        """
        from cctelegram.handlers import auq_source, interactive_ui
        from cctelegram.session import WindowState, session_manager

        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        auq_source.reset_for_tests()

        sid = "22222222-2222-4222-8222-222222222222"
        jsonl = tmp_path / "session.jsonl"
        _write_jsonl(jsonl, [_auq_tool_use_entry("auq_pending")])

        async def fake_scan_projects():
            return [SessionInfo(session_id=sid, file_path=jsonl)]

        monkeypatch.setattr(monitor, "scan_projects", fake_scan_projects)

        side = self._write_side_file(tmp_path, sid, "auq_pending")
        session_manager.window_states["@8"] = WindowState(cwd="/r", session_id=sid)
        try:
            await monitor._hydrate_ask_tool_input_cache({"@8": sid})
            assert side.exists(), "live AUQ side file must be kept"
        finally:
            session_manager.window_states.pop("@8", None)
            interactive_ui._last_completed_ask_tool_input.pop("@8", None)
            auq_source.reset_for_tests()

    @pytest.mark.asyncio
    async def test_reconcile_is_session_keyed_not_window_keyed(
        self, monitor, tmp_path, monkeypatch
    ):
        """Hermes round-2 P2: the reconciler keys on current_map's session_id,
        NOT a re-resolve via peek_session_id_for_window (window_states) — which
        at startup (hydration runs before the loop's load_session_map) can
        point at a DIFFERENT session. Checking liveness on one session while
        unlinking another is the mint/validate parity trap. Here window_states
        is deliberately stale; the orphan for the current-map session must
        still be reconciled (and a window-keyed check would have missed it).
        """
        from cctelegram.handlers import auq_source
        from cctelegram.session import WindowState, session_manager

        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        auq_source.reset_for_tests()

        cur = "11111111-1111-4111-8111-111111111111"  # current_map session
        stale = "99999999-9999-4999-8999-999999999999"  # stale window_states

        jsonl = tmp_path / "session.jsonl"
        _write_jsonl(
            jsonl,
            [_auq_tool_use_entry("auq_done"), _tool_result_entry("auq_done")],
        )

        async def fake_scan_projects():
            return [SessionInfo(session_id=cur, file_path=jsonl)]

        monkeypatch.setattr(monitor, "scan_projects", fake_scan_projects)

        side = self._write_side_file(tmp_path, cur, "auq_done")
        # window_states disagrees with current_map (stale binding at startup).
        session_manager.window_states["@7"] = WindowState(cwd="/r", session_id=stale)
        try:
            await monitor._hydrate_ask_tool_input_cache({"@7": cur})
            assert not side.exists(), (
                "reconciler must unlink the current-map session's orphan even "
                "when window_states points elsewhere (session-keyed)"
            )
        finally:
            session_manager.window_states.pop("@7", None)
            auq_source.reset_for_tests()

    # ── PR-B (stateless-callback Wave 1): positive-proof reconcile ──────────
    # Claude BUFFERS the AskUserQuestion tool_use in JSONL until the prompt
    # resolves, so a genuinely-LIVE AUQ shows NO pending AUQ in JSONL. The
    # reconciler must unlink ONLY on POSITIVE resolution proof (a matched AUQ
    # tool_result for the side file's tool_use_id) — never merely on "no pending
    # AUQ in JSONL", which is ALSO true for a live buffered tool_use.

    @pytest.mark.asyncio
    async def test_keeps_side_file_when_auq_tool_use_buffered(
        self, monitor, tmp_path, monkeypatch
    ):
        """§8.11 — a live AUQ whose tool_use is buffered (NOT in JSONL) shows no
        pending AUQ AND has no tool_result; the side file must be PRESERVED, not
        reconciled away. (Current code unlinks on "no pending AUQ" → RED.)
        """
        from cctelegram.handlers import auq_source
        from cctelegram.session import WindowState, session_manager

        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        auq_source.reset_for_tests()

        sid = "33333333-3333-4333-8333-333333333333"
        # JSONL carries NO AUQ tool_use (buffered) and NO tool_result.
        jsonl = tmp_path / "session.jsonl"
        _write_jsonl(jsonl, [])

        async def fake_scan_projects():
            return [SessionInfo(session_id=sid, file_path=jsonl)]

        monkeypatch.setattr(monitor, "scan_projects", fake_scan_projects)

        side = self._write_side_file(tmp_path, sid, "buffered-auq")
        session_manager.window_states["@7"] = WindowState(cwd="/r", session_id=sid)
        try:
            assert auq_source.side_file_live_for_window("@7") is True
            await monitor._hydrate_ask_tool_input_cache({"@7": sid})
            assert side.exists(), (
                "a live buffered-tool_use AUQ side file must be PRESERVED — no "
                "tool_result means no positive proof of resolution"
            )
            assert auq_source.side_file_live_for_window("@7") is True
        finally:
            session_manager.window_states.pop("@7", None)
            auq_source.reset_for_tests()

    @pytest.mark.asyncio
    async def test_keeps_side_file_when_tool_use_id_empty(
        self, monitor, tmp_path, monkeypatch
    ):
        """P3 (Codex R3) — a side file with an empty tool_use_id cannot be matched
        to a tool_result, so "no pending AUQ" is NOT proof THIS AUQ resolved.
        Preserve it until session-replacement / clear / 1h GC. (RED today.)
        """
        from cctelegram.handlers import auq_source
        from cctelegram.session import WindowState, session_manager

        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        auq_source.reset_for_tests()

        sid = "44444444-4444-4444-8444-444444444444"
        # An UNRELATED answered AUQ (so _find_latest_pending_auq returns None),
        # but the side file's tool_use_id is "" → cannot prove THIS one resolved.
        jsonl = tmp_path / "session.jsonl"
        _write_jsonl(
            jsonl,
            [_auq_tool_use_entry("other"), _tool_result_entry("other")],
        )

        async def fake_scan_projects():
            return [SessionInfo(session_id=sid, file_path=jsonl)]

        monkeypatch.setattr(monitor, "scan_projects", fake_scan_projects)

        side = self._write_side_file(tmp_path, sid, "")
        session_manager.window_states["@7"] = WindowState(cwd="/r", session_id=sid)
        try:
            await monitor._hydrate_ask_tool_input_cache({"@7": sid})
            assert side.exists(), (
                "empty tool_use_id → no positive resolution proof → side file "
                "must be PRESERVED"
            )
        finally:
            session_manager.window_states.pop("@7", None)
            auq_source.reset_for_tests()

    @pytest.mark.asyncio
    async def test_reconciles_orphan_when_tool_result_beyond_hydrate_tail(
        self, monitor, tmp_path, monkeypatch
    ):
        """Hermes PR-B P2 — a GENUINE orphan (answered, unlink missed) whose
        matching tool_result scrolled PAST the hydrate tail, on a still-tracked
        session, must STILL be reconciled. The positive-proof scan reads the
        WHOLE file (not just the tail), else the now-live-safe gc_stale would
        skip the tracked session and strand the dead card unboundedly. (RED with
        a tail-only proof scan: it misses the result → preserves the orphan.)
        """
        from cctelegram.handlers import auq_source
        from cctelegram.session import WindowState, session_manager

        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        auq_source.reset_for_tests()
        # Shrink the tail so the answered AUQ + its tool_result fall OUTSIDE it
        # cheaply (the real tail is 1 MB).
        monkeypatch.setattr(monitor, "_AUQ_HYDRATE_TAIL_BYTES", 50)
        monkeypatch.setattr(monitor, "_AUQ_HYDRATE_HARD_CAP", 50)

        sid = "55555555-5555-4555-8555-555555555555"
        jsonl = tmp_path / "session.jsonl"
        # The answered AUQ is near the START; many lines follow so the
        # tool_result is far past the (shrunk) tail.
        padding = [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "filler " * 30}],
                },
            }
            for _ in range(8)
        ]
        _write_jsonl(
            jsonl,
            [_auq_tool_use_entry("auq_far"), _tool_result_entry("auq_far"), *padding],
        )

        async def fake_scan_projects():
            return [SessionInfo(session_id=sid, file_path=jsonl)]

        monkeypatch.setattr(monitor, "scan_projects", fake_scan_projects)

        side = self._write_side_file(tmp_path, sid, "auq_far")
        session_manager.window_states["@7"] = WindowState(cwd="/r", session_id=sid)
        try:
            # Sanity: the tail no longer sees the answered AUQ → reconcile path.
            assert await monitor._find_latest_pending_auq(jsonl) is None
            # But the full-file proof scan DOES find the result → unlink.
            assert await monitor._auq_tool_result_present(jsonl, "auq_far") is True

            await monitor._hydrate_ask_tool_input_cache({"@7": sid})
            assert not side.exists(), (
                "an orphan whose tool_result is beyond the hydrate tail must "
                "still be reconciled (full-file positive-proof scan)"
            )
        finally:
            session_manager.window_states.pop("@7", None)
            auq_source.reset_for_tests()

    # ── Wave 0 (review finding 12): TOCTOU re-peek before unlink ────────────
    # The positive-proof scan (_auq_tool_result_present) is a whole-file JSONL
    # read that yields the event loop, potentially for seconds. A fresh
    # PreToolUse(AskUserQuestion) firing during that await atomically replaces
    # auq_pending/<session_id>.json; a blind unlink afterwards would delete the
    # NEW live AUQ's side file — the card-liveness authority (the exact class
    # PR-B exists to close; gc_stale got the sibling re-stat guard in the same
    # commit).

    @pytest.mark.asyncio
    async def test_skips_unlink_when_side_file_replaced_during_proof_scan(
        self, monitor, tmp_path, monkeypatch
    ):
        """RED (finding 12): the side file is atomically replaced with a NEW
        live AUQ (tool_use_id B) while _auq_tool_result_present awaits for the
        ORIGINAL id A. Positive proof for A must NOT unlink B's file — the
        reconciler must re-peek the tool_use_id immediately before unlink and
        skip on mismatch.
        """
        from cctelegram.handlers import auq_source
        from cctelegram.session import WindowState, session_manager

        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        auq_source.reset_for_tests()

        sid = "66666666-6666-4666-8666-666666666666"
        # Genuine orphan shape for id A: answered AUQ in the JSONL.
        jsonl = tmp_path / "session.jsonl"
        _write_jsonl(
            jsonl,
            [_auq_tool_use_entry("auq_old"), _tool_result_entry("auq_old")],
        )

        async def fake_scan_projects():
            return [SessionInfo(session_id=sid, file_path=jsonl)]

        monkeypatch.setattr(monitor, "scan_projects", fake_scan_projects)

        side = self._write_side_file(tmp_path, sid, "auq_old")

        real_proof_scan = monitor._auq_tool_result_present

        async def swapping_proof_scan(path, tuid):
            result = await real_proof_scan(path, tuid)
            # Mid-await: the live session's PreToolUse hook atomically
            # replaces the side file with a FRESH AUQ (tool_use_id B).
            self._write_side_file(tmp_path, sid, "auq_new_live")
            return result

        monkeypatch.setattr(monitor, "_auq_tool_result_present", swapping_proof_scan)

        session_manager.window_states["@7"] = WindowState(cwd="/r", session_id=sid)
        try:
            await monitor._hydrate_ask_tool_input_cache({"@7": sid})
            assert side.exists(), (
                "a fresh live AUQ side file written during the proof scan must "
                "be PRESERVED — unlinking it destroys the card-liveness "
                "authority (TOCTOU)"
            )
            assert auq_source.peek_side_file_tool_use_id(sid) == "auq_new_live", (
                "the surviving side file must be the NEW live AUQ's record"
            )
        finally:
            session_manager.window_states.pop("@7", None)
            auq_source.reset_for_tests()

    @pytest.mark.asyncio
    async def test_unlinks_orphan_when_side_file_unchanged_after_proof_scan(
        self, monitor, tmp_path, monkeypatch
    ):
        """Complement: no mid-await swap → the re-peek matches the original
        tool_use_id and positive proof still unlinks the orphan (the PR-B
        reconcile behavior is preserved under the new guard).
        """
        from cctelegram.handlers import auq_source
        from cctelegram.session import WindowState, session_manager

        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        auq_source.reset_for_tests()

        sid = "77777777-7777-4777-8777-777777777777"
        jsonl = tmp_path / "session.jsonl"
        _write_jsonl(
            jsonl,
            [_auq_tool_use_entry("auq_done"), _tool_result_entry("auq_done")],
        )

        async def fake_scan_projects():
            return [SessionInfo(session_id=sid, file_path=jsonl)]

        monkeypatch.setattr(monitor, "scan_projects", fake_scan_projects)

        side = self._write_side_file(tmp_path, sid, "auq_done")
        session_manager.window_states["@7"] = WindowState(cwd="/r", session_id=sid)
        try:
            await monitor._hydrate_ask_tool_input_cache({"@7": sid})
            assert not side.exists(), (
                "an unchanged orphan must still be reconciled away under the "
                "TOCTOU re-peek guard"
            )
        finally:
            session_manager.window_states.pop("@7", None)
            auq_source.reset_for_tests()

    # ── Wave 2 (Hermes R1 P2-1): reconciler releases the window's ledger ────
    # rows on the SAME positive proof that gates the side-file unlink. The
    # crash window: bot down between the AUQ tool_result and
    # forget_ask_tool_input — the side file lingers AND a stale `dispatched`
    # ledger row would block a same-day identical AUQ (the content-derived
    # key reconstructs the same triplet) with "Action already received".

    def _seed_ledger_row(self, tmp_path, window_id: str) -> str:
        from cctelegram.handlers import auq_ledger

        auq_ledger.reset_for_tests(path=tmp_path / "auq_action_ledger.jsonl")
        key = auq_ledger.make_ledger_key("deadbeef", "cafebabe", 2)
        auq_ledger.record(
            key,
            state="accepted",
            user_id=42,
            window_id=window_id,
            full_fingerprint="ff" * 20,
            option_number=2,
            option_label="alpha",
        )
        auq_ledger.record(key, state="dispatched")
        return key

    @pytest.mark.asyncio
    async def test_positive_proof_releases_resolved_windows_ledger_rows(
        self, monitor, tmp_path, monkeypatch
    ):
        """RED (Wave 2 / Hermes P2-1): positive proof + unchanged side file
        must — alongside the unlink — RELEASE the resolved window's ledger
        rows, so a same-day byte-identical AUQ in the same route is
        dispatchable again. Window-scoped: another window's rows stay put."""
        from cctelegram.handlers import auq_ledger, auq_source
        from cctelegram.session import WindowState, session_manager

        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        auq_source.reset_for_tests()

        sid = "88888888-8888-4888-8888-888888888888"
        jsonl = tmp_path / "session.jsonl"
        _write_jsonl(
            jsonl,
            [_auq_tool_use_entry("auq_done"), _tool_result_entry("auq_done")],
        )

        async def fake_scan_projects():
            return [SessionInfo(session_id=sid, file_path=jsonl)]

        monkeypatch.setattr(monitor, "scan_projects", fake_scan_projects)

        side = self._write_side_file(tmp_path, sid, "auq_done")
        key = self._seed_ledger_row(tmp_path, "@7")
        other_key = auq_ledger.make_ledger_key("deadbeef", "cafebabe", 3)
        auq_ledger.record(
            other_key,
            state="dispatched",
            user_id=42,
            window_id="@9",
            full_fingerprint="ee" * 20,
            option_number=3,
            option_label="other",
        )
        session_manager.window_states["@7"] = WindowState(cwd="/r", session_id=sid)
        try:
            await monitor._hydrate_ask_tool_input_cache({"@7": sid})
            assert not side.exists()
            assert auq_ledger.lookup(key) is None, (
                "positive proof of resolution must release the window's "
                "ledger rows (the crash-window seam)"
            )
            sibling = auq_ledger.lookup(other_key)
            assert sibling is not None and sibling.state == "dispatched", (
                "release is window-scoped — another window's rows stay"
            )
        finally:
            session_manager.window_states.pop("@7", None)
            auq_source.reset_for_tests()
            auq_ledger.reset_for_tests()

    @pytest.mark.asyncio
    async def test_toctou_swap_releases_nothing(self, monitor, tmp_path, monkeypatch):
        """RED (Wave 2): the TOCTOU-swap case (side file replaced by a fresh
        live AUQ during the proof scan) must release NOTHING — the proof is
        stale and the window may have a live card whose rows are load-bearing."""
        from cctelegram.handlers import auq_ledger, auq_source
        from cctelegram.session import WindowState, session_manager

        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        auq_source.reset_for_tests()

        sid = "99999999-9999-4999-8999-999999999999"
        jsonl = tmp_path / "session.jsonl"
        _write_jsonl(
            jsonl,
            [_auq_tool_use_entry("auq_old"), _tool_result_entry("auq_old")],
        )

        async def fake_scan_projects():
            return [SessionInfo(session_id=sid, file_path=jsonl)]

        monkeypatch.setattr(monitor, "scan_projects", fake_scan_projects)

        side = self._write_side_file(tmp_path, sid, "auq_old")
        key = self._seed_ledger_row(tmp_path, "@7")

        real_proof_scan = monitor._auq_tool_result_present

        async def swapping_proof_scan(path, tuid):
            result = await real_proof_scan(path, tuid)
            self._write_side_file(tmp_path, sid, "auq_new_live")
            return result

        monkeypatch.setattr(monitor, "_auq_tool_result_present", swapping_proof_scan)

        session_manager.window_states["@7"] = WindowState(cwd="/r", session_id=sid)
        try:
            await monitor._hydrate_ask_tool_input_cache({"@7": sid})
            assert side.exists()
            row = auq_ledger.lookup(key)
            assert row is not None and row.state == "dispatched", (
                "a stale proof (side file swapped mid-scan) must not release "
                "the window's ledger rows"
            )
        finally:
            session_manager.window_states.pop("@7", None)
            auq_source.reset_for_tests()
            auq_ledger.reset_for_tests()


class TestLoadCurrentSessionMapLegacyKeyFilter:
    """_load_current_session_map accepts only @-keyed (window_id) entries.

    Pre-2026-02-11 window_name-keyed session_map entries are dropped (the live
    hook only ever emits @N keys), but the drop MUST emit a one-shot warning
    naming the keys — otherwise a stale on-disk file would silently stop those
    sessions from being monitored (Hermes P2.2)."""

    @pytest.fixture
    def monitor(self, tmp_path):
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    @pytest.mark.asyncio
    async def test_legacy_window_name_keys_filtered_and_warned(
        self, monitor, tmp_path, monkeypatch, caplog
    ):
        import logging

        from cctelegram.session_monitor import config as monitor_config

        prefix = f"{monitor_config.tmux_session_name}:"
        session_map_file = tmp_path / "session_map.json"
        session_map_file.write_text(
            json.dumps(
                {
                    # @-keyed (window_id) entries pass through.
                    f"{prefix}@0": {"session_id": "sid-at-zero"},
                    f"{prefix}@12": {"session_id": "sid-at-twelve"},
                    # Legacy window_name-keyed entries are dropped + warned.
                    f"{prefix}myproject": {"session_id": "sid-legacy-1"},
                    f"{prefix}old-name": {"session_id": "sid-legacy-2"},
                    # Entry for a different tmux session is ignored entirely.
                    "other-session:@3": {"session_id": "sid-other"},
                }
            )
        )
        monkeypatch.setattr(monitor_config, "session_map_file", session_map_file)

        with caplog.at_level(logging.WARNING, logger="cctelegram.session_monitor"):
            result = await monitor._load_current_session_map()

        # Only the @-keyed entries for our tmux session survive.
        assert result == {"@0": "sid-at-zero", "@12": "sid-at-twelve"}
        assert "myproject" not in result
        assert "old-name" not in result

        # The drop is announced via a one-shot warning naming the dropped keys.
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any("dropping legacy window_name-keyed" in m for m in warnings)
        assert any(
            f"{prefix}myproject" in m and f"{prefix}old-name" in m for m in warnings
        )

        # One-shot guard: a second load does not re-warn.
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="cctelegram.session_monitor"):
            result2 = await monitor._load_current_session_map()
        assert result2 == {"@0": "sid-at-zero", "@12": "sid-at-twelve"}
        assert not [r for r in caplog.records if "dropping legacy" in r.getMessage()]
