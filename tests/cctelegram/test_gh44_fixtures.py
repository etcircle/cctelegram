"""GH #44 fixture gate — the plan's empirical premises, pinned on JSONL.

Four SYNTHETIC, schema-minimal fixtures modeled 1:1 on a real captured
episode (2026-06-12; live shapes verified in-session against the actual
parent + sidechain transcripts, then re-keyed and stripped to only the
fields the parser/extractors read — no prompt bodies, session ids, branch
names, paths, or signatures survive into git history):

  (a) ``parent_task_notification_user.jsonl`` — the parent ``user`` entry the
      harness writes when a background agent completes; carries
      ``<task-notification><task-id>`` equal to the agent key.
  (b) ``sidechain_mid_run_batch.jsonl`` — a mid-run sidechain batch whose
      entries carry the UTC ``Z`` ISO timestamps the adapter parses.
  (c) ``sidechain_final_turn.jsonl`` — the agent's final turn, ending in an
      ``end_turn`` assistant entry (the done-detection input).
  (d) ``parent_async_launch_tool_result.jsonl`` — the Agent tool_result for a
      ``run_in_background`` launch; carries the ``agentId:`` line (the §3.2a
      background discriminator).

The §3.0 contract test asserts the launch ``agentId``, the sidechain file
stem, and the task-notification ``<task-id>`` all normalize to ONE key via
``normalize_background_agent_key``.
"""

from __future__ import annotations

import json
from pathlib import Path

from cctelegram.handlers.response_builder import (
    extract_async_agent_launch_id,
    extract_task_notification_task_id,
    is_task_notification,
)
from cctelegram.route_runtime import TURN_END_REASONS, normalize_background_agent_key
from cctelegram.transcript_parser import TranscriptParser
from cctelegram.utils import parse_iso_timestamp

FIXTURES = Path(__file__).parent.parent / "fixtures" / "gh44"

# The synthetic agent key — every fixture references it (shape mirrors the
# real hex agentIds).
AGENT_KEY = "a1b2c3d4e5f6a7b89"
SIDECHAIN_STEM = "agent-a1b2c3d4e5f6a7b89"


def _parse_fixture(name: str):
    entries = [
        json.loads(ln) for ln in (FIXTURES / name).read_text().splitlines() if ln
    ]
    parsed, _pending = TranscriptParser.parse_entries(entries, pending_tools={})
    return parsed


# ── §3.0: one normalized key across all sources ─────────────────────────


def test_normalize_collapses_all_three_sources_to_one_key():
    launch_line = json.loads(
        (FIXTURES / "parent_async_launch_tool_result.jsonl").read_text()
    )
    launch_text = launch_line["message"]["content"][0]["content"][0]["text"]
    launch_id = extract_async_agent_launch_id(launch_text)
    assert launch_id == AGENT_KEY

    notif_line = json.loads(
        (FIXTURES / "parent_task_notification_user.jsonl").read_text()
    )
    notif_text = notif_line["message"]["content"]
    task_id = extract_task_notification_task_id(notif_text)
    assert task_id == AGENT_KEY

    assert normalize_background_agent_key(launch_id) == AGENT_KEY
    assert normalize_background_agent_key(SIDECHAIN_STEM) == AGENT_KEY
    assert normalize_background_agent_key(task_id) == AGENT_KEY


def test_normalize_is_idempotent_and_prefix_scoped():
    assert normalize_background_agent_key("a1b2c3") == "a1b2c3"
    assert normalize_background_agent_key("agent-a1b2c3") == "a1b2c3"
    # Only a LEADING prefix is stripped, exactly once.
    assert normalize_background_agent_key("agent-agent-x") == "agent-x"
    assert normalize_background_agent_key("xagent-y") == "xagent-y"


# ── (d) launch extractor — agentId: line is the anchor ──────────────────


def test_launch_extractor_parses_fixture_through_the_parser():
    """End-to-end: the parser's tool_result entry text still carries the
    agentId line, so the monitor-side extraction works on parsed entries."""
    parsed = _parse_fixture("parent_async_launch_tool_result.jsonl")
    results = [p for p in parsed if p.content_type == "tool_result"]
    assert results, "fixture must parse to a tool_result entry"
    assert extract_async_agent_launch_id(results[0].text) == AGENT_KEY


def test_launch_extractor_anchors_on_agent_id_line_not_the_sentence():
    # The success sentence is diagnostic-only — a future TUI rewording must
    # not break extraction as long as the structured line survives.
    assert (
        extract_async_agent_launch_id("Launched.\nagentId: deadbeef01 (internal)")
        == "deadbeef01"
    )
    # No agentId line → None, regardless of prose.
    assert extract_async_agent_launch_id("Async agent launched successfully.") is None
    # Ordinary tool output mentioning agents → None.
    assert extract_async_agent_launch_id("the agent id is fine") is None
    assert extract_async_agent_launch_id("") is None
    assert extract_async_agent_launch_id(None) is None  # type: ignore[arg-type]


# ── (a) task-notification extractor ─────────────────────────────────────


def test_task_notification_extractor_on_fixture():
    notif_line = json.loads(
        (FIXTURES / "parent_task_notification_user.jsonl").read_text()
    )
    text = notif_line["message"]["content"]
    assert is_task_notification(text)
    assert extract_task_notification_task_id(text) == AGENT_KEY


def test_task_notification_extractor_rejects_ordinary_text():
    assert extract_task_notification_task_id("hello world") is None
    assert extract_task_notification_task_id("") is None
    assert extract_task_notification_task_id("<task-notification>no id here") is None


# ── (b) timestamps — both sides parse with the shared helper ────────────


def test_sidechain_timestamps_parse_to_epoch_floats():
    lines = (FIXTURES / "sidechain_mid_run_batch.jsonl").read_text().splitlines()
    stamps = [parse_iso_timestamp(json.loads(ln).get("timestamp")) for ln in lines]
    assert all(isinstance(s, float) for s in stamps)
    parent_line = json.loads(
        (FIXTURES / "parent_task_notification_user.jsonl").read_text()
    )
    parent_ts = parse_iso_timestamp(parent_line.get("timestamp"))
    assert isinstance(parent_ts, float)
    # Same clock family: the completion notification postdates the mid-run batch.
    assert parent_ts > max(s for s in stamps if s is not None)


def test_parse_iso_timestamp_fails_closed():
    assert parse_iso_timestamp(None) is None
    assert parse_iso_timestamp("") is None
    assert parse_iso_timestamp("not-a-date") is None


# ── (c) final turn — end-of-turn detectable from parsed entries ──────────


def test_sidechain_final_turn_carries_end_of_turn():
    parsed = _parse_fixture("sidechain_final_turn.jsonl")
    assert any(p.stop_reason in TURN_END_REASONS for p in parsed), (
        "the agent's final turn must expose an end-of-turn stop_reason "
        f"(got {[(p.content_type, p.stop_reason) for p in parsed]})"
    )
