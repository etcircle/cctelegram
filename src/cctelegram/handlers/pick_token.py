"""The single home for the AUQ pick-token store + atomic validate-and-consume.

This module owns everything about an option-button's server-side lifecycle:
minting a single-use token at render, caching sibling tokens so a same-form
re-render stays byte-identical (Telegram ``MESSAGE_NOT_MODIFIED``), reading a
token without consuming it, deriving the Wave-3 ledger key, and — the reason
this module exists as one home — the atomic ``validate_and_consume`` that
re-resolves the AUQ source through the SAME ``auq_source.resolve_auq_source``
the minter used (measurable mint/validate SOURCE parity) and wins-or-loses the
single-use consume by EXCLUSIVE RESERVATION without holding the store lock
across pane/window I/O.

Core responsibilities:
  - Own ``_pick_tokens`` (token → entry) and ``_pick_token_cache`` (route +
    fingerprint → sibling-token row with generation + consumed-generation
    tombstone). Both are private; ``mint_row`` owns the cache-reuse logic.
  - Mint single-use tokens with a TTL prune; reuse cached tokens for an
    unchanged form WITHOUT bumping the row generation.
  - ``validate_and_consume``: phase (a) owner-check-then-reserve under the
    store lock; phase (b) the slow pane/window/source checks lock-RELEASED,
    wrapped in try/finally so an exception or task cancellation unreserves
    (only if still owned by this call); phase (c) win-or-lose the consume
    under the lock, popping the token + evicting siblings + tombstoning the
    cache row with the winner's generation.

Key components:
  - ``PickTokenEntry`` — the frozen per-button record (now public; carries the
    minted ``source_kind`` / ``source_fingerprint`` / ``row_generation``).
  - ``PickValidation`` — the typed outcome of ``validate_and_consume``.
  - ``mint`` / ``mint_row`` / ``peek`` / ``stable_key`` / ``prune_for_route``.
  - ``validate_and_consume`` — the atomic reservation-based finalizer.

Stays a leaf: imports ``resolve_auq_source`` / ``ResolvedAuqSource`` from the
``auq_source`` LEAF — NEVER from ``interactive_ui`` (no import cycle). Pane
capture / window lookup are INJECTED into ``validate_and_consume`` so this
module has no telegram/tmux import and is unit-testable.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from . import auq_ledger, pick_intent
from .auq_source import (
    RecoverySideFile,
    read_side_file_for_recovery,
    resolve_auq_source,
    side_file_live_for_session,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from ..terminal_parser import AskUserQuestionForm
    from .pick_intent import PickIntent

logger = logging.getLogger(__name__)


# TTL bounds MEMORY only — dead-tap correctness is handled by the poller's
# ``refresh_route_deadlines`` (D3-β: a visibly-live card's tokens are re-stamped
# every poll so a token's lifetime tracks the card's OBSERVED lifetime), NOT by
# this constant. A genuinely-abandoned card's tokens still prune at 300s. The
# pre-β assumption that "the picker stays open at most a few minutes" was false:
# a user can leave a live AUQ open for tens of minutes to hours.
_PICK_TOKEN_TTL_SECONDS = 300.0


@dataclass(frozen=True)
class PickTokenEntry:
    """Server-side state bound to a single option-button click.

    Frozen because once minted, the entry must not mutate (the staleness
    check compares the *minted* fingerprint against the *current* parse).
    Marking entries used is done by popping from the map inside
    ``validate_and_consume``'s phase (c), not flipping a field, so single-use
    semantics are enforced atomically.

    ``source_kind`` / ``source_fingerprint`` capture the ``ResolvedAuqSource``
    the minter resolved (measurable mint/validate source parity); they are
    DISTINCT from ``fingerprint``, which is the form's parse fingerprint.
    ``row_generation`` is the cache row's module-global generation at mint
    time: the winning consume writes it into the row's ``consumed_generation``
    tombstone, and a losing sibling compares its own ``row_generation`` to the
    row's tombstone to tell ``already_consumed`` (generation match) from
    ``expired`` (no match / row re-minted / row pruned).
    """

    window_id: str
    user_id: int
    thread_id: int | None
    fingerprint: str  # form.fingerprint() at the moment the keyboard rendered
    option_number: int  # the numeric shortcut to send (1-9)
    option_label: str  # human label, used for log messages + sanity
    is_review_submit: bool  # True iff this click should submit the review screen
    expires_at: float  # monotonic clock deadline
    source_kind: str  # "side_file" | "jsonl_cache" | "pane" — which branch minted
    source_fingerprint: str  # ResolvedAuqSource.source_fingerprint at mint
    row_generation: int  # the cache row's generation at mint time


@dataclass
class _CacheRow:
    """The value side of ``_pick_token_cache`` for one cache key.

    Carries the live sibling token list, the row's module-global generation
    (the counter value at this row's last FRESH mint — a cache-REUSE re-render
    does NOT bump it), the row's minted AUQ source tags (``source_kind`` /
    ``source_fingerprint`` — so a re-render whose FORM fingerprint is unchanged
    but whose SOURCE drifted is NOT reused; reusing it would hand back tokens
    carrying the stale source and dead-loop validate→source_drift→refresh), the
    consume tombstone (the winner's ``row_generation``, set in phase (c) when a
    token from this row is consumed), and ``tombstoned_at`` (the monotonic time
    the tombstone was set). A consumed row is KEPT as a tombstone with
    ``tokens`` emptied until it ages past the pick-row TTL — that lets a losing
    SIBLING token still read the tombstone for ``already_consumed`` instead of
    seeing a dropped row (which would misclassify as ``expired``). No separate
    tombstone map, no separate GC.
    """

    tokens: list[str]
    row_generation: int
    source_kind: str
    source_fingerprint: str
    consumed_generation: int | None = None
    tombstoned_at: float | None = None


_pick_tokens: dict[str, PickTokenEntry] = {}

# Stable per-route cache so a re-render of the same form (same fingerprint)
# reuses the same callback tokens. Without this, every status-polling tick
# would mint fresh random tokens, the reply_markup would never match the
# previous edit, Telegram would never return MESSAGE_NOT_MODIFIED, and the
# bot would re-edit the card every poll cycle while the user is reading it.
#
# Key: (user_id, thread_id_or_0, window_id, fingerprint)
# Value: _CacheRow — sibling tokens + generation + consume tombstone.
_pick_token_cache: dict[tuple[int, int, str, str], _CacheRow] = {}

# Per-token reservation markers. ``_reservations[token]`` holds the unique
# per-call owner id (a uuid hex string) of the in-flight ``validate_and_consume``
# call that currently owns the slow-phase reservation for that token. A second
# caller observing a reservation at phase (a) returns ``already_consumed``
# WITHOUT entering the slow path; the owning call's ``finally`` clears ONLY its
# own reservation (owner-id guard) so it never clears a reservation a re-minted
# or other call now owns.
_reservations: dict[str, str] = {}

# D2 row-scoped recovery reservations. Keyed by the ROW cache_key
# ``(user_id, thread_id_or_0, window_id, full_fingerprint)`` — NOT the per-token
# ``_reservations`` — so a concurrent post-restart tap on ANY sibling option of
# the same row serialises (single-select recovery is row-single-use). Value is
# the uuid owner of the in-flight ``recover_and_consume`` call.
_recovery_row_reservations: dict[tuple[int, int, str, str], str] = {}

# MODULE-GLOBAL monotonic generation counter. MUST be module-global, NOT
# per-row: a pruned G1 row re-minted under the same key must advance to G2 — a
# per-row counter would lose that memory and reuse G1, misclassifying a stale
# tombstone. The counter survives row prune within the process; only
# ``reset_for_tests`` resets it to its initial value.
_INITIAL_GENERATION = 0
_generation_counter = _INITIAL_GENERATION

# Serialises mutations to the token store (the dicts above + the counter).
# NEVER held across pane/window I/O — ``validate_and_consume`` releases it for
# the slow phase (b).
_store_lock = asyncio.Lock()


def _next_generation() -> int:
    """Allocate the next module-global monotonic generation value."""
    global _generation_counter
    _generation_counter += 1
    return _generation_counter


def _prune_expired_pick_tokens(now: float | None = None) -> None:
    """Drop expired tokens from the in-memory map.

    Runs on every mint — the map is small (≤ #options per active picker, so
    typically ≤ 10) so the O(n) scan is cheap.

    Cache-row pruning has two cases:
      - A TOMBSTONED row (``consumed_generation`` set) is KEPT until it ages
        past the pick-row TTL (``now - tombstoned_at > TTL``). Dropping it the
        instant its tokens emptied would let a still-in-flight losing sibling
        reach phase (c), find no row, and misclassify ``already_consumed`` as
        ``expired``. The TTL horizon bounds the tombstone so it can't pile up.
      - A NON-tombstoned row is dropped as soon as it has no live token (its
        tokens all expired) so a stale fingerprint can't pin a dead token list.
    """
    if now is None:
        now = time.monotonic()
    stale = [tok for tok, e in _pick_tokens.items() if e.expires_at <= now]
    for tok in stale:
        _pick_tokens.pop(tok, None)
        _reservations.pop(tok, None)
    for cache_key, row in list(_pick_token_cache.items()):
        if row.consumed_generation is not None:
            # Tombstone: keep until past TTL.
            if (
                row.tombstoned_at is not None
                and now - row.tombstoned_at > _PICK_TOKEN_TTL_SECONDS
            ):
                _pick_token_cache.pop(cache_key, None)
        elif not any(t in _pick_tokens for t in row.tokens):
            _pick_token_cache.pop(cache_key, None)


def mint(entry: PickTokenEntry) -> str:
    """Register a token for an option button. Returns the token id.

    Token is 12 hex chars from ``secrets.token_hex(6)``. Since Wave 3, the
    full callback payload is the keyed shape
    ``aqp:<route_hash>:<fp8>:<opt>:<token>`` (~33-34 bytes; well under
    Telegram's 64-byte cap). This is the only shape the callback handler
    parses; the pre-Wave-3 ``aqp:<token>`` legacy shape is no longer accepted.
    """
    _prune_expired_pick_tokens()
    # 6 bytes = 12 hex chars. Collision space ~2^48; with at most a few
    # tokens live at any moment, accidental clash is astronomically
    # unlikely. Loop on the off chance.
    for _ in range(8):
        token = secrets.token_hex(6)
        if token not in _pick_tokens:
            _pick_tokens[token] = entry
            return token
    raise RuntimeError("Unable to mint a unique pick token")


@dataclass(frozen=True)
class _MintSpec:
    """One option-button to mint a token for (the per-option mint inputs)."""

    option_number: int
    option_label: str
    is_review_submit: bool


def mint_row(
    *,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    fingerprint: str,
    source_kind: str,
    source_fingerprint: str,
    specs: Iterable[_MintSpec],
) -> tuple[list[str], bool]:
    """Mint (or reuse) the sibling-token row for one rendered form generation.

    Owns the cache-reuse logic so ``_pick_token_cache`` stays private. On a
    FRESH mint of a cache key — no live cached row, or a row whose tokens have
    been TTL-evicted — allocates the next module-global generation via
    ``_next_generation()``, stamps it on the row AND on each minted
    ``PickTokenEntry``, and (re)creates the token list. On the cache-REUSE
    path (the form is UNCHANGED, the SOURCE is unchanged, and the cached tokens
    are still live, returned to preserve Telegram's ``MESSAGE_NOT_MODIFIED``)
    returns the SAME tokens and does NOT bump the generation.

    Source-aware reuse (closes a dead-loop): the cache key is keyed on the FORM
    fingerprint only, so a re-render after a SOURCE drift (e.g. the side_file /
    jsonl_cache ``tool_input`` changed while the visible form stayed identical)
    hits the same key. Reusing that row would return tokens still stamped with
    the OLD ``source_fingerprint``, and the next tap would ``source_drift`` →
    refresh → reuse → ``source_drift`` forever. So reuse ALSO requires the
    cached row's ``(source_kind, source_fingerprint)`` to match the freshly
    resolved source; on a source drift we fall through to a FRESH mint (new
    tokens, new generation, new source tags) even though the form fingerprint
    is unchanged.

    Returns ``(tokens, fresh)`` — the token list in the order ``specs`` was
    emitted, and ``fresh=True`` iff this call took the FRESH-mint path (the aqp:
    callsite uses ``fresh`` to write the durable mint-intent only once per card,
    not on every byte-identical re-render).
    """
    _prune_expired_pick_tokens()
    cache_key = (user_id, thread_id or 0, window_id, fingerprint)
    spec_list = list(specs)

    cached = _pick_token_cache.get(cache_key)
    if (
        cached is not None
        and cached.consumed_generation is None
        and cached.source_kind == source_kind
        and cached.source_fingerprint == source_fingerprint
        and len(cached.tokens) == len(spec_list)
        and all(t in _pick_tokens for t in cached.tokens)
    ):
        # Cache-REUSE: unchanged form AND unchanged source, all tokens still
        # live, not tombstoned. Same tokens, NO generation bump.
        logger.debug(
            "AUQ_MINT path=reuse window=%s fp=%s source_kind=%s source_fp=%s generation=%s n_tokens=%d",
            window_id,
            fingerprint[:8],
            source_kind,
            source_fingerprint[:8],
            cached.row_generation,
            len(cached.tokens),
        )
        return cached.tokens, False

    # FRESH mint: drop any stale/tombstoned row at this key, allocate a new
    # generation, mint a token per spec stamped with that generation.
    _pick_token_cache.pop(cache_key, None)
    # Stale-row hygiene (so D3-β's route-wide deadline refresh only keeps the
    # CURRENT card's tokens alive, and memory stays bounded): a fresh mint means
    # a NEW card generation is rendered for this route, so any OTHER
    # NON-tombstoned row for the same (user, thread, window) from a prior
    # fingerprint is no longer the visible card — drop it + its tokens. Keep
    # TOMBSTONED rows (a losing sibling still reads their consumed_generation).
    for other_key in list(_pick_token_cache.keys()):
        o_user, o_thread, o_window, _o_fp = other_key
        if (
            o_user == user_id
            and o_thread == (thread_id or 0)
            and o_window == window_id
            and other_key != cache_key
        ):
            other_row = _pick_token_cache.get(other_key)
            if other_row is not None and other_row.consumed_generation is None:
                for tok in other_row.tokens:
                    _pick_tokens.pop(tok, None)
                    _reservations.pop(tok, None)
                _pick_token_cache.pop(other_key, None)
    deadline = time.monotonic() + _PICK_TOKEN_TTL_SECONDS
    generation = _next_generation()
    tokens = [
        mint(
            PickTokenEntry(
                window_id=window_id,
                user_id=user_id,
                thread_id=thread_id,
                fingerprint=fingerprint,
                option_number=spec.option_number,
                option_label=spec.option_label,
                is_review_submit=spec.is_review_submit,
                expires_at=deadline,
                source_kind=source_kind,
                source_fingerprint=source_fingerprint,
                row_generation=generation,
            )
        )
        for spec in spec_list
    ]
    _pick_token_cache[cache_key] = _CacheRow(
        tokens=tokens,
        row_generation=generation,
        source_kind=source_kind,
        source_fingerprint=source_fingerprint,
    )
    logger.debug(
        "AUQ_MINT path=fresh window=%s fp=%s source_kind=%s source_fp=%s generation=%s n_tokens=%d",
        window_id,
        fingerprint[:8],
        source_kind,
        source_fingerprint[:8],
        generation,
        len(tokens),
    )
    return tokens, True


def peek(token: str) -> PickTokenEntry | None:
    """Look up a token WITHOUT consuming it. Returns the entry or None.

    Expired tokens are pruned as a side effect so the caller can treat a None
    return as "definitely gone" without re-checking expiry. Used callback-side
    SOLELY to read ``entry.window_id`` before the stale-window lease check;
    the owner check + single-use consume happen atomically inside
    ``validate_and_consume`` (never via this peek).
    """
    _prune_expired_pick_tokens()
    return _pick_tokens.get(token)


def stable_key(entry: PickTokenEntry) -> str:
    """Reconstruct the Wave-3 ledger key from a live pick-token entry.

    Pure derivation over ``make_route_hash`` / ``make_ledger_key`` /
    ``fingerprint[:8]`` / ``option_number`` — the SAME construction the minter
    uses for callback_data, so the validator's reconstructed key and the
    minter's emitted key come from one function. Used by the callback's
    collision-defense branch.
    """
    return auq_ledger.make_ledger_key(
        auq_ledger.make_route_hash(entry.user_id, entry.thread_id, entry.window_id),
        entry.fingerprint[:8],
        entry.option_number,
    )


def prune_for_route(user_id: int, thread_id: int | None, window_id: str) -> None:
    """Drop every cached pick-token row + its tokens for one route's windows.

    Lock-prune callsite used by the render path before re-minting. Conservative
    O(n) scan over the small cache; clears the sibling tokens of any row whose
    key matches ``(user_id, thread_id_or_0, window_id, *)``.
    """
    norm_thread = thread_id or 0
    for cache_key in list(_pick_token_cache.keys()):
        key_user, key_thread, key_window, _fp = cache_key
        if (
            key_user == user_id
            and key_thread == norm_thread
            and key_window == window_id
        ):
            row = _pick_token_cache.pop(cache_key, None)
            if row is not None:
                for tok in row.tokens:
                    _pick_tokens.pop(tok, None)
                    _reservations.pop(tok, None)


async def refresh_route_deadlines(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    *,
    min_remaining_s: float,
    now: float | None = None,
) -> int:
    """D3-β: re-stamp the TTL of a VISIBLY-LIVE card's pick tokens.

    The poller calls this at every live-card-preserve branch (same-hash idle,
    anchor-visible Submit, side-file-live) so a token's lifetime tracks the
    card's OBSERVED lifetime instead of a fixed 300s wall clock — closing the
    reported "first tap after a long idle is swallowed" bug at its source (the
    token is never pruned out from under a still-on-screen card).

    For each live, NON-tombstoned cache row of the route, every token that is
    STILL LIVE (``now < expires_at``) and within ``min_remaining_s`` of its
    deadline is REPLACED in ``_pick_tokens`` with a copy whose only change is
    ``expires_at = now + TTL``. The token string, ``fingerprint``, source tags,
    and ``row_generation`` are preserved, so the rendered keyboard is
    byte-identical (Telegram ``MESSAGE_NOT_MODIFIED``) and ``_commit_phase_c``'s
    generation classification is untouched. A genuinely-expired token
    (``expires_at <= now``) is NOT resurrected — it still prunes; tombstoned
    rows (``consumed_generation`` set) are skipped. Returns the count refreshed.

    Holds ``_store_lock`` and does no ``await`` after acquiring it (the body is
    a non-yielding sync section), so it never interleaves with
    ``validate_and_consume``'s reserved slow phase.
    """
    norm_thread = thread_id or 0
    refreshed = 0
    async with _store_lock:
        if now is None:
            now = time.monotonic()
        deadline = now + _PICK_TOKEN_TTL_SECONDS
        for cache_key, row in _pick_token_cache.items():
            key_user, key_thread, key_window, _fp = cache_key
            if (
                key_user != user_id
                or key_thread != norm_thread
                or key_window != window_id
                or row.consumed_generation is not None
            ):
                continue
            for tok in row.tokens:
                entry = _pick_tokens.get(tok)
                if entry is None:
                    continue
                # Re-stamp ONLY a still-live token nearing expiry. ``now <
                # expires_at`` is the non-resurrection guard (a token already
                # past its deadline must still prune, never get a new lease).
                if now < entry.expires_at <= now + min_remaining_s:
                    _pick_tokens[tok] = replace(entry, expires_at=deadline)
                    refreshed += 1
    return refreshed


def reset_for_tests() -> None:
    """Test-only: clear the token store, cache, reservations, and generation.

    Resets ``_pick_tokens``, ``_pick_token_cache``, the reservation markers,
    the per-row generations/tombstones (cleared with the cache), AND the
    module-global ``_generation_counter`` back to its initial value so a test
    that consumed into a tombstone cannot leak a generation into the next test.
    """
    global _generation_counter
    _pick_tokens.clear()
    _pick_token_cache.clear()
    _reservations.clear()
    _recovery_row_reservations.clear()
    _generation_counter = _INITIAL_GENERATION


# ── The atomic reservation-based validate-and-consume ─────────────────────────


@dataclass(frozen=True)
class PickValidation:
    """The typed outcome of ``validate_and_consume``.

    ``entry`` is the minted ``PickTokenEntry`` (present whenever the token was
    found at phase (a)); ``current_form`` is the live re-parse, supplied on
    ``ok`` so the caller's submit-guard can compare against the same form the
    consume validated.
    """

    outcome: Literal[
        "ok",
        "wrong_user",
        "expired",
        "already_consumed",
        "stale_form",
        "source_drift",
        "window_gone",
    ]
    entry: PickTokenEntry | None
    current_form: AskUserQuestionForm | None


_ReservePhaseA = tuple[PickValidation | None, PickTokenEntry | None, str | None]


def _reserve_phase_a(token: str, sender_id: int) -> _ReservePhaseA:
    """Phase (a) body, run under the store lock (caller holds ``_store_lock``).

    Returns ``(early_result, entry, reserved_by)``:
      - a terminal ``PickValidation`` in ``early_result`` (token absent →
        ``expired``; sender mismatch → ``wrong_user`` WITHOUT reserving;
        already reserved/consumed by another in-flight caller →
        ``already_consumed``), with ``entry``/``reserved_by`` None; OR
      - ``(None, entry, reserved_by)`` after minting a unique reservation owner
        id and marking the token RESERVED.
    """
    _prune_expired_pick_tokens()
    entry = _pick_tokens.get(token)
    if entry is None:
        # Fully-consumed or never-existed token. The ledger (consulted by the
        # callback BEFORE this call) already answered any real sequential
        # duplicate, so this is the benign "refresh" case — NOT already_consumed.
        return PickValidation("expired", None, None), None, None
    if sender_id != entry.user_id:
        # Owner check BEFORE reserving: a wrong-user tap must not reserve or
        # burn the legitimate owner's token.
        return PickValidation("wrong_user", entry, None), None, None
    if token in _reservations:
        # A concurrent in-flight caller already owns the reservation — the
        # second caller never enters the slow path.
        return PickValidation("already_consumed", entry, None), None, None
    reserved_by = uuid.uuid4().hex
    _reservations[token] = reserved_by
    return None, entry, reserved_by


def _commit_phase_c(token: str, entry: PickTokenEntry) -> PickValidation:
    """Phase (c) body, run under the store lock (caller holds ``_store_lock``).

    WIN the consume, or classify the loss by generation. Pops the token +
    evicts siblings + tombstones the row on a win; reads the row's
    ``consumed_generation`` tombstone on a loss to distinguish
    ``already_consumed`` (generation match) from ``expired``.
    """
    cache_key = (
        entry.user_id,
        entry.thread_id or 0,
        entry.window_id,
        entry.fingerprint,
    )
    if token not in _pick_tokens:
        # Lost the race / TTL-pruned. Classify by the row's tombstone.
        row = _pick_token_cache.get(cache_key)
        if (
            row is not None
            and row.consumed_generation is not None
            and row.consumed_generation == entry.row_generation
        ):
            # A sibling from the SAME generation won the consume.
            return PickValidation("already_consumed", entry, None)
        # Row never tombstoned, re-minted to a newer generation, or TTL-pruned
        # away (no generation match) → benign refresh.
        return PickValidation("expired", entry, None)

    # WIN: pop this token, evict siblings, tombstone the row with this
    # generation. Keep the row as a tombstone (tokens emptied) until TTL prune.
    _pick_tokens.pop(token, None)
    _reservations.pop(token, None)
    row = _pick_token_cache.get(cache_key)
    if row is not None:
        for sib in row.tokens:
            if sib != token:
                _pick_tokens.pop(sib, None)
                _reservations.pop(sib, None)
        row.tokens = []
        row.consumed_generation = entry.row_generation
        row.tombstoned_at = time.monotonic()
    return PickValidation("ok", entry, None)


async def validate_and_consume(
    token: str,
    sender_id: int,
    *,
    capture_pane: Callable[[str, int], Awaitable[str | None]],
    find_window_by_id: Callable[[str], Awaitable[object | None]],
) -> PickValidation:
    """Atomically validate + single-use-consume a pick token by reservation.

    EXCLUSIVE RESERVATION — the store lock is NEVER held across pane/window
    I/O, and phase (b) is EXCEPTION/CANCELLATION-SAFE via a pre-initialized
    ``completed_ok`` boolean + try/finally:

      (a) Under the store lock: look up the token (absent → ``expired``); owner
          check BEFORE reserving (mismatch → ``wrong_user``, no reservation);
          already reserved/consumed by another in-flight caller →
          ``already_consumed``; else mint a unique ``reserved_by`` owner id,
          mark RESERVED, release the lock.
      (b) Lock RELEASED — the slow work, wrapped in try/finally:
            find_window_by_id → ``window_gone`` if None; capture_pane;
            resolve_auq_source (the SAME leaf resolver mint used) +
            resolve_ask_form; FORM-fingerprint compare → ``stale_form``;
            SOURCE compare (kind + source_fingerprint vs the minted tags) →
            ``source_drift``; then phase (c). On a WINNING consume set
            ``completed_ok = True``.
          finally: if not completed_ok (modeled reject, raised exception, OR
            task cancellation), re-acquire the lock and unreserve the token
            ONLY IF it is still reserved by THIS call's ``reserved_by`` owner
            id (so we never clear a reservation a re-minted/other call owns).
      (c) Re-acquire the lock to WIN the consume: token gone + row tombstoned
          with a MATCHING generation → ``already_consumed``; token gone with no
          generation match → ``expired``; else pop + evict siblings + tombstone
          the row (``ok`` to exactly one caller per cache-row generation).

    Pane capture / window lookup are INJECTED so this module has no
    telegram/tmux import; ``capture_pane(window_id, scrollback_lines)`` and
    ``find_window_by_id(window_id)`` mirror the live callsites.
    """
    from ..terminal_parser import resolve_ask_form

    # Phase (a): owner-check-then-reserve under the lock.
    async with _store_lock:
        early, entry, reserved_by = _reserve_phase_a(token, sender_id)
    if early is not None:
        return early
    assert entry is not None and reserved_by is not None

    window_id = entry.window_id
    completed_ok = False  # SOLE finally predicate; set before phase (b)'s try.
    try:
        w = await find_window_by_id(window_id)
        if w is None:
            return PickValidation("window_gone", entry, None)

        pane = await capture_pane(window_id, 500)
        live_source = resolve_auq_source(window_id, None, pane or "")
        current_form = resolve_ask_form(live_source.payload, pane) if pane else None

        if current_form is None or current_form.fingerprint() != entry.fingerprint:
            return PickValidation("stale_form", entry, None)

        # Source parity (measurable): the re-resolved (kind, source_fingerprint)
        # must match the minted tags. Reachable only for side_file/jsonl_cache —
        # for the pane kind the FORM-fingerprint check above fires first.
        if (
            live_source.kind != entry.source_kind
            or live_source.source_fingerprint != entry.source_fingerprint
        ):
            return PickValidation("source_drift", entry, None)

        # Phase (c): win-or-lose the consume under the lock.
        async with _store_lock:
            result = _commit_phase_c(token, entry)
        if result.outcome == "ok":
            completed_ok = True
            # Hand the live re-parse back so the caller's submit-guard compares
            # against the same form the consume validated.
            return PickValidation("ok", entry, current_form)
        return result
    finally:
        if not completed_ok:
            # Modeled reject, raised exception, OR task cancellation. Unreserve
            # ONLY this call's reservation (owner-id guard) so the token stays
            # in the store for a legitimate later tap and we never clear a
            # reservation a re-minted/other call now owns.
            async with _store_lock:
                if _reservations.get(token) == reserved_by:
                    _reservations.pop(token, None)


# ── D2 restart-recovery: re-dispatch a token-less tap after a bot restart ─────


@dataclass(frozen=True)
class PickRecovery:
    """Typed outcome of ``recover_and_consume``.

    On ``ok`` the ``accepted`` ledger claim has ALREADY been written (inside the
    row reservation), so the caller only dispatches the digit + writes
    ``digit_sent`` / ``dispatched`` at ``ledger_key``. ``current_form`` is the
    live re-parse so the caller's Submit guard compares the same form.
    """

    outcome: Literal[
        "ok",
        "wrong_user",
        "window_gone",
        "stale_form",
        "source_drift",
        "superseded",
        "already",
        "in_progress",
    ]
    ledger_key: str | None = None
    window_id: str | None = None
    thread_id: int | None = None
    option_number: int | None = None
    is_review_submit: bool = False
    option_label: str | None = None
    current_form: AskUserQuestionForm | None = None


def _any_sibling_claimed(
    route_hash: str, fp8: str, option_numbers: Iterable[int]
) -> bool:
    """True iff ANY of the row's sibling option ledger keys already has a row.

    Row-level single-use, restart-DURABLE: a single-select row is spent the
    moment ANY one of its options reaches the action ledger
    (accepted/digit_sent/dispatched/failed). Bounded ≤9 ``lookup``s — no ledger
    scan API. Covers the crash-between-accepted-and-row-tomb case (the 24h ledger
    row outlives the in-memory tomb) AND a sibling whose key the top callback gate
    missed via collision-suppression (``ledger_key=None``).
    """
    return any(
        auq_ledger.lookup(auq_ledger.make_ledger_key(route_hash, fp8, n)) is not None
        for n in option_numbers
    )


def _recovery_source_parity_ok(
    intent: PickIntent,
    sf: RecoverySideFile | None,
) -> bool:
    """Source parity at recovery (the caller already matched the FORM fingerprint).

    - ``pane``: the form-fingerprint match subsumes source parity.
    - ``side_file``: present → canonical digest must equal the stored one; genuinely
      gone (read-TTL-FREE liveness ``False``) → pane fallback (the form already
      matches); else (present-but-drifted) → drift.
    - ``jsonl_cache``: the in-process getter is wiped on restart → cannot
      re-derive → decline.
    """
    kind = intent.source_kind
    if kind == "pane":
        return True
    if kind == "side_file":
        session = intent.session_id
        if not session:
            return False
        if sf is not None:
            return sf.source_fingerprint == intent.source_fingerprint
        return not side_file_live_for_session(session)
    return False


async def recover_and_consume(
    token: str,
    intent: PickIntent,
    sender_id: int,
    *,
    capture_pane: Callable[[str, int], Awaitable[str | None]],
    find_window_by_id: Callable[[str], Awaitable[object | None]],
) -> PickRecovery:
    """Row-scoped restart-recovery of a token-less pick tap.

    Reached only from the callback's dead branches AFTER the top ledger gate, so
    a recoverable tap provably has no blocking ledger row for its own option key.
    This re-runs the live validation against the DURABLE ``intent`` (there is no
    in-memory token to anchor ``validate_and_consume``):

      (A) Under the store lock: owner-auth; POSITIVE PROOF OF IN-MEMORY LOSS (any
          ``_pick_token_cache`` row at the row key ⇒ this process still has /
          just consumed the card → decline ``superseded``; this is what makes D2
          strictly the restart net); row reservation (concurrent sibling tap →
          ``in_progress``); per-sibling ledger guard (any sibling claimed →
          ``already``).
      (B) Lock RELEASED: read the side file read-TTL-free; capture the pane;
          rebuild the FULL form from the side-file payload (so a >5min compressed
          pane still matches the minted fingerprint); FORM-fp compare →
          ``stale_form``; source parity → ``source_drift``.
      (C) Re-acquire the lock, RE-RUN the proofs (cache-row + sibling ledger — a
          live re-render or a racing live dispatch could have appeared across the
          await), write the ``accepted`` claim at the reconstructed key, and tomb
          the whole durable row. Releasing the reservation after ``accepted`` is
          safe — the top gate then blocks every subsequent tap.

    Pane capture / window lookup are INJECTED (no telegram/tmux import).
    """
    from ..terminal_parser import resolve_ask_form

    route_hash = auq_ledger.make_route_hash(
        intent.user_id, intent.thread_id, intent.window_id
    )
    fp8 = intent.full_fingerprint[:8]
    ledger_key = auq_ledger.make_ledger_key(route_hash, fp8, intent.option_number)
    cache_key = (
        intent.user_id,
        intent.thread_id or 0,
        intent.window_id,
        intent.full_fingerprint,
    )

    # Phase (A): owner-auth + positive-proof-of-loss + reserve + sibling guard.
    async with _store_lock:
        if sender_id != intent.user_id:
            return PickRecovery("wrong_user")
        if cache_key in _pick_token_cache:
            # Live row → normal path owns it; tombstoned row → this process just
            # consumed it. Either way NOT a restart loss.
            return PickRecovery("superseded")
        if cache_key in _recovery_row_reservations:
            return PickRecovery("in_progress")
        if _any_sibling_claimed(route_hash, fp8, intent.sibling_option_numbers):
            return PickRecovery("already")
        reserved_by = uuid.uuid4().hex
        _recovery_row_reservations[cache_key] = reserved_by

    try:
        # Phase (B): lock released — pane / form / source parity.
        sf: RecoverySideFile | None = None
        if intent.source_kind == "side_file" and intent.session_id:
            sf = read_side_file_for_recovery(intent.session_id)

        w = await find_window_by_id(intent.window_id)
        if w is None:
            return PickRecovery("window_gone")
        pane = await capture_pane(intent.window_id, 500)
        payload = sf.payload if sf is not None else None
        current_form = resolve_ask_form(payload, pane) if pane else None
        if (
            current_form is None
            or current_form.fingerprint() != intent.full_fingerprint
        ):
            return PickRecovery("stale_form")
        if not _recovery_source_parity_ok(intent, sf):
            return PickRecovery("source_drift")
        # Submit guard BEFORE the accepted claim: a review-Submit intent only
        # fires when the live review screen still has the cursor on Submit
        # (option 1) with a matching label. Replicates the live path's
        # ``_review_submit_cursor_ok`` (kept here, not in the caller, so a moved
        # review screen declines BEFORE phase (C) writes ``accepted`` — otherwise
        # the ledger would be stuck at ``accepted`` and later taps would answer
        # "Action in progress" forever).
        if intent.is_review_submit and not (
            current_form.is_review_screen
            and current_form.options
            and current_form.options[0].cursor
            and current_form.options[0].number == 1
            and current_form.options[0].label == intent.option_label
        ):
            return PickRecovery("stale_form")

        # Phase (C): re-acquire, re-run the proofs, claim + tomb under the lock.
        async with _store_lock:
            if cache_key in _pick_token_cache:
                return PickRecovery("superseded")
            if _any_sibling_claimed(route_hash, fp8, intent.sibling_option_numbers):
                return PickRecovery("already")
            auq_ledger.record(
                ledger_key,
                state="accepted",
                user_id=intent.user_id,
                window_id=intent.window_id,
                full_fingerprint=intent.full_fingerprint,
                option_number=intent.option_number,
                option_label=intent.option_label,
            )
            pick_intent.consume_row(token)
        return PickRecovery(
            outcome="ok",
            ledger_key=ledger_key,
            window_id=intent.window_id,
            thread_id=intent.thread_id,
            option_number=intent.option_number,
            is_review_submit=intent.is_review_submit,
            option_label=intent.option_label,
            current_form=current_form,
        )
    finally:
        # Always release THIS call's row reservation (owner-id guard). After a
        # winning claim the ledger gate takes over, so the reservation is no
        # longer needed; on any decline/raise it must not leak.
        async with _store_lock:
            if _recovery_row_reservations.get(cache_key) == reserved_by:
                _recovery_row_reservations.pop(cache_key, None)


def _mint_spec(
    option_number: int, option_label: str, is_review_submit: bool
) -> _MintSpec:
    """Construct a ``_MintSpec`` (public-ish helper for the minter callsites)."""
    return _MintSpec(
        option_number=option_number,
        option_label=option_label,
        is_review_submit=is_review_submit,
    )
