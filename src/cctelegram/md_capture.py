"""Bot-side reader + lifecycle for the MessageDisplay live-prose capture (Bug 2).

The ``MessageDisplay`` hook appender (``_md_display_appender.py``) writes each
streaming ``delta`` of an assistant message to a per-session NDJSON under
``<CC_TELEGRAM_DIR>/msg_display/<session>.ndjson`` while the message is still on
screen — BEFORE Claude Code co-flushes the whole turn (prose + the trailing
AskUserQuestion / ExitPlanMode ``tool_use``) to the session JSONL at resolution.
That buffering is Bug 2: the bot derives content from the JSONL via byte-offset
reads, so during a live prompt the explanatory prose isn't on the bridge and the
Telegram user chooses blind.

This module is the bot side of that mechanism. It:
  * resolves the appender path + writes the bot-managed ``--settings`` file that
    scopes the hook to bot-launched sessions (``ensure_capture_settings``);
  * reads the per-session NDJSON ON DEMAND at picker-render time and
    reconstructs the completed prose of each finalized message as a
    ``ProseRecord`` (``read_prose_records``) — accumulating the per-flush
    ``delta`` values because each hook invocation is a fresh process that cannot
    accumulate in memory, and ``MessageDisplay.message_id`` has no JSONL
    counterpart (so the bot, not the hook, owns grouping);
  * exposes ``normalize_prose`` — the SINGLE normalization contract shared by
    the live capture's ``norm_hash`` here and the post-resolution JSONL dedup
    (PR-D). Using one function on both sides is the mint/validate parity that
    keeps the live-shown text and the JSONL copy comparing equal;
  * tears down a session's capture file on resolution / teardown
    (``teardown_session``) and sweeps stale files at startup (``gc_stale``).

Pull-only by construction: there is no background tailer or observer channel
(the c313657 fan-out pattern is forbidden). The render path reads when it needs
the data; the bounded retry that waits for a not-yet-final message lives in the
caller (status_polling, PR-C).

The surface that POSTS a ``ProseRecord`` before the picker card, the freshness
gate, the shown-live marker, and the JSONL dedup all land in PR-C+D; this module
ships the capture + read + normalization + lifecycle primitives they build on.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .utils import app_dir, atomic_write_json

logger = logging.getLogger(__name__)

# Per-session NDJSON capture lives under this subdirectory of the config dir.
MD_DISPLAY_DIRNAME = "msg_display"
# The bot-managed settings file passed to ``claude --settings`` so the
# MessageDisplay hook fires only for bot-launched sessions (merges with the
# global SessionStart / PreToolUse hooks; verified for new launches).
_SETTINGS_FILENAME = "md_hook_settings.json"
# Hook timeout (seconds). The appender is sub-millisecond; this is a generous
# ceiling matching the AUQ PreToolUse precedent. Hook failures are non-blocking.
_MD_HOOK_TIMEOUT_S = 5


# ── Paths + capture-settings management ──────────────────────────────────────


def appender_path() -> Path:
    """Absolute path to the stdlib MessageDisplay appender script (shipped in
    the package, run directly so the package is never imported)."""
    return Path(__file__).resolve().parent / "_md_display_appender.py"


def capture_settings_path() -> Path:
    """Path to the bot-managed ``--settings`` file registering the hook."""
    return app_dir() / _SETTINGS_FILENAME


def msg_display_dir() -> Path:
    return app_dir() / MD_DISPLAY_DIRNAME


def session_ndjson_path(session_id: str) -> Path:
    return msg_display_dir() / f"{session_id}.ndjson"


def _resolve_session_path(session_id: str, base_dir: Path | None) -> Path:
    if base_dir is not None:
        return base_dir / MD_DISPLAY_DIRNAME / f"{session_id}.ndjson"
    return session_ndjson_path(session_id)


def _append_json_line(path: Path, obj: dict) -> None:
    """Append one compact NDJSON line via a single O_APPEND write (same atomic
    pattern as the hook appender + the AUQ ledger). Best-effort; a failed write
    is swallowed (a missed marker degrades to at worst a benign double-post)."""
    line = json.dumps(obj, separators=(",", ":")) + "\n"
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as e:
        logger.warning("md_capture: could not append marker to %s: %s", path, e)


def _hook_command() -> str:
    """The shell command Claude runs for the hook: the bot's own interpreter
    (absolute, guaranteed to exist with a stdlib) running the appender. Both
    paths are shell-quoted; the hook executes in the tmux pane, not the bot."""
    python = sys.executable or "python3"
    return f"{shlex.quote(python)} {shlex.quote(str(appender_path()))}"


def _desired_settings() -> dict:
    return {
        "hooks": {
            "MessageDisplay": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": _hook_command(),
                            "timeout": _MD_HOOK_TIMEOUT_S,
                        }
                    ]
                }
            ]
        }
    }


def ensure_capture_dir() -> Path:
    """Create the capture dir at mode 0700 (prose can carry sensitive context)
    and return it. Idempotent; tightens a pre-existing loose-mode dir."""
    d = msg_display_dir()
    try:
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    except OSError as e:
        logger.warning("md_capture: could not ensure %s at 0700: %s", d, e)
    return d


def ensure_capture_settings() -> Path:
    """Write (idempotently) the bot-managed MessageDisplay ``--settings`` file
    and return its path. Rewrites only when the desired content differs (the
    interpreter / appender path can move across installs)."""
    ensure_capture_dir()
    path = capture_settings_path()
    desired = _desired_settings()
    try:
        if path.exists() and json.loads(path.read_text()) == desired:
            return path
    except (OSError, json.JSONDecodeError):
        pass
    try:
        atomic_write_json(path, desired)
    except OSError as e:
        logger.warning("md_capture: could not write %s: %s", path, e)
    return path


def capture_settings_has_message_display() -> bool:
    """Whether the bot-managed settings file currently registers a
    MessageDisplay hook (used by the startup self-check warning)."""
    try:
        data = json.loads(capture_settings_path().read_text())
    except (OSError, json.JSONDecodeError):
        return False
    entries = data.get("hooks", {}).get("MessageDisplay")
    return isinstance(entries, list) and len(entries) > 0


# ── Normalization (the shared dedup contract) ────────────────────────────────


def normalize_prose(text: str) -> str:
    """Canonicalize prose for cross-source equality.

    The ONLY transforms (per the locked dedup contract): CR/CRLF → LF, strip
    trailing whitespace per line, strip leading/trailing blank lines. No
    interior whitespace collapse — that would conflate genuinely different
    prose. This same function normalizes (via ``prose_norm_hash``) BOTH the live
    captured text (the marker ``norm_hash``) and the post-resolution JSONL
    aggregate (``session_monitor.filter_live_prose_duplicates``), so the two
    compare equal regardless of streaming-vs-flush quirks.

    MULTI-BLOCK RESIDUAL (codex + panel PR-B P2): for a SINGLE assistant text
    block — Bug 2's observed shape (every captured message was single-block) —
    the two sides are provably equal. The dedup aggregates the JSONL side by
    joining the parser-STRIPPED text blocks with ``\n``; that matches the live
    whole-string form for single-block and adjacent multi-block prose. The only
    divergence is a multi-text-block message whose live display carries a blank
    line BETWEEN blocks (the per-block strip drops it) → a hash MISMATCH → a
    dedup MISS → the prose double-posts (benign in the current turn — a mismatch
    can only fail to suppress, never suppress different prose). Second-order
    hazard (panel PR-C+D P3): a missed dedup leaves the shown-live marker
    UNCONSUMED. It is cleared at resolution by ``teardown_session`` (so the
    normal flow is safe), but if teardown is skipped between turns (a
    crash / down-bot window), a later turn whose prose normalizes to the same
    hash AND carries an interactive tool_use could match the lingering marker
    and be falsely SUPPRESSED — bounded by the 1h ``gc_stale`` backstop and
    gated on that crash window, so latent + low-probability. Closing the root
    miss would need the JSONL side aggregated from RAW pre-strip block text,
    which the post-strip ``NewMessage`` stream does not expose. A marker TTL is
    NOT a clean fix (it must outlive an arbitrarily slow user answer, so it
    can't be short enough to expire a crash-orphan before the next turn);
    deferred as the documented residual.
    """
    lf = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in lf.split("\n")]
    return "\n".join(lines).strip()


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def prose_norm_hash(text: str) -> str:
    """The normalized-prose hash used for cross-source dedup. The SINGLE
    function for both the live capture's ``ProseRecord.norm_hash`` and the
    post-resolution JSONL aggregate (``session_monitor`` dedup), so the two are
    equal-by-construction for the same prose (mint/validate parity)."""
    return _sha256(normalize_prose(text))


# ── On-demand read + accumulation ────────────────────────────────────────────


@dataclass(frozen=True)
class ProseRecord:
    """A completed (``final=True``) assistant prose message, reconstructed from
    the per-flush MessageDisplay deltas of one ``message_id``."""

    session_id: str
    transcript_path: str
    md_message_id: str
    text: str
    raw_hash: str
    norm_hash: str
    first_seen_at: float
    final_at: float


@dataclass
class _Accumulator:
    transcript_path: str = ""
    deltas: dict[int, str] | None = None  # index -> delta (last write wins)
    finalized: bool = False
    first_seen_at: float = 0.0
    final_at: float = 0.0

    def add(self, *, index: int, delta: str, final: bool, captured_at: float) -> None:
        if self.deltas is None:
            self.deltas = {}
            self.first_seen_at = captured_at
        self.deltas[index] = delta
        self.first_seen_at = min(self.first_seen_at, captured_at)
        if final:
            self.finalized = True
            self.final_at = max(self.final_at, captured_at)

    def text(self) -> str:
        if not self.deltas:
            return ""
        # Deltas are "newly completed lines" already carrying their own
        # newlines; concatenate in index order with no separator.
        return "".join(self.deltas[i] for i in sorted(self.deltas))


def read_prose_records(
    session_id: str, *, base_dir: Path | None = None
) -> list[ProseRecord]:
    """Read the session's MessageDisplay NDJSON and return one ``ProseRecord``
    per FINALIZED message, ordered by ``final_at`` ascending (freshest last).

    Tolerant by construction: a missing file yields ``[]``; corrupt or partial
    (un-terminated final) lines are skipped; a not-yet-final message is omitted
    (the caller's bounded retry re-reads until its final delta lands). Returns
    only finalized messages — the "recent-final" set the render path selects
    from.

    COST (panel PR-B P3): this re-reads + re-parses the WHOLE per-session file
    on every call. The file holds every delta since the last ``teardown_session``
    and MessageDisplay fires for every assistant message, so a long heavy-
    streaming stretch between prompts could grow it. PR-C's bounded retry calls
    this repeatedly on the picker-render hot path — if that proves costly, read
    incrementally from a persisted byte offset (per-resolution teardown keeps it
    small in the common case).
    """
    path = _resolve_session_path(session_id, base_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return []

    accs: dict[str, _Accumulator] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            # Corrupt or partially-written (e.g. last line during a concurrent
            # append) — skip; a real final lands on a subsequent read.
            continue
        if not isinstance(rec, dict):
            continue
        payload = rec.get("payload")
        captured_at = rec.get("captured_at")
        if not isinstance(payload, dict) or not isinstance(captured_at, (int, float)):
            continue
        mid = payload.get("message_id")
        delta = payload.get("delta")
        index = payload.get("index")
        if not isinstance(mid, str) or not mid:
            continue
        if not isinstance(delta, str):
            delta = ""
        if not isinstance(index, int):
            # Without an ordering index we cannot place the delta; treat each
            # such flush as its own slot so nothing is silently dropped.
            index = len(accs.get(mid, _Accumulator()).deltas or {})
        final = bool(payload.get("final"))
        tp = payload.get("transcript_path")
        acc = accs.get(mid)
        if acc is None:
            acc = _Accumulator()
            accs[mid] = acc
        if isinstance(tp, str) and tp:
            acc.transcript_path = tp
        acc.add(index=index, delta=delta, final=final, captured_at=float(captured_at))

    records: list[ProseRecord] = []
    for mid, acc in accs.items():
        if not acc.finalized:
            continue
        text = acc.text()
        records.append(
            ProseRecord(
                session_id=session_id,
                transcript_path=acc.transcript_path,
                md_message_id=mid,
                text=text,
                raw_hash=_sha256(text),
                norm_hash=prose_norm_hash(text),
                first_seen_at=acc.first_seen_at,
                final_at=acc.final_at,
            )
        )
    records.sort(key=lambda r: r.final_at)
    return records


def is_prose_streaming(
    session_id: str,
    *,
    now: float | None = None,
    recency_window_s: float = 8.0,
    base_dir: Path | None = None,
) -> bool:
    """Whether a prose message for this session is ACTIVELY STREAMING — it has
    deltas, no ``final`` yet, and its LATEST delta is recent.

    The live-prose render path consults this ONLY when ``select_fresh_prose``
    returned None at the base catch-up deadline: if a message is mid-stream we
    keep waiting (bounded) for its final delta so the prose still posts BEFORE
    the picker card; if nothing is streaming we bail immediately, so a prose-less
    picker incurs zero added delay.

    Recency is anchored on the MAX ``captured_at`` across the message's deltas
    (NOT ``first_seen_at``): a legitimately long stream (deltas spanning tens of
    seconds) stays "live" because its newest delta is fresh, while a
    crash-orphaned unfinalized message ages out once its deltas stop landing — so
    stale leftover deltas can never trigger the wait. Turn-boundary / TTL
    staleness stays ``select_fresh_prose``'s job; this only governs whether to
    keep polling, never lowers the freshness bar.

    Tolerant exactly like ``read_prose_records`` (missing file / corrupt lines /
    marker lines — which carry no ``payload`` dict — are ignored). Leaf-clean:
    reads the same NDJSON, imports nothing new.
    """
    path = _resolve_session_path(session_id, base_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return False

    # message_id -> [finalized, latest_captured_at]
    state: dict[str, list] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        payload = rec.get("payload")
        captured_at = rec.get("captured_at")
        if not isinstance(payload, dict) or not isinstance(captured_at, (int, float)):
            continue
        mid = payload.get("message_id")
        if not isinstance(mid, str) or not mid:
            continue
        final = bool(payload.get("final"))
        st = state.get(mid)
        if st is None:
            state[mid] = [final, float(captured_at)]
        else:
            st[0] = st[0] or final
            st[1] = max(st[1], float(captured_at))

    cutoff = (time.time() if now is None else now) - recency_window_s
    return any(
        not finalized and latest_at >= cutoff for finalized, latest_at in state.values()
    )


# Freshness TTLs for the live-prose render path (the render-time `now` upper
# bound). REALITY (PR-1, measured — NOT the old "~0.68s before the picker"
# assumption, which was inverted): the prose finalizes a meaningful gap BEFORE
# the picker is DETECTED — ~5.44s on an idle rig and up to ~20.7s under bot load
# (the poller can only scrape the pane on its ~1s cadence, and the adaptive
# capture / watchdog can skip the blocked frame). So a fixed TTL measured from
# render-time `now` routinely ages the matching prose out before render. The TTL
# stays as one freshness leg; PR-1 ORs it with a STABLE emission anchor (below)
# so a lagged-but-real pairing is still accepted. These bound how stale a
# candidate can be on the TTL leg before it's rejected (a previous turn's
# leftover prose) and the path falls back to JSONL delivery.
AUQ_PROSE_TTL_S = 8.0
EPM_PROSE_TTL_S = 12.0

# Emission-anchor tolerances for the PR-1 additive-OR freshness leg. The anchor
# `emitted_at` is a STABLE picker-emission instant — AUQ: the PreToolUse
# side-file `written_at` (the tool_use invocation, hook-stamped ~at the
# tool_use); EPM: the poller's FIRST-DETECTION stamp (no side file). The OR leg
# accepts a record iff `emitted_at - lookback <= final_at <= emitted_at + eps`.
# Pinned by the Wave-0 capture (2026-06-17, Claude Code 2.1.172: EPM gap
# detect-minus-final = 5.44s on an idle rig) + the live loaded figure (~20.7s).
#
# UPPER eps: the turn's prose finalizes BEFORE the tool_use / detect, so the
# upper bound holds with eps≈0; eps only guards streaming/flush jitter where the
# final delta lands a hair after the anchor instant.
#
# LOWER lookback: how far BELOW the anchor a legit same-turn prose can sit.
#  * AUQ: written_at ≈ the tool_use instant, so the prose sits at most the
#    in-turn prose→tool_use gap below it (≤ ~8.5s live). Kept TIGHT because it is
#    ALSO the A1-FIX restart-asymmetry guard: post-restart the on-disk written_at
#    survives but the in-memory not_before is wiped, so the lookback alone must
#    reject a stale prior-turn prose.
#  * EPM: the poller-stamp anchor lags the tool_use by the WHOLE detect latency
#    (5.44s idle, ~20.7s loaded), so the legit prose can sit that far below it →
#    a much larger lookback. Safe to be generous: in-process `not_before` guards
#    prior turns, and EPM has NO on-disk anchor so `emitted_at` is None
#    post-restart → the OR leg simply doesn't fire (TTL-only).
_EMIT_ANCHOR_EPS_S = 2.0
_EMIT_ANCHOR_LOOKBACK_S = 10.0
_EMIT_ANCHOR_EPS_EPM_S = 2.0
_EMIT_ANCHOR_LOOKBACK_EPM_S = 30.0


def select_fresh_prose(
    session_id: str,
    *,
    now: float,
    ttl_seconds: float,
    not_before: float | None = None,
    emitted_at: float | None = None,
    emit_anchor_eps_s: float = 0.0,
    emit_anchor_lookback_s: float = 0.0,
    base_dir: Path | None = None,
) -> ProseRecord | None:
    """Pick the freshest FINALIZED prose record fresh enough to post above the
    live picker, or ``None``. Records are per-session by construction (the file is
    keyed by the session's transcript stem). Returns the MOST RECENT match — the
    prose that streamed immediately before this picker.

    FRESHNESS is a STRICTLY-ADDITIVE OR of two legs (PR-1):
      * the TTL leg (today): ``now - final_at <= ttl_seconds``;
      * the emission-anchor leg: when ``emitted_at`` is supplied (a STABLE
        picker-emission instant — AUQ ``written_at`` / the EPM poller stamp),
        ``emitted_at - emit_anchor_lookback_s <= final_at <= emitted_at +
        emit_anchor_eps_s``.
    The OR can only WIDEN acceptance over the TTL leg, so it is non-regressive on
    the upper bound (it never rejects what the TTL accepted) while it RECOVERS the
    dominant miss — a poller that detected the picker long after the prose
    finalized (live: 20.7s), which blew the render-time TTL. ``emitted_at=None``
    (the default) is byte-for-byte the prior TTL-only behavior.

    TURN-BOUNDARY FILTER (Item 3 / P2-1): ``not_before`` is the wall-clock
    instant the bot DELIVERED the current user turn into tmux (the same
    ``time.time()`` clock the appender stamps as ``captured_at``, so directly
    comparable). It ANDs against BOTH legs: the current turn's prose is captured
    AFTER delivery (``final_at > not_before``); a PRIOR turn's leftover prose —
    still in the per-session file because teardown only fires at AUQ/EPM
    resolution — finalized BEFORE it. STRICT ``final_at > not_before`` (prose
    captured exactly at the boundary is not causally after the delivered
    message). ``not_before=None`` (the restart / first-render degradation) leans
    on the anchor leg's OWN lookback lower bound (A1-FIX) to reject a stale
    prior-turn prose — on a restart the on-disk AUQ ``written_at`` survives while
    the in-memory ``not_before`` is wiped, so the lookback is the only floor
    left."""

    def _keep(r: ProseRecord) -> bool:
        ttl_ok = (now - r.final_at) <= ttl_seconds
        anchor_ok = (
            emitted_at is not None
            and r.final_at <= emitted_at + emit_anchor_eps_s
            and r.final_at >= emitted_at - emit_anchor_lookback_s
        )
        if not (ttl_ok or anchor_ok):
            return False
        return not_before is None or r.final_at > not_before

    fresh = [r for r in read_prose_records(session_id, base_dir=base_dir) if _keep(r)]
    return fresh[-1] if fresh else None


# ── Shown-live markers (the dedup bridge to the post-resolution JSONL copy) ──
#
# When the render path posts a captured prose live (before the picker card), it
# records a "shown_live" marker in the SAME per-session NDJSON file, so the
# marker shares the capture's lifecycle (``teardown_session`` unlinks it) and is
# restart-safe (a ``launchctl kickstart`` between live-post and answer can't
# double-post). The batch dedup (session_monitor) reads the unconsumed markers
# at the JSONL flush, matches a group's aggregated real-prose ``norm_hash``, and
# ``consume``s the marker so the post-resolution copy is suppressed exactly once.
# Marker lines carry a ``marker`` key; ``read_prose_records`` ignores them (no
# ``payload``), and this reader ignores the delta lines — they coexist cleanly.


@dataclass(frozen=True)
class ShownLiveMarker:
    md_message_id: str
    norm_hash: str
    shown_at: float


def record_shown_live(
    session_id: str,
    *,
    md_message_id: str,
    norm_hash: str,
    shown_at: float,
    base_dir: Path | None = None,
) -> None:
    """Mark a captured prose message as delivered live (before the picker)."""
    _append_json_line(
        _resolve_session_path(session_id, base_dir),
        {
            "marker": "shown_live",
            "md_message_id": md_message_id,
            "norm_hash": norm_hash,
            "shown_at": shown_at,
        },
    )


def consume_shown_live(
    session_id: str, md_message_id: str, *, base_dir: Path | None = None
) -> None:
    """Mark a shown-live marker consumed (its post-resolution copy was
    suppressed) so a later read no longer returns it."""
    _append_json_line(
        _resolve_session_path(session_id, base_dir),
        {"marker": "consumed", "md_message_id": md_message_id},
    )


def read_shown_live_markers(
    session_id: str, *, base_dir: Path | None = None
) -> list[ShownLiveMarker]:
    """Return the UNCONSUMED shown-live markers for the session (latest
    ``shown_live`` per ``md_message_id`` minus any later ``consumed`` line).
    Missing file → ``[]``; corrupt lines skipped."""
    try:
        raw = _resolve_session_path(session_id, base_dir).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return []
    shown: dict[str, ShownLiveMarker] = {}
    consumed: set[str] = set()
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        kind = rec.get("marker")
        mid = rec.get("md_message_id")
        if not isinstance(mid, str) or not mid:
            continue
        if kind == "shown_live":
            nh = rec.get("norm_hash")
            sa = rec.get("shown_at")
            if isinstance(nh, str) and isinstance(sa, (int, float)):
                shown[mid] = ShownLiveMarker(
                    md_message_id=mid, norm_hash=nh, shown_at=float(sa)
                )
        elif kind == "consumed":
            consumed.add(mid)
    return [m for mid, m in shown.items() if mid not in consumed]


def was_shown_live(
    session_id: str, md_message_id: str, *, base_dir: Path | None = None
) -> bool:
    """True if a ``shown_live`` marker was EVER recorded for this MessageDisplay
    message — consumed or not. The render-path idempotency guard: once a prose
    has been delivered live, never re-deliver it, even after the batch dedup
    consumes its marker or if the picker pane is still apparently live on a
    later ``handle_interactive_ui`` call. (Distinct from ``read_shown_live_
    markers``, which returns only UNCONSUMED markers for the dedup to match.)"""
    try:
        raw = _resolve_session_path(session_id, base_dir).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return False
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if (
            isinstance(rec, dict)
            and rec.get("marker") == "shown_live"
            and rec.get("md_message_id") == md_message_id
        ):
            return True
    return False


# ── Lifecycle / teardown ─────────────────────────────────────────────────────


def teardown_session(session_id: str, *, base_dir: Path | None = None) -> None:
    """Remove a session's capture file (on AUQ/EPM resolution, session
    replacement, ``/clear``, topic close). Best-effort; missing is fine."""
    path = (
        (base_dir / MD_DISPLAY_DIRNAME / f"{session_id}.ndjson")
        if base_dir is not None
        else session_ndjson_path(session_id)
    )
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("md_capture: could not unlink %s: %s", path, e)


def gc_stale(
    max_age_seconds: float = 3600.0,
    *,
    is_live_session: Callable[[str], bool] | None = None,
    base_dir: Path | None = None,
) -> int:
    """Sweep capture files older than ``max_age_seconds`` (startup GC, mirroring
    the AUQ side-file 1h GC). Returns the count removed. A live but unanswered
    prompt's file is younger than the TTL; a crashed/abandoned one ages out.

    LIVENESS GATE (Item 3 / P2-2): a long-open picker's capture file (which ALSO
    carries its shown_live/consumed dedup markers in the same file) can age past
    ``max_age_seconds`` while the prompt is still genuinely live; reaping it
    would drop the markers and double-post the prose at resolution. When
    ``is_live_session`` is supplied, it is called with the file STEM (= the
    original session id, the ndjson key) after the age test passes: True → SKIP
    (keep the live file); an EXCEPTION → conservative SKIP (never delete on
    uncertainty; the raise is caught around the predicate call only so the rest
    of the pass continues). The predicate is INJECTED — ``md_capture`` stays a
    leaf and never imports a cctelegram module to learn liveness.

    TOCTOU: after the age + liveness checks pass, the mtime is re-``stat``-ed
    immediately before ``unlink``; if a concurrent append refreshed the file
    (``now - st_mtime <= max_age_seconds`` at re-stat) it is SKIPPED."""
    d = (base_dir / MD_DISPLAY_DIRNAME) if base_dir is not None else msg_display_dir()
    removed = 0
    now = time.time()
    try:
        entries = list(d.iterdir())
    except (FileNotFoundError, OSError):
        return 0
    for f in entries:
        if not f.name.endswith(".ndjson"):
            continue
        try:
            if now - f.stat().st_mtime <= max_age_seconds:
                continue
            if is_live_session is not None:
                try:
                    if is_live_session(f.stem):
                        continue  # live prompt — keep its file + dedup markers
                except Exception:
                    # Never delete on uncertainty; the predicate raising must
                    # not abort the whole GC pass either.
                    continue
            # Re-stat immediately before unlink: a concurrent append between the
            # age check and now could have refreshed the file.
            if now - f.stat().st_mtime <= max_age_seconds:
                continue
            f.unlink()
            removed += 1
        except OSError:
            continue
    return removed
