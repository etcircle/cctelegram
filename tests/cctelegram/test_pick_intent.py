"""Unit coverage for the D2 durable pick mint-intent store (``pick_intent``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cctelegram.handlers import pick_intent

_FP_A = "a" * 16
_FP_B = "b" * 16


def _spec(
    token: str, n: int, label: str = "L", submit: bool = False
) -> pick_intent.TokenSpec:
    return pick_intent.TokenSpec(
        token=token, option_number=n, option_label=label, is_review_submit=submit
    )


def _record(
    tmp: Path,
    *,
    fp: str = _FP_A,
    window: str = "@1",
    user: int = 7,
    thread: int | None = 42,
    session: str | None = "sess-1",
    minted_at: float = 1000.0,
    specs: list[pick_intent.TokenSpec] | None = None,
) -> None:
    pick_intent.record_row(
        full_fingerprint=fp,
        source_kind="side_file",
        source_fingerprint="sfp",
        user_id=user,
        thread_id=thread,
        window_id=window,
        session_id=session,
        minted_at=minted_at,
        token_specs=specs
        or [
            _spec("aaaaaaaaaaaa", 1),
            _spec("bbbbbbbbbbbb", 2),
            _spec("cccccccccccc", 3),
        ],
    )


@pytest.fixture(autouse=True)
def _scope(tmp_path: Path):
    pick_intent.reset_for_tests(path=tmp_path / "pick_intent.jsonl", now=lambda: 1000.0)
    yield
    pick_intent.reset_for_tests()


def test_record_and_lookup_roundtrip(tmp_path: Path) -> None:
    _record(tmp_path)
    intent = pick_intent.lookup_intent("bbbbbbbbbbbb")
    assert intent is not None
    assert intent.option_number == 2
    assert intent.full_fingerprint == _FP_A
    assert intent.window_id == "@1"
    assert intent.session_id == "sess-1"
    assert intent.sibling_option_numbers == (1, 2, 3)
    assert intent.sibling_tokens == ("aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc")


def test_survives_simulated_restart(tmp_path: Path) -> None:
    _record(tmp_path)
    # Simulate a process restart: wipe in-memory, keep the on-disk file.
    pick_intent.reset_for_tests(path=tmp_path / "pick_intent.jsonl", now=lambda: 1000.0)
    intent = pick_intent.lookup_intent("bbbbbbbbbbbb")
    assert intent is not None and intent.option_number == 2


def test_consume_row_tombs_all_siblings(tmp_path: Path) -> None:
    _record(tmp_path)
    pick_intent.consume_row("bbbbbbbbbbbb")
    for tok in ("aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"):
        assert pick_intent.lookup_intent(tok) is None
    # survives restart (tomb persisted)
    pick_intent.reset_for_tests(path=tmp_path / "pick_intent.jsonl", now=lambda: 1000.0)
    assert pick_intent.lookup_intent("aaaaaaaaaaaa") is None


def test_supersede_only_different_fingerprint(tmp_path: Path) -> None:
    _record(tmp_path, fp=_FP_A, specs=[_spec("aaaaaaaaaaaa", 1)])
    # Identical re-render (same fp, new token) must NOT tomb the old token.
    _record(tmp_path, fp=_FP_A, specs=[_spec("dddddddddddd", 1)])
    assert pick_intent.lookup_intent("aaaaaaaaaaaa") is not None
    assert pick_intent.lookup_intent("dddddddddddd") is not None
    # A genuinely different card (different fp) supersedes the prior row.
    _record(tmp_path, fp=_FP_B, specs=[_spec("eeeeeeeeeeee", 1)])
    assert pick_intent.lookup_intent("aaaaaaaaaaaa") is None
    assert pick_intent.lookup_intent("dddddddddddd") is None
    assert pick_intent.lookup_intent("eeeeeeeeeeee") is not None


def test_teardown_window(tmp_path: Path) -> None:
    _record(tmp_path, window="@1", specs=[_spec("aaaaaaaaaaaa", 1)])
    _record(tmp_path, window="@2", specs=[_spec("bbbbbbbbbbbb", 1)])
    pick_intent.teardown_window("@1")
    assert pick_intent.lookup_intent("aaaaaaaaaaaa") is None
    assert pick_intent.lookup_intent("bbbbbbbbbbbb") is not None


def test_retention_drops_old_rows(tmp_path: Path) -> None:
    _record(tmp_path, minted_at=1000.0)
    # Advance "now" well past retention; the row must no longer be recoverable.
    pick_intent.reset_for_tests(
        path=tmp_path / "pick_intent.jsonl",
        now=lambda: 1000.0 + pick_intent.RETENTION_SECONDS + 1.0,
    )
    assert pick_intent.lookup_intent("bbbbbbbbbbbb") is None


def test_corrupt_trailing_line_skipped(tmp_path: Path) -> None:
    _record(tmp_path)
    path = tmp_path / "pick_intent.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"v":1,"full_fingerprint":"aaaa","tokens":[  <<torn\n')
    pick_intent.reset_for_tests(path=path, now=lambda: 1000.0)
    # The good row still loads; the torn line is skipped (no crash).
    assert pick_intent.lookup_intent("bbbbbbbbbbbb") is not None


def test_malformed_row_rejected_whole(tmp_path: Path) -> None:
    path = tmp_path / "pick_intent.jsonl"
    # option_number out of range → the whole row is rejected (untrusted-on-read).
    bad = {
        "v": 1,
        "full_fingerprint": _FP_A,
        "source_kind": "side_file",
        "source_fingerprint": "sfp",
        "user_id": 7,
        "thread_id": 42,
        "window_id": "@1",
        "session_id": "s",
        "minted_at": 1000.0,
        "tokens": [{"t": "aaaaaaaaaaaa", "n": 99, "label": "L", "submit": False}],
    }
    path.write_text(json.dumps(bad) + "\n")
    pick_intent.reset_for_tests(path=path, now=lambda: 1000.0)
    assert pick_intent.lookup_intent("aaaaaaaaaaaa") is None


def test_bad_token_shape_rejected(tmp_path: Path) -> None:
    path = tmp_path / "pick_intent.jsonl"
    bad = {
        "v": 1,
        "full_fingerprint": _FP_A,
        "source_kind": "side_file",
        "source_fingerprint": "sfp",
        "user_id": 7,
        "thread_id": None,
        "window_id": "@1",
        "session_id": None,
        "minted_at": 1000.0,
        "tokens": [{"t": "NOTHEX", "n": 1, "label": "L", "submit": False}],
    }
    path.write_text(json.dumps(bad) + "\n")
    pick_intent.reset_for_tests(path=path, now=lambda: 1000.0)
    assert pick_intent.lookup_intent("NOTHEX") is None


def test_compaction_preserves_live_rows(tmp_path: Path) -> None:
    path = tmp_path / "pick_intent.jsonl"
    pick_intent.reset_for_tests(path=path, now=lambda: 1000.0)
    # Force many lines so the load-time compaction trips.
    original_cap = pick_intent.LRU_CAP
    pick_intent.LRU_CAP = 3
    try:
        _record(tmp_path, fp=_FP_A, specs=[_spec("aaaaaaaaaaaa", 1)])
        _record(
            tmp_path, fp=_FP_B, specs=[_spec("bbbbbbbbbbbb", 1)]
        )  # supersede tomb + row
        _record(tmp_path, fp="c" * 16, specs=[_spec("cccccccccccc", 1)])
        _record(tmp_path, fp="d" * 16, specs=[_spec("dddddddddddd", 1)])
        pick_intent.reset_for_tests(path=path, now=lambda: 1000.0)  # triggers _compact
        # Only the latest live row survives (each different-fp record superseded prior).
        assert pick_intent.lookup_intent("dddddddddddd") is not None
        assert pick_intent.lookup_intent("aaaaaaaaaaaa") is None
    finally:
        pick_intent.LRU_CAP = original_cap


class TestDiskFailureGraceful:
    """Finding 24: disk-write failures must not raise into the live render.

    ``record_row`` is called inside the AUQ picker render path
    (``interactive_ui``); an OSError (disk full / read-only config dir) must
    degrade to a logged warning + no-op — losing only restart-recovery, never
    the picker. Same posture for the other public writers (``consume_row``,
    ``teardown_window``) and the lazy-load ``_compact``.
    """

    def _blocked_path(self, tmp_path: Path) -> Path:
        # Parent is a regular FILE → ``mkdir(parents=True, exist_ok=True)``
        # and ``os.open`` both raise OSError.
        blocker = tmp_path / "blocker"
        blocker.write_text("")
        return blocker / "pick_intent.jsonl"

    def test_record_row_disk_failure_does_not_raise(self, tmp_path: Path) -> None:
        pick_intent.reset_for_tests(
            path=self._blocked_path(tmp_path), now=lambda: 1000.0
        )
        _record(tmp_path)  # must not raise

    def test_consume_and_teardown_disk_failure_does_not_raise(
        self, tmp_path: Path
    ) -> None:
        pick_intent.reset_for_tests(
            path=self._blocked_path(tmp_path), now=lambda: 1000.0
        )
        _record(tmp_path)
        pick_intent.consume_row("aaaaaaaaaaaa")  # must not raise
        pick_intent.teardown_window("@1")  # must not raise

    def test_lazy_load_compact_oserror_does_not_raise(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        path = tmp_path / "pick_intent.jsonl"
        pick_intent.reset_for_tests(path=path, now=lambda: 1000.0)
        _record(tmp_path)
        # Force the lazy-load compaction branch and make compaction fail.
        pick_intent.reset_for_tests(path=path, now=lambda: 1000.0)
        monkeypatch.setattr(pick_intent, "LRU_CAP", 0)

        def _boom() -> None:
            raise OSError("disk full")

        monkeypatch.setattr(pick_intent, "_compact", _boom)
        # Lazy load (triggered by any public read) must swallow the OSError.
        intent = pick_intent.lookup_intent("aaaaaaaaaaaa")
        assert intent is not None  # the in-memory replay still succeeded
