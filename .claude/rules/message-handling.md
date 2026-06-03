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
callers keep their specific non-modal warnings). Restart re-dispatch of a
token-less stale tap (so even that first tap registers) is a deferred follow-up.

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
so they share that lifecycle. Pull-only throughout (no observer; c313657
forbidden).

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
