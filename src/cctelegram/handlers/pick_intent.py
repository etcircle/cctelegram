"""Durable per-callback-TOKEN AUQ pick mint-intent store (D2 restart-recovery).

D3-β keeps a live AUQ card's *in-memory* pick tokens alive while the poller
observes the card on-pane, so an idle picker no longer dies. But a bot
**restart** wipes the in-memory ``pick_token`` store, and the already-published
Telegram card keeps its old keyboard with the now-dead token strings baked into
the callback_data — so the first tap hits ``peek_none`` and degrades to the
honest D3-α modal for the card's whole remaining lifetime. This module persists
the *mint intent* for each option button so the callback handler can RECOVER and
re-dispatch that first token-less tap after a restart.

Why a separate store (NOT the action ledger): the action ledger
(``auq_action_ledger.jsonl``) is keyed by ``(route_hash, fp8, opt)`` latest-wins
and is the idempotency authority — writing recovery state into it would clobber a
``dispatched`` row and re-open double-dispatch. This store is keyed by the
**token string** (so a stale tap for form A can't read a newer same-key row B)
and never participates in that dedup.

Design:
  - Append-only JSONL at ``~/.cc-telegram/pick_intent.jsonl`` (mode 0600). Two
    line shapes: a ROW record (one rendered card's sibling option tokens, written
    as ONE line so a row is atomic-as-a-set) and a TOMB record (a list of token
    strings to retire). A token is live iff it appears in a row line and in no
    later tomb line and its row is within ``RETENTION_SECONDS``.
  - ``record_row`` supersedes only prior rows of the same route whose
    ``full_fingerprint`` DIFFERS (an identical re-render is left live — old and
    new tokens reconstruct the same ledger key, so the ledger gives exactly-once;
    tombing them would make a stale-keyboard tap during a re-render race wrongly
    decline).
  - ``lookup_intent`` validates the untrusted file on read and returns the
    per-token intent enriched with the row's sibling option-numbers + tokens
    (recovery needs them for row-scoped single-use).
  - ``consume_row`` tombs ALL sibling tokens of a row (row-level single-use).
  - ``teardown_window`` tombs every live token of a window (resolution seams).

Durability: each line is written with a single ``O_APPEND`` open + a full-write
loop; a torn or corrupt trailing line is skipped on read. This is NOT a
``PIPE_BUF`` regular-file atomicity claim — correctness rests on "partial/corrupt
lines decline, never mis-dispatch".

Stays a LEAF: imports only ``..utils`` (no telegram/tmux/handler imports), so the
no-import-cycle guard stays green and the module is unit-testable.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from ..utils import app_dir

logger = logging.getLogger(__name__)

STORE_FILENAME = "pick_intent.jsonl"
_SCHEMA_VERSION = 1

# Mirror the action ledger's horizons. The retention MUST comfortably exceed the
# realistic max open-card lifetime (a user can leave a picker open for hours), so
# a long-idle card's intent is still recoverable after a restart. Pruning is by
# wall-clock ``minted_at`` — NEVER by the 300s in-memory token TTL.
RETENTION_SECONDS = 24 * 60 * 60.0
LRU_CAP = 10_000  # compact when more than this many lines accumulate

_VALID_SOURCE_KINDS = frozenset({"side_file", "jsonl_cache", "pane"})
_TOKEN_RE = re.compile(r"^[0-9a-f]{12}$")
_FP_RE = re.compile(r"^[0-9a-f]{16}$")
_MAX_LABEL_LEN = 4096
_MAX_WINDOW_LEN = 256


@dataclass(frozen=True)
class TokenSpec:
    """One option button's per-token mint inputs (the ``record_row`` payload)."""

    token: str
    option_number: int
    option_label: str
    is_review_submit: bool


@dataclass(frozen=True)
class PickIntent:
    """A recovered per-token mint intent + its row context.

    The row-shared fields (``full_fingerprint`` / source tags / route /
    ``session_id`` / ``minted_at``) plus this token's own option fields, enriched
    with the row's ``sibling_option_numbers`` and ``sibling_tokens`` so recovery
    can enforce ROW-level single-use (a sibling-ledger guard + a row-consume tomb).
    """

    token: str
    full_fingerprint: str
    source_kind: str
    source_fingerprint: str
    user_id: int
    thread_id: int | None
    window_id: str
    session_id: str | None
    option_number: int
    option_label: str
    is_review_submit: bool
    minted_at: float
    sibling_option_numbers: tuple[int, ...]
    sibling_tokens: tuple[str, ...]


# Token → live intent. Rebuilt on load by replaying row + tomb lines.
_live: dict[str, PickIntent] = {}
_loaded = False
_lines_read = 0

# Injection seams (tests).
_path_override: Path | None = None
_now: Callable[[], float] = time.time


def _store_path() -> Path:
    return _path_override if _path_override is not None else app_dir() / STORE_FILENAME


def _validate_token(tok: object) -> str | None:
    return tok if isinstance(tok, str) and _TOKEN_RE.match(tok) else None


def _parse_row(data: dict) -> list[PickIntent] | None:
    """Decode one ROW record into per-token ``PickIntent``s, or None if invalid.

    Untrusted-on-read: every dispatch-affecting field is shape/type/range
    checked. Any failure rejects the WHOLE row (its tokens become unrecoverable,
    degrading to the honest modal — never a mis-dispatch).
    """
    try:
        full_fingerprint = data["full_fingerprint"]
        source_kind = data["source_kind"]
        source_fingerprint = data["source_fingerprint"]
        user_id = data["user_id"]
        window_id = data["window_id"]
        minted_at = float(data["minted_at"])
        raw_tokens = data["tokens"]
    except (KeyError, TypeError, ValueError):
        return None
    thread_id = data.get("thread_id")
    session_id = data.get("session_id")

    if (
        not isinstance(full_fingerprint, str)
        or not _FP_RE.match(full_fingerprint)
        or source_kind not in _VALID_SOURCE_KINDS
        or not isinstance(source_fingerprint, str)
        or not source_fingerprint
        or not isinstance(user_id, int)
        or isinstance(user_id, bool)
        or not isinstance(window_id, str)
        or not window_id
        or len(window_id) > _MAX_WINDOW_LEN
        or (
            thread_id is not None
            and (not isinstance(thread_id, int) or isinstance(thread_id, bool))
        )
        or (session_id is not None and not isinstance(session_id, str))
        or not isinstance(raw_tokens, list)
        or not raw_tokens
    ):
        return None
    # A wall-clock minted_at must be finite and not implausibly in the future
    # (a clock-skew/forged row that claims the far future would never expire).
    if not (minted_at == minted_at) or minted_at > _now() + 86400.0:  # NaN guard + skew
        return None

    parsed: list[tuple[str, int, str, bool]] = []
    seen_tokens: set[str] = set()
    for t in raw_tokens:
        if not isinstance(t, dict):
            return None
        tok = _validate_token(t.get("t"))
        n = t.get("n")
        label = t.get("label")
        submit = t.get("submit")
        if (
            tok is None
            or tok in seen_tokens
            or not isinstance(n, int)
            or isinstance(n, bool)
            or not (1 <= n <= 9)
            or not isinstance(label, str)
            or len(label) > _MAX_LABEL_LEN
            or not isinstance(submit, bool)
        ):
            return None
        seen_tokens.add(tok)
        parsed.append((tok, n, label, submit))

    sibling_numbers = tuple(n for _t, n, _l, _s in parsed)
    sibling_tokens = tuple(t for t, _n, _l, _s in parsed)
    return [
        PickIntent(
            token=tok,
            full_fingerprint=full_fingerprint,
            source_kind=source_kind,
            source_fingerprint=source_fingerprint,
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
            session_id=session_id,
            option_number=n,
            option_label=label,
            is_review_submit=submit,
            minted_at=minted_at,
            sibling_option_numbers=sibling_numbers,
            sibling_tokens=sibling_tokens,
        )
        for tok, n, label, submit in parsed
    ]


def _parse_tomb(data: dict) -> list[str] | None:
    raw = data.get("tomb")
    if not isinstance(raw, list):
        return None
    out = [tok for tok in (_validate_token(t) for t in raw) if tok is not None]
    return out


def _load_from_disk() -> None:
    global _loaded, _lines_read
    _loaded = True
    _lines_read = 0
    path = _store_path()
    if not path.exists():
        _live.clear()
        return
    cutoff = _now() - RETENTION_SECONDS
    working: dict[str, PickIntent] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.rstrip("\n")
                if not raw:
                    continue
                _lines_read += 1
                try:
                    data = json.loads(raw)
                except ValueError:
                    continue  # torn/corrupt trailing line — skip
                if not isinstance(data, dict):
                    continue
                if "tomb" in data:
                    tomb = _parse_tomb(data)
                    if tomb:
                        for tok in tomb:
                            working.pop(tok, None)
                    continue
                intents = _parse_row(data)
                if intents is None:
                    continue
                if intents[0].minted_at < cutoff:
                    continue  # expired row — never recoverable
                for intent in intents:
                    working[intent.token] = intent
    except OSError as exc:
        logger.warning("pick_intent: failed to read %s: %s", path, exc)
        _live.clear()
        return
    _live.clear()
    _live.update(working)
    if _lines_read > LRU_CAP:
        # Best-effort (finding 24): the lazy compaction is triggered from the
        # public read/write seams — a disk failure must not raise into the
        # live render. The in-memory replay above already succeeded.
        try:
            _compact()
        except OSError as exc:
            logger.warning("pick_intent: compaction of %s failed: %s", path, exc)


def _ensure_loaded() -> None:
    if not _loaded:
        _load_from_disk()


def _append_line(obj: dict) -> None:
    """Append one JSONL line via a single ``O_APPEND`` open + full-write loop.

    The safety property is "one logical row per line; a partial/corrupt trailing
    line is skipped on read" — NOT a ``PIPE_BUF`` regular-file atomicity claim.
    The full-write loop covers the rare short ``os.write``; single-process so no
    cross-writer interleave.

    Best-effort (finding 24): an ``OSError`` (disk full / read-only config dir)
    is logged and swallowed so it never raises through the LIVE picker render
    (``interactive_ui`` calls ``record_row`` on the render path). The in-memory
    state stays authoritative for this process; only restart-recovery
    durability is lost — mirroring ``md_capture``'s posture.
    """
    path = _store_path()
    line = json.dumps(obj, separators=(",", ":"), ensure_ascii=True) + "\n"
    view = memoryview(line.encode("utf-8"))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            while view:
                n = os.write(fd, view)
                if n <= 0:
                    raise OSError("pick_intent: short write to store")
                view = view[n:]
        finally:
            os.close(fd)
    except OSError as exc:
        logger.warning(
            "pick_intent: append to %s failed (%s) — row not durable across restart",
            path,
            exc,
        )


def _row_payload(intents: Iterable[PickIntent]) -> dict:
    intents = list(intents)
    first = intents[0]
    return {
        "v": _SCHEMA_VERSION,
        "full_fingerprint": first.full_fingerprint,
        "source_kind": first.source_kind,
        "source_fingerprint": first.source_fingerprint,
        "user_id": first.user_id,
        "thread_id": first.thread_id,
        "window_id": first.window_id,
        "session_id": first.session_id,
        "minted_at": first.minted_at,
        "tokens": [
            {
                "t": it.token,
                "n": it.option_number,
                "label": it.option_label,
                "submit": it.is_review_submit,
            }
            for it in intents
        ],
    }


def record_row(
    *,
    full_fingerprint: str,
    source_kind: str,
    source_fingerprint: str,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    session_id: str | None,
    minted_at: float,
    token_specs: Iterable[TokenSpec],
) -> None:
    """Persist one rendered card's sibling option tokens as ONE atomic row line.

    Supersede ONLY prior live rows of the same ``(user_id, thread_id, window_id)``
    route whose ``full_fingerprint`` DIFFERS (an identical re-render is left
    live). Writes the supersede tomb BEFORE the new row so a crash between the two
    leaves the prior card un-recoverable (safe) and the new card un-recoverable
    until re-render (safe) — never a wrong dispatch. Fully synchronous (no
    ``await``) so within the single-threaded event loop the tomb-then-row pair is
    atomic w.r.t. other coroutines.
    """
    _ensure_loaded()
    specs = list(token_specs)
    if not specs:
        return
    norm_thread = thread_id or 0
    superseded = [
        intent.token
        for intent in _live.values()
        if intent.user_id == user_id
        and (intent.thread_id or 0) == norm_thread
        and intent.window_id == window_id
        and intent.full_fingerprint != full_fingerprint
    ]
    if superseded:
        _append_line({"v": _SCHEMA_VERSION, "tomb": superseded})
        for tok in superseded:
            _live.pop(tok, None)

    sibling_numbers = tuple(s.option_number for s in specs)
    sibling_tokens = tuple(s.token for s in specs)
    intents = [
        PickIntent(
            token=s.token,
            full_fingerprint=full_fingerprint,
            source_kind=source_kind,
            source_fingerprint=source_fingerprint,
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
            session_id=session_id,
            option_number=s.option_number,
            option_label=s.option_label,
            is_review_submit=s.is_review_submit,
            minted_at=minted_at,
            sibling_option_numbers=sibling_numbers,
            sibling_tokens=sibling_tokens,
        )
        for s in specs
    ]
    _append_line(_row_payload(intents))
    for intent in intents:
        _live[intent.token] = intent


def lookup_intent(token: str) -> PickIntent | None:
    """Return the live, validated, non-expired ``PickIntent`` for ``token`` or None.

    Enforces ``RETENTION_SECONDS`` on EVERY lookup, not just at load: a
    long-lived process (no restart) could otherwise still hold an over-24h row in
    ``_live`` after its in-memory pick token / cache row pruned, and recover it.
    """
    _ensure_loaded()
    intent = _live.get(token)
    if intent is None:
        return None
    if intent.minted_at < _now() - RETENTION_SECONDS:
        return None
    return intent


def consume_row(token: str) -> None:
    """Tomb ALL sibling tokens of the row containing ``token`` (row single-use)."""
    _ensure_loaded()
    intent = _live.get(token)
    if intent is None:
        return
    sibs = [t for t in intent.sibling_tokens if t in _live] or [token]
    _append_line({"v": _SCHEMA_VERSION, "tomb": sibs})
    for tok in sibs:
        _live.pop(tok, None)


def teardown_window(window_id: str) -> None:
    """Tomb every live token bound to ``window_id`` (resolution / teardown seam)."""
    _ensure_loaded()
    doomed = [tok for tok, intent in _live.items() if intent.window_id == window_id]
    if not doomed:
        return
    _append_line({"v": _SCHEMA_VERSION, "tomb": doomed})
    for tok in doomed:
        _live.pop(tok, None)


def _compact() -> None:
    """Rewrite the store keeping only live, non-expired rows (temp + rename)."""
    cutoff = _now() - RETENTION_SECONDS
    groups: dict[tuple, list[PickIntent]] = {}
    for intent in _live.values():
        if intent.minted_at < cutoff:
            continue
        sig = (
            intent.full_fingerprint,
            intent.source_kind,
            intent.source_fingerprint,
            intent.user_id,
            intent.thread_id,
            intent.window_id,
            intent.session_id,
            intent.minted_at,
            intent.sibling_tokens,
        )
        groups.setdefault(sig, []).append(intent)
    path = _store_path()
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp_path, "w", encoding="utf-8") as f:
        for intents in groups.values():
            f.write(
                json.dumps(
                    _row_payload(intents), separators=(",", ":"), ensure_ascii=True
                )
                + "\n"
            )
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, path)
    global _lines_read
    _lines_read = len(groups)
    logger.info("pick_intent: compacted to %d rows", len(groups))


def reset_for_tests(
    *, path: Path | None = None, now: Callable[[], float] | None = None
) -> None:
    """Clear in-memory state and optionally inject path / time helpers."""
    global _path_override, _now, _loaded, _lines_read
    _live.clear()
    _loaded = False
    _lines_read = 0
    _path_override = path
    _now = now if now is not None else time.time
