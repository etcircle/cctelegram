"""Unit tests for the R4 pick-token store + atomic validate_and_consume.

Pure unit tests with fake ``capture_pane`` / ``find_window_by_id`` (the module
takes no telegram/tmux import — both are injected). Covers:

  - The validate_and_consume outcome matrix (wrong_user / stale_form /
    source_drift / expired / window_gone / ok), asserting the token is left
    UNRESERVED on every modeled reject so a later legitimate owner tap wins.
  - Sequential post-consume duplicate is ``expired`` (NOT already_consumed —
    that's the ledger's job; a separate callback-level test in
    test_interactive_ledger.py pins the ledger gate).
  - EXCEPTION-SAFETY and CANCELLATION-SAFETY: a raise / a cancel mid-validation
    leaves the token UNRESERVED via the try/finally + owner-id guard.
  - CONCURRENT same-token reservation: caller-2 returns already_consumed WITHOUT
    entering capture_pane (call-count stays 1).
  - CONCURRENT sibling-token: exactly one ``ok``, the loser ``already_consumed``
    via the row's matching consumed_generation (NOT expired).
  - CACHE-REUSE re-render (same tokens, NO generation bump) and
    GENERATION-after-prune (module-global counter → G2, NOT a stale G1
    tombstone read).
  - TTL-prune-during-validation → expired (NOT already_consumed).
  - Source-parity regression (the f0c3f0c dead-button guard) for side_file and
    jsonl_cache against REAL paired fixtures; pane-kind only ok + stale_form.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from cctelegram.handlers import auq_source, pick_token
from cctelegram.session import WindowState, session_manager
from cctelegram.terminal_parser import resolve_ask_form

_FIXTURE_DIR = Path(__file__).parents[1] / "fixtures"
_BASELINE_PANE = (_FIXTURE_DIR / "auq-baseline-pane.txt").read_text()
_AFFORDANCE_PANE = (
    _FIXTURE_DIR / "auq_single_select_with_affordances_pane.txt"
).read_text()
_AFFORDANCE_SIDEFILE = json.loads(
    (_FIXTURE_DIR / "auq_single_select_with_affordances_sidefile.json").read_text()
)

_USER = 42
_THREAD = 7
_WINDOW = "@1"


# ── fakes ─────────────────────────────────────────────────────────────────────


def _window_finder(window_id: str | None = _WINDOW):
    """Return an injected find_window_by_id that yields a fake window (or None)."""

    async def _find(_wid: str):
        if window_id is None:
            return None
        return SimpleNamespace(window_id=window_id)

    return _find


def _pane_capture(pane: str):
    """Return an injected capture_pane that always yields ``pane``."""

    async def _capture(_wid: str, _scrollback: int) -> str:
        return pane

    return _capture


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset():
    pick_token.reset_for_tests()
    auq_source.reset_for_tests()
    yield
    pick_token.reset_for_tests()
    auq_source.reset_for_tests()


def _baseline_source() -> auq_source.ResolvedAuqSource:
    return auq_source.resolve_auq_source(_WINDOW, None, _BASELINE_PANE)


def _mint_baseline_token(
    *,
    user_id: int = _USER,
    option_number: int = 1,
    window_id: str = _WINDOW,
    is_review_submit: bool = False,
) -> str:
    """Mint a single token whose form fingerprint + source tags MATCH the
    baseline pane, so a validate against that pane wins ``ok``."""
    form = resolve_ask_form(None, _BASELINE_PANE)
    assert form is not None
    src = _baseline_source()
    return pick_token.mint(
        pick_token.PickTokenEntry(
            window_id=window_id,
            user_id=user_id,
            thread_id=_THREAD,
            fingerprint=form.fingerprint(),
            option_number=option_number,
            option_label="Done navigating",
            is_review_submit=is_review_submit,
            expires_at=time.monotonic() + 300,
            source_kind=src.kind,
            source_fingerprint=src.source_fingerprint,
            row_generation=1,
        )
    )


async def _validate(token: str, sender: int, *, pane=_BASELINE_PANE, window=_WINDOW):
    return await pick_token.validate_and_consume(
        token,
        sender,
        capture_pane=_pane_capture(pane),
        find_window_by_id=_window_finder(window),
    )


# ── outcome matrix ──────────────────────────────────────────────────────────────


class TestOutcomeMatrix:
    @pytest.mark.asyncio
    async def test_ok(self):
        token = _mint_baseline_token()
        result = await _validate(token, _USER)
        assert result.outcome == "ok"
        assert result.entry is not None
        assert result.current_form is not None
        # Single-use: token gone afterward.
        assert pick_token.peek(token) is None

    @pytest.mark.asyncio
    async def test_wrong_user_leaves_token_unreserved(self):
        token = _mint_baseline_token(user_id=_USER)
        # A non-owner tap → wrong_user WITHOUT reserving/consuming.
        result = await _validate(token, sender=999)
        assert result.outcome == "wrong_user"
        assert pick_token.peek(token) is not None
        assert token not in pick_token._reservations
        # The legitimate owner can still win afterward.
        owner = await _validate(token, _USER)
        assert owner.outcome == "ok"

    @pytest.mark.asyncio
    async def test_stale_form_unreserves_and_later_valid_tap_wins(self):
        token = _mint_baseline_token()
        # Capture a pane that does NOT parse to the minted form → stale_form.
        result = await _validate(token, _USER, pane="no form here")
        assert result.outcome == "stale_form"
        assert token not in pick_token._reservations
        assert pick_token.peek(token) is not None
        # A later tap against the correct pane still wins.
        good = await _validate(token, _USER, pane=_BASELINE_PANE)
        assert good.outcome == "ok"

    @pytest.mark.asyncio
    async def test_expired_when_token_absent_at_phase_a(self):
        # Never minted → absent at phase (a) → expired.
        result = await _validate("deadbeefdead", _USER)
        assert result.outcome == "expired"
        assert result.entry is None

    @pytest.mark.asyncio
    async def test_window_gone_unreserves(self):
        token = _mint_baseline_token()
        result = await pick_token.validate_and_consume(
            token,
            _USER,
            capture_pane=_pane_capture(_BASELINE_PANE),
            find_window_by_id=_window_finder(None),  # window vanished
        )
        assert result.outcome == "window_gone"
        assert token not in pick_token._reservations
        assert pick_token.peek(token) is not None


# ── sequential vs concurrent duplicate ownership (Codex P1) ──────────────────────


class TestSequentialDuplicateIsExpired:
    @pytest.mark.asyncio
    async def test_second_call_on_one_token_is_expired_not_already_consumed(self):
        token = _mint_baseline_token()
        first = await _validate(token, _USER)
        assert first.outcome == "ok"
        # The token is fully absent now; a SEQUENTIAL duplicate is the ledger's
        # job, so validate_and_consume returns the benign expired, NOT
        # already_consumed.
        second = await _validate(token, _USER)
        assert second.outcome == "expired"


# ── exception / cancellation safety (load-bearing) ───────────────────────────────


class TestExceptionAndCancellationSafety:
    @pytest.mark.asyncio
    async def test_capture_raises_leaves_token_unreserved(self):
        token = _mint_baseline_token()

        async def _boom(_wid, _scroll):
            raise RuntimeError("capture blew up")

        with pytest.raises(RuntimeError, match="capture blew up"):
            await pick_token.validate_and_consume(
                token,
                _USER,
                capture_pane=_boom,
                find_window_by_id=_window_finder(_WINDOW),
            )
        # The finally ran: token NOT burned, NOT left reserved.
        assert token not in pick_token._reservations
        assert pick_token.peek(token) is not None
        # A later legitimate owner tap on the SAME token still wins.
        later = await _validate(token, _USER)
        assert later.outcome == "ok"

    @pytest.mark.asyncio
    async def test_cancellation_mid_validation_leaves_token_unreserved(self):
        token = _mint_baseline_token()
        gate = asyncio.Event()

        async def _blocking_capture(_wid, _scroll):
            await gate.wait()  # never set — the task is cancelled here
            return _BASELINE_PANE

        task = asyncio.create_task(
            pick_token.validate_and_consume(
                token,
                _USER,
                capture_pane=_blocking_capture,
                find_window_by_id=_window_finder(_WINDOW),
            )
        )
        # Let the task reserve + block inside capture_pane.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert pick_token._reservations.get(token) is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The finally ran on cancellation: token UNRESERVED, not burned.
        assert token not in pick_token._reservations
        assert pick_token.peek(token) is not None
        later = await _validate(token, _USER)
        assert later.outcome == "ok"


# ── concurrent same-token reservation (load-bearing) ─────────────────────────────


class TestConcurrentSameToken:
    @pytest.mark.asyncio
    async def test_second_same_token_caller_is_already_consumed_without_capture(self):
        token = _mint_baseline_token()
        gate = asyncio.Event()
        calls = {"n": 0}

        async def _blocking_capture(_wid, _scroll):
            calls["n"] += 1
            await gate.wait()
            return _BASELINE_PANE

        find = _window_finder(_WINDOW)
        t1 = asyncio.create_task(
            pick_token.validate_and_consume(
                token, _USER, capture_pane=_blocking_capture, find_window_by_id=find
            )
        )
        # Let caller-1 reserve + enter capture_pane (call-count 1, blocked).
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert calls["n"] == 1
        # Caller-2 on the SAME token: rejected at phase (a), never enters capture.
        r2 = await pick_token.validate_and_consume(
            token, _USER, capture_pane=_blocking_capture, find_window_by_id=find
        )
        assert r2.outcome == "already_consumed"
        assert calls["n"] == 1  # caller-2 did NOT enter capture_pane
        # Release caller-1 → ok.
        gate.set()
        r1 = await t1
        assert r1.outcome == "ok"


# ── concurrent sibling-token (load-bearing) ──────────────────────────────────────


class TestConcurrentSiblingToken:
    @pytest.mark.asyncio
    async def test_exactly_one_ok_loser_is_already_consumed(self):
        # Two option tokens from the SAME cache row (same key, same generation).
        form = resolve_ask_form(None, _BASELINE_PANE)
        assert form is not None
        src = _baseline_source()
        tokens = pick_token.mint_row(
            user_id=_USER,
            thread_id=_THREAD,
            window_id=_WINDOW,
            fingerprint=form.fingerprint(),
            source_kind=src.kind,
            source_fingerprint=src.source_fingerprint,
            specs=[
                pick_token._mint_spec(1, "Done navigating", False),
                pick_token._mint_spec(2, "Pick option 2 directly", False),
            ],
        )
        t1, t2 = tokens

        # Force BOTH callers to reserve (phase a) + enter the slow phase BEFORE
        # either reaches phase (c). The barrier releases only once both are
        # inside capture_pane, so neither token is evicted at phase (a) — the
        # loser must lose at phase (c) and read the row tombstone.
        entered = asyncio.Event()
        first_in = {"flag": False}

        async def _barrier_capture(_wid, _scroll):
            if not first_in["flag"]:
                first_in["flag"] = True
                # Wait for the second caller to arrive.
                await entered.wait()
            else:
                entered.set()
            return _BASELINE_PANE

        find = _window_finder(_WINDOW)
        results = await asyncio.gather(
            pick_token.validate_and_consume(
                t1, _USER, capture_pane=_barrier_capture, find_window_by_id=find
            ),
            pick_token.validate_and_consume(
                t2, _USER, capture_pane=_barrier_capture, find_window_by_id=find
            ),
        )
        outcomes = sorted(r.outcome for r in results)
        # EXACTLY one ok; the loser is already_consumed (NOT expired) via the
        # row's consumed_generation matching the loser's row_generation.
        assert outcomes == ["already_consumed", "ok"]
        # Both tokens gone afterward (one popped, sibling evicted).
        assert pick_token.peek(t1) is None
        assert pick_token.peek(t2) is None


# ── cache-reuse + generation-after-prune (load-bearing) ──────────────────────────


class TestGenerationSemantics:
    def _mint_row_once(self, *, fingerprint: str):
        src = _baseline_source()
        return pick_token.mint_row(
            user_id=_USER,
            thread_id=_THREAD,
            window_id=_WINDOW,
            fingerprint=fingerprint,
            source_kind=src.kind,
            source_fingerprint=src.source_fingerprint,
            specs=[pick_token._mint_spec(1, "Done navigating", False)],
        )

    def _row_for(self, fingerprint: str):
        return pick_token._pick_token_cache[(_USER, _THREAD, _WINDOW, fingerprint)]

    @pytest.mark.asyncio
    async def test_cache_reuse_same_tokens_no_generation_bump(self):
        form = resolve_ask_form(None, _BASELINE_PANE)
        assert form is not None
        fp = form.fingerprint()
        first = self._mint_row_once(fingerprint=fp)
        g1 = self._row_for(fp).row_generation
        # Re-mint the SAME unchanged form → SAME tokens, generation unchanged.
        second = self._mint_row_once(fingerprint=fp)
        assert second == first
        assert self._row_for(fp).row_generation == g1
        # A reused token still validates ok against the matching pane.
        result = await _validate(first[0], _USER)
        assert result.outcome == "ok"

    @pytest.mark.asyncio
    async def test_generation_after_prune_is_g2_not_stale_tombstone(self):
        form = resolve_ask_form(None, _BASELINE_PANE)
        assert form is not None
        fp = form.fingerprint()
        first = self._mint_row_once(fingerprint=fp)
        g1 = self._row_for(fp).row_generation
        stale_g1_token = first[0]
        # Consume one token → row tombstoned with consumed_generation == g1.
        consumed = await _validate(stale_g1_token, _USER)
        assert consumed.outcome == "ok"
        row = self._row_for(fp)
        assert row.consumed_generation == g1
        # TTL-prune the (tombstoned) row away, then re-mint the SAME key.
        cache_key = (_USER, _THREAD, _WINDOW, fp)
        pick_token._pick_token_cache.pop(cache_key, None)
        second = self._mint_row_once(fingerprint=fp)
        g2 = self._row_for(fp).row_generation
        # The module-global counter survived the prune → G2 > G1.
        assert g2 > g1
        # A fresh G2 token validates ok (NOT a stale already_consumed).
        fresh = await _validate(second[0], _USER)
        assert fresh.outcome == "ok"

    @pytest.mark.asyncio
    async def test_stale_g1_token_after_remint_is_expired_not_already_consumed(self):
        # A G1 token that disappears mid-validation while the row has been
        # re-minted to G2 classifies as expired (no generation match against
        # the G2 row), NOT a stale already_consumed reading G1's tombstone.
        form = resolve_ask_form(None, _BASELINE_PANE)
        assert form is not None
        fp = form.fingerprint()
        cache_key = (_USER, _THREAD, _WINDOW, fp)
        g1_tokens = self._mint_row_once(fingerprint=fp)
        g1 = self._row_for(fp).row_generation
        g1_token = g1_tokens[0]

        async def _capture_then_remint_g2(_wid, _scroll):
            # The G1 token disappears (its tap raced away / TTL) AND the cache
            # key is re-minted to a NEW generation G2 during the slow phase.
            pick_token._pick_tokens.pop(g1_token, None)
            pick_token._pick_token_cache.pop(cache_key, None)
            self._mint_row_once(fingerprint=fp)  # fresh G2 row
            return _BASELINE_PANE

        result = await pick_token.validate_and_consume(
            g1_token,
            _USER,
            capture_pane=_capture_then_remint_g2,
            find_window_by_id=_window_finder(_WINDOW),
        )
        g2 = self._row_for(fp).row_generation
        assert g2 > g1
        # Phase (c): G1 token gone, row exists at G2 with no matching tombstone
        # → expired, NOT a stale already_consumed.
        assert result.outcome == "expired"

    def _mint_row_with_source(self, *, fingerprint: str, source_fingerprint: str):
        return pick_token.mint_row(
            user_id=_USER,
            thread_id=_THREAD,
            window_id=_WINDOW,
            fingerprint=fingerprint,
            source_kind="jsonl_cache",
            source_fingerprint=source_fingerprint,
            specs=[pick_token._mint_spec(1, "Done navigating", False)],
        )

    @pytest.mark.asyncio
    async def test_cache_reuse_broken_on_source_drift(self):
        # Same FORM fingerprint, DRIFTED source (e.g. the side_file/jsonl_cache
        # tool_input changed while the visible form stayed identical). The cache
        # key is keyed on the form fingerprint only, so the drifted re-render
        # hits the same row. mint_row must NOT reuse it — reusing would hand back
        # tokens still stamped with the stale source_fingerprint, and every tap
        # would validate→source_drift→refresh→reuse forever (a dead-loop).
        fp = "stable-form-fp"
        first = self._mint_row_with_source(fingerprint=fp, source_fingerprint="SRC_A")
        g1 = self._row_for(fp).row_generation
        assert pick_token._pick_tokens[first[0]].source_fingerprint == "SRC_A"

        # Re-render: SAME form fingerprint, DRIFTED source SRC_B.
        second = self._mint_row_with_source(fingerprint=fp, source_fingerprint="SRC_B")

        # NOT reused: fresh tokens, new generation, new source tag on the row +
        # entries. (Were the reuse not source-aware, second == first and the
        # entry would still carry SRC_A → the dead-loop.)
        assert second != first
        row = self._row_for(fp)
        assert row.row_generation > g1
        assert row.source_fingerprint == "SRC_B"
        assert pick_token._pick_tokens[second[0]].source_fingerprint == "SRC_B"

    @pytest.mark.asyncio
    async def test_cache_reuse_holds_when_source_unchanged(self):
        # The converse guard: identical form fingerprint AND identical source
        # → genuine reuse (same tokens, no generation bump), so the
        # MESSAGE_NOT_MODIFIED optimization is preserved.
        fp = "stable-form-fp-2"
        first = self._mint_row_with_source(fingerprint=fp, source_fingerprint="SRC_X")
        g1 = self._row_for(fp).row_generation
        second = self._mint_row_with_source(fingerprint=fp, source_fingerprint="SRC_X")
        assert second == first
        assert self._row_for(fp).row_generation == g1


# ── TTL-prune during validation ─────────────────────────────────────────────────


class TestTtlPruneDuringValidation:
    @pytest.mark.asyncio
    async def test_prune_of_never_tombstoned_row_during_validation_is_expired(self):
        token = _mint_baseline_token()
        gate = asyncio.Event()

        async def _capture_then_prune(_wid, _scroll):
            # Reserve happened in phase (a). Now force the token out of the
            # store (TTL prune of a row that was NEVER tombstoned) while the
            # slow phase is "in flight".
            pick_token._pick_tokens.pop(token, None)
            await asyncio.sleep(0)
            gate.set()
            return _BASELINE_PANE

        result = await pick_token.validate_and_consume(
            token,
            _USER,
            capture_pane=_capture_then_prune,
            find_window_by_id=_window_finder(_WINDOW),
        )
        # Phase (c) re-check: token gone, row never tombstoned → expired, NOT
        # already_consumed.
        assert result.outcome == "expired"


# ── source-parity regression (load-bearing — guards f0c3f0c) ─────────────────────


def _bind(window_id: str, session_id: str) -> None:
    session_manager.window_states[window_id] = WindowState(
        cwd="/tmp/cwd", session_id=session_id
    )


def _unbind(window_id: str) -> None:
    session_manager.window_states.pop(window_id, None)


def _write_affordance_side_file(
    cc_dir: Path, session_id: str, tool_input: dict
) -> None:
    pending = cc_dir / "auq_pending"
    pending.mkdir(mode=0o700, exist_ok=True)
    (pending / f"{session_id}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": session_id,
                "tool_use_id": _AFFORDANCE_SIDEFILE["tool_use_id"],
                "written_at": time.time(),
                "tool_input": tool_input,
            }
        )
    )


class TestSourceParitySideFile:
    """side_file kind: mint→validate ok on the same source; source_drift on a
    drifted side-file dict that still matches the pane labels."""

    _WID = "@parity-sf"
    _SID = "4766fb07-7057-4981-9832-93e524ab943e"

    @pytest.fixture
    def _cc_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        auq_source.reset_for_tests()
        _bind(self._WID, self._SID)
        yield tmp_path
        _unbind(self._WID)
        auq_source.reset_for_tests()

    def _mint_against_current_source(self) -> str:
        src = auq_source.resolve_auq_source(self._WID, None, _AFFORDANCE_PANE)
        assert src.kind == "side_file"
        form = resolve_ask_form(src.payload, _AFFORDANCE_PANE)
        assert form is not None
        return pick_token.mint(
            pick_token.PickTokenEntry(
                window_id=self._WID,
                user_id=_USER,
                thread_id=_THREAD,
                fingerprint=form.fingerprint(),
                option_number=1,
                option_label="opt",
                is_review_submit=False,
                expires_at=time.monotonic() + 300,
                source_kind=src.kind,
                source_fingerprint=src.source_fingerprint,
                row_generation=1,
            )
        )

    @pytest.mark.asyncio
    async def test_mint_validate_ok_same_source(self, _cc_dir):
        _write_affordance_side_file(
            _cc_dir, self._SID, _AFFORDANCE_SIDEFILE["tool_input"]
        )
        token = self._mint_against_current_source()
        result = await pick_token.validate_and_consume(
            token,
            _USER,
            capture_pane=_pane_capture(_AFFORDANCE_PANE),
            find_window_by_id=_window_finder(self._WID),
        )
        assert result.outcome == "ok"

    @pytest.mark.asyncio
    async def test_source_drift_when_side_file_dict_changes(self, _cc_dir):
        _write_affordance_side_file(
            _cc_dir, self._SID, _AFFORDANCE_SIDEFILE["tool_input"]
        )
        token = self._mint_against_current_source()
        # Mutate the side-file source dict (keep labels so the pane still
        # matches → still side_file, but a DIFFERENT source fingerprint).
        mutated = json.loads(json.dumps(_AFFORDANCE_SIDEFILE["tool_input"]))
        mutated["questions"][0]["header"] = "MUTATED HEADER"
        _write_affordance_side_file(_cc_dir, self._SID, mutated)
        auq_source.reset_for_tests()  # drop the cached record so the new dict is read
        result = await pick_token.validate_and_consume(
            token,
            _USER,
            capture_pane=_pane_capture(_AFFORDANCE_PANE),
            find_window_by_id=_window_finder(self._WID),
        )
        assert result.outcome == "source_drift"


class TestSourceParityJsonlCache:
    """jsonl_cache kind: mint→validate ok on the same injected cache; drift on
    a mutated cache dict that still resolves to the same FORM fingerprint."""

    _WID = "@parity-jc"
    # A jsonl source whose extra (non-rendered) field can drift without
    # changing the pane-derived FORM fingerprint.
    _CACHE = {
        "questions": [
            {
                "question": "Pick a fruit",
                "header": "fruit",
                "options": [{"label": "Apple"}, {"label": "Banana"}],
            }
        ]
    }

    @pytest.fixture
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        auq_source.reset_for_tests()
        yield
        auq_source.reset_for_tests()

    def _mint(self, cache: dict, pane: str) -> str:
        # Inject the jsonl cache getter, resolve the source, mint against it.
        auq_source.set_jsonl_cache_getter(
            lambda wid: cache if wid == self._WID else None
        )
        src = auq_source.resolve_auq_source(self._WID, None, pane)
        assert src.kind == "jsonl_cache"
        form = resolve_ask_form(src.payload, pane)
        assert form is not None
        return pick_token.mint(
            pick_token.PickTokenEntry(
                window_id=self._WID,
                user_id=_USER,
                thread_id=_THREAD,
                fingerprint=form.fingerprint(),
                option_number=1,
                option_label="opt",
                is_review_submit=False,
                expires_at=time.monotonic() + 300,
                source_kind=src.kind,
                source_fingerprint=src.source_fingerprint,
                row_generation=1,
            )
        )

    # A non-empty pane that does NOT itself parse to a competing AUQ form, so
    # resolve_ask_form derives the form from the jsonl cache dict. (Empty pane
    # would make validate_and_consume short-circuit current_form to None.)
    _JUNK_PANE = "some terminal output\nnothing structured here\n"

    @pytest.mark.asyncio
    async def test_mint_validate_ok_same_cache(self, _setup):
        # The jsonl cache supplies the structured options; the junk pane has no
        # competing form, so mint and validate (both
        # resolve_auq_source(..., None, pane)) agree. ``_mint`` injects the
        # getter so the validate-side jsonl_cache branch reads the same dict.
        pane = self._JUNK_PANE
        token = self._mint(self._CACHE, pane)
        result = await pick_token.validate_and_consume(
            token,
            _USER,
            capture_pane=_pane_capture(pane),
            find_window_by_id=_window_finder(self._WID),
        )
        assert result.outcome == "ok"

    @pytest.mark.asyncio
    async def test_source_drift_when_cache_dict_changes(self, _setup):
        pane = self._JUNK_PANE
        token = self._mint(self._CACHE, pane)
        # Mutate a non-rendered field so the FORM fingerprint stays equal but
        # the SOURCE fingerprint (over the whole dict) drifts.
        drifted = json.loads(json.dumps(self._CACHE))
        drifted["questions"][0]["header"] = "DRIFTED"
        # Re-point the getter to the drifted dict; the validator re-resolves.
        auq_source.set_jsonl_cache_getter(
            lambda wid: drifted if wid == self._WID else None
        )
        result = await pick_token.validate_and_consume(
            token,
            _USER,
            capture_pane=_pane_capture(pane),
            find_window_by_id=_window_finder(self._WID),
        )
        # The form fingerprint must be unchanged (drifted field is not
        # rendered), so we reach the SOURCE compare → source_drift.
        assert result.outcome == "source_drift"


class TestSourceParityPaneKind:
    """pane kind: ok on a stable pane, stale_form on a changed pane. There is
    NO pane source_drift (a changed pane changes the FORM fingerprint first)."""

    @pytest.mark.asyncio
    async def test_ok_on_stable_pane(self):
        token = _mint_baseline_token()
        result = await _validate(token, _USER, pane=_BASELINE_PANE)
        assert result.outcome == "ok"

    @pytest.mark.asyncio
    async def test_changed_pane_is_stale_form(self):
        token = _mint_baseline_token()
        # The affordances pane parses to a DIFFERENT form fingerprint → the
        # FORM check fires first; pane never yields source_drift.
        result = await _validate(token, _USER, pane=_AFFORDANCE_PANE)
        assert result.outcome == "stale_form"


# ── D3-β: refresh_route_deadlines (keep a visibly-live card's tokens alive) ──────


def _mint_row(
    fingerprint: str,
    specs,
    *,
    window: str = _WINDOW,
    user: int = _USER,
    thread: int = _THREAD,
):
    """Mint a row at an arbitrary fingerprint (no pane validation needed for the
    deadline-refresh unit tests — they read/replace ``_pick_tokens`` directly)."""
    return pick_token.mint_row(
        user_id=user,
        thread_id=thread,
        window_id=window,
        fingerprint=fingerprint,
        source_kind="pane",
        source_fingerprint="srcfp",
        specs=specs,
    )


class TestRefreshRouteDeadlines:
    """D3-β: a poll on a visibly-live card re-stamps its tokens' deadlines so a
    token's lifetime tracks the card's OBSERVED lifetime, not a fixed 300s wall
    clock — closing the reported idle dead-tap. SAME token string + generation
    (no callback churn); never resurrects an expired or tombstoned token."""

    @pytest.mark.asyncio
    async def test_near_expiry_token_refreshed_same_token_and_generation(self):
        toks = _mint_row("fpA", [pick_token._mint_spec(1, "Opt 1", False)])
        tok = toks[0]
        entry = pick_token._pick_tokens[tok]
        exp, gen, fp = entry.expires_at, entry.row_generation, entry.fingerprint
        n = await pick_token.refresh_route_deadlines(
            _USER, _THREAD, _WINDOW, min_remaining_s=60, now=exp - 30
        )
        assert n == 1
        new = pick_token._pick_tokens[tok]  # SAME token key
        assert new.expires_at == pytest.approx(
            (exp - 30) + pick_token._PICK_TOKEN_TTL_SECONDS
        )
        assert new.row_generation == gen  # generation preserved (no churn)
        assert new.fingerprint == fp

    @pytest.mark.asyncio
    async def test_token_not_near_expiry_is_not_refreshed(self):
        toks = _mint_row("fpA", [pick_token._mint_spec(1, "Opt 1", False)])
        entry = pick_token._pick_tokens[toks[0]]
        exp = entry.expires_at
        # 200s of life left, margin is 60s → no-op.
        n = await pick_token.refresh_route_deadlines(
            _USER, _THREAD, _WINDOW, min_remaining_s=60, now=exp - 200
        )
        assert n == 0
        assert pick_token._pick_tokens[toks[0]].expires_at == exp

    @pytest.mark.asyncio
    async def test_expired_token_is_not_resurrected(self):
        toks = _mint_row("fpA", [pick_token._mint_spec(1, "Opt 1", False)])
        entry = pick_token._pick_tokens[toks[0]]
        exp = entry.expires_at
        # now is PAST the deadline → the resurrection guard must skip it.
        n = await pick_token.refresh_route_deadlines(
            _USER, _THREAD, _WINDOW, min_remaining_s=60, now=exp + 10
        )
        assert n == 0
        assert pick_token._pick_tokens[toks[0]].expires_at == exp

    @pytest.mark.asyncio
    async def test_tombstoned_row_is_not_refreshed(self):
        # A consumed row is a tombstone (consumed_generation set, tokens=[]).
        token = _mint_baseline_token()
        result = await _validate(token, _USER)
        assert result.outcome == "ok"
        n = await pick_token.refresh_route_deadlines(
            _USER, _THREAD, _WINDOW, min_remaining_s=600, now=time.monotonic()
        )
        assert n == 0

    @pytest.mark.asyncio
    async def test_multi_option_row_all_tokens_refreshed(self):
        toks = _mint_row(
            "fpA",
            [
                pick_token._mint_spec(1, "Opt 1", False),
                pick_token._mint_spec(2, "Opt 2", False),
            ],
        )
        exp = pick_token._pick_tokens[toks[0]].expires_at
        n = await pick_token.refresh_route_deadlines(
            _USER, _THREAD, _WINDOW, min_remaining_s=60, now=exp - 30
        )
        assert n == 2
        for t in toks:
            assert pick_token._pick_tokens[t].expires_at == pytest.approx(
                (exp - 30) + pick_token._PICK_TOKEN_TTL_SECONDS
            )

    @pytest.mark.asyncio
    async def test_only_the_matching_route_window_is_refreshed(self):
        ta = _mint_row("fpA", [pick_token._mint_spec(1, "A", False)], window="@1")
        tb = _mint_row("fpB", [pick_token._mint_spec(1, "B", False)], window="@2")
        ea = pick_token._pick_tokens[ta[0]].expires_at
        eb = pick_token._pick_tokens[tb[0]].expires_at
        n = await pick_token.refresh_route_deadlines(
            _USER, _THREAD, "@1", min_remaining_s=60, now=ea - 30
        )
        assert n == 1
        assert pick_token._pick_tokens[ta[0]].expires_at > ea  # @1 refreshed
        assert pick_token._pick_tokens[tb[0]].expires_at == eb  # @2 untouched

    @pytest.mark.asyncio
    async def test_fresh_mint_prunes_prior_generation_route_rows(self):
        # codex v3 P2a: a fresh mint = a new card generation for this route;
        # prior non-tombstoned rows (different fingerprint, same route) are
        # dropped so β only keeps the CURRENT card alive + memory stays bounded.
        a = _mint_row("fpA", [pick_token._mint_spec(1, "A", False)])
        assert (_USER, _THREAD, _WINDOW, "fpA") in pick_token._pick_token_cache
        b = _mint_row("fpB", [pick_token._mint_spec(1, "B", False)])
        assert (_USER, _THREAD, _WINDOW, "fpA") not in pick_token._pick_token_cache
        assert pick_token.peek(a[0]) is None  # prior-gen token evicted
        assert (_USER, _THREAD, _WINDOW, "fpB") in pick_token._pick_token_cache
        assert pick_token.peek(b[0]) is not None  # current card intact
