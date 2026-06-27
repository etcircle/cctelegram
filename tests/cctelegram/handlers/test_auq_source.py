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


# The di-copilot @4 false-bail shape, derived from the REAL affordance fixture:
# the recommended option's LABEL carries the literal ``(Recommended)`` suffix.
# The pane renders the suffix; the terminal parser strips it into a structured
# ``recommended`` flag, so the visible pane label loses the suffix while the
# side-file label keeps it — a naive label compare then false-mismatches.
def _build_recommended_tool_input() -> dict:
    sidefile = json.loads(_AFFORDANCE_SIDEFILE.read_text())
    tool_input = json.loads(json.dumps(sidefile["tool_input"]))  # deep copy
    opt0 = tool_input["questions"][0]["options"][0]
    opt0["label"] = opt0["label"] + " (Recommended)"
    return tool_input


def _build_recommended_pane() -> str:
    lines = _AFFORDANCE_PANE.read_text().splitlines()
    lines[0] = lines[0].rstrip() + " (Recommended)"  # option 1's cursor line
    return "\n".join(lines) + "\n"


_RECOMMENDED_TOOL_INPUT = _build_recommended_tool_input()
_RECOMMENDED_PANE = _build_recommended_pane()


def _write_recommended_side_file(cc_dir: Path, session_id: str) -> dict:
    """Write a side file whose recommended option label carries ``(Recommended)``."""
    sidefile = json.loads(_AFFORDANCE_SIDEFILE.read_text())
    pending = cc_dir / "auq_pending"
    pending.mkdir(mode=0o700, exist_ok=True)
    (pending / f"{session_id}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": session_id,
                "tool_use_id": sidefile["tool_use_id"],
                "written_at": time.time(),
                "tool_input": _RECOMMENDED_TOOL_INPUT,
            }
        )
    )
    return _RECOMMENDED_TOOL_INPUT


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

    def test_recommended_suffix_side_file_stays_side_file(self, _cc_dir):
        """Dispatch path: a side file whose recommended option label carries the
        literal ``(Recommended)`` suffix must still resolve to ``side_file``
        (consistent) — the pane parser strips the suffix while the side-file
        label keeps it, and the predicate normalizes both sides. Pre-fix this
        fail-closed to ``pane`` (label_mismatch) — the di-copilot @4 shape that
        also dropped the descriptions card on the render path.
        """
        _bind_window(self._WID, self._SID)
        try:
            tool_input = _write_recommended_side_file(_cc_dir, self._SID)
            src = auq_source.resolve_auq_source_for_dispatch(
                self._WID, _RECOMMENDED_PANE
            )
            assert src.kind == "side_file"
            assert src.payload == tool_input
            assert src.form is not None
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


# ── RENDER-path resolver + render identity (PR-3 PR-B) ───────────────────────

_BAIL_PANE = (
    "Pick a deploy target for this change:\n"
    "\n"
    "❯ 1. Staging\n"
    "     Push to the staging cluster first.\n"
    "  2. Production\n"
    "     Go straight to prod.\n"
    "  3. Type something.\n"
    "────────────────────────────────────\n"
    "  4. Chat about this\n"
    "\n"
    "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
)


class TestRenderResolver:
    _WID = "@auqsrc-render"
    _SID = "4766fb07-7057-4981-9832-93e524ab943e"

    def test_consistent_pane_is_side_file_ok_trusted(self, _cc_dir):
        _bind_window(self._WID, self._SID)
        try:
            tool_input = _write_affordance_side_file(_cc_dir, self._SID)
            pane = _AFFORDANCE_PANE.read_text()
            r = auq_source.resolve_auq_source_for_render(self._WID, pane)
            assert r.decision == "side_file_ok"
            assert r.kind == "side_file"
            assert r.dispatch_trusted is True
            assert r.payload == tool_input
            # Mint/validate parity: the trusted source tags match the strict
            # resolver's (kind + canonical-dict fingerprint).
            strict = auq_source.resolve_auq_source(self._WID, None, pane)
            assert (r.kind, r.source_fingerprint) == (
                strict.kind,
                strict.source_fingerprint,
            )
        finally:
            _unbind_window(self._WID)

    def test_unparseable_pane_is_rescue_untrusted(self, _cc_dir):
        _bind_window(self._WID, self._SID)
        try:
            _write_affordance_side_file(_cc_dir, self._SID)
            # Busy scrollback that carries no picker at all → unparseable pane.
            pane = "\n".join(f"  trace {i}: tool output churn" for i in range(40))
            r = auq_source.resolve_auq_source_for_render(self._WID, pane)
            assert r.decision == "rescue"
            assert r.dispatch_trusted is False
            assert r.kind == "side_file"
            # Rescue still renders the FULL side-file content (options present).
            assert r.form is not None and len(r.form.options) >= 1
        finally:
            _unbind_window(self._WID)

    def test_empty_pane_is_rescue_untrusted(self, _cc_dir):
        _bind_window(self._WID, self._SID)
        try:
            _write_affordance_side_file(_cc_dir, self._SID)
            r = auq_source.resolve_auq_source_for_render(self._WID, "")
            assert r.decision == "rescue"
            assert r.dispatch_trusted is False
        finally:
            _unbind_window(self._WID)

    def test_different_complete_pane_bails_to_pane_trusted(self, _cc_dir):
        _bind_window(self._WID, self._SID)
        try:
            _write_affordance_side_file(_cc_dir, self._SID)
            # The live pane shows a DIFFERENT, COMPLETE picker → never serve the
            # stale side file; bail to the pane (trusted).
            r = auq_source.resolve_auq_source_for_render(self._WID, _BAIL_PANE)
            assert r.decision == "bail"
            assert r.kind == "pane"
            assert r.dispatch_trusted is True
            assert r.payload is None
        finally:
            _unbind_window(self._WID)

    def test_different_incomplete_pane_renders_pane_not_stale_side_file(self, _cc_dir):
        """Wrong-question fix (hermes + internal review): when the live pane
        parses a DIFFERENT, INCOMPLETE picker (scrolled/partial), the resolver
        must render the PANE display-only — NEVER rescue the STALE side file's
        question. Rescue is reserved for a genuinely UNPARSEABLE pane
        (pane_form is None). dispatch_trusted=False (partial → no safe dispatch);
        kind=pane so NO stale side-file ctx card is posted.
        """
        _bind_window(self._WID, self._SID)
        try:
            _write_affordance_side_file(_cc_dir, self._SID)
            # A genuinely DIFFERENT live picker, scrolled so option 1 is off the
            # top (options start at 3 → not contiguous-from-1 → incomplete).
            pane = (
                "  3. Rebase onto main\n"
                "     Replay your commits onto the updated base.\n"
                "❯ 4. Type something.\n"
                "────────────────────────────────────\n"
                "  5. Chat about this\n"
                "\n"
                "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
            )
            r = auq_source.resolve_auq_source_for_render(self._WID, pane)
            assert r.decision == "bail"
            assert r.kind == "pane"
            assert r.dispatch_trusted is False
            assert r.reason.startswith("bail_partial")
            # It serves the LIVE pane's (partial) options exactly — option 3
            # only (4/5 are dropped affordances) — NOT the stale side file's
            # question/options.
            assert r.form is not None
            assert [o.label for o in r.form.options] == ["Rebase onto main"]
        finally:
            _unbind_window(self._WID)

    def test_aged_consistent_side_file_does_not_mint_trusted(self, _cc_dir):
        """A consistent side file PAST the read-TTL must NOT yield a trusted
        side_file_ok token (the TTL'd validate path would reject it → dead-tap).
        With a complete consistent pane it bails to the pane instead.
        """
        _bind_window(self._WID, self._SID)
        try:
            sidefile = json.loads(_AFFORDANCE_SIDEFILE.read_text())
            pending = _cc_dir / "auq_pending"
            pending.mkdir(mode=0o700, exist_ok=True)
            (pending / f"{self._SID}.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": self._SID,
                        "tool_use_id": sidefile["tool_use_id"],
                        # Aged well past the 300s read-TTL.
                        "written_at": time.time() - 1000,
                        "tool_input": sidefile["tool_input"],
                    }
                )
            )
            pane = _AFFORDANCE_PANE.read_text()
            r = auq_source.resolve_auq_source_for_render(self._WID, pane)
            assert r.decision != "side_file_ok"
            # The affordance pane is a complete picker → bail to it (trusted via
            # the pane source, which the TTL'd validate path also resolves to).
            assert r.decision == "bail"
        finally:
            _unbind_window(self._WID)

    def test_no_side_file_falls_back_to_pane(self, _cc_dir):
        _bind_window(self._WID, self._SID)
        try:
            r = auq_source.resolve_auq_source_for_render(self._WID, _BAIL_PANE)
            assert r.decision == "pane"
            assert r.dispatch_trusted is True
        finally:
            _unbind_window(self._WID)

    def test_recommended_suffix_label_is_side_file_ok_not_false_bail(self, _cc_dir):
        """A side file whose recommended option label carries the literal
        ``(Recommended)`` suffix must still be judged CONSISTENT with the live
        pane and yield ``side_file_ok`` (so the 📋 descriptions card posts).

        The pane parser strips ``(Recommended)`` from the visible label into a
        structured flag, while the PreToolUse side file retains it verbatim — so
        a naive label compare false-mismatches and the resolver bails
        (``bail_label_mismatch``), dropping the descriptions for the SAME
        question. Regression from di-copilot @4 (2026-06-24): every AUQ whose
        recommended option carried the suffix lost its descriptions card.
        """
        _bind_window(self._WID, self._SID)
        try:
            tool_input = _write_recommended_side_file(_cc_dir, self._SID)
            r = auq_source.resolve_auq_source_for_render(self._WID, _RECOMMENDED_PANE)
            assert r.decision == "side_file_ok"
            assert r.kind == "side_file"
            assert r.dispatch_trusted is True
            assert r.payload == tool_input
        finally:
            _unbind_window(self._WID)

    def test_recommended_suffix_record_consistent_with_pane(self, _cc_dir):
        """Unit: the consistency predicate accepts a recommended-suffix label
        mismatch (the root predicate behind the false bail above)."""
        from cctelegram.handlers.auq_source import (
            PreToolAskRecord,
            _record_consistent_with_pane,
        )

        tool_input = _RECOMMENDED_TOOL_INPUT
        record = PreToolAskRecord(
            session_id=self._SID,
            tool_use_id="toolu_rec_test",
            tool_input=tool_input,
            written_at=time.time(),
            input_fingerprint="",
        )
        pane_form = resolve_ask_form(None, _RECOMMENDED_PANE)
        assert pane_form is not None
        assert _record_consistent_with_pane(record, pane_form) == (True, "ok")


class TestRenderIdentity:
    _WID = "@auqsrc-identity"
    _SID = "4766fb07-7057-4981-9832-93e524ab943e"

    def test_rescue_identity_stable_under_scrollback_churn(self, _cc_dir):
        """The loop kill: a rescue card's render identity is INVARIANT as
        unrelated scrollback scrolls under it (the pure side-file form has no
        pane fields), so the poller never re-renders it every tick.
        """
        _bind_window(self._WID, self._SID)
        try:
            _write_affordance_side_file(_cc_dir, self._SID)
            churn_a = "\n".join(f"trace {i}: alpha" for i in range(30))
            churn_b = "\n".join(f"DIFFERENT line {i}: beta gamma" for i in range(55))
            id_a = auq_source.peek_render_identity(self._WID, churn_a)
            id_b = auq_source.peek_render_identity(self._WID, churn_b)
            # Both rescue; identity unchanged despite totally different scrollback.
            assert (
                auq_source.resolve_auq_source_for_render(self._WID, churn_a).decision
                == "rescue"
            )
            assert id_a == id_b
        finally:
            _unbind_window(self._WID)

    def test_title_less_pane_identity_stable_under_scrollback_churn(self, _cc_dir):
        """Regression (internal review): the DOMINANT live single-select shape —
        a `pane` / `bail` decision whose `current_question_title` is None (Claude
        hasn't flushed the AUQ tool_use to JSONL) — must stay STABLE under
        scrollback churn. The first cut folded `pane_walkback_title` (scraped
        from churning scrollback above the option block) into render_signature,
        which re-rendered the card every tick. render_signature now uses
        current_question_title ONLY, matching the OLD content hash's stability.
        """
        # No side file → `pane` decision; identical complete picker block, two
        # totally different busy-topic scrollbacks above it (the line just above
        # the options becomes pane_walkback_title).
        picker = (
            "❯ 1. Staging\n"
            "     Push to the staging cluster first.\n"
            "  2. Production\n"
            "     Go straight to prod.\n"
            "  3. Type something.\n"
            "────────────────────────────────────\n"
            "  4. Chat about this\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        pane_a = "⏺ Bash(build 12)\n⏺ Bash(build 13)\n" + picker
        pane_b = "⎿ test 98 ok\n⎿ test 99 ok\n" + picker
        # Sanity: this IS the title-less pane decision, and the walkback titles
        # genuinely DIFFER (so a regression would be caught).
        ra = auq_source.resolve_auq_source_for_render(self._WID, pane_a)
        rb = auq_source.resolve_auq_source_for_render(self._WID, pane_b)
        assert ra.decision == "pane" and rb.decision == "pane"
        assert ra.form is not None and ra.form.current_question_title is None
        assert ra.form.pane_walkback_title != rb.form.pane_walkback_title
        # The render identity must be EQUAL despite the differing scrollback.
        assert auq_source.peek_render_identity(
            self._WID, pane_a
        ) == auq_source.peek_render_identity(self._WID, pane_b)

    def test_render_signature_changes_on_cursor_move(self):
        from cctelegram.terminal_parser import AskOption, AskUserQuestionForm

        base = AskUserQuestionForm(
            current_question_title="Pick one.",
            options=(
                AskOption(label="A", recommended=False, cursor=True, number=1),
                AskOption(label="B", recommended=False, cursor=False, number=2),
            ),
            select_mode="single",
        )
        moved = AskUserQuestionForm(
            current_question_title="Pick one.",
            options=(
                AskOption(label="A", recommended=False, cursor=False, number=1),
                AskOption(label="B", recommended=False, cursor=True, number=2),
            ),
            select_mode="single",
        )
        # The pick-token fingerprint is cursor-BLIND (stable token); the render
        # signature is cursor-AWARE (the renderer paints ❯) → it MUST change.
        assert base.fingerprint() == moved.fingerprint()
        assert auq_source.render_signature(base) != auq_source.render_signature(moved)

    def test_render_signature_changes_on_multiselect_toggle(self):
        from cctelegram.terminal_parser import AskOption, AskUserQuestionForm

        base = AskUserQuestionForm(
            current_question_title="Pick several.",
            options=(
                AskOption(
                    label="A", recommended=False, cursor=True, number=1, selected=False
                ),
                AskOption(
                    label="B", recommended=False, cursor=False, number=2, selected=False
                ),
            ),
            select_mode="multi",
        )
        toggled = AskUserQuestionForm(
            current_question_title="Pick several.",
            options=(
                AskOption(
                    label="A", recommended=False, cursor=True, number=1, selected=True
                ),
                AskOption(
                    label="B", recommended=False, cursor=False, number=2, selected=False
                ),
            ),
            select_mode="multi",
        )
        assert auq_source.render_signature(base) != auq_source.render_signature(toggled)


# ── partial-bail ctx recovery (v5 plan §6.2) ─────────────────────────────────


def _write_side_file_aged(
    cc_dir: Path,
    session_id: str,
    tool_input: dict,
    *,
    tool_use_id: str = "toolu_aged_recover",
) -> dict:
    """Write a side file aged past the 300s ``_PRETOOL_TTL_SECONDS`` read-TTL.

    ``written_at = time.time() - 1000`` mirrors
    ``test_aged_consistent_side_file_does_not_mint_trusted`` — the long-open
    busy-card shape the recovery helper targets.
    """
    pending = cc_dir / "auq_pending"
    pending.mkdir(mode=0o700, exist_ok=True)
    (pending / f"{session_id}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": session_id,
                "tool_use_id": tool_use_id,
                "written_at": time.time() - 1000,
                "tool_input": tool_input,
            }
        )
    )
    return tool_input


def _single_q_input(labels: list[str], *, title: str) -> dict:
    """A single-question tool_input with the given option labels + title."""
    return {
        "questions": [
            {
                "question": title,
                "header": "Scope",
                "multiSelect": False,
                "options": [{"label": label, "description": ""} for label in labels],
            }
        ]
    }


def _partial_pane(
    rows: list[tuple[int, str]],
    *,
    title: str | None = None,
    cursor_number: int | None = None,
    affordances: bool = True,
) -> str:
    """Build a partial single-tab picker pane (no governing ``←…→`` tab header).

    Each ``(number, label)`` row renders a numbered option; ``cursor_number``
    marks the live ``❯`` cursor. Without a tab header the parser leaves
    ``current_question_title is None`` (the title goes to ``pane_walkback_title``,
    forbidden for identity) — the titleless residual-(b) shape.
    """
    lines: list[str] = []
    if title is not None:
        lines.append(title)
        lines.append("")
    for number, label in rows:
        prefix = "❯" if number == cursor_number else " "
        lines.append(f"{prefix} {number}. {label}")
        lines.append(f"     description for option {number}")
    if affordances:
        next_num = rows[-1][0] + 1
        lines.append(f"  {next_num}. Type something.")
        lines.append("─" * 40)
        lines.append(f"  {next_num + 1}. Chat about this")
    lines.append("")
    lines.append("Enter to select · ↑/↓ to navigate · Esc to cancel")
    return "\n".join(lines) + "\n"


def _tab_header_partial_pane(
    rows: list[tuple[int, str]], *, title: str, cursor_number: int | None = None
) -> str:
    """A partial pane WITH a governing ``←…→`` tab header so the parser
    populates ``current_question_title`` (the Leg A title-corroboration shape).
    """
    lines = [f"← ☐ {title[:20]}  ✔ Submit →", title, ""]
    for number, label in rows:
        prefix = "❯" if number == cursor_number else " "
        lines.append(f"{prefix} {number}. {label}")
    lines.append("Enter to select · ↑/↓ to navigate · Esc to cancel")
    return "\n".join(lines) + "\n"


class TestRecoverConsistentSideFileForCtx:
    """v5 plan §6.2 — the read-only ctx-recovery helper + its evidence floor.

    The helper ``recover_consistent_side_file_for_ctx`` returns the side-file
    payload ONLY on a consistent PARTIAL-pane bail that clears the
    anti-coincidence floor (Leg A reliable ≥8-char title OR Leg B ≥2 distinct
    numbered slot-matches). Read-only; never mutates ``_pretool_ask_records``.
    """

    _WID = "@auqsrc-ctxrec"
    _SID = "4766fb07-7057-4981-9832-93e524ab943e"

    # The DiCopilot positive: option 1 scrolled off, options 2,3 visible.
    _DICO_LABELS = [
        "A) Review the 66 proposals",
        "B) Draft the synthesis doc",
        "C) Defer to next session",
    ]
    _DICO_TITLE = "What should we do next with the proposals?"

    def test_titleless_partial_one_coincidental_label_returns_none(self, _cc_dir):
        """P1b micro-pin (STEPWISE/SCAFFOLD: RED vs a no-floor helper, NOT vs
        bare main where it fails by missing-import). A STALE/different side file
        shares ONE numbered label with a titleless partial pane; the resolver
        ALONE would accept (``(True, "ok")``), but the floor (Leg A dead via
        ``current_question_title=None``, Leg B needs ≥2) rejects → ``None``.
        """
        _bind_window(self._WID, self._SID)
        try:
            tool_input = _single_q_input(
                ["X) Stale option one", "Y) Stale option two", "Z) Stale option three"],
                title="A totally different stale question",
            )
            _write_side_file_aged(_cc_dir, self._SID, tool_input)
            # Titleless partial pane with ONE numbered option coincidentally
            # equal to the side file's slot-2 label.
            pane = _partial_pane(
                [(2, "Y) Stale option two")], title=None, cursor_number=2
            )
            pane_form = resolve_ask_form(None, pane)
            assert pane_form is not None
            # Premise: the resolver ALONE accepts (proves the floor is what rejects).
            assert pane_form.current_question_title is None
            from cctelegram.handlers.auq_source import (
                PreToolAskRecord,
                _record_consistent_with_pane,
            )

            record = PreToolAskRecord(
                session_id=self._SID,
                tool_use_id="t",
                tool_input=tool_input,
                written_at=time.time(),
                input_fingerprint="",
            )
            assert _record_consistent_with_pane(record, pane_form) == (True, "ok")
            # The floor rejects the single-coincidence wrong-question case.
            assert (
                auq_source.recover_consistent_side_file_for_ctx(self._WID, pane) is None
            )
        finally:
            _unbind_window(self._WID)

    def test_short_title_coincidence_floored_to_none(self, _cc_dir):
        """Tautology micro-pin (STEPWISE/SCAFFOLD: RED vs a v2-style
        ``min(8, len)`` Leg A, NOT vs bare main). A 3-char pane title ("OK?")
        substring-matches the candidate title + ONE numbered match. The fixed-8
        ``_CTX_TITLE_MIN_CHARS`` floor rejects (``min(8, len(shorter))`` would
        clear the 3-char title).
        """
        _bind_window(self._WID, self._SID)
        try:
            tool_input = _single_q_input(
                ["only option here"], title="OK? are we proceeding with this"
            )
            _write_side_file_aged(_cc_dir, self._SID, tool_input)
            # Tab-header pane so current_question_title is populated ("OK?", 3
            # chars) and substring-matches the candidate; only ONE numbered match.
            pane = _tab_header_partial_pane(
                [(1, "only option here")], title="OK?", cursor_number=1
            )
            pane_form = resolve_ask_form(None, pane)
            assert pane_form is not None
            assert pane_form.current_question_title == "OK?"
            assert (
                auq_source.recover_consistent_side_file_for_ctx(self._WID, pane) is None
            )
        finally:
            _unbind_window(self._WID)

    def test_long_title_corroboration_recovers(self, _cc_dir):
        """Over-tightening guard (GREEN): a candidate title ≥8 chars + a true
        ≥8-char pane-title substring + ONE numbered match rides Leg A alone and
        recovers the payload. Guards Leg A against becoming useless.
        """
        _bind_window(self._WID, self._SID)
        try:
            tool_input = _single_q_input(
                ["A) Review the 66 proposals"], title="Review the 66 proposals"
            )
            _write_side_file_aged(_cc_dir, self._SID, tool_input)
            pane = _tab_header_partial_pane(
                [(1, "A) Review the 66 proposals")],
                title="Review the 66 proposals",
                cursor_number=1,
            )
            recovered = auq_source.recover_consistent_side_file_for_ctx(self._WID, pane)
            assert recovered is not None
            assert recovered.payload == tool_input
        finally:
            _unbind_window(self._WID)

    def test_dicopilot_partial_bail_two_plus_matches_recovers_payload(self, _cc_dir):
        """The target-positive over-tightening guard. A 3-option side file; the
        pane shows numbered slots 2,3 (title absent) → Leg B (2 ≥ 2) recovers the
        payload, which carries option 1's label ("A) Review the 66 proposals").
        """
        _bind_window(self._WID, self._SID)
        try:
            tool_input = _single_q_input(self._DICO_LABELS, title=self._DICO_TITLE)
            _write_side_file_aged(_cc_dir, self._SID, tool_input)
            pane = _partial_pane(
                [(2, self._DICO_LABELS[1]), (3, self._DICO_LABELS[2])],
                title=None,
                cursor_number=3,
            )
            recovered = auq_source.recover_consistent_side_file_for_ctx(self._WID, pane)
            assert recovered is not None
            assert recovered.payload == tool_input
            labels = [o["label"] for o in recovered.payload["questions"][0]["options"]]
            assert "A) Review the 66 proposals" in labels
        finally:
            _unbind_window(self._WID)

    def test_two_option_partial_with_title_recovers(self, _cc_dir):
        """A genuinely-2-option AUQ; only option 2 survives the scroll BUT
        ``current_question_title`` is present and a ≥8-char match → recovers via
        Leg A. Proves the title escape hatch + that fixed-8 doesn't kill a real
        long title.
        """
        _bind_window(self._WID, self._SID)
        try:
            tool_input = _single_q_input(
                ["A) Keep the legacy path", "B) Migrate to the new pipeline"],
                title="Choose the migration approach for this service",
            )
            _write_side_file_aged(_cc_dir, self._SID, tool_input)
            pane = _tab_header_partial_pane(
                [(2, "B) Migrate to the new pipeline")],
                title="Choose the migration approach for this service",
                cursor_number=2,
            )
            recovered = auq_source.recover_consistent_side_file_for_ctx(self._WID, pane)
            assert recovered is not None
            assert recovered.payload == tool_input
        finally:
            _unbind_window(self._WID)

    def test_titleless_partial_two_matching_labels_recovers(self, _cc_dir):
        """The DiCopilot 3-option positive at the unit level (N=2 load-bearing):
        a titleless pane with slots 2,3 both matching by number → Leg B → payload.
        Pins the exact threshold so the implementer can't make the floor
        title-only.
        """
        _bind_window(self._WID, self._SID)
        try:
            tool_input = _single_q_input(self._DICO_LABELS, title=self._DICO_TITLE)
            _write_side_file_aged(_cc_dir, self._SID, tool_input)
            pane = _partial_pane(
                [(2, self._DICO_LABELS[1]), (3, self._DICO_LABELS[2])],
                title=None,
                cursor_number=2,
            )
            pane_form = resolve_ask_form(None, pane)
            assert pane_form is not None
            assert pane_form.current_question_title is None
            recovered = auq_source.recover_consistent_side_file_for_ctx(self._WID, pane)
            assert recovered is not None
            assert recovered.payload == tool_input
        finally:
            _unbind_window(self._WID)

    def test_one_real_option_plus_affordances_floored(self, _cc_dir):
        """One real numbered option + ``Type something.`` + ``Chat about this``
        → ONE real match (< 2) → ``None``. Affordances never inflate the count.
        """
        _bind_window(self._WID, self._SID)
        try:
            tool_input = _single_q_input(self._DICO_LABELS, title=self._DICO_TITLE)
            _write_side_file_aged(_cc_dir, self._SID, tool_input)
            # Only slot 2 is a real option; the pane builder adds affordances.
            pane = _partial_pane(
                [(2, self._DICO_LABELS[1])],
                title=None,
                cursor_number=2,
                affordances=True,
            )
            assert (
                auq_source.recover_consistent_side_file_for_ctx(self._WID, pane) is None
            )
        finally:
            _unbind_window(self._WID)

    def test_duplicate_label_does_not_inflate_count(self, _cc_dir):
        """ARTIFICIAL micro-pin (round-4 Hermes P3) of the ``set``-keyed-on-
        ``o.number`` invariant — NOT naturally parser-reachable, and does NOT
        substitute for the §6.1 seam tests. A degenerate form duplicating one
        label at two slots both mapping to the SAME candidate index must NOT reach
        2 distinct slot-matches → the floor stays unsatisfied.

        Drives ``_ctx_evidence_floor_ok`` directly with a hand-built form so the
        duplicate-slot arithmetic is exercised in isolation.
        """
        from cctelegram.handlers.auq_source import (
            PreToolAskRecord,
            _ctx_evidence_floor_ok,
        )
        from cctelegram.terminal_parser import AskOption, AskUserQuestionForm

        # Candidate has 3 distinct labels; the pane (degenerately) shows the SAME
        # candidate-slot-2 label twice, numbered 2 and 2 is impossible, so we use
        # two options whose numbers map to the SAME candidate index via an
        # out-of-range duplicate. Build a form where two options both match slot 2.
        tool_input = _single_q_input(self._DICO_LABELS, title=self._DICO_TITLE)
        record = PreToolAskRecord(
            session_id=self._SID,
            tool_use_id="t",
            tool_input=tool_input,
            written_at=time.time(),
            input_fingerprint="",
        )
        # Two pane options that BOTH carry number 2 (degenerate) → one distinct
        # slot → < 2 even though two rows "match".
        form = AskUserQuestionForm(
            current_question_title=None,
            options=(
                AskOption(
                    label=self._DICO_LABELS[1],
                    recommended=False,
                    cursor=True,
                    number=2,
                ),
                AskOption(
                    label=self._DICO_LABELS[1],
                    recommended=False,
                    cursor=False,
                    number=2,
                ),
            ),
            select_mode="single",
        )
        assert _ctx_evidence_floor_ok(record, form) is False

    def test_complete_picker_trusted_bail_returns_none(self, _cc_dir):
        """A complete DIFFERENT picker (``_BAIL_PANE``, ``dispatch_trusted=True``)
        → helper ``None`` via the ``pane_form_is_complete_picker`` short-circuit.
        Premise asserts the render decision is a TRUSTED bail.
        """
        _bind_window(self._WID, self._SID)
        try:
            _write_affordance_side_file(_cc_dir, self._SID)
            r = auq_source.resolve_auq_source_for_render(self._WID, _BAIL_PANE)
            assert r.decision == "bail" and r.dispatch_trusted is True
            assert (
                auq_source.recover_consistent_side_file_for_ctx(self._WID, _BAIL_PANE)
                is None
            )
        finally:
            _unbind_window(self._WID)

    def test_inconsistent_partial_bail_returns_none(self, _cc_dir):
        """An aged side file whose title genuinely differs from a title-bearing
        partial pane → ``_record_consistent_with_pane`` ``title_mismatch`` →
        helper ``None``.
        """
        _bind_window(self._WID, self._SID)
        try:
            tool_input = _single_q_input(
                ["A) Keep the legacy path", "B) Migrate to the new pipeline"],
                title="Choose the migration approach for this service",
            )
            _write_side_file_aged(_cc_dir, self._SID, tool_input)
            # Tab-header pane whose title genuinely differs from the candidate.
            pane = _tab_header_partial_pane(
                [(2, "B) Migrate to the new pipeline")],
                title="Pick the rollback strategy for the deploy",
                cursor_number=2,
            )
            assert (
                auq_source.recover_consistent_side_file_for_ctx(self._WID, pane) is None
            )
        finally:
            _unbind_window(self._WID)

    def test_no_side_file_returns_none(self, _cc_dir):
        """No side file → ``None`` (trivial guard)."""
        _bind_window(self._WID, self._SID)
        try:
            pane = _partial_pane(
                [(2, self._DICO_LABELS[1]), (3, self._DICO_LABELS[2])],
                title=None,
                cursor_number=2,
            )
            assert (
                auq_source.recover_consistent_side_file_for_ctx(self._WID, pane) is None
            )
        finally:
            _unbind_window(self._WID)

    def test_unparseable_pane_returns_none(self, _cc_dir):
        """An unparseable pane (``pane_form is None``) → ``None`` (the rescue path
        owns that case)."""
        _bind_window(self._WID, self._SID)
        try:
            tool_input = _single_q_input(self._DICO_LABELS, title=self._DICO_TITLE)
            _write_side_file_aged(_cc_dir, self._SID, tool_input)
            pane = "\n".join(f"  trace {i}: tool output churn" for i in range(40))
            assert (
                auq_source.recover_consistent_side_file_for_ctx(self._WID, pane) is None
            )
        finally:
            _unbind_window(self._WID)

    def test_multiq_single_coincidence_floored_to_none(self, _cc_dir):
        """A multi-question side file where one coincidental label across
        questions matches a titleless partial pane → ``None`` (the multi-Q
        fallthrough is looser than ``_strong_match``, so the floor must catch it).
        """
        _bind_window(self._WID, self._SID)
        try:
            tool_input = {
                "questions": [
                    {
                        "question": "Which migration strategy?",
                        "header": "Strategy",
                        "options": [
                            {"label": "P) Lift and shift", "description": ""},
                            {"label": "Q) Rewrite incrementally", "description": ""},
                        ],
                    },
                    {
                        "question": "Which rollout cadence?",
                        "header": "Cadence",
                        "options": [
                            {"label": "R) Big bang", "description": ""},
                            {"label": "S) Canary", "description": ""},
                        ],
                    },
                ]
            }
            _write_side_file_aged(_cc_dir, self._SID, tool_input)
            # Titleless pane with ONE numbered option coincidentally equal to
            # question-1 slot-2 ("Q) Rewrite incrementally").
            pane = _partial_pane(
                [(2, "Q) Rewrite incrementally")], title=None, cursor_number=2
            )
            assert (
                auq_source.recover_consistent_side_file_for_ctx(self._WID, pane) is None
            )
        finally:
            _unbind_window(self._WID)

    def test_ctx_recovery_candidate_is_subset_of_record_consistent_with_pane(
        self, _cc_dir
    ):
        """Round-3 P3 SUBSET-parity unit (REPLACES exact-equality).

        PREMISE (round-4 Hermes P3): the subset guarantee
        ``helper-accepts ⇒ resolver-accepts`` holds only in the INTENDED calling
        context — AFTER ``_record_consistent_with_pane`` has accepted, or for
        fixtures deliberately built to satisfy that premise. ``_ctx_recovery_
        candidate`` is a standalone candidate-picker that can return a single-Q
        candidate (``raw[0]``) BEFORE any consistency check, so this is a
        statement about the PRODUCTION call path, NOT an unconditional property
        of the isolated candidate-picker.

        For each accepted fixture this loop now PROVES candidate selection: it
        asserts the resolver-accepted premise (``_record_consistent_with_pane
        (...)[0] is True``) AND that ``_ctx_recovery_candidate`` returns the
        SPECIFIC intended question dict (the forward subset — helper-accepts ⇒
        resolver-accepts, picking the INTENDED candidate, not merely a candidate
        the resolver also accepts). The trailing mixed-label case documents the
        fail-closed converse gap: a gappy/cross-question pane the resolver's
        numbered-slot accept would allow but the helper's contiguous-subsequence
        picker DECLINES (a bounded, safe false-negative that can only DROP a
        card, never post a wrong one). It does NOT assert the converse.
        """
        from cctelegram.handlers.auq_source import (
            PreToolAskRecord,
            _ctx_recovery_candidate,
            _record_consistent_with_pane,
        )

        def _rec(tool_input: dict) -> PreToolAskRecord:
            return PreToolAskRecord(
                session_id=self._SID,
                tool_use_id="t",
                tool_input=tool_input,
                written_at=time.time(),
                input_fingerprint="",
            )

        # (i) single-Q, resolver-accepted (titleless partial, ≥2 slot matches).
        single = _single_q_input(self._DICO_LABELS, title=self._DICO_TITLE)
        single_pane = resolve_ask_form(
            None,
            _partial_pane(
                [(2, self._DICO_LABELS[1]), (3, self._DICO_LABELS[2])],
                title=None,
                cursor_number=2,
            ),
        )
        assert single_pane is not None

        # (ii) tab-inferred + title-match multi-Q.
        multi = {
            "questions": [
                {
                    "question": "Choose the migration approach for this service",
                    "header": "Approach",
                    "options": [
                        {"label": "A) Keep the legacy path", "description": ""},
                        {"label": "B) Migrate to the new pipeline", "description": ""},
                    ],
                },
                {
                    "question": "Pick the rollback strategy for the deploy",
                    "header": "Rollback",
                    "options": [
                        {"label": "C) Manual rollback", "description": ""},
                        {"label": "D) Automated rollback", "description": ""},
                    ],
                },
            ]
        }
        multi_pane = resolve_ask_form(
            None,
            _tab_header_partial_pane(
                [(2, "B) Migrate to the new pipeline")],
                title="Choose the migration approach for this service",
                cursor_number=2,
            ),
        )
        assert multi_pane is not None

        # (iii) subsequence-fallback multi-Q (titleless, contiguous subsequence).
        multi_sub_pane = resolve_ask_form(
            None,
            _partial_pane(
                [(1, "A) Keep the legacy path"), (2, "B) Migrate to the new pipeline")],
                title=None,
                cursor_number=1,
            ),
        )
        assert multi_sub_pane is not None

        # (tool_input, pane_form, expected_candidate) — the SPECIFIC question dict
        # the helper must return: single-Q → q0; tab-inferred+title → the
        # title-matching q0; subsequence fallback → the q whose labels A,B form
        # the visible contiguous subsequence (q0).
        for tool_input, pane_form, expected_candidate in (
            (single, single_pane, single["questions"][0]),
            (multi, multi_pane, multi["questions"][0]),
            (multi, multi_sub_pane, multi["questions"][0]),
        ):
            record = _rec(tool_input)
            # Premise: the resolver accepts these constructed inputs.
            assert _record_consistent_with_pane(record, pane_form)[0] is True
            # The helper selects the INTENDED candidate (forward subset: the SAME
            # dict object out of record.tool_input["questions"]).
            candidate = _ctx_recovery_candidate(record, pane_form)
            assert candidate is expected_candidate

        # Subset DIRECTION (the helper is a strict, fail-closed subset): a multi-Q
        # pane whose visible labels are NOT a contiguous subsequence of ANY
        # question (cross-question labels mixed) → the helper's subsequence picker
        # DECLINES (returns None), never inventing a wrong candidate. The
        # converse is NOT asserted: the resolver's numbered-slot final accept can
        # be broader than the helper's contiguous-subsequence pick — a bounded,
        # safe false-negative that can only DROP a card, never post a wrong one.
        # "B) Migrate…" is in q1, "C) Manual rollback" is in q2 → no single
        # question's labels are a contiguous subsequence of the pane → decline.
        mixed_pane = resolve_ask_form(
            None,
            _partial_pane(
                [(2, "B) Migrate to the new pipeline"), (3, "C) Manual rollback")],
                title=None,
                cursor_number=2,
            ),
        )
        assert mixed_pane is not None
        assert _ctx_recovery_candidate(_rec(multi), mixed_pane) is None

    def test_kickstart_renumber_duplicate_is_bounded_to_one(self):
        """MANDATORY (round-4 convergent P2) — residual §11(c).

        BOUND PIN, NOT a main-RED repro: it asserts the kickstart-renumber
        duplicate is BOUNDED (≤1 re-post per uninterrupted hydrate/render
        cycle), not absent. PURE READ-PATH — it deliberately does NOT call
        ``hydrate_interactive_state`` (v4/v5 makes ZERO hydrate change;
        ``TestHydrateInteractiveState`` owns that seam byte-for-byte).

        It seeds the post-prune end-state of an ``@old→@new`` renumber directly:
        ``_auq_context_msgs["@new"]`` holds a recovered DICT ctx-msg record while
        ``_auq_context_posted`` has NO ``"@new"`` marker (the divergence the
        hydrate path produces — the msgs loop remaps, the marker loop prunes
        because it has no ``window_remaps``). Then it exercises ONLY the existing
        ``claim → commit`` once-only gate: the first claim is ALLOWED (the one
        bounded re-post), and after commit installs the marker a second claim is
        BLOCKED (the gate caps re-posts at one).
        """
        from cctelegram.handlers import interactive_ui

        interactive_ui.reset_for_tests()
        try:
            wid = "@new"
            sid = "4766fb07-7057-4981-9832-93e524ab943e"
            dedup_key = "pretool:deadbeefdeadbeef"
            # Seed the post-prune msgs record (source="dict") with NO marker.
            rec = interactive_ui._ContextMsgRecord.from_dict(
                {
                    "message_ids": [4242],
                    "source": "dict",
                    "dedup_key": dedup_key,
                    "tool_use_id": None,
                    "render_sha1": "",
                    "user_id": 12345,
                    "chat_id": -100123,
                    "thread_id": 42,
                    "session_id": sid,
                    "created_at": "",
                }
            )
            assert rec is not None
            interactive_ui._auq_context_msgs[wid] = rec
            assert interactive_ui._auq_context_posted.get(wid) is None

            # First render re-post is ALLOWED (the bounded duplicate).
            token = interactive_ui.claim_auq_context_post_in_memory(wid, dedup_key)
            assert token is not None

            # Commit installs the marker.
            wrote = interactive_ui.commit_auq_context_post(
                wid,
                token,
                (4243,),
                text="📋 AskUserQuestion — full details\n\nbody",
                source={"questions": [{"question": "q", "options": []}]},
                user_id=12345,
                chat_id=-100123,
                thread_id=42,
                session_id=sid,
            )
            assert wrote is True
            assert interactive_ui._auq_context_posted.get(wid) == dedup_key

            # Subsequent renders are BLOCKED (bound = exactly one).
            assert (
                interactive_ui.claim_auq_context_post_in_memory(wid, dedup_key) is None
            )
        finally:
            interactive_ui.reset_for_tests()
