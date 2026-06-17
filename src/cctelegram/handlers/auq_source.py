"""Trust boundary for the PreToolUse AskUserQuestion side file + the single AUQ-source resolver.

This neutral leaf owns two things behind a tiny interface:

  1. The *untrusted-file trust boundary* — nothing outside this module
     reads ``auq_pending/<session_id>.json`` or constructs that path.
     Path-traversal defense, schema/fingerprint validation (recompute,
     never trust the stored fingerprint), TTL + future-skew guards, the
     pane-projection consistency predicate (with the ``checked_any``
     vacuous-true fail-closed guard), and the bot-startup GC all live here.

  2. The *single AUQ-source resolver* — ``resolve_auq_source(...)`` returns a
     typed ``ResolvedAuqSource`` with a per-kind ``source_fingerprint``. Both
     the render/mint path (``interactive_ui``) and the validate path
     (``pick_token``) import this resolver, so mint/validate source parity is
     a call-graph fact rather than a comment promise, and the fingerprint is
     the measurable parity witness.

Core responsibilities:
  - Resolve "what is the trustworthy AUQ source for this window right now?"
    via the priority chain side_file → jsonl_cache → pane.
  - Own the per-window ``_pretool_ask_records`` cache (revalidate-on-every-call;
    never stale-serve).
  - Stay a true leaf: imports neither ``interactive_ui`` nor ``pick_token``;
    the in-process JSONL cache is read through an injected getter.

Key components:
  - ``ResolvedAuqSource`` / ``resolve_auq_source`` — the typed resolver.
  - ``DispatchAuqSource`` / ``resolve_auq_source_for_dispatch`` — the read-TTL-FREE
    but pane-consistency-CHECKED dispatch source (carries the resolved form); a
    long-open card never flaps ``side_file``→``pane`` on read-TTL ageout.
  - ``PreToolAskRecord`` / ``resolve_record`` — the side-file trust boundary.
  - ``peek_sticky_source`` — re-resolve the EXACT source a callback was minted
    against (side_file / jsonl_cache), pane-AGNOSTIC, so the ``aqt:`` toggle can
    pin its minted source through a transient render→tap source flip.
  - ``side_file_live_for_session`` / ``side_file_live_for_window`` —
    pane-INDEPENDENT "is the AUQ still live?" authority for the card-clear
    gate and the startup orphan reconciler (presence + schema + future-skew,
    deliberately NO read-TTL and NO pane-consistency check). The session-keyed
    form is canonical; the window form is a ``peek``-resolving wrapper.
  - ``unlink_for_session`` / ``forget_for_window`` / ``gc_stale`` — lifecycle.
  - ``set_jsonl_cache_getter`` / ``reset_for_tests`` — injection + test seam.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ..session import peek_session_id_for_window
from ..utils import app_dir

if TYPE_CHECKING:
    # Annotation-only — kept out of module import time so ``auq_source`` stays a
    # leaf with no eager ``terminal_parser`` edge (the parser fns it calls are
    # deferred-imported inside the functions that use them).
    from ..terminal_parser import AskUserQuestionForm

logger = logging.getLogger(__name__)


# ── Injected in-process JSONL cache getter (avoids the import cycle) ─────────
#
# The ``jsonl_cache`` branch needs to read ``interactive_ui``'s in-process
# ``_last_completed_ask_tool_input`` cache, but this leaf must not import
# ``interactive_ui``. So the getter is INJECTED, with a pinned lifecycle:
#   - default = the no-op below, so the module is importable and resolves the
#     ``pane`` branch correctly even before any wiring;
#   - ``set_jsonl_cache_getter`` rebinds it once at ``bot.post_init``;
#   - ``reset_for_tests`` rebinds it back to the no-op default.
_jsonl_cache_getter: Callable[[str], dict | None] = lambda _window_id: None  # noqa: E731


def set_jsonl_cache_getter(getter: Callable[[str], dict | None]) -> None:
    """Inject the in-process JSONL cache reader for the ``jsonl_cache`` branch.

    Called once at ``bot.post_init`` with a closure over
    ``interactive_ui._last_completed_ask_tool_input.get``. Keeping this an
    injection (rather than importing ``interactive_ui``) is what makes this
    module a true leaf with no import cycle.
    """
    global _jsonl_cache_getter
    _jsonl_cache_getter = getter


# ── The typed AUQ source ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class ResolvedAuqSource:
    """The trustworthy AUQ source for a window, plus a parity fingerprint.

    ``kind`` records which priority branch won; ``payload`` is the source
    ``tool_input`` dict for ``side_file``/``jsonl_cache`` and ``None`` for the
    ``pane`` branch (no source dict exists). ``source_fingerprint`` is a stable
    sha over the canonical source representation (per-kind, below). Because the
    resolver is called identically at render/mint and at validate, a consumer
    that records this fingerprint at mint and recomputes it at validate can
    surface a render/tap source mismatch as a MEASURABLE ``source_drift``
    outcome rather than a silent pass.
    """

    kind: Literal["side_file", "jsonl_cache", "pane"]
    payload: dict | None
    source_fingerprint: str


def _canonical_dict_fingerprint(payload: dict) -> str:
    """sha256 over the canonical JSON of a source ``tool_input`` dict.

    Used by the ``side_file`` and ``jsonl_cache`` kinds. Canonical JSON
    (sorted keys, no whitespace) makes the digest stable across dict
    construction order, so the same logical source always fingerprints the
    same and a mutated source dict fingerprints differently.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _pane_fingerprint(pane_text: str) -> str:
    """sha256 over the resolved form's canonical repr for the ``pane`` kind.

    The pane-only case has no source dict, so the resolved FORM is the source
    representation. Hashing ``AskUserQuestionForm._canonical_repr()`` — the
    SAME canonical input ``terminal_parser.fingerprint()`` hashes — keeps the
    pane branch fingerprintable. NOTE: because this shares the canonical input
    with the FORM fingerprint, and ``validate_and_consume`` checks the form
    fingerprint BEFORE the source fingerprint, a changed pane always returns
    ``stale_form`` first; the pane kind can never yield ``source_drift``.
    """
    from ..terminal_parser import resolve_ask_form

    form = resolve_ask_form(None, pane_text) if pane_text else None
    canonical = form._canonical_repr() if form is not None else ""
    return hashlib.sha256(canonical.encode()).hexdigest()


def resolve_auq_source(
    window_id: str,
    explicit: dict | None,
    pane_text: str,
) -> ResolvedAuqSource:
    """Resolve the trustworthy AUQ source for a window, with a parity fingerprint.

    Priority chain (UNCHANGED — formerly the resolver inlined in interactive_ui):

      1. ``side_file``  — a validated ``PreToolAskRecord`` matching the live pane
                          (``resolve_record`` below). ``payload`` = the record's
                          ``tool_input``; fingerprint over its canonical JSON.
      2. ``jsonl_cache`` — else the ``explicit`` JSONL dict if given, else the
                          INJECTED in-process cache. ``payload`` = that dict;
                          fingerprint over its canonical JSON.
      3. ``pane``       — no source dict. ``payload`` = ``None``; fingerprint over
                          the resolved form's ``_canonical_repr()``.

    INVARIANT: render and validate both call THIS function. The returned
    ``source_fingerprint`` is the parity witness a pick-token consumer records
    at mint and recomputes at validate, so source parity is a call-graph fact
    rather than a comment promise. (As of R5 the consumer does not yet record
    it; R4's ``pick_token.validate_and_consume`` wires the mint/validate
    compare.)
    """
    from ..terminal_parser import resolve_ask_form

    pane_form = resolve_ask_form(None, pane_text) if pane_text else None
    pretool_record = resolve_record(window_id, pane_form)
    if pretool_record is not None:
        return ResolvedAuqSource(
            kind="side_file",
            payload=pretool_record.tool_input,
            source_fingerprint=_canonical_dict_fingerprint(pretool_record.tool_input),
        )
    if explicit is not None:
        return ResolvedAuqSource(
            kind="jsonl_cache",
            payload=explicit,
            source_fingerprint=_canonical_dict_fingerprint(explicit),
        )
    cached = _jsonl_cache_getter(window_id)
    if cached is not None:
        return ResolvedAuqSource(
            kind="jsonl_cache",
            payload=cached,
            source_fingerprint=_canonical_dict_fingerprint(cached),
        )
    return ResolvedAuqSource(
        kind="pane",
        payload=None,
        source_fingerprint=_pane_fingerprint(pane_text),
    )


@dataclass(frozen=True)
class DispatchAuqSource:
    """The TTL-free dispatch AUQ source: source identity PLUS the resolved form.

    Distinct from :class:`ResolvedAuqSource` in two ways the dispatch path needs:

      1. It carries the resolved ``form`` (the dispatch compares its
         ``.fingerprint()`` to the minted fp16 — ``ResolvedAuqSource`` has no
         form), and
      2. it is produced by the read-TTL-FREE :func:`resolve_auq_source_for_dispatch`,
         so a long-open card never flaps ``side_file``→``pane`` purely because it
         aged past the 300s read-TTL (the item-1 source-drift class).

    ``payload`` is the side-file ``tool_input`` for the ``side_file`` kind and
    ``None`` for ``pane``; ``form`` may be ``None`` when the pane carries no
    parseable AUQ form (obscured/empty pane).
    """

    kind: Literal["side_file", "pane"]
    payload: dict | None
    source_fingerprint: str
    form: AskUserQuestionForm | None


def resolve_auq_source_for_dispatch(
    window_id: str, pane_text: str
) -> DispatchAuqSource:
    """Resolve the AUQ dispatch source TTL-FREE but pane-consistency-CHECKED.

    The dispatch path (PR-C; this is added + unit-tested only here) must trust
    the PreToolUse side file even on a card a user has left open for hours —
    past the 300s read-TTL :func:`resolve_auq_source` applies — yet must still
    fail CLOSED if the side file no longer matches the visible pane (a genuine
    advance/resolution, or a stale not-overwritten file). So it reads the record
    read-TTL-free (``_read_live_pretool_record(apply_ttl=False)``, which KEEPS
    the future-skew guard), KEEPS ``_record_consistent_with_pane``, and:

      - consistent side file → ``side_file`` kind with the SIDE-FILE form
        (``resolve_ask_form(record.tool_input, pane_text)`` — the side file
        carries the question TITLE the pane form lacks) and
        ``source_fingerprint=_canonical_dict_fingerprint(record.tool_input)``;
      - else → ``pane`` kind (``_pane_fingerprint`` + the pane form, which may be
        ``None`` on an obscured/empty pane).

    MUST NOT mutate ``_pretool_ask_records`` — ``resolve_record`` stays its sole
    mutator; ``_read_live_pretool_record`` already doesn't write it.
    """
    from ..terminal_parser import resolve_ask_form

    pane_form = resolve_ask_form(None, pane_text) if pane_text else None
    record = _read_live_pretool_record(window_id, apply_ttl=False)
    if record is not None:
        consistent, _reason = _record_consistent_with_pane(record, pane_form)
        if consistent:
            # The side-file form carries the question title (the pane form has
            # title=None on single-select panes); build it from the side-file
            # tool_input + the live pane (for cursor/layout).
            form = resolve_ask_form(record.tool_input, pane_text)
            return DispatchAuqSource(
                kind="side_file",
                payload=record.tool_input,
                source_fingerprint=_canonical_dict_fingerprint(record.tool_input),
                form=form,
            )
    # Fall back to the pane: a genuine advance/resolution, an obscured pane, or
    # no consistent side file.
    return DispatchAuqSource(
        kind="pane",
        payload=None,
        source_fingerprint=_pane_fingerprint(pane_text),
        form=pane_form,
    )


# ── The PreToolUse-hook side-file trust boundary ─────────────────────────────


@dataclass(frozen=True)
class PreToolAskRecord:
    """A PreToolUse-hook AUQ side-file record.

    Carries the structured ``tool_input`` from the AUQ tool_use payload
    PLUS provenance fields (session_id, tool_use_id, written_at,
    input_fingerprint) so the context gate can distinguish this source from
    JSONL-derived cache entries. Acceptance into the cache requires passing
    the projection predicate in ``_record_consistent_with_pane`` — NOT digest
    equality. The fingerprint is only a logging/integrity field.
    """

    tool_input: dict[str, Any]
    session_id: str
    tool_use_id: str  # may be "" if hook payload didn't carry one
    written_at: float
    input_fingerprint: str


# Per-window in-memory cache of accepted PreToolAskRecord. Populated by
# ``resolve_record`` on each gate use; revalidated on every call (no
# stale-serve). Cleared by ``forget_for_window`` when the AUQ resolves.
_pretool_ask_records: dict[str, PreToolAskRecord] = {}

_PRETOOL_TTL_SECONDS = 300  # 5 minutes (v4 plan; lowered from v2's 10)
# Codex chunk-3 P1: future-timestamp guard. A side file with
# ``written_at`` far in the future (clock skew, time tamper) would
# otherwise stay valid indefinitely because ``time.time() - written_at``
# is negative and the ``age > TTL`` check passes. Reject anything more
# than this many seconds ahead of the bot's clock.
_PRETOOL_FUTURE_SKEW_SECONDS = 30
_PRETOOL_SCHEMA_VERSION = 1
_PRETOOL_GC_AGE_SECONDS = 3600  # 1h — bot startup cleanup

# Codex chunk-3 P2 (path-traversal defense in depth): require the
# session_id used to construct ``auq_pending/<session_id>.json`` to
# be a canonical UUID. The hook validates this upstream, and the bot
# only resolves session_id via ``session_id_for_window`` which returns
# whatever the session map stored — but defense-in-depth keeps a
# corrupt/maliciously-edited session_map from constructing a side-file
# path outside the pending directory. Use ``fullmatch()`` (codex P3 —
# chunks 3+4) to reject trailing-newline edge cases that ``$`` would
# tolerate.
_SESSION_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
# Codex chunk-3 P1: ``input_fingerprint`` is read from the side file
# (UNTRUSTED) and previously logged as-is. A malformed or malicious
# write could inject question text. Recompute the fingerprint from
# the validated tool_input before logging — never trust the stored
# value. Defense-in-depth: also reject anything that isn't a strict
# 12-char hex digest before it enters the log surface.
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{12}")


def _pretool_side_file_path(session_id: str) -> Path | None:
    """Resolve the side-file path for ``session_id`` after UUID validation.

    Returns ``None`` if ``session_id`` isn't a canonical UUID — defense
    in depth against a corrupt session_map that ever stored e.g. ``../x``
    in the session_id field.
    """
    if not _SESSION_ID_RE.fullmatch(session_id):
        return None
    return app_dir() / "auq_pending" / f"{session_id}.json"


def _read_pretool_side_file(session_id: str) -> PreToolAskRecord | None:
    """Read and parse the AUQ PreToolUse side file for ``session_id``.

    Returns ``None`` on missing file (silent — hook hasn't fired yet or
    already cleaned up), invalid session_id (path-traversal defense in
    depth), JSON parse error, schema_version mismatch, or shape mismatch.

    Codex chunk-3 P1 fix: the ``input_fingerprint`` carried in the
    PreToolAskRecord is RECOMPUTED from the validated tool_input — never
    trusted from the file. The stored value could otherwise be poisoned
    by a malformed write and leak through the rejection-reason logs.

    Does NOT validate TTL or pane compatibility — those happen in
    ``resolve_record`` against the live pane.
    """
    path = _pretool_side_file_path(session_id)
    if path is None:
        # session_id failed UUID validation — refuse to construct a
        # path that could escape auq_pending/.
        logger.warning(
            "Pretool side file: refusing to resolve non-UUID session_id=%r",
            session_id,
        )
        return None
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.debug("Pretool side file unreadable for %s: %s", session_id, e)
        return None
    try:
        rec = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("Pretool side file malformed JSON for %s: %s", session_id, e)
        return None
    if not isinstance(rec, dict):
        logger.warning("Pretool side file is not a dict: %s", session_id)
        return None
    if rec.get("schema_version") != _PRETOOL_SCHEMA_VERSION:
        logger.warning(
            "Pretool side file schema_version=%r unknown for %s",
            rec.get("schema_version"),
            session_id,
        )
        return None
    tool_input = rec.get("tool_input")
    if not isinstance(tool_input, dict):
        logger.warning("Pretool side file tool_input invalid for %s", session_id)
        return None
    try:
        written_at = float(rec.get("written_at", 0))
    except (TypeError, ValueError):
        return None

    # Recompute the fingerprint from the validated tool_input — never
    # trust the stored value (codex chunk-3 P1). If pairs extraction
    # fails, the side file is malformed; reject.
    from ..terminal_parser import (
        questions_content_digest,
        questions_content_pairs_from_tool_input,
    )

    pairs = questions_content_pairs_from_tool_input(tool_input)
    if pairs is None:
        logger.warning(
            "Pretool side file tool_input failed shape validation for %s",
            session_id,
        )
        return None
    fingerprint = questions_content_digest(pairs)

    return PreToolAskRecord(
        tool_input=tool_input,
        session_id=str(rec.get("session_id", "") or session_id),
        tool_use_id=str(rec.get("tool_use_id", "") or ""),
        written_at=written_at,
        input_fingerprint=fingerprint,
    )


def _safe_record_labels(question: dict) -> tuple[str, ...] | None:
    """Extract ordered option labels from a tool_input question dict.

    Returns ``None`` on shape mismatch. Mirrors
    ``terminal_parser.questions_content_pairs_from_tool_input`` validation
    so the predicate fails closed on malformed records.
    """
    options = question.get("options")
    if not isinstance(options, list):
        return None
    labels: list[str] = []
    for o in options:
        if not isinstance(o, dict):
            return None
        label = o.get("label")
        if not isinstance(label, str):
            return None
        labels.append(label)
    return tuple(labels)


def _labels_are_subsequence(visible: tuple[str, ...], full: tuple[str, ...]) -> bool:
    """True if ``visible`` is a contiguous subsequence of ``full``.

    The pane may render only the visible region; earlier options can be
    pushed off the top by long descriptions. We still accept the record
    if whatever IS visible matches the corresponding contiguous slice of
    the record's labels.
    """
    if not visible:
        return False
    if len(visible) > len(full):
        return False
    for start in range(len(full) - len(visible) + 1):
        if full[start : start + len(visible)] == visible:
            return True
    return False


def _pane_labels_match_candidate_by_number(
    pane_form: AskUserQuestionForm,
    candidate_labels: tuple[str, ...],
) -> bool:
    """True when every visible numbered pane option matches that candidate slot.

    Compressed panes preserve option numbers even when earlier options are
    off-screen, so a visible ``3. Label C`` must match ``candidate_labels[2]`` —
    not just any occurrence of ``Label C``. If a future parser ever emits a
    visible option without a number, fall back to the legacy contiguous
    subsequence check because there is no stable slot to validate against.
    """
    from ..terminal_parser import is_affordance_label

    pane_labels = tuple(o.label for o in pane_form.options)
    if any(o.number is None for o in pane_form.options):
        return _labels_are_subsequence(pane_labels, candidate_labels)

    checked_any = False
    for option in pane_form.options:
        assert option.number is not None
        index = option.number - 1
        if 0 <= index < len(candidate_labels):
            if candidate_labels[index] != option.label:
                return False
            checked_any = True
        else:
            if is_affordance_label(option.label):
                continue
            return False
    return checked_any


def _record_consistent_with_pane(
    record: PreToolAskRecord,
    pane_form: AskUserQuestionForm | None,
) -> tuple[bool, str]:
    """v4 plan step 5: projection-based structural predicate.

    Returns ``(accepted, reason_code)``. On accept: ``(True, "ok")``.
    On reject: ``(False, code)`` where ``code`` is one of:
    ``no_pane_form``, ``no_candidate``, ``title_mismatch``,
    ``label_mismatch``, ``count_sanity``.

    Acceptance is structural — NOT digest equality. We compare projected
    fields one at a time so each edge case (title-missing,
    walkback-only-title, multi-question subset) has a principled answer.
    NEVER computes ``AskUserQuestionForm.fingerprint()`` here — that
    includes cursor/recommended/tab state and would reject valid records.
    """
    if pane_form is None or not pane_form.options:
        return False, "no_pane_form"

    raw_questions = record.tool_input.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        return False, "no_candidate"

    pane_labels = tuple(o.label for o in pane_form.options)
    pane_title = (pane_form.current_question_title or "").strip()
    candidate: dict | None = None

    # Step 5.a — pick a candidate record-question.
    if len(raw_questions) == 1 and isinstance(raw_questions[0], dict):
        candidate = raw_questions[0]
    elif pane_form.current_tab_inferred and pane_title:
        for q in raw_questions:
            if not isinstance(q, dict):
                continue
            qt = (q.get("question") or "").strip()
            if not qt:
                continue
            if qt.startswith(pane_title) or pane_title.startswith(qt):
                candidate = q
                break

    if candidate is None:
        # Multi-question fallthrough (current_tab_inferred=False, or no
        # title match): accept the FIRST question whose labels match the
        # visible labels as a subsequence.
        for q in raw_questions:
            if not isinstance(q, dict):
                continue
            q_labels = _safe_record_labels(q)
            if q_labels is None:
                continue
            if _labels_are_subsequence(pane_labels, q_labels):
                candidate = q
                break
    if candidate is None:
        return False, "no_candidate"

    # Step 5.b — TITLE check (conditional).
    # Skip only when:
    #   - pane title empty (including compressed panes where the current
    #     question has scrolled off-screen)
    #   - pane title sourced from walkback only (current_question_title is
    #     empty, pane_walkback_title may be set but is unreliable per the
    #     parser's docstring — DON'T use it for acceptance)
    #   - candidate has no question title
    #
    # Deliberately do NOT require contiguous-from-1 options here: compressed
    # panes can still expose a reliable current_question_title, and accepting
    # a stale side file on labels alone can dispatch the old digit against a
    # different live question. Residual: if the pane title is genuinely
    # unparseable and labels coincidentally match a stale not-overwritten side
    # file within TTL, that edge remains irreducible without question text.
    candidate_title = (candidate.get("question") or "").strip()
    if pane_title and candidate_title:
        if not (
            candidate_title.startswith(pane_title)
            or pane_title.startswith(candidate_title)
        ):
            return False, "title_mismatch"

    # Step 5.c — LABEL check (mandatory).
    candidate_labels = _safe_record_labels(candidate)
    if candidate_labels is None:
        return False, "no_candidate"
    if not _pane_labels_match_candidate_by_number(pane_form, candidate_labels):
        return False, "label_mismatch"

    # Step 5.d — option-count sanity for full match.
    if pane_labels == candidate_labels:
        if not pane_form.options_contiguous_from_one():
            return False, "count_sanity"

    return True, "ok"


def _read_live_pretool_record(
    window_id: str, *, apply_ttl: bool = True
) -> PreToolAskRecord | None:
    """Read the PreToolUse side-file record for ``window_id``, pane-AGNOSTIC.

    Everything ``resolve_record`` does EXCEPT the final
    ``_record_consistent_with_pane`` pane check:
    ``peek_session_id_for_window`` → ``_read_pretool_side_file`` → TTL guard
    (``age > _PRETOOL_TTL_SECONDS``) → future-skew guard
    (``age < -_PRETOOL_FUTURE_SKEW_SECONDS``) → return the ``PreToolAskRecord``,
    else ``None``.

    ``apply_ttl=False`` (the dispatch path, ``resolve_auq_source_for_dispatch``)
    SKIPS only the ``age > _PRETOOL_TTL_SECONDS`` read-TTL block — session-resolve,
    read, and the future-skew guard are KEPT. A long-open card aged past the
    read-TTL must not flip the dispatch source side_file→pane (the item-1
    source-drift class), but a clock-tampered file is still rejected.

    DELIBERATELY does NOT write ``_pretool_ask_records``: that cache's
    invariant is "consistent-with-pane records only", so ``resolve_record``
    stays its sole mutator. This helper is the pane-agnostic core used by
    both ``resolve_record`` (which then applies the pane check) and
    ``peek_sticky_source`` (which deliberately skips it so a transiently
    degraded pane can't break a source-stickiness pin). Reason codes are
    logged at DEBUG (same as ``resolve_record``); question text is never
    logged.
    """
    # Codex chunk-3 P2: peek (read-only) — never mutate session_manager
    # state by auto-creating a WindowState on miss. session_id_for_window
    # via get_window_state would have that side-effect for unknown windows.
    session_id = peek_session_id_for_window(window_id)
    if not session_id:
        logger.debug("Pretool resolve window=%s reason=missing_map", window_id)
        return None

    record = _read_pretool_side_file(session_id)
    if record is None:
        return None

    # Defense-in-depth: only log fingerprints that match the strict
    # hex shape. The reader recomputed it from validated tool_input,
    # so this should always be true, but guard against future drift.
    safe_fp = (
        record.input_fingerprint
        if _FINGERPRINT_RE.fullmatch(record.input_fingerprint)
        else "<invalid>"
    )

    # TTL check + future-skew guard (codex chunk-3 P1). Negative age
    # (timestamp in the future) is rejected to prevent a tampered or
    # clock-skewed file from staying valid indefinitely.
    age = time.time() - record.written_at
    if age < -_PRETOOL_FUTURE_SKEW_SECONDS:
        logger.debug(
            "Pretool resolve window=%s reason=future_skew age=%.1fs fp=%s",
            window_id,
            age,
            safe_fp,
        )
        return None
    if apply_ttl and age > _PRETOOL_TTL_SECONDS:
        logger.debug(
            "Pretool resolve window=%s reason=stale age=%.1fs fp=%s",
            window_id,
            age,
            safe_fp,
        )
        return None

    return record


def resolve_record(
    window_id: str,
    pane_form: AskUserQuestionForm | None,
) -> PreToolAskRecord | None:
    """Return the PreToolUse side-file record for ``window_id`` if it is
    consistent with the live pane parse, else ``None``.

    The cache invariant is revalidate-on-every-call: a record that no
    longer matches the pane (user navigated, picker advanced, label set
    drifted) MUST be evicted at the next call, not stale-served. This
    keeps wrong-action class bugs out of the cache layer.

    Reason codes for rejection are logged at DEBUG level (not INFO — the
    reader runs on every status-poll iteration when an AUQ is visible,
    and we don't want to flood the log). Question text is NEVER logged
    here; only the reason code + the record's fingerprint.

    The session/read/TTL/skew portion is delegated to
    ``_read_live_pretool_record`` (the pane-agnostic core); this function
    layers on the ``_record_consistent_with_pane`` check and remains the
    SOLE mutator of ``_pretool_ask_records``.
    """
    record = _read_live_pretool_record(window_id)
    if record is None:
        # Missing map, malformed, TTL/skew, or no session — evict any stale
        # cache (matching the pre-refactor behavior on every failure path).
        _pretool_ask_records.pop(window_id, None)
        return None

    # Defense-in-depth: only log fingerprints that match the strict
    # hex shape. The reader recomputed it from validated tool_input,
    # so this should always be true, but guard against future drift.
    safe_fp = (
        record.input_fingerprint
        if _FINGERPRINT_RE.fullmatch(record.input_fingerprint)
        else "<invalid>"
    )

    # Pane-compatibility predicate (revalidated every call).
    consistent, reason = _record_consistent_with_pane(record, pane_form)
    if not consistent:
        logger.debug(
            "Pretool resolve window=%s reason=%s fp=%s",
            window_id,
            reason,
            safe_fp,
        )
        _pretool_ask_records.pop(window_id, None)
        return None

    _pretool_ask_records[window_id] = record
    return record


def peek_sticky_source(
    window_id: str, minted_kind: str, minted_fingerprint: str
) -> dict | None:
    """Return the minted AUQ source's ``tool_input`` IF that exact source is
    still live and UNCHANGED since mint, WITHOUT the pane-consistency check.

    Used by the ``aqt:`` toggle to PIN the source it was minted against, so a
    transient pane degradation that would flip ``resolve_auq_source``
    (side_file → pane) cannot break the toggle. Returns None when the minted
    source is gone, changed (a new question replaced it), or was pane-only.

    Parity: the fingerprint is computed with the SAME
    ``_canonical_dict_fingerprint`` the minter used in ``resolve_auq_source``
    (NOT ``PreToolAskRecord.input_fingerprint``, which is a DIFFERENT digest —
    ``questions_content_digest``). Comparing the wrong digest would silently
    never match (mint/validate source-parity trap).
    """
    if minted_kind == "side_file":
        record = _read_live_pretool_record(window_id)
        if record is not None and (
            _canonical_dict_fingerprint(record.tool_input) == minted_fingerprint
        ):
            return record.tool_input
        return None
    if minted_kind == "jsonl_cache":
        cached = _jsonl_cache_getter(window_id)
        if isinstance(cached, dict) and (
            _canonical_dict_fingerprint(cached) == minted_fingerprint
        ):
            return cached
        return None
    return None


# ── Side-file lifecycle ───────────────────────────────────────────────────────


def unlink_for_session(session_id: str) -> None:
    """Best-effort unlink of the side file for ``session_id``.

    Public helper used by session_monitor when the OLD session_id is known
    at /clear time (the current ``WindowState.session_id`` has already been
    swapped to the new session by then). Silent on missing file / non-UUID
    session_id.
    """
    path = _pretool_side_file_path(session_id)
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as e:
        logger.debug(
            "Pretool side file unlink for session=%s failed: %s",
            session_id,
            e,
        )


def forget_for_window(window_id: str) -> None:
    """Evict the in-memory record for ``window_id`` and unlink its current side file.

    The side-file half of ``interactive_ui.forget_ask_tool_input`` —
    ``interactive_ui`` keeps its own context-marker / pending-claim
    bookkeeping and delegates this part. Resolves the window's CURRENT
    session_id; the /clear race (where the session_id is swapped under us
    with the OLD id) is handled separately by session_monitor via
    ``unlink_for_session`` with the old id.
    """
    _pretool_ask_records.pop(window_id, None)
    session_id = peek_session_id_for_window(window_id)
    if not session_id:
        return
    unlink_for_session(session_id)


def side_file_live_for_window(window_id: str) -> bool:
    """Pane-INDEPENDENT "is an AskUserQuestion still live for this window?".

    The lifecycle authority for the card-clear gate. Returns ``True`` iff a
    Thin window-keyed wrapper over :func:`side_file_live_for_session` — it
    resolves the window's session via ``peek_session_id_for_window`` and
    defers all liveness logic to the session-keyed core. Callers that ALREADY
    hold the session_id (e.g. the startup orphan reconciler, which also calls
    ``unlink_for_session``) MUST use :func:`side_file_live_for_session`
    directly so the liveness check and the unlink act on the SAME session —
    checking one source (peek → window_states) and acting on another
    (a session_id from elsewhere) is the mint/validate parity trap.

    Returns ``True`` iff a schema-valid PreToolUse side file exists for the
    window's bound session and its ``written_at`` is not implausibly in the
    future.

    Deliberately UNLIKE ``resolve_record`` in two load-bearing ways:

      1. It does NOT run ``_record_consistent_with_pane``. The whole point
         is that the visible pane may be obstructed (a Claude task-list
         overlay, a scrolled/compressed multi-step Submit screen,
         tool-output spam) while the question is genuinely pending on the
         Claude side. Requiring pane consistency here would re-create the
         exact bug this guards against — a live card torn down because its
         anchors scrolled out of the captured pane.

      2. It does NOT apply the ``_PRETOOL_TTL_SECONDS`` read-TTL. That TTL
         bounds *stale-render* risk on the resolve path; it must NOT bound
         *card liveness*. A question left unanswered longer than the TTL
         has NOT "expired on the other side of the bridge" — it is still
         waiting on the user. Only the future-skew guard is applied
         (rejects clock-tampered timestamps). Orphans are bounded by the
         real unlink paths (``forget_for_window`` on tool_result,
         ``unlink_for_session`` on /clear & window-delete) and the 1h
         startup ``gc_stale`` — never by a clear-gate timer.

    Strictly higher authority than the visible-pane liveness predicates,
    per the RouteRuntime contract that pane snapshots are reconciliation
    events of LOWER authority than the resolution lifecycle.

    Read-only: must NOT mutate ``_pretool_ask_records`` (``resolve_record``
    stays the sole cache mutator); a pure probe used by ``status_polling``'s
    pane-absent clear gate to refuse tombstoning a still-live card.

    Off-contract limitation: the side file is keyed by *session*, not
    *window*. Under 1 topic = 1 window = 1 session this is exact. If two
    windows are bound to one session (only reachable by double-``--resume``
    of the same session into two topics), a live AUQ on one window keeps
    this ``True`` for its sibling, so a *dead* card on the sibling lingers
    until the tool_result fan-out, a window switch, a topic close, or the
    1h GC clears it. A ``tool_use_id`` correlation would NOT help here: the
    JSONL ``tool_use`` (hence ``interactive_ui._last_auq_tool_use_id``) is
    buffered until the answer, and the side file's ``tool_use_id`` "may be
    ''", so both are typically unavailable during the live window. A
    schema-v2 side file capturing the hook-side ``window_id`` (the
    SessionStart hook already resolves it via ``TMUX_PANE``) COULD
    discriminate and is the natural fix if double-resume ever becomes
    supported; it is deferred here as off-contract. The bounded dead-card
    linger is accepted for now.
    """
    return side_file_live_for_session(peek_session_id_for_window(window_id) or "")


def side_file_live_for_session(session_id: str) -> bool:
    """Pane-INDEPENDENT AUQ liveness keyed by SESSION.

    The session-keyed core of :func:`side_file_live_for_window`. The side file
    is itself keyed by session (``auq_pending/<session_id>.json``), so this is
    the canonical form. Returns ``True`` iff a schema-valid side file exists
    for ``session_id`` and its ``written_at`` is not implausibly in the future
    — DELIBERATELY without the ``_PRETOOL_TTL_SECONDS`` read-TTL and without
    the pane-consistency check (see :func:`side_file_live_for_window` for the
    full rationale).

    Read-only: must NOT mutate ``_pretool_ask_records``. Use this (not the
    window wrapper) wherever the caller already holds the session_id and will
    act on it (e.g. pairing the check with ``unlink_for_session(session_id)``),
    so the check and the action target the same session.
    """
    if not session_id:
        return False
    record = _read_pretool_side_file(session_id)
    if record is None:
        return False
    # Future-skew guard ONLY (NOT the read-TTL): a side file written
    # implausibly far in the future (clock tamper) is rejected; an
    # old-but-unanswered AUQ is STILL live.
    if time.time() - record.written_at < -_PRETOOL_FUTURE_SKEW_SECONDS:
        return False
    return True


def peek_side_file_tool_use_id(session_id: str) -> str | None:
    """Return the side file's captured ``tool_use_id`` for ``session_id``.

    Thin public accessor over the private side-file reader so callers (the
    startup positive-proof reconciler in ``session_monitor``) need not import
    ``_read_pretool_side_file``. Returns ``None`` when no valid side file exists,
    or the captured ``tool_use_id`` (which "may be ''" if the hook payload didn't
    carry one) when it does. Read-only; pane-AGNOSTIC; no TTL.
    """
    rec = _read_pretool_side_file(session_id)
    return rec.tool_use_id if rec else None


def peek_side_file_written_at(session_id: str) -> float | None:
    """Return the side file's ``written_at`` for ``session_id`` — the PreToolUse
    hook's stamp of the AUQ tool_use INVOCATION instant.

    The AUQ prose-ordering anchor (PR-1): the turn's prose finalizes just before
    the tool_use, so ``final_at <= written_at + EPS`` pairs the prose to THIS
    picker even when the poller detected it long after the TTL aged out. Mirrors
    :func:`peek_side_file_tool_use_id` — a thin public accessor over the private
    reader, read-only, pane-AGNOSTIC, no read-TTL. Returns ``None`` when no valid
    side file exists or its ``written_at`` is implausibly in the future (the same
    future-skew guard the liveness predicate applies — a tampered/skewed stamp
    must not widen the freshness window)."""
    rec = _read_pretool_side_file(session_id)
    if rec is None:
        return None
    if time.time() - rec.written_at < -_PRETOOL_FUTURE_SKEW_SECONDS:
        return None
    return rec.written_at


@dataclass(frozen=True)
class RecoverySideFile:
    """The read-TTL-free side-file payload + its canonical source fingerprint.

    ``source_fingerprint`` is ``_canonical_dict_fingerprint(payload)`` — the SAME
    digest :func:`resolve_auq_source` stores as the ``side_file`` kind's
    ``source_fingerprint`` (NOT ``PreToolAskRecord.input_fingerprint``, a
    different 12-hex questions-content digest). ``payload`` is the side file's
    ``tool_input`` so recovery can reconstruct the FULL-options form (matching the
    minted fingerprint even when the live pane is compressed).
    """

    payload: dict
    source_fingerprint: str


def read_side_file_for_recovery(session_id: str) -> RecoverySideFile | None:
    """Read the side file for D2 restart-recovery, read-TTL-free + pane-agnostic.

    Returns the validated ``tool_input`` payload + its canonical source
    fingerprint, or ``None`` if the side file is absent / invalid.

    DELIBERATELY bypasses the 300s ``_PRETOOL_TTL_SECONDS`` read-TTL and the
    pane-consistency (``resolve_record``) demotion: D2 targets a card a user may
    have left open for hours (well past the read-TTL), and recovery compares the
    canonical digest DIRECTLY to the stored ``source_fingerprint`` and rebuilds
    the full form from ``payload`` — rather than re-resolving through the
    read-TTL'd / pane-demoting resolver, which would falsely report a long-idle
    side file as ``pane`` and wrongly decline.
    """
    record = _read_pretool_side_file(session_id)
    if record is None:
        return None
    return RecoverySideFile(
        payload=record.tool_input,
        source_fingerprint=_canonical_dict_fingerprint(record.tool_input),
    )


def gc_stale(*, is_live_session: Callable[[str], bool] | None = None) -> int:
    """Delete AUQ side files older than ``_PRETOOL_GC_AGE_SECONDS``.

    Best-effort. Called on bot startup. Returns the number of files
    deleted (useful for tests; the bot's startup log doesn't need it).
    Anything older than 1h is presumed stale — TTL on the read path is
    only 5min, so a 1h file definitely cannot be served. Crashes /
    kickstart-between-AUQs are the typical sources of these orphans.

    LIVENESS GATE (mirrors ``md_capture.gc_stale``): Claude BUFFERS the
    AskUserQuestion tool_use in JSONL until the prompt resolves, so a
    genuinely-live AUQ left open >1h has a stale-mtime side file that is
    STILL the card's liveness authority — reaping it on age alone would
    strand the live card. When ``is_live_session`` is supplied, it is called
    with the file STEM (= the ``<session_id>``) after the age test passes:
    True → SKIP (keep the live side file); an EXCEPTION → conservative SKIP
    (never delete on uncertainty; the raise is caught around the predicate
    call only so the rest of the pass continues). The predicate is INJECTED
    — ``auq_source`` stays a leaf and never imports a session module to learn
    liveness.
    """
    pending_dir = app_dir() / "auq_pending"
    if not pending_dir.is_dir():
        return 0
    cutoff = time.time() - _PRETOOL_GC_AGE_SECONDS
    deleted = 0
    try:
        entries = list(pending_dir.iterdir())
    except OSError as e:
        logger.warning("Pretool GC: iterdir on %s failed: %s", pending_dir, e)
        return 0
    for entry in entries:
        # Skip non-regular files; reject anything that doesn't match the
        # canonical "<uuid>.json" name to avoid touching unexpected files.
        if not entry.is_file():
            continue
        if not entry.name.endswith(".json"):
            continue
        stem = entry.stem
        if not _SESSION_ID_RE.fullmatch(stem):
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        if is_live_session is not None:
            try:
                if is_live_session(stem):
                    continue  # live AUQ — keep its side file
            except Exception:
                # Never delete on uncertainty; the predicate raising must not
                # abort the whole GC pass either.
                continue
        # Codex P2 (chunk 5): re-check mtime just before unlink. The
        # hook may have replaced this side file (atomic temp+rename)
        # between our initial stat and now; if so, skip — deleting a
        # fresh file would force fallback to labels-only for the
        # next AUQ on this session.
        try:
            current_mtime = entry.stat().st_mtime
        except OSError:
            continue
        if current_mtime >= cutoff:
            continue
        try:
            entry.unlink()
            deleted += 1
        except OSError as e:
            logger.debug("Pretool GC: unlink %s failed: %s", entry, e)
    if deleted:
        logger.info("Pretool GC: deleted %d stale side file(s)", deleted)
    return deleted


def reset_for_tests() -> None:
    """Test-only: clear the pretool record cache AND reset the injected getter.

    Rebinds ``_jsonl_cache_getter`` back to the no-op default so a test that
    re-points the getter to a fake cache cannot leak that behavior into the
    next test (the next test re-injects what it needs).
    """
    global _jsonl_cache_getter
    _pretool_ask_records.clear()
    _jsonl_cache_getter = lambda _window_id: None  # noqa: E731
