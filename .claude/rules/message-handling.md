# Message Handling

## Message Queue Architecture

Per-route message queues + worker pattern for all send tasks. A route is `(user_id, thread_id_or_0, window_id)`:
- Messages are sent in receive order (FIFO) **per route**
- Each route has its own worker, content queue, and latest-wins ephemeral slot
- Multi-user / multi-topic concurrent processing without interference

**Per-route status semantics**: Per-route workers drain the ephemeral slot
after every content task. Status updates are coalesced — only the latest
text per route survives between drains. Across routes, no global ordering
is enforced; each route's content-then-status order is independent of
others. (Under the previous per-user FIFO, status-after-content was a
global invariant; now it's a per-route invariant only, which is the
intended Stage 2 trade-off so a backlog in one topic doesn't delay
status / interactive prompts in another.)

**Message merging**: The worker automatically merges consecutive mergeable content messages on dequeue:
- Content messages for the same window can be merged (including text, thinking)
- tool_use breaks the merge chain and is sent separately (message ID recorded for later editing)
- tool_result breaks the merge chain and is edited into the tool_use message (preventing order confusion)
- Merging stops when combined length exceeds 3800 characters (to avoid pagination)

## Status Message Handling

**Conversion**: The status message is edited into the first content message, reducing message count:
- When a status message exists, the first content message updates it via edit
- Subsequent content messages are sent as new messages

**Polling**: Background task polls terminal status for all active windows at 1-second intervals. Send-layer rate limiting ensures flood control is not triggered.

**Deduplication**: The worker compares `last_text` when processing status updates; identical content skips the edit, reducing API calls.

## Run-state and idle reconciliation

`route_runtime` is the **sole** run-state / context-usage / idle-clear
authority — a single per-route state machine that exposes immutable
`RouteRuntimeSnapshot` reads. Every mutation (`ingest_transcript_event`,
`mark_*`) acquires a per-route `asyncio.Lock`, applies the transition,
and freezes an immutable snapshot read via `snapshot(route)` (no
observer/push channel). Snapshot
fields: `run_state`, `open_tools`, `waiting_on_user_tools`,
`context_usage`, `last_event_at`, `idle_clear_at`, `pane_idle_clear_at`,
`typing_eligible`, `status_card_visible`, `status_card_msg_id`,
`interactive_pending`. The two
idle deadlines are distinct:
`idle_clear_at` is the run-state `IDLE_RECENT → IDLE_CLEARED` decay
(armed by a transcript end-of-turn), while `pane_idle_clear_at` is the
debounced "🟡 Busy" *card-clear* deadline (armed by `status_polling`
on a confirmed-idle pane via `arm_pane_idle_clear`, read back via
`pane_idle_clear_due`, committed by `commit_pane_idle_clear`; activity
re-arms/cancels it inside `ingest_transcript_event` /
`mark_inbound_sent`). The consumers — `typing_action_loop`, the
activity-digest renderer, and the status-card lifecycle in
`message_queue` — read only from `route_runtime.snapshot(route)`. The
shared types `RunState`, `ContextUsage`, and `IDLE_CLEAR_DELAY_SECONDS`
live in `route_runtime`.

**`message_queue` boundary** — `message_queue` remains the only
sender/editor of status cards. It owns `_status_msg_info[skey]` as the
send-layer cache but mirrors `mark_status_card_published(route, msg_id)`
/ `mark_status_card_cleared(route)` into `route_runtime` so the
snapshot's `status_card_visible` flag is accurate for external
consumers. If a change ever needs to mutate `message_queue` internals
beyond that boundary, the kill criterion fires — promote a Route Outbox
slice now.

**Pane-set `WAITING_ON_USER` (live AUQ / ExitPlanMode "🔔 Waiting on you")** —
Claude Code buffers the interactive `tool_use` (AskUserQuestion / ExitPlanMode)
in JSONL until the prompt resolves, so `route_runtime` never ingests it and the
route would otherwise stay `RUNNING` ("🟡 Busy" + false "typing…"). The
lower-authority `pane_interactive_pending` bit is a **derivation input** (NOT a
parallel `run_state`): the deriver folds it into the empty-`open_tools` branch
(`WAITING_ON_USER` if the bit else `RUNNING`), so the single committed
`run_state` flips and the digest header + `typing_eligible` follow. The mutator
pair: `mark_interactive_pending` PROMOTES an **active `RUNNING` route with an
empty `open_tools` set** (the only state where setting the bit derives a clean
pane-set `WAITING`; `RUNNING` does not imply empty — a user turn mid-tool leaves
a stale entry) and re-arms the pane-idle debounce; `mark_interactive_cleared` is
the sole programmatic retract (NO-OP against a transcript-set `WAITING`).
**SET is pane-confirmed only**, fired by `status_polling.update_status_message`
at the live-picker proof points — site (a) `ui_content` present, site (b)
`is_picker_anchor_visible`, site (d) first-render dispatch — so the bit is True
⟺ a pane-set `WAITING`. **Site (c) (`side_file_live_for_window`, obscured pane)
is BIT-NEUTRAL**: it preserves the card but never promotes, so the bit shares
the AUQ card's liveness boundary and a double-`--resume` sibling (whose pane
never shows the picker) is never falsely lit. **CLEAR** is: the transcript
reclaim (primary — the `tool_use`/known-`tool_result`/end-of-turn/user branches
zero the bit when the buffered turn flushes; plain-text/thinking and an
unknown-id `tool_result` preserve it); the poller **mode-ended liveness
reconciliation** in the `interactive_window != window_id` block (gap-free —
covers mode-popped / window-switch / ExitPlanMode-no-flush, no flush dependency);
the **in-mode tombstone** (`mark_interactive_cleared` alongside the
`clear_interactive_msg(tombstone=True)`); and route teardown — the bit is
dropped wherever route_runtime state is cleared: **directly** at the
`inbound_telegram` stale-window unbinds (`clear_route`) and via
`mark_session_reset` (`/clear`), and via `clear_topic_state` →
`route_runtime.clear_routes_for_topic(user, thread)` on topic-close /
poller window-gone. The topic seam is **route_runtime's own** — it drops every
route under `(user, thread)` and is NOT derived from
`message_queue._route_queues` (a route can carry run-state /
`pane_interactive_pending` via `mark_inbound_sent` / replay /
`mark_interactive_pending` with no queue worker, so a `_route_queues`-only
enumeration would strand it; hermes round-2 P2). The digest header repaints on a
run-state transition via the poller's `_maybe_repaint_digest_on_transition`
(seeds without an edit on first observation; fires
`message_queue.refresh_activity_digest_if_present` once per change, both
directions; backed by the poller-local self-healing `_prev_run_state` dedup
cache, torn down only in the window-gone path — popping it on the bot-less
interactive-clear seam would mask the post-clear repaint). Pull-only; no
observer channel (c313657 stays forbidden). The bot-less `_on_interactive_clear`
seam is UNCHANGED — it touches neither the bit nor `_prev_run_state`.

**AUQ card-liveness authority (pane is lower authority than the
lifecycle)** — `status_polling`'s pane-absent clear gate must not tombstone
an AskUserQuestion card on visible-pane absence alone. The visible tmux pane
is only a *display*: a Claude task-list overlay, a scrolled/compressed
multi-step Submit screen, or tool-output spam can push the picker/Submit
anchors out of the captured pane while the question is still genuinely
pending on the Claude side (2026-05-31 @4/msg48427 — a live multi-select
card was tombstoned after the task-list overlay defeated both pane
predicates for 3 polls). The lifecycle authority is the PreToolUse side
file `auq_pending/<session>.json`, queried via
`auq_source.side_file_live_for_window(window_id)` (presence + schema +
future-skew, **deliberately NOT** the 5-min read-TTL and **NOT** the
pane-consistency check — a live-but-unanswered AUQ has not "expired on the
other side of the bridge", and `resolve_record` cannot be used because it
needs a pane-parsed form that is `None` under exactly the obstructing
overlay). While the side file is live the gate refreshes/keeps the card
and never enters the absent-streak countdown; the card is cleared only by
the genuine resolution (`tool_result` → `forget_ask_tool_input` unlinks the
side file), a window switch, a topic close, or the 1h startup `gc_stale`.
**Orphan reconciliation** — an *answered* AUQ whose side file was never
unlinked would keep the liveness probe `True` forever and strand a *dead*
card (the inverse failure the TTL-drop must not introduce). Two paths close
it: (1) **at the source** — `bot.handle_new_message` runs the AUQ
`tool_result` `forget_ask_tool_input` (which unlinks the side file) *before*
the awaited `clear_interactive_msg`, so a raise in the card clear can't
orphan it; (2) **on startup** — the monitor advances its byte offset inside
`check_for_updates` before the callback runs, so a crash/down-bot at that
moment leaves an orphan that path (1) can't catch;
`session_monitor._hydrate_ask_tool_input_cache` reconciles it on startup: for
each bound session whose JSONL shows **no pending AUQ**
(`_find_latest_pending_auq` is `None`) it unlinks any live side file via
**`side_file_live_for_session(session_id)` keyed on the same `current_map`
session it then unlinks** — never the window-keyed wrapper, whose `peek →
window_states` lookup can disagree with `current_map` at startup (checking one
source while unlinking another is the mint/validate parity trap). So presence
again tracks genuine liveness. Off-contract limitation: the
side file is keyed by *session*, so under a double-`--resume` of one session
into two windows a dead card on the sibling can linger (bounded by the
tool_result fan-out + window-switch + topic-close + 1h GC + the startup
reconciliation); a `tool_use_id` correlation would not help (the JSONL
`tool_use` / `_last_auq_tool_use_id` and the side file's `tool_use_id` are
typically unavailable during the live window), but a schema-v2 side file
carrying the hook-captured `window_id` could discriminate — deferred as
off-contract.

**Pick-token deadline refresh (D3-β — a live card's tokens track its OBSERVED
lifetime).** `pick_token._PICK_TOKEN_TTL_SECONDS = 300.0` bounds MEMORY only, not
correctness: a user can leave a live AUQ picker open for tens of minutes to
hours, and the old assumption that the token TTL outlives the picker was false —
a long idle pruned the option token out from under a still-on-screen card, so
the first tap hit `peek_none` and the handler *refreshed instead of
dispatching* (the dead-first-tap). Fix: at EVERY live-card-preserve branch where
`status_polling` resets the absent-streak and returns without re-rendering
(same-hash idle, `is_picker_anchor_visible` Submit, `side_file_live_for_window`
preservation), the poller calls `await
pick_token.refresh_route_deadlines(user, thread, window,
min_remaining_s=_DEADLINE_REFRESH_MARGIN_S)`. It re-stamps each live, non-expired
token within the margin of its deadline by REPLACING the frozen `PickTokenEntry`
with `expires_at = now + TTL` — **same token string, fingerprint, source tags,
and `row_generation`**, so the keyboard stays byte-identical (`MESSAGE_NOT_MODIFIED`,
no churn) and `_commit_phase_c`'s generation logic is untouched. It never
resurrects an already-expired token (the `now < expires_at` guard) or a
tombstoned row (`consumed_generation is None`), gated on the same liveness
authorities the clear-gate trusts; a genuinely-abandoned card's tokens still
prune at 300s. A fresh mint prunes prior-generation non-tombstoned rows for the
route so the refresh only keeps the CURRENT card alive. Pull-only (rides the 1 Hz
poll; no observer — c313657 forbidden). The residual cases — a restart (in-memory
tokens wiped) or a liveness-gate false-negative — degrade to the honest
`_refresh_pick_card` MODAL "↻ Refreshed — tap your choice again." (D3-α,
`show_alert=True` at the `peek_none`/`expired` callsites only; the ledger-state
callers keep their specific non-modal warnings).

**Source-drift re-mint (item 1 — a live card's TOKENS track its OBSERVED SOURCE;
the D3-β sibling).** D3-β keeps the token *deadlines* fresh but PRESERVES the
minted *source tags* (`dataclasses.replace(entry, expires_at=...)`). So a
single-select picker left open >300s ages its PreToolUse side file past the
read-TTL, `resolve_auq_source` flips `side_file`→`pane`, and the same-hash idle
branch — which only `refresh_route_deadlines` and returns — keeps the stale
`side_file` tokens. The user's first tap then hits `validate_and_consume`'s
source check → `source_drift` (swallowed + a misleading "Form changed,
refreshing."; self-heals on the 2nd tap via the existing source_drift re-render).
Fix (item 1): the read-TTL is **UNTOUCHED** (it stays the orphan time-bound —
nothing about side-file trust/lifetime changes), and the poller's same-hash idle
branch, BEFORE `refresh_route_deadlines`, re-resolves
`resolve_auq_source(window, None, pane)`, parses the live form via
`resolve_ask_form` (added to `status_polling`'s imports — the poller had only
`ui_content`, not a parsed form, and the parse also gates out non-AUQ panes like
the /model Settings picker), and compares the displayed card's minted
`(source_kind, source_fingerprint)` — read via the PURE, tombstone-aware
`pick_token.peek_route_source` — against the live source. On a mismatch it
re-renders via `handle_interactive_ui` (re-mint to the CURRENT source) instead of
refreshing deadlines, so the first tap dispatches. **Route-based lookup (the
item-1 P1 fix):** production mints a side_file card at the SIDE-FILE form's
fingerprint (the side-file dict carries the question TITLE), but after the side
file ages out the poller can only see the PANE form, whose
`current_question_title=None` on single-select panes — so the side-file-form and
pane-form fingerprints DIFFER (verified `3f00e2a2…` side-file vs `d24b9db9…` pane
on `auq_single_select_with_affordances_*`). The earlier fingerprint-keyed
`peek_route_source` therefore MISSED the row and never detected the drift. The fix
looks the displayed card up by ROUTE (`user, thread or 0, window`) across ALL
fingerprints — `mint_row`'s stale-row hygiene drops every OTHER non-tombstoned
row for a route on each fresh mint, so there is AT MOST ONE live card row per
route and the search is unambiguous (0 or, defensively, >1 live rows → None).
**Loop-safe (exactly ONE re-mint):** the drift re-mint fresh-mints `pane` and the
hygiene drops the old side_file-fp row, so the next tick finds the single pane row
→ live `pane` == minted `pane` → no further re-render.
`peek_route_source` skips TOMBSTONED rows (`consumed_generation is not None`) so a
just-consumed card is never falsely drifted into a re-render of a dead card. Being
fingerprint-agnostic, the route-based lookup also fixes the MULTI-question shape
(a pane fingerprint that shifts on ageout no longer hides the row). Pull-only
(rides the 1 Hz poll; no observer — c313657 forbidden). Residuals (all safe): a
≤1-poll-cycle boundary race at the 300s ageout (one tap routes through the
existing source_drift re-render, the 2nd dispatches); and a scrolled pane (visible
options start >1) where the re-mint drops the keyboard (`p14_suppress_picks`).

**Restart re-dispatch (D2 — the durable mint-intent net for the case D3-β can't
cover).** D3-β keeps a live card's tokens alive only while the process is up; a
bot **restart** wipes the in-memory `_pick_tokens`/`_pick_token_cache`, and the
published card keeps its old keyboard with dead token strings, so the first tap
hits `peek_none` for the card's whole remaining life. D2 persists the per-token
mint intent at the fresh `aqp:` single-select/Submit render to a new leaf store
(`pick_intent.py` → `pick_intent.jsonl`; `aqt:` toggles excluded) and the
`peek_none` / `expired` callback branches call `_attempt_pick_recovery` →
`pick_token.recover_and_consume` to re-dispatch that tap. It is the **idle net's
sibling, not its overlap**: recovery fires ONLY on **positive proof of in-memory
loss** — no `_pick_token_cache` row at the reconstructed
`(user, thread_or_0, window, full_fingerprint)` cache_key (a live row ⇒ the normal
`validate_and_consume` path owns it; a tombstoned row ⇒ this process just consumed
it) — so an idle-kept-alive token (D3-β) never enters recovery. Recovery is
**row-scoped single-use** (a `_recovery_row_reservations[cache_key]` for concurrent
sibling taps + a per-sibling action-ledger guard for the restart-durable /
crash-between-`accepted`-and-tomb case + a `consume_row` tomb for hygiene), adds
the full **owner + `reject_stale_window_callback`** auth pair the `peek_none`
branch historically lacked plus a callback-payload parity check vs the stored
intent, and re-validates **read-TTL-free** source parity
(`auq_source.read_side_file_for_recovery` comparing `_canonical_dict_fingerprint`,
NOT the 12-hex `input_fingerprint`; pane fallback only when the side file is
genuinely gone via `side_file_live_for_session`). The `accepted` claim is written
at the reconstructed ledger key INSIDE the row reservation (no release-then-claim
gap; a re-check of the cache-row + sibling proofs precedes it), and the action
ledger stays the **24h durable single-use authority** — `pick_intent.jsonl` is a
SEPARATE token-keyed store (writing recovery state into the latest-wins action
ledger would clobber a `dispatched` row). The store is **NOT a `route_runtime`
field** — render-path write, callback-path read, pull-only, no observer (c313657
forbidden). Tombed at `forget_ask_tool_input` (AUQ/EPM resolution + the `/clear`
race via the OLD-window `forget_ask_tool_input(wid)` call) and `clear_topic_state`;
orphan-safe via the recovery-time form/source re-validation + the 24h GC.
Off-contract residual (safe DECLINE, never a wrong dispatch): a `jsonl_cache`-minted
card DECLINES (its in-process getter is wiped on restart). The form fingerprint is
now cursor-blind on **every** screen — `AskUserQuestionForm._canonical_repr` omits
the per-option cursor bit UNCONDITIONALLY (not just when `is_review_screen`), and
`auq_source._pane_fingerprint` hashes the SAME `_canonical_repr` so the pane source
fingerprint collapses in lockstep. The cursor-blind fingerprint stays load-bearing
under the v2.1.168 navigate-to-target dispatch (the bot MOVES the cursor to the
target before committing, so the form identity must not shift as the cursor moves —
else the nav-verify re-parse would no longer match the minted fingerprint and every
pick would bail). A moved cursor — Submit↔Cancel on the review screen OR any option
on a non-review picker — no longer rotates the pick token (live OR across a
restart), and D2 recovery SURVIVES a moved cursor on **every** screen (**the former
D3-γ non-review DECLINE is RETIRED**). Both the live and recovery Submit guards
share the cursor-blind `AskUserQuestionForm.review_submit_dispatchable`
predicate (anchored on `is_review_screen` + option #1 + the literal
`REVIEW_SUBMIT_LABEL` "Submit answers" + the minted label; verified on Claude Code
v2.1.161/.167/.168). The `_pane_fingerprint` ⇄ `_canonical_repr` shared-canonical
coupling is load-bearing for this fix — a refactor giving the pane source its own
fingerprint basis would re-break it; the fingerprint-EQUALITY-across-cursor-move
tests (for BOTH the review screen and non-review pickers) guard the coupling.

**AUQ pick dispatch NAVIGATES the cursor to the target, VERIFIES, then Enter
(v2.1.168 model — single-select `aqp:` + review Submit/Cancel ONLY).** On Claude
Code v2.1.168 a richer "notes side-panel" picker variant makes a bare digit only
MOVE the cursor (no select), so the form sticks and the bot would wrongly record
`dispatched` → an "Action already received" hard lock. Fix: `_dispatch_pick`
(shared by the live `aqp:` pick path AND D2 recovery) finds the live `❯` cursor in
`current_form`, computes `delta = target − cursor.number`, sends `Down`/`Up` ×
|delta| (`send_keys(enter=False, literal=False)`, MONOTONIC — never a wrap
shortcut, each return-checked), waits `NAV_SETTLE` (0.5s), re-parses to VERIFY the
cursor landed on the target (same cursor-blind `fingerprint` + `vc.number ==
target` + `_loose_label_match(vc.label, minted_label)` + the
`review_submit_dispatchable` anchor for Submit), presses `Enter` (`enter=False,
literal=False` — the version-stable commit, True in every variant), waits
`COMMIT_SETTLE` (0.5s), re-parses, and records `dispatched` ONLY after
`_classify_advance` confirms the EXACT expected transition (a positive forward
advance / resolution — over-advance, wrong-tab, no-flip all fail CLOSED). Ledger
non-success states: a **pre-commit bail** (`cursor_unknown` / `nav_send_failed` /
`verify_failed` — Enter provably never sent) records `not_advanced` and the
callback **falls through** (a fresh-token re-tap re-validates against the live
form; safe because nothing was committed); once `Enter` is sent an unconfirmed
advance (`commit_unconfirmed` / `confirm_capture_failed` / `confirm_parse_failed` —
a parse-fail with picker markers still present is AMBIGUOUS, never `dispatched`)
records `commit_unconfirmed` and the callback **refreshes-only, never
auto-redispatches** (no re-tap can re-send the commit key). The bare digit + the
`auq_ledger` `digit_sent` / `failed_*_digit` states are now **legacy-only** (kept
for on-disk compat). The nav `⏎ Enter` button (`CB_ASK_ENTER`) + arrow nav still
send Enter — the orthogonal navigation path, unchanged, AND the user's manual
escape if a future variant defeats the auto-dispatch. **Scoped to single-select
`aqp:` + review Submit/Cancel; the multi-select `aqt:` toggle still dispatches a
bare digit — a filed fast-follow (AUQ is NOT globally fixed).** Validated against
Claude Code v2.1.168 terminal behavior.

## MessageDisplay live-prose capture (Bug 2)

Assistant free-text prose written in the same turn as an `AskUserQuestion` /
`ExitPlanMode` `tool_use` is co-flushed to the session JSONL only at
resolution, so during a live prompt the monitor's byte-offset read sees no new
bytes and the prose is not on the bridge — the Telegram user would see only the
picker card and choose blind. Claude Code's `MessageDisplay` hook fires with
each streaming `delta` of an assistant message BEFORE the picker blocks; the
tiny stdlib appender (`_md_display_appender.py`) writes each raw payload as one
NDJSON line to `msg_display/<session>.ndjson`, keyed by
`Path(transcript_path).stem` (resume-safe). The hook is scoped to bot-launched
sessions via a bot-managed `md_hook_settings.json` passed as `claude
--settings` (it merges with the global `SessionStart` / `PreToolUse` hooks and
is never installed into `~/.claude/settings.json`).

`MessageDisplay.message_id` has no JSONL counterpart and `delta` is per-flush
(`final=True` marks end-of-message), so **accumulation is bot-side**:
`md_capture.read_prose_records(session_id)` reads the per-session NDJSON ON
DEMAND (pull-only — no background tailer / observer; c313657 stays forbidden),
groups deltas by `message_id`, concatenates them in index order, and returns one
`ProseRecord` per FINALIZED message (`{session_id, transcript_path,
md_message_id, text, raw_hash, norm_hash, first_seen_at, final_at}`) ordered by
`final_at`. It tolerates a missing file, corrupt / partially-written lines, and
not-yet-final messages (omitted — the render-path bounded retry re-reads).
`md_capture.normalize_prose` (CR/CRLF→LF + per-line trailing-trim + edge strip,
NO interior collapse) is the SINGLE normalization used for both the live
`norm_hash` here and the post-resolution JSONL dedup, so the two compare equal
regardless of streaming-vs-flush quirks — the mint/validate parity that keeps
dedup from silently failing.

The §3.0 data-model prerequisite plumbs JSONL `message.id` + a `block_origin`
marker through `ParsedEntry` / `TranscriptEvent` / `NewMessage` (a single
backfill stamps every entry of an assistant line with its `message.id`; the
synthetic ExitPlanMode plan body — emitted as `content_type="text"` from
`input.plan` — is marked `BLOCK_ORIGIN_EXIT_PLAN` so dedup never suppresses real
prose by matching it).

**Live delivery (PR-C).** `interactive_ui.handle_interactive_ui`, under the
route lock and BEFORE the picker card / AUQ context message,
`_maybe_post_live_prose` reads the freshest finalized capture
(`md_capture.select_fresh_prose`, freshness = `final_at` within a per-mode TTL
of now — `AUQ_PROSE_TTL_S` / `EPM_PROSE_TTL_S`, a previous turn's leftover ages
out), posts it as its own message, and records a **shown-live marker** in the
same per-session capture file. Idempotent via `md_capture.was_shown_live`
(consume-INCLUSIVE: a re-render / poll re-detect / post-`kickstart` / the dedup
having consumed the marker all skip a re-post). A miss is a silent no-op — the
JSONL copy delivers post-resolution exactly as before (no marker, no dedup,
never a delayed picker). A bounded ≤250ms retry covers the rare same-tick race
(the prose finalizes ~0.68s before the picker blocks, so it almost never fires).
Render-path state only — NOT a RouteRuntime field (Bug-1 contract intact).

**Turn-boundary anchor (Item 3 / P2-1 — the prior-turn-prose leak).** Freshness
was session + TTL only, so a PRIOR turn's leftover prose (still in the per-session
file because teardown only fires at AUQ/EPM resolution, and still within the TTL)
could be posted above a picker whose OWN turn produced no prose. Fix: a
**delivery-seam `not_before` anchor**. `message_queue.set_route_user_turn_at`
stamps the route's wall-clock delivery instant (`time.time()`) **PRE-SEND** —
immediately BEFORE `send_to_window` at the user-turn delivery seams
(`inbound_aggregator._send_bundle`, the slash-command `bot.forward_command_handler`,
and the `/effort` callback) so a fast prose→AUQ turn can't finalize its prose
before the stamp lands. `_maybe_post_live_prose` reads it non-consumingly
(`peek_route_user_turn_at`, resolved INSIDE the function so the 22
`handle_interactive_ui` callers are untouched — auto-closes the inbound:1061
on-pane + restart first-render holes) and passes it as `not_before` to
`select_fresh_prose`, which adds a **STRICT `final_at > not_before`** gate: the
current turn's prose is captured AFTER delivery, a prior turn's BEFORE it
(`==` boundary is excluded — not causally after the delivered message). The stamp
shares the appender's `captured_at` clock, so they compare directly. The store is
torn down with the route (beside `_route_last_user_message`) and cleared by
`reset_for_tests`; it is **render/callback-path state, NOT a RouteRuntime field**
(pull-only; c313657 forbidden). **Residuals (all safe):** after a **restart** the
in-memory stamp is gone → `not_before=None` → TTL-only (the prior-turn leak is
NOT fixed across a restart — documented degradation, never a false-negative on the
live path); a rare **wall-clock-backwards** jump could mis-order a stamp vs a
`captured_at` (NO epsilon is added — accepted as a rare residual); the per-session
file's tracked-idle disk retention is unchanged (teardown still owns reclaim). A
**concurrent-send clobber** — a LATER delivery whose stamp overwrites the route's
single boundary BEFORE an earlier, not-yet-rendered picker first-renders — can
suppress that earlier picker's prose (it then arrives post-resolution via JSONL,
never a wrong post). The common "send while a picker is on the pane" case is
defused upstream: `inbound_telegram` renders the on-pane picker with the prior
stamp BEFORE offering the new message; the only residual is delivering into a
still-streaming Claude before its picker appears (bounded, degrades to JSONL).
A per-picker boundary would close it but is disproportionate for this benign,
already-degenerate edge.

**Dedup (PR-D).** `session_monitor.filter_live_prose_duplicates` runs on the
poll BATCH before per-message dispatch (the prose text block and its sibling
interactive `tool_use` are separate `NewMessage`s of one `message_id`, prose
first — only the batch sees the pairing). For each `(session_id, message_id)`
group with an AskUserQuestion / ExitPlanMode `tool_use`, it aggregates the REAL
text blocks (excludes `BLOCK_ORIGIN_EXIT_PLAN`), hashes via the SINGLE shared
`md_capture.prose_norm_hash`, matches an unconsumed shown-live marker, and
suppresses + consumes (consume-once, restart-safe). EPM ambiguity safety: >1
group sharing one `(session, norm_hash)` marker → suppress NONE. Multi-block
parity: aggregation joins parser-stripped blocks with `\n` — exact for
single-block (Bug 2's observed shape) and adjacent multi-block, a benign
double-post only for the rare blank-line-between-blocks case. Within one poll
batch the dedup runs BEFORE the dispatch that triggers teardown, so it reads the
marker first; the only gap is the split-batch edge (prose and its tool_use land
in SEPARATE poll batches — unlikely given the turn co-flushes atomically), where
the prose batch can dispatch undeduped and teardown can fire before the later
tool_use batch → another benign double-post, never a crash.

**Lifecycle.** `md_capture.teardown_session` (unlinks the per-session capture +
its markers) is wired at AUQ/EPM resolution (`forget_ask_tool_input`, the
primary seam — fires for both via `bot.handle_new_message`'s
`has_interactive_surface` branch), the `/clear` race + deleted windows
(`session_monitor` via the OLD session id), and topic close (`clear_topic_state`
→ the thread's bound window). The 1h startup `gc_stale` is the backstop. The
shown-live / consumed marker lines live in the SAME `msg_display/<session>.ndjson`
as the capture deltas (the delta reader ignores `marker` lines and vice-versa),
so they share that lifecycle. **Startup-GC liveness gate (Item 3 / P2-2).**
`gc_stale` previously reaped ANY `*.ndjson` >1h with no liveness check, so a
long-open picker's capture file (which carries its shown_live/consumed dedup
markers) was reaped at startup → the post-resolution dedup double-posted. Fix: an
**INJECTED `is_live_session` predicate** — the `bot.py` callsite passes
`lambda sid: monitor.state.get_session(sid) is not None` (keyed by the file STEM =
the original session id the monitor tracks under `--resume`, covering BOTH AUQ and
EPM since it is session-keyed, not prompt-typed). After the age test, a `True` →
**SKIP** (keep the live file + its markers); a predicate **raise** → conservative
SKIP (never delete on uncertainty; caught around the predicate call only so the
pass continues); and a **re-`stat` before `unlink`** is the TOCTOU guard (a
concurrent append refreshing the mtime within `max_age` → skip). The predicate is
NEVER imported into `md_capture` (it stays a leaf — only stdlib + `utils`). Pull-only
throughout (no observer; c313657 forbidden).

## Rate Limiting

- `AIORateLimiter(max_retries=5)` on the Application (30/s global)
- On 429, AIORateLimiter pauses all concurrent requests (`_retry_after_event`) and retries after the ban
- On restart, the global bucket is pre-filled (`_level=max_rate`) to avoid burst against Telegram's persisted server-side counter
- Status polling interval: 1 second (skips enqueue when queue is non-empty)

## Performance Optimizations

**mtime cache**: The monitoring loop maintains an in-memory file mtime cache, skipping reads for unchanged files.

**Byte offset incremental reads**: Each tracked session records `last_byte_offset`, reading only new content. File truncation (offset > file_size) is detected and offset is auto-reset.

## No Message Truncation

Historical messages (tool_use summaries, tool_result text, user/assistant messages) are always kept in full — no character-level truncation at the parsing layer. Long text is handled exclusively at the send layer: `split_message` splits by Telegram's 4096-character limit; real-time messages get `[1/N]` text suffixes, history pages get inline keyboard navigation.
