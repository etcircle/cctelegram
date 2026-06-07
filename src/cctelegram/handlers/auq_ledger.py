"""Restart-safe write-ahead ledger for AskUserQuestion option-pick dispatches.

Records every option-pick callback's lifecycle (``accepted`` →
``dispatched``, or the ``not_advanced`` / ``commit_unconfirmed`` /
``failed_before_digit`` non-success states) so the callback handler can
detect duplicate taps even after a process restart. The in-memory pick-token
store (``pick_token._pick_tokens``) does not survive restart; this ledger does.

v2.1.168 navigate-to-target + Enter model: a pick no longer trusts a sent
keystroke to mean "committed". The dispatch path (``_navigate_and_commit``)
arrow-navigates the live cursor to the tapped option, VERIFIES the cursor
landed there, presses ``Enter`` (the version-stable commit), re-parses the
pane, and records ``dispatched`` ONLY after a confirmed expected advance.
The non-success states:

  - ``not_advanced``       — a PRE-COMMIT bail (``Enter`` provably never sent:
                             cursor unknown, a nav send returned False, or the
                             post-nav verify failed). Nothing was committed, so
                             the callback handler FALLS THROUGH on a re-tap (a
                             fresh-token tap re-validates against the live form).
  - ``commit_unconfirmed`` — ``Enter`` WAS sent but the expected advance could
                             not be confirmed (incl. confirm-capture / parse
                             failure). The callback handler REFRESHES ONLY and
                             never auto-redispatches (no re-tap can re-send the
                             commit key for this key).

v2.1.167 legacy: that build dispatched a single BARE DIGIT. The
``digit_sent``, ``failed_before_digit``, and ``failed_after_digit`` states are
**legacy-only** — kept defined here so on-disk rows from older builds still
load and project correctly, but they are no longer *written* by the dispatch
path. (``digit_sent`` marked the gap between a digit and a since-deleted Enter;
``failed_before_digit`` / ``failed_after_digit`` marked digit-send failures.)

Storage: append-only JSONL at ``<CC_TELEGRAM_DIR>/auq_action_ledger.jsonl``
(mode ``0600``). Each line is one persisted state transition for one
ledger key. The latest line per key wins on lookup. Corrupt trailing
lines (partial writes during crash) are tolerated by skipping them with
a WARNING.

Persisted states:
  - ``accepted``            — token validated, BEFORE navigation/commit.
  - ``dispatched``          — confirmed expected advance (terminal success).
  - ``not_advanced``        — a PRE-COMMIT bail (``Enter`` never sent). The
                              ``failed_reason`` carries the sub-reason:
                              ``"cursor_unknown"``, ``"nav_send_failed"``,
                              ``"verify_failed"``, or ``"commit_send_failed"``.
                              Callback FALLS THROUGH (re-tap re-validates).
  - ``commit_unconfirmed``  — ``Enter`` was sent, advance unconfirmed. The
                              ``failed_reason`` carries: ``"commit_unconfirmed"``,
                              ``"confirm_capture_failed"``, or
                              ``"confirm_parse_failed"``. Callback REFRESHES
                              ONLY, never auto-redispatches.
  - ``failed_before_digit`` — LEGACY (v2.1.167 bare-digit): digit send returned
                              False / raised before tmux. No longer written.
  - ``digit_sent``          — LEGACY (pre-v2.1.167 digit+Enter): digit landed,
                              Enter not yet. No longer written.
  - ``failed_after_digit``  — LEGACY (pre-v2.1.167 digit+Enter): digit landed,
                              Enter raised (ambiguous). No longer written.

``unknown`` is NOT a persisted state — it is a load-time projection.
Callers compare an entry's ``accepted_at`` against ``process_start_time()``
to decide whether an ``accepted``/``digit_sent`` entry came from a prior
process that crashed mid-dispatch (project to ``unknown``) or from the
current process (keep the real state).

Ledger key shape: ``"<route_hash>:<fp8>:<opt>"`` where
``route_hash = sha1(f"{user_id}:{thread_id}:{window_id}")[:8]``,
``fp8`` is the first 8 chars of the pick-token fingerprint, and ``opt``
is the option number. The key is stable across restarts; the full
fingerprint + user_id are stored per entry for diagnostics and the
collision-defense check in the callback handler.

``lookup()`` is a pure read — it returns the latest raw row for a key
or ``None``. It does NOT enforce owner / collision semantics; that
classification lives in the callback handler per v4 §7.2 (owner-check
first, then collision via live-pick-token peek).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Final, Literal

from ..utils import app_dir

logger = logging.getLogger(__name__)

LEDGER_FILENAME: Final[str] = "auq_action_ledger.jsonl"
LRU_CAP: Final[int] = 10_000
RETENTION_SECONDS: Final[float] = 24 * 60 * 60.0


LedgerState = Literal[
    "accepted",
    "dispatched",
    "not_advanced",
    "commit_unconfirmed",
    "digit_sent",
    "failed_before_digit",
    "failed_after_digit",
]

_PERSISTED_STATES: Final[frozenset[str]] = frozenset(
    {
        "accepted",
        "dispatched",
        "not_advanced",
        "commit_unconfirmed",
        "digit_sent",
        "failed_before_digit",
        "failed_after_digit",
    }
)


@dataclass(frozen=True)
class LedgerEntry:
    """Latest persisted state for one ledger key.

    Mirrors the JSONL line shape one-to-one — ``asdict(entry)`` is the
    on-disk payload.
    """

    key: str
    state: LedgerState
    user_id: int
    window_id: str
    full_fingerprint: str
    option_number: int
    option_label: str
    accepted_at: float
    digit_sent_at: float | None = None
    dispatched_at: float | None = None
    failed_reason: str | None = None


# Injection seams for tests — replace via reset_for_tests() instead of
# monkeypatching the time/path globally.
_now: Callable[[], float] = time.time
_process_start_time: float = time.time()
_path_override: Path | None = None

_entries: dict[str, LedgerEntry] = {}
_loaded: bool = False


def process_start_time() -> float:
    """Wall-clock seconds at module import (or test override).

    Callback handler compares ``entry.accepted_at`` against this value
    to project pre-restart ``accepted``/``digit_sent`` rows into
    ``unknown``.
    """
    return _process_start_time


def make_route_hash(user_id: int, thread_id: int | None, window_id: str) -> str:
    """Return the 8-hex-char route component of the ledger key.

    ``thread_id`` is normalized to ``0`` for None so a route without a
    thread (DM scenario, future-proofing) and the same window picks the
    same hash.
    """
    raw = f"{user_id}:{thread_id or 0}:{window_id}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def make_ledger_key(route_hash: str, fp8: str, option_number: int) -> str:
    """Compose the stable ledger key shared between mint + lookup sites.

    The triplet ``(route_hash, fp8, opt)`` is stable across restarts:
    the same (user, thread, window, form-shape, option) always produces
    the same key.
    """
    return f"{route_hash}:{fp8}:{option_number}"


def _ledger_path() -> Path:
    return _path_override if _path_override is not None else app_dir() / LEDGER_FILENAME


def _parse_line(raw: str, line_no: int) -> LedgerEntry | None:
    """Decode one JSONL line into a LedgerEntry, or None if corrupt."""
    try:
        data = json.loads(raw)
        state = data.get("state")
        if state not in _PERSISTED_STATES:
            logger.warning(
                "auq_ledger: unknown state %r on line %d; skipping",
                state,
                line_no,
            )
            return None
        return LedgerEntry(
            key=data["key"],
            state=state,
            user_id=int(data["user_id"]),
            window_id=data["window_id"],
            full_fingerprint=data["full_fingerprint"],
            option_number=int(data["option_number"]),
            option_label=data["option_label"],
            accepted_at=float(data["accepted_at"]),
            digit_sent_at=(
                float(data["digit_sent_at"])
                if data.get("digit_sent_at") is not None
                else None
            ),
            dispatched_at=(
                float(data["dispatched_at"])
                if data.get("dispatched_at") is not None
                else None
            ),
            failed_reason=data.get("failed_reason"),
        )
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("auq_ledger: corrupt line %d (skipping): %s", line_no, exc)
        return None


def _load_from_disk() -> None:
    global _loaded
    _loaded = True
    path = _ledger_path()
    if not path.exists():
        return
    latest: dict[str, LedgerEntry] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line_no, raw in enumerate(f, start=1):
                raw = raw.rstrip("\n")
                if not raw:
                    continue
                entry = _parse_line(raw, line_no)
                if entry is not None:
                    latest[entry.key] = entry
    except OSError as exc:
        logger.warning("auq_ledger: failed to read %s: %s", path, exc)
        return
    _entries.clear()
    _entries.update(latest)
    if len(_entries) > LRU_CAP:
        _compact()


def _ensure_loaded() -> None:
    if not _loaded:
        _load_from_disk()


def lookup(key: str | None) -> LedgerEntry | None:
    """Return the latest LedgerEntry for ``key``, or None.

    Pure read. Owner-check / collision classification lives in the
    callback handler (v4 §7.2).
    """
    if key is None:
        return None
    _ensure_loaded()
    return _entries.get(key)


def record(
    key: str,
    *,
    state: LedgerState,
    user_id: int | None = None,
    window_id: str | None = None,
    full_fingerprint: str | None = None,
    option_number: int | None = None,
    option_label: str | None = None,
    failed_reason: str | None = None,
) -> LedgerEntry:
    """Append a state transition for ``key`` and return the merged entry.

    The first write for a key MUST pass the per-entry identification
    fields (``user_id``, ``window_id``, ``full_fingerprint``,
    ``option_number``, ``option_label``). Subsequent writes inherit them
    from the existing entry; only ``state`` and ``failed_reason`` are
    expected to vary.

    Idempotency: writing the same terminal state for an entry that is
    already in that state still appends a new JSONL line but the
    in-memory snapshot is unchanged in shape — the latest-line-wins
    loader will collapse them on next startup.
    """
    if state not in _PERSISTED_STATES:
        raise ValueError(f"Invalid ledger state: {state!r}")
    _ensure_loaded()
    now = _now()
    existing = _entries.get(key)
    if existing is None:
        if (
            user_id is None
            or window_id is None
            or full_fingerprint is None
            or option_number is None
            or option_label is None
        ):
            raise ValueError(
                "First record() for a new key requires user_id, window_id, "
                "full_fingerprint, option_number, and option_label"
            )
        entry = LedgerEntry(
            key=key,
            state=state,
            user_id=user_id,
            window_id=window_id,
            full_fingerprint=full_fingerprint,
            option_number=option_number,
            option_label=option_label,
            accepted_at=now,
            digit_sent_at=now if state == "digit_sent" else None,
            dispatched_at=now if state == "dispatched" else None,
            failed_reason=failed_reason,
        )
    else:
        entry = LedgerEntry(
            key=existing.key,
            state=state,
            user_id=existing.user_id,
            window_id=existing.window_id,
            full_fingerprint=existing.full_fingerprint,
            option_number=existing.option_number,
            option_label=existing.option_label,
            accepted_at=existing.accepted_at,
            digit_sent_at=now if state == "digit_sent" else existing.digit_sent_at,
            dispatched_at=now if state == "dispatched" else existing.dispatched_at,
            failed_reason=(
                failed_reason if failed_reason is not None else existing.failed_reason
            ),
        )
    _append_line(entry)
    _entries[key] = entry
    return entry


def _append_line(entry: LedgerEntry) -> None:
    """Append one JSONL line via a single O_APPEND write.

    POSIX ``O_APPEND`` guarantees that single ``write()`` calls of size
    < ``PIPE_BUF`` (~4KB on macOS/Linux) are atomic with respect to
    other appenders. Our lines are well under that — even a 250-char
    ``option_label`` plus full fingerprint keeps the line < 1KB.
    """
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(entry)
    line = json.dumps(payload, separators=(",", ":"), ensure_ascii=True) + "\n"
    encoded = line.encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, encoded)
    finally:
        os.close(fd)


def _compact() -> None:
    """Rewrite the ledger keeping the LRU_CAP most-recent-per-key entries.

    Drops entries older than ``RETENTION_SECONDS`` regardless of cap.
    Called at startup when the loaded set exceeds LRU_CAP. Uses a
    temp-file + atomic rename so a crash mid-rewrite leaves either the
    old or the new file intact.
    """
    cutoff = _now() - RETENTION_SECONDS
    survivors = [e for e in _entries.values() if e.accepted_at >= cutoff]
    survivors.sort(key=lambda e: e.accepted_at, reverse=True)
    dropped = max(0, len(_entries) - len(survivors))
    survivors = survivors[:LRU_CAP]
    path = _ledger_path()
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp_path, "w", encoding="utf-8") as f:
        for entry in survivors:
            payload = asdict(entry)
            f.write(
                json.dumps(payload, separators=(",", ":"), ensure_ascii=True) + "\n"
            )
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, path)
    _entries.clear()
    _entries.update({e.key: e for e in survivors})
    logger.info(
        "auq_ledger: compacted to %d entries (dropped %d expired/over-cap)",
        len(survivors),
        dropped,
    )


def reset_for_tests(
    *,
    path: Path | None = None,
    now: Callable[[], float] | None = None,
    start_time: float | None = None,
) -> None:
    """Clear in-memory state and optionally inject path / time helpers.

    Tests pass ``path=tmp_path/"ledger.jsonl"`` to scope writes,
    ``now=lambda: t`` to drive timestamps, and ``start_time=t0`` to
    control the post-restart projection threshold.
    """
    global _now, _process_start_time, _path_override, _loaded
    _entries.clear()
    _loaded = False
    _path_override = path
    _now = now if now is not None else time.time
    _process_start_time = start_time if start_time is not None else time.time()
