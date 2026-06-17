"""Tests for the AUQ-source resolver leaf (R5).

Covers the public ``auq_source`` seam: the typed ``resolve_auq_source``
resolver with its per-kind ``source_fingerprint`` (the mint/validate parity
witness), the injected JSONL-cache getter lifecycle, and the
remember-before-mint parity invariant (§8.1). The trust-boundary unit tests
(path traversal, schema/fingerprint, TTL/skew, the ``checked_any``
vacuous-true case) live in ``test_interactive_ui.py``'s pretool block, now
re-pointed at this seam; this file adds the resolver-return + fingerprint
coverage that R5 introduces.

Fixtures are REAL captures: ``auq_single_select_with_affordances_*`` is a
paired pane + side file for the ``side_file`` kind; ``auq-baseline-pane.txt``
is a real picker capture for the ``pane`` kind.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from cctelegram.handlers import auq_source
from cctelegram.session import WindowState, session_manager
from cctelegram.terminal_parser import resolve_ask_form

_FIXTURE_DIR = Path(__file__).parents[1] / "fixtures"
_AFFORDANCE_SIDEFILE = _FIXTURE_DIR / "auq_single_select_with_affordances_sidefile.json"
_AFFORDANCE_PANE = _FIXTURE_DIR / "auq_single_select_with_affordances_pane.txt"
_BASELINE_PANE = _FIXTURE_DIR / "auq-baseline-pane.txt"


@pytest.fixture
def _cc_dir(tmp_path, monkeypatch):
    """Point app_dir() at tmp_path and reset the leaf before/after."""
    monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
    auq_source.reset_for_tests()
    yield tmp_path
    auq_source.reset_for_tests()


def _bind_window(window_id: str, session_id: str) -> None:
    session_manager.window_states[window_id] = WindowState(
        cwd="/tmp/cwd", session_id=session_id
    )


def _unbind_window(window_id: str) -> None:
    session_manager.window_states.pop(window_id, None)


def _write_affordance_side_file(cc_dir: Path, session_id: str) -> dict:
    """Write the real affordances side file under cc_dir, fresh ``written_at``.

    Returns the ``tool_input`` dict the side file carries.
    """
    sidefile = json.loads(_AFFORDANCE_SIDEFILE.read_text())
    tool_input = sidefile["tool_input"]
    pending = cc_dir / "auq_pending"
    pending.mkdir(mode=0o700, exist_ok=True)
    (pending / f"{session_id}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": session_id,
                "tool_use_id": sidefile["tool_use_id"],
                "written_at": time.time(),
                "tool_input": tool_input,
            }
        )
    )
    return tool_input


# ── side_file kind ───────────────────────────────────────────────────────────


class TestResolveSideFileKind:
    _WID = "@auqsrc-sf"
    _SID = "4766fb07-7057-4981-9832-93e524ab943e"

    def test_resolves_side_file_kind_with_stable_fingerprint(self, _cc_dir):
        _bind_window(self._WID, self._SID)
        try:
            tool_input = _write_affordance_side_file(_cc_dir, self._SID)
            pane = _AFFORDANCE_PANE.read_text()

            resolved = auq_source.resolve_auq_source(self._WID, None, pane)
            assert resolved.kind == "side_file"
            assert resolved.payload == tool_input

            # Same inputs → same fingerprint (stable witness).
            again = auq_source.resolve_auq_source(self._WID, None, pane)
            assert again.source_fingerprint == resolved.source_fingerprint
        finally:
            _unbind_window(self._WID)

    def test_mutated_side_file_source_yields_different_fingerprint(self, _cc_dir):
        _bind_window(self._WID, self._SID)
        try:
            _write_affordance_side_file(_cc_dir, self._SID)
            pane = _AFFORDANCE_PANE.read_text()
            base = auq_source.resolve_auq_source(self._WID, None, pane)
            assert base.kind == "side_file"

            # Mutate the side file's tool_input (drop an option), keeping the
            # first three labels so the pane still matches → still side_file,
            # but a DIFFERENT source fingerprint (the drift case).
            sidefile = json.loads(_AFFORDANCE_SIDEFILE.read_text())
            mutated = sidefile["tool_input"]
            mutated["questions"][0]["header"] = "MUTATED HEADER"
            (_cc_dir / "auq_pending" / f"{self._SID}.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": self._SID,
                        "tool_use_id": sidefile["tool_use_id"],
                        "written_at": time.time(),
                        "tool_input": mutated,
                    }
                )
            )
            auq_source.reset_for_tests()  # drop the cached record
            drifted = auq_source.resolve_auq_source(self._WID, None, pane)
            assert drifted.kind == "side_file"
            assert drifted.source_fingerprint != base.source_fingerprint
        finally:
            _unbind_window(self._WID)


# ── jsonl_cache kind ─────────────────────────────────────────────────────────


class TestResolveJsonlCacheKind:
    _WID = "@auqsrc-jc"

    _CACHE_INPUT = {
        "questions": [
            {
                "question": "Pick a fruit",
                "options": [{"label": "Apple"}, {"label": "Banana"}],
            }
        ]
    }

    def test_explicit_dict_resolves_jsonl_cache_kind(self, _cc_dir):
        # No side file, explicit dict given → jsonl_cache branch.
        resolved = auq_source.resolve_auq_source(self._WID, self._CACHE_INPUT, "")
        assert resolved.kind == "jsonl_cache"
        assert resolved.payload == self._CACHE_INPUT
        again = auq_source.resolve_auq_source(self._WID, self._CACHE_INPUT, "")
        assert again.source_fingerprint == resolved.source_fingerprint

    def test_injected_cache_resolves_jsonl_cache_kind(self, _cc_dir):
        # No side file, explicit None, injected cache populated → jsonl_cache.
        auq_source.set_jsonl_cache_getter(
            lambda wid: self._CACHE_INPUT if wid == self._WID else None
        )
        resolved = auq_source.resolve_auq_source(self._WID, None, "")
        assert resolved.kind == "jsonl_cache"
        assert resolved.payload == self._CACHE_INPUT

    def test_mutated_cache_source_yields_different_fingerprint(self, _cc_dir):
        base = auq_source.resolve_auq_source(self._WID, self._CACHE_INPUT, "")
        mutated = {
            "questions": [
                {
                    "question": "Pick a fruit",
                    "options": [{"label": "Apple"}, {"label": "Cherry"}],
                }
            ]
        }
        drifted = auq_source.resolve_auq_source(self._WID, mutated, "")
        assert drifted.kind == "jsonl_cache"
        assert drifted.source_fingerprint != base.source_fingerprint


# ── pane kind ─────────────────────────────────────────────────────────────────


class TestResolvePaneKind:
    _WID = "@auqsrc-pane"

    def test_resolves_pane_kind_with_stable_fingerprint(self, _cc_dir):
        # No side file, explicit None, no injected cache (reset default) →
        # the pane branch. payload is None; fingerprint over the form's
        # canonical repr.
        pane = _BASELINE_PANE.read_text()
        # Sanity: the baseline pane really parses to a form.
        assert resolve_ask_form(None, pane) is not None

        resolved = auq_source.resolve_auq_source(self._WID, None, pane)
        assert resolved.kind == "pane"
        assert resolved.payload is None
        assert resolved.source_fingerprint  # non-empty sha

        # Same pane → same fingerprint. (NO drift test: a changed pane changes
        # the FORM fingerprint, and validation returns stale_form first — the
        # pane source fp shares the canonical input with the form fp; §8.1.)
        again = auq_source.resolve_auq_source(self._WID, None, pane)
        assert again.source_fingerprint == resolved.source_fingerprint

    def test_pane_source_fingerprint_equal_across_cursor_move(self, _cc_dir):
        """RED pre-fix / GREEN post-fix (item 2 coupling guard).

        The pane-kind ``source_fingerprint`` hashes the form's
        ``_canonical_repr`` (``auq_source._pane_fingerprint``), shared in
        lockstep with the FORM fingerprint. A NON-review cursor move must NOT
        change it — otherwise a pane-sourced live card ``source_drift``s when
        the cursor moves. The non-review twin of the review-screen lockstep
        guard.

        FAILS on current main: the per-option cursor bit is in
        ``_canonical_repr`` (terminal_parser.py:692), so the two pane source
        fingerprints differ."""
        pane3 = (_FIXTURE_DIR / "auq_single_long_scrolled_cursor3_S500.txt").read_text()
        pane4 = (_FIXTURE_DIR / "auq_single_long_scrolled_cursor4_S500.txt").read_text()
        r3 = auq_source.resolve_auq_source(self._WID, None, pane3)
        r4 = auq_source.resolve_auq_source(self._WID, None, pane4)
        assert r3.kind == "pane" and r4.kind == "pane"
        assert r3.source_fingerprint == r4.source_fingerprint


# ── getter lifecycle / reset isolation ───────────────────────────────────────


class TestGetterResetIsolation:
    _WID = "@auqsrc-reset"

    _CACHE_INPUT = {
        "questions": [{"question": "Q", "options": [{"label": "A"}, {"label": "B"}]}]
    }

    def test_reset_restores_noop_getter(self, _cc_dir):
        pane = _BASELINE_PANE.read_text()
        auq_source.set_jsonl_cache_getter(
            lambda wid: self._CACHE_INPUT if wid == self._WID else None
        )
        # With the fake getter, explicit=None resolves jsonl_cache.
        resolved = auq_source.resolve_auq_source(self._WID, None, pane)
        assert resolved.kind == "jsonl_cache"

        # reset_for_tests rebinds the getter back to the no-op default.
        auq_source.reset_for_tests()
        after = auq_source.resolve_auq_source(self._WID, None, pane)
        assert after.kind == "pane", (
            "reset_for_tests() must restore the no-op getter so a fake cache "
            "cannot leak across tests"
        )


# ── remember-before-mint parity invariant (§8.1) ─────────────────────────────


class TestRememberBeforeMintParity:
    """The load-bearing JSONL-render parity dependency (§8.1).

    The JSONL render path calls ``interactive_ui.remember_ask_tool_input``
    BEFORE mint, which populates ``_last_completed_ask_tool_input``. The
    injected getter reads exactly that dict, so a validator calling
    ``resolve_auq_source(wid, None, pane)`` lands on the SAME source the
    minter saw — same dict, same fingerprint. This pins that the production
    getter (wired in conftest, mirroring bot.post_init) reads the cache.
    """

    _WID = "@auqsrc-parity"
    _INPUT = {
        "questions": [{"question": "Q", "options": [{"label": "A"}, {"label": "B"}]}]
    }

    def test_remember_then_resolve_sees_same_jsonl_source(self, _cc_dir):
        from cctelegram.handlers import interactive_ui

        # Mirror bot.post_init / conftest wiring: the production getter reads
        # interactive_ui's in-process cache.
        auq_source.set_jsonl_cache_getter(
            lambda wid: interactive_ui._last_completed_ask_tool_input.get(wid)
        )
        try:
            interactive_ui.remember_ask_tool_input(self._WID, self._INPUT, "toolu_x")

            # No side file, explicit None, empty pane → jsonl_cache branch reads
            # the remembered dict via the getter.
            resolved = auq_source.resolve_auq_source(self._WID, None, "")
            assert resolved.kind == "jsonl_cache"
            assert resolved.payload == self._INPUT

            # The fingerprint is the exact same one the minter would record for
            # this source (deterministic over the same dict).
            again = auq_source.resolve_auq_source(self._WID, None, "")
            assert again.source_fingerprint == resolved.source_fingerprint
        finally:
            interactive_ui._last_completed_ask_tool_input.pop(self._WID, None)


def _write_side_file_at(
    cc_dir: Path,
    session_id: str,
    *,
    written_at: float,
    schema_version: int = 1,
) -> dict:
    """Write a (by default) schema-valid side file with a controllable
    ``written_at`` / ``schema_version``, reusing the real affordance
    ``tool_input`` so the trust-boundary reader accepts the shape. Returns the
    written ``tool_input`` (mirrors ``_write_affordance_side_file``).
    """
    sidefile = json.loads(_AFFORDANCE_SIDEFILE.read_text())
    pending = cc_dir / "auq_pending"
    pending.mkdir(mode=0o700, exist_ok=True)
    (pending / f"{session_id}.json").write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "session_id": session_id,
                "tool_use_id": sidefile["tool_use_id"],
                "written_at": written_at,
                "tool_input": sidefile["tool_input"],
            }
        )
    )
    return sidefile["tool_input"]


class TestPeekSideFileWrittenAt:
    """PR-1 prose-ORDER AUQ anchor: ``peek_side_file_written_at`` returns the
    PreToolUse hook's ``written_at`` (the AUQ tool_use invocation instant) so the
    live-prose freshness gate can anchor the prose to THIS picker. Mirrors
    ``peek_side_file_tool_use_id``: read-TTL-free, non-mutating, future-skew
    validated, session-keyed.
    """

    _SID = "4766fb07-7057-4981-9832-93e524ab943e"

    def test_returns_written_at(self, _cc_dir):
        ts = time.time() - 123.0
        _write_side_file_at(_cc_dir, self._SID, written_at=ts)
        got = auq_source.peek_side_file_written_at(self._SID)
        assert got == pytest.approx(ts)
        # Read-only: must NOT populate the resolve cache.
        assert not auq_source._pretool_ask_records

    def test_none_when_absent(self, _cc_dir):
        assert auq_source.peek_side_file_written_at(self._SID) is None

    def test_none_on_future_skew(self, _cc_dir):
        _write_side_file_at(
            _cc_dir,
            self._SID,
            written_at=time.time() + auq_source._PRETOOL_FUTURE_SKEW_SECONDS + 30,
        )
        assert auq_source.peek_side_file_written_at(self._SID) is None

    def test_live_past_read_ttl(self, _cc_dir):
        """A genuinely-old-but-unanswered AUQ still yields its written_at (the
        anchor is read-TTL-FREE — the prose-ordering freshness uses the lookback,
        not this TTL)."""
        ts = time.time() - (auq_source._PRETOOL_TTL_SECONDS + 60)
        _write_side_file_at(_cc_dir, self._SID, written_at=ts)
        assert auq_source.peek_side_file_written_at(self._SID) == pytest.approx(ts)


class TestSideFileLiveForWindow:
    """The pane-INDEPENDENT card-clear authority (2026-05-31 disappearing-card
    fix). ``side_file_live_for_window`` is True iff a schema-valid side file
    exists for the window's session and is not future-skewed — deliberately
    WITHOUT the read-TTL and WITHOUT any pane-consistency check.
    """

    _WID = "@auqsrc-live"
    _SID = "4766fb07-7057-4981-9832-93e524ab943e"

    def test_true_when_present_and_pane_independent(self, _cc_dir):
        """A fresh valid side file → True with NO pane supplied at all (proves
        pane-independence, the whole point) AND without populating the
        ``_pretool_ask_records`` cache (read-only invariant: ``resolve_record``
        stays the sole mutator).
        """
        _bind_window(self._WID, self._SID)
        try:
            _write_affordance_side_file(_cc_dir, self._SID)
            assert auq_source.side_file_live_for_window(self._WID) is True
            assert self._WID not in auq_source._pretool_ask_records
        finally:
            _unbind_window(self._WID)

    def test_true_past_ttl_still_live(self, _cc_dir):
        """KEY regression: a genuinely-live AUQ unanswered well past the 5-min
        read-TTL must STILL be live for the clear gate. The user's bar is
        literal — the card "shouldn't expire ... unless it expired on the other
        side of the bridge"; a read-TTL is NOT that bridge. A regression to a
        TTL-based predicate would flip this to False and resurrect the bug.
        """
        _bind_window(self._WID, self._SID)
        try:
            _write_side_file_at(
                _cc_dir,
                self._SID,
                written_at=time.time() - (auq_source._PRETOOL_TTL_SECONDS + 60),
            )
            assert auq_source.side_file_live_for_window(self._WID) is True
        finally:
            _unbind_window(self._WID)

    def test_false_when_no_session_bound(self, _cc_dir):
        # No window_states entry → peek returns None → False (file never read).
        assert auq_source.side_file_live_for_window("@auqsrc-unbound") is False

    def test_false_when_no_side_file(self, _cc_dir):
        _bind_window(self._WID, self._SID)
        try:
            assert auq_source.side_file_live_for_window(self._WID) is False
        finally:
            _unbind_window(self._WID)

    def test_false_on_schema_mismatch(self, _cc_dir):
        _bind_window(self._WID, self._SID)
        try:
            _write_side_file_at(
                _cc_dir, self._SID, written_at=time.time(), schema_version=2
            )
            assert auq_source.side_file_live_for_window(self._WID) is False
        finally:
            _unbind_window(self._WID)

    def test_false_on_future_skew(self, _cc_dir):
        _bind_window(self._WID, self._SID)
        try:
            _write_side_file_at(
                _cc_dir,
                self._SID,
                written_at=time.time() + auq_source._PRETOOL_FUTURE_SKEW_SECONDS + 30,
            )
            assert auq_source.side_file_live_for_window(self._WID) is False
        finally:
            _unbind_window(self._WID)

    def test_session_keyed_core_is_window_independent(self, _cc_dir):
        """side_file_live_for_session works off the session_id alone — no
        window binding, no pane. This is the canonical form the startup orphan
        reconciler uses so its liveness check and the unlink target the SAME
        session (the window wrapper would re-resolve via peek/window_states).
        """
        # No _bind_window: the session-keyed core never consults window_states.
        _write_affordance_side_file(_cc_dir, self._SID)
        assert auq_source.side_file_live_for_session(self._SID) is True
        assert auq_source.side_file_live_for_session("") is False
        assert (
            auq_source.side_file_live_for_session(
                "00000000-0000-4000-8000-000000000000"
            )
            is False
        )


class TestPeekStickySource:
    """The source-stickiness pin for the ``aqt:`` multi-select toggle.

    ``peek_sticky_source`` re-resolves the EXACT source a toggle button was
    minted against (side_file / jsonl_cache), pane-AGNOSTIC, so a transient
    render→tap source flip (side_file → pane, on a degraded pane) cannot break
    the toggle. The fingerprint compared is the minter's
    ``_canonical_dict_fingerprint`` — NOT the side file's stored
    ``input_fingerprint`` (``questions_content_digest``), which is a different
    digest (the mint/validate source-parity trap).
    """

    _WID = "@auqsrc-sticky"
    _SID = "4766fb07-7057-4981-9832-93e524ab943e"

    _CACHE_INPUT = {
        "questions": [
            {
                "question": "Pick a fruit",
                "options": [{"label": "Apple"}, {"label": "Banana"}],
            }
        ]
    }

    def test_side_file_matching_canonical_fp_returns_tool_input(self, _cc_dir):
        # The exact minted source is still live + unchanged → returns its
        # tool_input dict, pane-agnostic (no pane is supplied at all).
        _bind_window(self._WID, self._SID)
        try:
            tool_input = _write_affordance_side_file(_cc_dir, self._SID)
            minted_fp = auq_source._canonical_dict_fingerprint(tool_input)
            got = auq_source.peek_sticky_source(self._WID, "side_file", minted_fp)
            assert got == tool_input
            # Read-only invariant: the pane-agnostic helper must NOT populate
            # the consistent-with-pane cache.
            assert self._WID not in auq_source._pretool_ask_records
        finally:
            _unbind_window(self._WID)

    def test_side_file_changed_canonical_fp_returns_none(self, _cc_dir):
        # A new question replaced the side file → its canonical fingerprint
        # differs from the minted one → no pin (fall back to resolve).
        _bind_window(self._WID, self._SID)
        try:
            tool_input = _write_affordance_side_file(_cc_dir, self._SID)
            minted_fp = auq_source._canonical_dict_fingerprint(tool_input)

            # Mutate the live side file's tool_input (different canonical fp).
            sidefile = json.loads(_AFFORDANCE_SIDEFILE.read_text())
            mutated = sidefile["tool_input"]
            mutated["questions"][0]["header"] = "DIFFERENT QUESTION"
            (_cc_dir / "auq_pending" / f"{self._SID}.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": self._SID,
                        "tool_use_id": sidefile["tool_use_id"],
                        "written_at": time.time(),
                        "tool_input": mutated,
                    }
                )
            )
            assert (
                auq_source.peek_sticky_source(self._WID, "side_file", minted_fp) is None
            )
        finally:
            _unbind_window(self._WID)

    def test_side_file_absent_returns_none(self, _cc_dir):
        # No side file on disk → no pin.
        _bind_window(self._WID, self._SID)
        try:
            assert (
                auq_source.peek_sticky_source(self._WID, "side_file", "deadbeef")
                is None
            )
        finally:
            _unbind_window(self._WID)

    def test_pane_minted_kind_returns_none(self, _cc_dir):
        # A pane-minted button has no sticky source to pin → always None.
        _bind_window(self._WID, self._SID)
        try:
            _write_affordance_side_file(_cc_dir, self._SID)
            assert auq_source.peek_sticky_source(self._WID, "pane", "anything") is None
        finally:
            _unbind_window(self._WID)

    def test_jsonl_cache_matching_fp_returns_cached(self, _cc_dir):
        auq_source.set_jsonl_cache_getter(
            lambda wid: self._CACHE_INPUT if wid == self._WID else None
        )
        minted_fp = auq_source._canonical_dict_fingerprint(self._CACHE_INPUT)
        got = auq_source.peek_sticky_source(self._WID, "jsonl_cache", minted_fp)
        assert got == self._CACHE_INPUT

    def test_jsonl_cache_non_matching_fp_returns_none(self, _cc_dir):
        auq_source.set_jsonl_cache_getter(
            lambda wid: self._CACHE_INPUT if wid == self._WID else None
        )
        assert (
            auq_source.peek_sticky_source(self._WID, "jsonl_cache", "notthefp") is None
        )

    def test_jsonl_cache_absent_returns_none(self, _cc_dir):
        # reset default getter returns None → no cached dict → no pin.
        assert (
            auq_source.peek_sticky_source(self._WID, "jsonl_cache", "anything") is None
        )

    def test_uses_canonical_fp_not_input_fingerprint(self, _cc_dir):
        # Parity guard: the side file's stored ``input_fingerprint``
        # (questions_content_digest, a 12-char hex) is a DIFFERENT digest from
        # ``_canonical_dict_fingerprint`` (a 64-char sha256). The helper must
        # match on the canonical digest the minter used — passing the
        # input_fingerprint must NOT match, but the canonical fp MUST.
        _bind_window(self._WID, self._SID)
        try:
            tool_input = _write_affordance_side_file(_cc_dir, self._SID)
            record = auq_source._read_live_pretool_record(self._WID)
            assert record is not None
            canonical_fp = auq_source._canonical_dict_fingerprint(tool_input)
            input_fp = record.input_fingerprint
            # The two digests genuinely differ (else this guard is vacuous).
            assert canonical_fp != input_fp
            # Matching the canonical fp pins; matching the input_fingerprint
            # does NOT (proving the helper does not use input_fingerprint).
            assert (
                auq_source.peek_sticky_source(self._WID, "side_file", canonical_fp)
                == tool_input
            )
            assert (
                auq_source.peek_sticky_source(self._WID, "side_file", input_fp) is None
            )
        finally:
            _unbind_window(self._WID)


# ═══════════════════════════════════════════════════════════════════════════
# PR-B (stateless-callback Wave 1) — TTL-free dispatch source + live-safe GC
# ═══════════════════════════════════════════════════════════════════════════


class TestResolveAuqSourceForDispatch:
    """Plan v3 §4 / §8 test 3 — ``resolve_auq_source_for_dispatch`` is TTL-free
    but KEEPS the pane-consistency check, so a long-open card's source never
    flaps side_file→pane (the item-1 source-drift class). Distinct from
    ``resolve_auq_source`` (read-TTL'd) and ``side_file_live_*`` (a bool).
    """

    _WID = "@auqsrc-disp"
    _SID = "4766fb07-7057-4981-9832-93e524ab943e"

    def test_fresh_side_file_resolves_side_file(self, _cc_dir):
        _bind_window(self._WID, self._SID)
        try:
            tool_input = _write_affordance_side_file(_cc_dir, self._SID)
            pane = _AFFORDANCE_PANE.read_text()
            src = auq_source.resolve_auq_source_for_dispatch(self._WID, pane)
            assert src.kind == "side_file"
            assert src.payload == tool_input
            assert src.form is not None
            assert src.source_fingerprint == auq_source._canonical_dict_fingerprint(
                tool_input
            )
        finally:
            _unbind_window(self._WID)

    def test_aged_side_file_stays_side_file_unlike_resolve_auq_source(self, _cc_dir):
        """The drift kill: a side file aged PAST the 300s read-TTL flips
        ``resolve_auq_source`` to ``pane`` but ``..._for_dispatch`` keeps it
        ``side_file`` (TTL-free), so the dispatch identity does not drift.
        """
        _bind_window(self._WID, self._SID)
        try:
            tool_input = _write_side_file_at(
                _cc_dir,
                self._SID,
                written_at=time.time() - (auq_source._PRETOOL_TTL_SECONDS + 60),
            )
            pane = _AFFORDANCE_PANE.read_text()

            # The legacy read-TTL'd resolver drifts to pane on the aged file.
            assert auq_source.resolve_auq_source(self._WID, None, pane).kind == "pane"

            # The dispatch resolver stays on the side file.
            src = auq_source.resolve_auq_source_for_dispatch(self._WID, pane)
            assert src.kind == "side_file"
            assert src.form is not None
            assert src.source_fingerprint == auq_source._canonical_dict_fingerprint(
                tool_input
            )
        finally:
            _unbind_window(self._WID)

    def test_no_side_file_falls_back_to_pane(self, _cc_dir):
        _bind_window(self._WID, self._SID)
        try:
            pane = _AFFORDANCE_PANE.read_text()
            src = auq_source.resolve_auq_source_for_dispatch(self._WID, pane)
            assert src.kind == "pane"
            assert src.payload is None
        finally:
            _unbind_window(self._WID)

    def test_future_skew_side_file_falls_back_to_pane(self, _cc_dir):
        """The future-skew guard is RETAINED even though the read-TTL is skipped."""
        _bind_window(self._WID, self._SID)
        try:
            _write_side_file_at(
                _cc_dir,
                self._SID,
                written_at=time.time() + auq_source._PRETOOL_FUTURE_SKEW_SECONDS + 30,
            )
            pane = _AFFORDANCE_PANE.read_text()
            src = auq_source.resolve_auq_source_for_dispatch(self._WID, pane)
            assert src.kind == "pane"
        finally:
            _unbind_window(self._WID)

    def test_pane_inconsistent_side_file_fails_closed_to_pane(self, _cc_dir):
        """KEEPS ``_record_consistent_with_pane`` (fail-closed): a live side file
        whose questions do NOT match the visible pane is NOT trusted for dispatch.
        """
        _bind_window(self._WID, self._SID)
        try:
            _write_affordance_side_file(_cc_dir, self._SID)
            # A DIFFERENT picker on the pane — inconsistent with the side file.
            pane = _BASELINE_PANE.read_text()
            src = auq_source.resolve_auq_source_for_dispatch(self._WID, pane)
            assert src.kind == "pane"
        finally:
            _unbind_window(self._WID)


class TestGcStaleLiveSafe:
    """Plan v3 §8 tests 11/12 — the startup side-file GC gains an INJECTED
    ``is_live_session`` predicate (mirroring ``md_capture.gc_stale``) so a
    long-open live AUQ's side file (>1h, but the prompt is still pending —
    its tool_use is buffered, never in JSONL) is NOT reaped at startup.
    ``gc_stale`` keys on the file MTIME, so we age the file via ``os.utime``.
    """

    _SID = "4766fb07-7057-4981-9832-93e524ab943e"

    def _age_side_file(self, cc_dir: Path, session_id: str) -> Path:
        _write_side_file_at(cc_dir, session_id, written_at=time.time())
        path = cc_dir / "auq_pending" / f"{session_id}.json"
        old = time.time() - (auq_source._PRETOOL_GC_AGE_SECONDS + 600)
        os.utime(path, (old, old))
        return path

    def test_live_session_predicate_preserves_old_file(self, _cc_dir):
        path = self._age_side_file(_cc_dir, self._SID)
        deleted = auq_source.gc_stale(is_live_session=lambda sid: True)
        assert deleted == 0
        assert path.exists()

    def test_dead_session_predicate_deletes_old_file(self, _cc_dir):
        path = self._age_side_file(_cc_dir, self._SID)
        deleted = auq_source.gc_stale(is_live_session=lambda sid: False)
        assert deleted == 1
        assert not path.exists()

    def test_predicate_raise_is_conservative_skip(self, _cc_dir):
        def _boom(_sid: str) -> bool:
            raise RuntimeError("liveness probe failed")

        path = self._age_side_file(_cc_dir, self._SID)
        deleted = auq_source.gc_stale(is_live_session=_boom)
        assert deleted == 0
        assert path.exists()

    def test_no_predicate_keeps_legacy_delete_behavior(self, _cc_dir):
        path = self._age_side_file(_cc_dir, self._SID)
        deleted = auq_source.gc_stale()
        assert deleted == 1
        assert not path.exists()

    def test_fresh_file_never_deleted_even_with_dead_predicate(self, _cc_dir):
        _write_side_file_at(_cc_dir, self._SID, written_at=time.time())
        path = _cc_dir / "auq_pending" / f"{self._SID}.json"
        deleted = auq_source.gc_stale(is_live_session=lambda sid: False)
        assert deleted == 0
        assert path.exists()
