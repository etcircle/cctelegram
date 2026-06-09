"""Unit tests for the AUQ action ledger.

Covers:
  - State-transition correctness across all 5 persisted states.
  - First-write field-required validation.
  - Latest-line-wins reload semantics.
  - Corrupt-line tolerance (skip with warning, keep parsing).
  - Duplicate-state-line idempotency.
  - LRU compaction at startup.
  - Pure ``lookup()`` — does NOT classify owner / collision (that lives
    in the callback handler per the v4 §7.2 contract).
  - Injectable clock + path so we never touch real disk or wall-time.

The ledger is module-level singleton state; every test calls
``reset_for_tests(path=..., now=..., start_time=...)`` in a fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cctelegram.handlers import auq_ledger


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "auq_action_ledger.jsonl"


@pytest.fixture
def clock():
    """Mutable wall-clock — tests advance it explicitly."""

    class _Clock:
        t: float = 1000.0

        def __call__(self) -> float:
            return self.t

        def tick(self, delta: float = 1.0) -> None:
            self.t += delta

    return _Clock()


@pytest.fixture
def setup_ledger(ledger_path: Path, clock):
    """Wire the module to the tmp path + injected clock; reset at end."""
    auq_ledger.reset_for_tests(path=ledger_path, now=clock, start_time=clock())
    yield
    auq_ledger.reset_for_tests()


def _first_write_kwargs(**overrides):
    base = dict(
        state="accepted",
        user_id=42,
        window_id="@7",
        full_fingerprint="ff" * 20,
        option_number=2,
        option_label="alpha",
    )
    base.update(overrides)
    return base


class TestRecordFirstWrite:
    def test_first_record_succeeds_and_returns_entry(self, setup_ledger):
        entry = auq_ledger.record("rh:fp:2", **_first_write_kwargs())
        assert entry.key == "rh:fp:2"
        assert entry.state == "accepted"
        assert entry.user_id == 42
        assert entry.window_id == "@7"
        assert entry.option_number == 2
        assert entry.option_label == "alpha"
        assert entry.accepted_at == 1000.0
        assert entry.digit_sent_at is None
        assert entry.dispatched_at is None
        assert entry.failed_reason is None

    def test_first_record_requires_identity_fields(self, setup_ledger):
        with pytest.raises(ValueError, match="First record"):
            auq_ledger.record("rh:fp:2", state="accepted", user_id=42)

    def test_invalid_state_raises(self, setup_ledger):
        with pytest.raises(ValueError, match="Invalid ledger state"):
            auq_ledger.record(
                "rh:fp:2",
                state="bogus",  # type: ignore[arg-type]
                user_id=1,
                window_id="@1",
                full_fingerprint="ff",
                option_number=1,
                option_label="x",
            )

    def test_first_write_can_directly_set_digit_sent(self, setup_ledger):
        # Edge: caller can skip 'accepted' on the very first write. This is
        # a defensive contract — handler always writes 'accepted' first in
        # practice, but the module must not corrupt state if it doesn't.
        entry = auq_ledger.record("rh:fp:2", **_first_write_kwargs(state="digit_sent"))
        assert entry.state == "digit_sent"
        assert entry.accepted_at == 1000.0
        assert entry.digit_sent_at == 1000.0


class TestStateTransitions:
    def test_accepted_then_digit_sent_then_dispatched(self, setup_ledger, clock):
        auq_ledger.record("k:1", **_first_write_kwargs())
        clock.tick(0.5)
        e2 = auq_ledger.record("k:1", state="digit_sent")
        clock.tick(0.5)
        e3 = auq_ledger.record("k:1", state="dispatched")

        assert e2.state == "digit_sent"
        assert e2.accepted_at == 1000.0  # preserved from first write
        assert e2.digit_sent_at == 1000.5
        assert e2.dispatched_at is None

        assert e3.state == "dispatched"
        assert e3.accepted_at == 1000.0
        assert e3.digit_sent_at == 1000.5
        assert e3.dispatched_at == 1001.0

    def test_failed_before_digit_terminal(self, setup_ledger, clock):
        auq_ledger.record("k:1", **_first_write_kwargs())
        clock.tick(0.1)
        entry = auq_ledger.record(
            "k:1", state="failed_before_digit", failed_reason="tmux gone"
        )
        assert entry.state == "failed_before_digit"
        assert entry.digit_sent_at is None
        assert entry.dispatched_at is None
        assert entry.failed_reason == "tmux gone"

    def test_failed_after_digit_preserves_digit_sent_at(self, setup_ledger, clock):
        auq_ledger.record("k:1", **_first_write_kwargs())
        clock.tick(0.1)
        auq_ledger.record("k:1", state="digit_sent")
        clock.tick(0.4)
        entry = auq_ledger.record(
            "k:1", state="failed_after_digit", failed_reason="enter raised"
        )
        assert entry.state == "failed_after_digit"
        assert entry.digit_sent_at == 1000.1
        assert entry.dispatched_at is None
        assert entry.failed_reason == "enter raised"


class TestLookupAndReload:
    def test_lookup_none_returns_none(self, setup_ledger):
        assert auq_ledger.lookup(None) is None
        assert auq_ledger.lookup("not-here") is None

    def test_lookup_returns_latest_entry_per_key(self, setup_ledger, clock):
        auq_ledger.record("k:1", **_first_write_kwargs())
        clock.tick(0.5)
        auq_ledger.record("k:1", state="digit_sent")
        clock.tick(0.5)
        auq_ledger.record("k:1", state="dispatched")
        latest = auq_ledger.lookup("k:1")
        assert latest is not None
        assert latest.state == "dispatched"

    def test_reload_picks_up_latest_line_wins(self, setup_ledger, clock, ledger_path):
        auq_ledger.record("k:1", **_first_write_kwargs())
        clock.tick(0.5)
        auq_ledger.record("k:1", state="digit_sent")
        clock.tick(0.5)
        auq_ledger.record("k:1", state="dispatched")

        # Simulate process restart by clearing in-memory + reloading from
        # the same path (different clock — restart preserves disk state).
        auq_ledger.reset_for_tests(path=ledger_path, now=clock, start_time=clock())
        loaded = auq_ledger.lookup("k:1")
        assert loaded is not None
        assert loaded.state == "dispatched"
        assert loaded.accepted_at == 1000.0
        assert loaded.digit_sent_at == 1000.5
        assert loaded.dispatched_at == 1001.0

    def test_duplicate_terminal_state_is_idempotent_on_reload(
        self, setup_ledger, clock, ledger_path
    ):
        # Same key, same terminal state written twice. After reload only
        # the latest line wins — shape is preserved.
        auq_ledger.record("k:1", **_first_write_kwargs(state="dispatched"))
        clock.tick(0.5)
        auq_ledger.record("k:1", state="dispatched")
        auq_ledger.reset_for_tests(path=ledger_path, now=clock, start_time=clock())
        loaded = auq_ledger.lookup("k:1")
        assert loaded is not None
        assert loaded.state == "dispatched"


class TestCorruptLineTolerance:
    def test_skips_garbage_lines_in_middle(
        self, setup_ledger, clock, ledger_path, caplog
    ):
        auq_ledger.record("k:1", **_first_write_kwargs())
        # Manually append a corrupt line + a valid second-key line.
        with open(ledger_path, "a", encoding="utf-8") as f:
            f.write("this is not json at all\n")
            f.write(
                '{"key": "k:2", "state": "dispatched", "user_id": 99}\n'
            )  # incomplete
            f.write(
                json.dumps(
                    {
                        "key": "k:3",
                        "state": "accepted",
                        "user_id": 7,
                        "window_id": "@9",
                        "full_fingerprint": "ee" * 20,
                        "option_number": 1,
                        "option_label": "good",
                        "accepted_at": 1234.0,
                        "digit_sent_at": None,
                        "dispatched_at": None,
                        "failed_reason": None,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
        auq_ledger.reset_for_tests(path=ledger_path, now=clock, start_time=clock())
        # Original k:1 survives; corrupt k:2 line is skipped; k:3 loads.
        assert auq_ledger.lookup("k:1") is not None
        assert auq_ledger.lookup("k:2") is None
        loaded = auq_ledger.lookup("k:3")
        assert loaded is not None
        assert loaded.option_label == "good"
        assert any("corrupt line" in rec.message for rec in caplog.records)

    def test_skips_unknown_state(self, setup_ledger, clock, ledger_path, caplog):
        with open(ledger_path, "w", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "key": "k:1",
                        "state": "made_up_state",
                        "user_id": 1,
                        "window_id": "@1",
                        "full_fingerprint": "aa",
                        "option_number": 1,
                        "option_label": "x",
                        "accepted_at": 1.0,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
        auq_ledger.reset_for_tests(path=ledger_path, now=clock, start_time=clock())
        assert auq_ledger.lookup("k:1") is None
        assert any("unknown state" in rec.message for rec in caplog.records)


class TestLRUCompaction:
    def test_compaction_at_startup_when_over_cap(self, monkeypatch, ledger_path, clock):
        # Force a small cap so the test is cheap.
        monkeypatch.setattr(auq_ledger, "LRU_CAP", 5)
        auq_ledger.reset_for_tests(path=ledger_path, now=clock, start_time=clock())
        # Write 10 entries spread over time so accepted_at differs.
        for i in range(10):
            clock.tick(1.0)
            auq_ledger.record(f"k:{i}", **_first_write_kwargs(option_number=i + 1))
        # Reload — startup compaction trims to the 5 most-recent-per-key.
        auq_ledger.reset_for_tests(path=ledger_path, now=clock, start_time=clock())
        survivors = [auq_ledger.lookup(f"k:{i}") for i in range(10)]
        present = [s for s in survivors if s is not None]
        # The 5 newest keys (k:5..k:9) survive.
        assert len(present) == 5
        kept_keys = {e.key for e in present}
        assert kept_keys == {"k:5", "k:6", "k:7", "k:8", "k:9"}

    def test_compaction_drops_entries_older_than_retention(
        self, monkeypatch, ledger_path, clock
    ):
        monkeypatch.setattr(auq_ledger, "LRU_CAP", 100)  # not LRU-bound here
        monkeypatch.setattr(auq_ledger, "RETENTION_SECONDS", 100.0)
        # Seed an old + a new entry then force compaction by exceeding LRU
        # cap (LRU_CAP=2 forces it).
        auq_ledger.reset_for_tests(path=ledger_path, now=clock, start_time=clock())
        auq_ledger.record("old", **_first_write_kwargs())  # accepted_at=1000
        clock.tick(1000.0)  # advance well past retention
        auq_ledger.record("new", **_first_write_kwargs(option_number=3))
        monkeypatch.setattr(auq_ledger, "LRU_CAP", 1)
        auq_ledger.reset_for_tests(path=ledger_path, now=clock, start_time=clock())
        assert auq_ledger.lookup("old") is None  # dropped by retention
        assert auq_ledger.lookup("new") is not None


class TestProcessStartTimeProjection:
    """Callers use process_start_time() to decide whether a pre-process-
    start accepted/digit_sent entry should be projected to ``unknown``.
    """

    def test_process_start_time_is_stable_within_a_run(self, setup_ledger):
        a = auq_ledger.process_start_time()
        b = auq_ledger.process_start_time()
        assert a == b

    def test_reset_for_tests_can_set_start_time(self, ledger_path, clock):
        auq_ledger.reset_for_tests(path=ledger_path, now=clock, start_time=12345.0)
        assert auq_ledger.process_start_time() == 12345.0

    def test_caller_projects_pre_start_entries_to_unknown(self, ledger_path, clock):
        # Simulate: prior process wrote an `accepted` entry, then crashed;
        # current process started later, so the entry's accepted_at is
        # before process_start_time.
        auq_ledger.reset_for_tests(path=ledger_path, now=clock, start_time=clock())
        auq_ledger.record("k:1", **_first_write_kwargs())
        # Bump start time forward (simulates restart).
        auq_ledger.reset_for_tests(
            path=ledger_path,
            now=clock,
            start_time=clock() + 100.0,
        )
        entry = auq_ledger.lookup("k:1")
        assert entry is not None
        assert entry.state == "accepted"  # raw row unchanged
        # The caller's projection rule — exercised in callback handler
        # tests — applies HERE; this test just confirms the inputs.
        assert entry.accepted_at < auq_ledger.process_start_time()


def _raw_line(key: str, state: str, accepted_at: float, **overrides) -> str:
    """Hand-write one JSONL line so tests control line ORDER + timestamps
    independently of record()'s merge semantics."""
    payload = {
        "key": key,
        "state": state,
        "user_id": 42,
        "window_id": "@7",
        "full_fingerprint": "ff" * 20,
        "option_number": 2,
        "option_label": "alpha",
        "accepted_at": accepted_at,
        "digit_sent_at": None,
        "dispatched_at": None,
        "failed_reason": None,
    }
    payload.update(overrides)
    return json.dumps(payload, separators=(",", ":")) + "\n"


class TestRetentionOnRead:
    """Wave 2 fix 3a — retention is enforced on READ (load + lookup), not
    only inside the over-LRU-cap compaction rewrite. Latest-wins-AWARE:
    collapse to the latest row per key FIRST, then apply the cutoff to that
    latest row; an expired latest row drops the KEY entirely and must never
    resurrect an older row (Hermes R1 P2-2)."""

    def test_expired_latest_row_drops_key_at_load(self, ledger_path, clock):
        clock.t = 200_000.0
        cutoff_age = auq_ledger.RETENTION_SECONDS
        with open(ledger_path, "w", encoding="utf-8") as f:
            f.write(_raw_line("k:1", "dispatched", clock() - cutoff_age - 10.0))
        auq_ledger.reset_for_tests(path=ledger_path, now=clock, start_time=clock())
        assert auq_ledger.lookup("k:1") is None, (
            "an expired latest row must drop the key at load — without this "
            "the '24h durable' contract is infinitely durable"
        )

    def test_expired_latest_never_resurrects_older_in_retention_row(
        self, ledger_path, clock
    ):
        """The killer ordering case: line 1 (older) is IN retention, line 2
        (latest, same key) is EXPIRED. A retention filter applied per-line
        BEFORE the latest-wins collapse would drop line 2 and resurrect
        line 1's `dispatched` — re-locking the key. Correct: collapse first,
        latest is expired, the KEY drops entirely."""
        clock.t = 200_000.0
        with open(ledger_path, "w", encoding="utf-8") as f:
            # Older line: recent accepted_at (in retention), dispatched.
            f.write(_raw_line("k:1", "dispatched", clock() - 10.0))
            # Latest line: expired accepted_at.
            f.write(
                _raw_line(
                    "k:1",
                    "accepted",
                    clock() - auq_ledger.RETENTION_SECONDS - 10.0,
                )
            )
        auq_ledger.reset_for_tests(path=ledger_path, now=clock, start_time=clock())
        assert auq_ledger.lookup("k:1") is None, (
            "an expired LATEST row must drop the key — never resurrect an "
            "older row for the same key"
        )

    def test_in_retention_latest_survives_load(self, ledger_path, clock):
        clock.t = 200_000.0
        with open(ledger_path, "w", encoding="utf-8") as f:
            f.write(
                _raw_line(
                    "k:1",
                    "dispatched",
                    clock() - auq_ledger.RETENTION_SECONDS - 10.0,
                )
            )
            f.write(_raw_line("k:1", "dispatched", clock() - 10.0))
        auq_ledger.reset_for_tests(path=ledger_path, now=clock, start_time=clock())
        entry = auq_ledger.lookup("k:1")
        assert entry is not None
        assert entry.state == "dispatched"

    def test_lookup_returns_none_when_in_memory_entry_ages_out(
        self, setup_ledger, clock, ledger_path
    ):
        """A process running >24h: the in-memory map was loaded before the
        cutoff. lookup() must apply the cutoff itself (the entry may be
        evicted from memory, but the FILE is never rewritten on read)."""
        auq_ledger.record("k:1", **_first_write_kwargs(state="dispatched"))
        before = ledger_path.read_text()
        clock.tick(auq_ledger.RETENTION_SECONDS + 1.0)
        assert auq_ledger.lookup("k:1") is None, (
            "lookup must enforce retention for a long-running process whose "
            "in-memory map predates the cutoff"
        )
        assert ledger_path.read_text() == before, (
            "retention-on-read must never rewrite the file"
        )


class TestReleasedTombstone:
    """Wave 2 fix 3b — `released` tombstone at AUQ resolution closes the
    same-day identical-AUQ collision (the content-derived key reconstructs
    the same triplet; a stale `dispatched` row answered 'Action already
    received' forever)."""

    def test_release_window_makes_lookup_none(self, setup_ledger):
        auq_ledger.record("k:1", **_first_write_kwargs(state="dispatched"))
        count = auq_ledger.release_window("@7")
        assert count == 1
        assert auq_ledger.lookup("k:1") is None

    def test_release_window_scopes_by_window(self, setup_ledger):
        """Window-scoped, NEVER session-scoped — a double-`--resume` sibling
        window's unresolved card must keep its rows (Hermes Q1)."""
        auq_ledger.record("k:1", **_first_write_kwargs(state="dispatched"))
        auq_ledger.record(
            "k:2", **_first_write_kwargs(state="dispatched", window_id="@8")
        )
        count = auq_ledger.release_window("@7")
        assert count == 1
        assert auq_ledger.lookup("k:1") is None
        sibling = auq_ledger.lookup("k:2")
        assert sibling is not None and sibling.state == "dispatched"

    def test_released_round_trips_restart(self, setup_ledger, clock, ledger_path):
        """`released` must be in _PERSISTED_STATES — unknown states are
        skipped on load, so without it the tombstone is a restart no-op
        (Hermes R1 P2-2)."""
        auq_ledger.record("k:1", **_first_write_kwargs(state="dispatched"))
        auq_ledger.release_window("@7")
        # Simulate restart: reload from disk.
        auq_ledger.reset_for_tests(path=ledger_path, now=clock, start_time=clock())
        assert auq_ledger.lookup("k:1") is None, (
            "a released row must survive restart (persisted-state parse) "
            "and lookup must treat it as None"
        )

    def test_same_key_reask_after_release_is_dispatchable(self, setup_ledger, clock):
        """A byte-identical re-asked AUQ reconstructs the same key; after
        the prior instance resolved + released, the fresh pick must be able
        to claim the key again."""
        auq_ledger.record("k:1", **_first_write_kwargs(state="dispatched"))
        auq_ledger.release_window("@7")
        clock.tick(60.0)
        entry = auq_ledger.record("k:1", state="accepted")
        assert entry.state == "accepted"
        live = auq_ledger.lookup("k:1")
        assert live is not None and live.state == "accepted"

    def test_fresh_accept_over_released_replaces_identity(self, setup_ledger, clock):
        """Hermes P3-1 — a fresh `accepted` over a `released` latest entry is
        a NEW instance reusing the content-derived key (same-day re-ask or a
        rare fp8 collision). The new write's identifying fields must replace
        the dead instance's, not inherit them — otherwise diagnostics for a
        collision carry the wrong window/user/fingerprint."""
        auq_ledger.record("k:1", **_first_write_kwargs(state="dispatched"))
        auq_ledger.release_window("@7")
        clock.tick(60.0)
        entry = auq_ledger.record(
            "k:1",
            **_first_write_kwargs(
                state="accepted",
                user_id=77,
                window_id="@9",
                full_fingerprint="ab" * 20,
                option_number=3,
                option_label="beta",
            ),
        )
        assert entry.user_id == 77
        assert entry.window_id == "@9"
        assert entry.full_fingerprint == "ab" * 20
        assert entry.option_number == 3
        assert entry.option_label == "beta"

    def test_fresh_accept_over_released_without_fields_inherits(
        self, setup_ledger, clock
    ):
        """Backward-compat pin for the field-less re-accept (the
        test_same_key_reask_after_release_is_dispatchable shape): with no
        provided identity, inheritance still applies."""
        auq_ledger.record("k:1", **_first_write_kwargs(state="dispatched"))
        auq_ledger.release_window("@7")
        clock.tick(60.0)
        entry = auq_ledger.record("k:1", state="accepted")
        assert entry.window_id == "@7"
        assert entry.user_id == 42

    def test_non_released_merge_keeps_strict_inheritance(self, setup_ledger):
        """The P3-1 replacement is scoped to accepted-over-released ONLY —
        an ordinary state transition ignores any provided identity fields."""
        auq_ledger.record("k:1", **_first_write_kwargs())
        entry = auq_ledger.record(
            "k:1",
            state="dispatched",
            user_id=99,
            window_id="@99",
        )
        assert entry.user_id == 42
        assert entry.window_id == "@7"

    def test_release_window_is_idempotent(self, setup_ledger):
        auq_ledger.record("k:1", **_first_write_kwargs(state="dispatched"))
        assert auq_ledger.release_window("@7") == 1
        assert auq_ledger.release_window("@7") == 0

    def test_release_window_skips_out_of_retention_rows(self, setup_ledger, clock):
        auq_ledger.record("k:1", **_first_write_kwargs(state="dispatched"))
        clock.tick(auq_ledger.RETENTION_SECONDS + 1.0)
        assert auq_ledger.release_window("@7") == 0

    def test_release_window_no_rows_returns_zero(self, setup_ledger):
        assert auq_ledger.release_window("@99") == 0


class TestRecordRefreshesAcceptedAt:
    """Wave 2 fix 13 — a later `accepted` write must refresh accepted_at,
    else a fresh in-flight dispatch on a key with pre-restart history
    projects to `unknown` and a concurrent duplicate tap re-renders
    mid-dispatch instead of holding."""

    def test_re_accept_refreshes_accepted_at(self, setup_ledger, clock):
        auq_ledger.record("k:1", **_first_write_kwargs())  # accepted @1000
        clock.tick(500.0)
        entry = auq_ledger.record("k:1", state="accepted")
        assert entry.accepted_at == clock(), (
            "a later `accepted` write must stamp accepted_at=now so the "
            "in-progress lock holds against process_start_time projection"
        )

    def test_non_accepted_write_preserves_accepted_at(self, setup_ledger, clock):
        auq_ledger.record("k:1", **_first_write_kwargs())
        clock.tick(500.0)
        entry = auq_ledger.record("k:1", state="dispatched")
        assert entry.accepted_at == 1000.0
