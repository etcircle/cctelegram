"""Tests for the Bug 2 MessageDisplay live-prose capture mechanism (PR-B).

Covers the two-process pipeline end to end:
  * the stdlib appender (``_md_display_appender.py``), exercised as a real
    subprocess the way Claude Code runs the hook;
  * the bot-side reader / accumulator / normalization / lifecycle in
    ``md_capture``;
  * the ``--settings`` launch-command composition + quoting;
  * a latency gate proving the appender adds negligible cost over a bare
    interpreter start (the F4 ``forceSyncExecution`` budget).

The surface that posts a record before the picker, the freshness gate, and the
JSONL dedup land in PR-C+D; this file locks the capture primitives.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cctelegram import md_capture
from cctelegram.md_capture import (
    ProseRecord,
    appender_path,
    normalize_prose,
    read_prose_records,
)
from cctelegram.tmux_manager import _compose_launch_command

_SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture
def cc_dir(tmp_path, monkeypatch):
    """Point ``app_dir()`` at a tmp dir for the duration of a test."""
    monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
    return tmp_path


def _transcript_path(session_id: str = _SID) -> str:
    return f"/Users/x/.claude/projects/-repo/{session_id}.jsonl"


def _md_payload(
    *,
    message_id: str,
    index: int,
    final: bool,
    delta: str,
    transcript_path: str | None = None,
    session_id: str = _SID,
) -> dict:
    return {
        "hook_event_name": "MessageDisplay",
        "session_id": session_id,
        "transcript_path": transcript_path or _transcript_path(session_id),
        "turn_id": "turn-1",
        "message_id": message_id,
        "index": index,
        "final": final,
        "delta": delta,
    }


def _run_appender(payload, cc_dir: Path) -> subprocess.CompletedProcess:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    env = {**os.environ, "CC_TELEGRAM_DIR": str(cc_dir)}
    return subprocess.run(
        [sys.executable, str(appender_path())],
        input=raw,
        capture_output=True,
        text=True,
        env=env,
    )


# ── Appender (subprocess) ────────────────────────────────────────────────────


def test_appender_writes_ndjson_keyed_by_transcript_stem(cc_dir):
    proc = _run_appender(
        _md_payload(message_id="M1", index=0, final=True, delta="hello"), cc_dir
    )
    assert proc.returncode == 0
    out = cc_dir / "msg_display" / f"{_SID}.ndjson"
    assert out.exists(), "appender must key the file by the transcript_path stem"
    line = json.loads(out.read_text().strip())
    # Full raw payload preserved under "payload", plus an honest capture clock.
    assert line["payload"]["delta"] == "hello"
    assert line["payload"]["message_id"] == "M1"
    assert isinstance(line["captured_at"], (int, float))


def test_appender_keys_by_transcript_not_payload_session_id(cc_dir):
    """Resume safety: under ``--resume`` the payload ``session_id`` is the NEW
    id but ``transcript_path`` still points at the ORIGINAL session's file (the
    id the bot tracks). The capture file must be keyed by the transcript stem."""
    orig = "11111111-2222-3333-4444-555555555555"
    payload = _md_payload(
        message_id="M1",
        index=0,
        final=True,
        delta="x",
        transcript_path=_transcript_path(orig),
        session_id="99999999-9999-9999-9999-999999999999",  # the resumed id
    )
    proc = _run_appender(payload, cc_dir)
    assert proc.returncode == 0
    assert (cc_dir / "msg_display" / f"{orig}.ndjson").exists()
    assert not (
        cc_dir / "msg_display" / "99999999-9999-9999-9999-999999999999.ndjson"
    ).exists()


def test_appender_appends_multiple_lines(cc_dir):
    _run_appender(_md_payload(message_id="M1", index=0, final=False, delta="a"), cc_dir)
    _run_appender(_md_payload(message_id="M1", index=1, final=True, delta="b"), cc_dir)
    out = cc_dir / "msg_display" / f"{_SID}.ndjson"
    assert len(out.read_text().splitlines()) == 2


def test_appender_sets_owner_only_perms(cc_dir):
    _run_appender(_md_payload(message_id="M1", index=0, final=True, delta="x"), cc_dir)
    d = cc_dir / "msg_display"
    f = d / f"{_SID}.ndjson"
    # No group/other access on either — prose can carry sensitive context.
    assert d.stat().st_mode & 0o077 == 0
    assert f.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize(
    "raw",
    [
        "",  # empty stdin
        "not json at all",  # invalid JSON
        "[1, 2, 3]",  # JSON but not a dict
        json.dumps({"hook_event_name": "MessageDisplay", "delta": "x"}),  # no tp
        json.dumps({"transcript_path": "", "delta": "x"}),  # empty tp
        # Path-special stem ("..") must not escape the capture dir.
        json.dumps({"transcript_path": "/p/...jsonl", "delta": "x"}),
    ],
)
def test_appender_tolerates_bad_input_without_writing(cc_dir, raw):
    proc = _run_appender(raw, cc_dir)
    assert proc.returncode == 0, "the appender must never block Claude (exit 0)"
    md = cc_dir / "msg_display"
    # No NDJSON should be written for an unroutable / malformed payload.
    written = list(md.glob("*.ndjson")) if md.exists() else []
    assert written == []


def test_appender_nul_transcript_path_exits_clean(cc_dir):
    """Regression (codex PR-B P1): an embedded NUL in transcript_path makes
    os.open raise ValueError (NOT OSError). The broad hook-boundary guard must
    still exit 0 and write nothing -- a force-sync hook may never exit nonzero."""
    nul_tp = "/p/a" + chr(0) + "b.jsonl"  # a genuine embedded NUL byte
    payload = json.dumps({"transcript_path": nul_tp, "delta": "x"})
    proc = _run_appender(payload, cc_dir)
    assert proc.returncode == 0
    md = cc_dir / "msg_display"
    assert (list(md.glob("*.ndjson")) if md.exists() else []) == []


# ── Reader / accumulator ─────────────────────────────────────────────────────


def _seed(cc_dir: Path, session_id: str, lines: list[dict]) -> Path:
    d = cc_dir / "msg_display"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    p = d / f"{session_id}.ndjson"
    p.write_text("".join(json.dumps(ln) + "\n" for ln in lines))
    return p


def test_read_accumulates_multiflush_in_index_order(cc_dir):
    tp = _transcript_path()
    _seed(
        cc_dir,
        _SID,
        [
            {
                "captured_at": 100.0,
                "payload": _md_payload(
                    message_id="M1", index=0, final=False, delta="ALPHA\nBETA\n"
                ),
            },
            {
                "captured_at": 100.2,
                "payload": _md_payload(
                    message_id="M1", index=1, final=True, delta="GAMMA"
                ),
            },
        ],
    )
    recs = read_prose_records(_SID)
    assert len(recs) == 1
    assert recs[0].text == "ALPHA\nBETA\nGAMMA"
    assert recs[0].transcript_path == tp
    assert recs[0].final_at == 100.2
    assert recs[0].first_seen_at == 100.0


def test_read_excludes_not_finalized(cc_dir):
    _seed(
        cc_dir,
        _SID,
        [
            {
                "captured_at": 1.0,
                "payload": _md_payload(
                    message_id="M1", index=0, final=False, delta="streaming"
                ),
            }
        ],
    )
    assert read_prose_records(_SID) == []


def test_read_orders_by_final_at(cc_dir):
    _seed(
        cc_dir,
        _SID,
        [
            {
                "captured_at": 5.0,
                "payload": _md_payload(
                    message_id="LATE", index=0, final=True, delta="late"
                ),
            },
            {
                "captured_at": 1.0,
                "payload": _md_payload(
                    message_id="EARLY", index=0, final=True, delta="early"
                ),
            },
        ],
    )
    recs = read_prose_records(_SID)
    assert [r.md_message_id for r in recs] == ["EARLY", "LATE"]


def test_read_skips_corrupt_and_partial_lines(cc_dir):
    d = cc_dir / "msg_display"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    p = d / f"{_SID}.ndjson"
    good = json.dumps(
        {
            "captured_at": 1.0,
            "payload": _md_payload(message_id="M1", index=0, final=True, delta="ok"),
        }
    )
    # corrupt line, the good line, and a truncated trailing line (partial write).
    p.write_text("{not json\n" + good + "\n" + '{"captured_at":2.0,"payload":{')
    recs = read_prose_records(_SID)
    assert len(recs) == 1
    assert recs[0].text == "ok"


def test_read_missing_file_returns_empty(cc_dir):
    assert read_prose_records("no-such-session") == []


def test_read_sorts_out_of_order_indices(cc_dir):
    _seed(
        cc_dir,
        _SID,
        [
            {
                "captured_at": 1.0,
                "payload": _md_payload(message_id="M1", index=1, final=True, delta="B"),
            },
            {
                "captured_at": 0.9,
                "payload": _md_payload(
                    message_id="M1", index=0, final=False, delta="A"
                ),
            },
        ],
    )
    recs = read_prose_records(_SID)
    assert recs[0].text == "AB"


def test_read_hashes_distinguish_raw_from_normalized(cc_dir):
    _seed(
        cc_dir,
        _SID,
        [
            {
                "captured_at": 1.0,
                "payload": _md_payload(
                    message_id="M1",
                    index=0,
                    final=True,
                    delta="line one  \r\nline two ",
                ),
            }
        ],
    )
    rec = read_prose_records(_SID)[0]
    # raw_hash is of the verbatim text; norm_hash is of normalize_prose(text).
    assert rec.raw_hash != rec.norm_hash
    import hashlib

    assert (
        rec.norm_hash
        == hashlib.sha256(normalize_prose(rec.text).encode("utf-8")).hexdigest()
    )


# ── Normalization contract ───────────────────────────────────────────────────


def test_normalize_crlf_to_lf():
    assert normalize_prose("a\r\nb\r\nc") == "a\nb\nc"
    assert normalize_prose("a\rb") == "a\nb"


def test_normalize_trims_trailing_whitespace_per_line_and_blank_edges():
    # Per-line trailing whitespace is trimmed; blank leading/trailing lines and
    # the first line's leading whitespace go via the whole-string strip — which
    # mirrors the JSONL parser's ``entry.text.strip()`` so the live text and the
    # post-resolution copy normalize to the same form (mint/validate parity).
    assert normalize_prose("  a  \n  b  \n\n") == "a\n  b"


def test_normalize_preserves_interior_whitespace_and_indentation():
    # No interior collapse — double spaces and INTERIOR-line indentation survive
    # (they distinguish genuinely different prose / markdown structure). Only
    # the very-first line's leading whitespace is removed by the edge strip.
    assert normalize_prose("a  b\n    indented") == "a  b\n    indented"
    assert normalize_prose("first\n    indented\n        deeper") == (
        "first\n    indented\n        deeper"
    )


def test_normalize_idempotent():
    once = normalize_prose("x \r\n y\r\n")
    assert normalize_prose(once) == once


# ── Lifecycle: teardown + gc ─────────────────────────────────────────────────


def test_teardown_removes_file_and_tolerates_missing(cc_dir):
    p = _seed(
        cc_dir,
        _SID,
        [
            {
                "captured_at": 1.0,
                "payload": _md_payload(message_id="M1", index=0, final=True, delta="x"),
            }
        ],
    )
    assert p.exists()
    md_capture.teardown_session(_SID)
    assert not p.exists()
    # Idempotent — a second teardown on a missing file does not raise.
    md_capture.teardown_session(_SID)


def test_gc_stale_removes_old_keeps_fresh(cc_dir):
    d = cc_dir / "msg_display"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    old = d / "old.ndjson"
    fresh = d / "fresh.ndjson"
    other = d / "keep.txt"  # not an ndjson — must be ignored
    for f in (old, fresh, other):
        f.write_text("{}\n")
    stale_mtime = time.time() - 7200
    os.utime(old, (stale_mtime, stale_mtime))
    removed = md_capture.gc_stale(max_age_seconds=3600)
    assert removed == 1
    assert not old.exists()
    assert fresh.exists()
    assert other.exists()


def test_gc_stale_missing_dir_returns_zero(cc_dir):
    assert md_capture.gc_stale() == 0


# ── gc_stale liveness gate (Item 3 / P2-2) ───────────────────────────────────
#
# A live picker's capture file (which also holds its shown_live/consumed dedup
# markers) must survive startup GC even when >max_age, or the post-resolution
# dedup double-posts. `is_live_session(stem)` is injected (md_capture stays a
# leaf); the predicate keys by the file stem (= the original session id).


def test_gc_stale_keeps_live_session_file(cc_dir):
    d = cc_dir / "msg_display"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    live = d / "live-sess.ndjson"
    dead = d / "dead-sess.ndjson"
    for f in (live, dead):
        f.write_text("{}\n")
    stale = time.time() - 7200
    for f in (live, dead):
        os.utime(f, (stale, stale))
    removed = md_capture.gc_stale(
        max_age_seconds=3600,
        is_live_session=lambda sid: sid == "live-sess",
    )
    assert removed == 1
    assert live.exists()
    assert not dead.exists()


def test_gc_stale_predicate_receives_stem(cc_dir):
    d = cc_dir / "msg_display"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    f = d / "session-xyz.ndjson"
    f.write_text("{}\n")
    stale = time.time() - 7200
    os.utime(f, (stale, stale))
    seen: list[str] = []

    def pred(sid: str) -> bool:
        seen.append(sid)
        return False

    md_capture.gc_stale(max_age_seconds=3600, is_live_session=pred)
    assert seen == ["session-xyz"]


def test_gc_stale_none_predicate_reaps(cc_dir):
    d = cc_dir / "msg_display"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    f = d / "s.ndjson"
    f.write_text("{}\n")
    stale = time.time() - 7200
    os.utime(f, (stale, stale))
    assert md_capture.gc_stale(max_age_seconds=3600, is_live_session=None) == 1
    assert not f.exists()


def test_gc_stale_predicate_exception_skips_reap(cc_dir):
    """A predicate that raises must NOT delete the file (conservative: skip on
    uncertainty) and must not abort the whole GC pass."""
    d = cc_dir / "msg_display"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    boom = d / "boom.ndjson"
    ok = d / "ok.ndjson"
    for f in (boom, ok):
        f.write_text("{}\n")
    stale = time.time() - 7200
    for f in (boom, ok):
        os.utime(f, (stale, stale))

    def pred(sid: str) -> bool:
        if sid == "boom":
            raise RuntimeError("predicate boom")
        return False

    removed = md_capture.gc_stale(max_age_seconds=3600, is_live_session=pred)
    assert boom.exists()  # raise → conservative skip
    assert not ok.exists()  # other file still processed
    assert removed == 1


def test_gc_stale_monitor_tracked_predicate_keeps_live_reaps_dead(cc_dir):
    """Integration: the bot.py predicate shape
    ``lambda sid: monitor.state.get_session(sid) is not None`` keeps a tracked
    (live AUQ OR EPM — the predicate is session-keyed, not prompt-typed)
    session's capture file and reaps an untracked one. Locks the wiring without
    standing up post_init."""
    from cctelegram.monitor_state import MonitorState, TrackedSession

    state = MonitorState(state_file=cc_dir / "monitor_state.json")
    state.update_session(
        TrackedSession(session_id="tracked-sess", file_path="/p/tracked.jsonl")
    )

    d = cc_dir / "msg_display"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    tracked = d / "tracked-sess.ndjson"  # stem == the tracked original session id
    untracked = d / "gone-sess.ndjson"
    for f in (tracked, untracked):
        f.write_text("{}\n")
    stale = time.time() - 7200
    for f in (tracked, untracked):
        os.utime(f, (stale, stale))

    removed = md_capture.gc_stale(
        max_age_seconds=3600,
        is_live_session=lambda sid: state.get_session(sid) is not None,
    )
    assert removed == 1
    assert tracked.exists()  # live session's file (+ its dedup markers) survives
    assert not untracked.exists()


def test_gc_stale_toctou_reskip_when_refreshed_before_unlink(cc_dir):
    """TOCTOU: if the file is refreshed (mtime advances within max_age) between
    the age check and unlink, it is NOT reaped. Simulated via a predicate that
    touches the file's mtime to 'now' as a side effect, then returns False."""
    d = cc_dir / "msg_display"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    f = d / "race.ndjson"
    f.write_text("{}\n")
    stale = time.time() - 7200
    os.utime(f, (stale, stale))

    def pred(sid: str) -> bool:
        now = time.time()
        os.utime(f, (now, now))  # concurrent append landed
        return False

    removed = md_capture.gc_stale(max_age_seconds=3600, is_live_session=pred)
    assert f.exists()  # re-stat before unlink sees fresh mtime → skip
    assert removed == 0


# ── Capture settings ─────────────────────────────────────────────────────────


def test_ensure_capture_settings_registers_message_display_hook(cc_dir):
    path = md_capture.ensure_capture_settings()
    assert path.exists()
    data = json.loads(path.read_text())
    entries = data["hooks"]["MessageDisplay"]
    assert len(entries) == 1
    cmd = entries[0]["hooks"][0]["command"]
    # The hook command points at the appender (run by an interpreter), not at
    # the heavy `cc-telegram hook` entrypoint.
    assert str(appender_path()) in cmd
    assert "cc-telegram hook" not in cmd
    assert md_capture.capture_settings_has_message_display() is True


def test_ensure_capture_settings_idempotent_no_rewrite(cc_dir):
    path = md_capture.ensure_capture_settings()
    mtime1 = path.stat().st_mtime_ns
    time.sleep(0.01)
    md_capture.ensure_capture_settings()
    assert path.stat().st_mtime_ns == mtime1, "current settings must not be rewritten"


def test_capture_settings_has_message_display_false_when_absent(cc_dir):
    assert md_capture.capture_settings_has_message_display() is False


# ── Launch command composition ───────────────────────────────────────────────


def test_compose_launch_command_base_only():
    assert _compose_launch_command("claude", "", None) == "claude"


def test_compose_launch_command_with_settings_quoted():
    cmd = _compose_launch_command("claude", "/cfg/md hook.json", None)
    assert cmd == "claude --settings '/cfg/md hook.json'"
    # shlex round-trips to the right argv (the path stays one token).
    import shlex

    assert shlex.split(cmd) == ["claude", "--settings", "/cfg/md hook.json"]


def test_compose_launch_command_with_settings_and_resume():
    cmd = _compose_launch_command("claude", "/cfg/s.json", "sess-123")
    import shlex

    assert shlex.split(cmd) == [
        "claude",
        "--settings",
        "/cfg/s.json",
        "--resume",
        "sess-123",
    ]


def test_compose_launch_command_resume_only_when_no_settings():
    assert _compose_launch_command("claude", "", "abc") == "claude --resume abc"


def test_compose_launch_command_preserves_base_flags():
    cmd = _compose_launch_command(
        "claude --dangerously-skip-permissions", "/s.json", None
    )
    assert cmd == "claude --dangerously-skip-permissions --settings /s.json"


# ── Latency gate (F4: forceSyncExecution budget) ─────────────────────────────


@pytest.mark.benchmark
def test_appender_latency_negligible_over_bare_interpreter(cc_dir, capsys):
    """The appender must add negligible cost over a bare interpreter start —
    that is the whole reason it is NOT the heavy ``cc-telegram hook`` entry.
    Measuring the DELTA over ``python -c pass`` cancels interpreter-startup
    variance, so the gate is stable across machines / CI load."""
    payload = json.dumps(
        _md_payload(message_id="M1", index=0, final=True, delta="x" * 512)
    )
    env = {**os.environ, "CC_TELEGRAM_DIR": str(cc_dir)}
    n = 25

    def _median(samples: list[float]) -> float:
        s = sorted(samples)
        return s[len(s) // 2]

    base, app = [], []
    for _ in range(n):
        t0 = time.perf_counter()
        subprocess.run([sys.executable, "-c", "pass"], capture_output=True, env=env)
        base.append(time.perf_counter() - t0)
        t0 = time.perf_counter()
        subprocess.run(
            [sys.executable, str(appender_path())],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
        )
        app.append(time.perf_counter() - t0)

    delta_ms = (_median(app) - _median(base)) * 1000.0
    with capsys.disabled():
        print(
            f"\n[md_capture] appender median={_median(app) * 1000:.1f}ms "
            f"baseline={_median(base) * 1000:.1f}ms delta={delta_ms:.1f}ms"
        )
    # The script's own work (read stdin, json round-trip, one append) is a few
    # ms; the I/O it does contends more than ``python -c pass`` under load, so
    # the delta inflates on a busy box. This gate guards against a GROSS
    # regression — e.g. importing the package (~50ms+) — not micro jitter, so
    # the threshold has generous headroom over the observed ~15-40ms.
    assert delta_ms < 60.0, f"appender adds {delta_ms:.1f}ms over bare interpreter"


def test_prose_record_is_frozen():
    rec = ProseRecord(
        session_id=_SID,
        transcript_path="x",
        md_message_id="M1",
        text="t",
        raw_hash="r",
        norm_hash="n",
        first_seen_at=1.0,
        final_at=2.0,
    )
    with pytest.raises(Exception):
        rec.text = "mutated"  # type: ignore[misc]
